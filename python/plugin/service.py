"""ScrapeRack Python plugin.

Environments: builds immutable virtualenv disks for Python package sets and
keeps one ScrapeRack pool per environment.

Executions: Python code run in those environments. Each execution is a core
task on the environment's pool; this plugin owns how they are submitted,
listed, described and shown.

Standard library only: the plugin installs without downloading anything.
"""
import base64
import binascii
import http.client
import json
import os
import pickletools
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from hashlib import sha256
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

PLUGIN = os.environ.get("SCRAPERACK_PLUGIN_NAME", "python")
OWNER = f"plugin:{PLUGIN}"
PLUGIN_SOCKET = os.environ.get("SCRAPERACK_PLUGIN_SOCKET", "/tmp/scraperack-python.sock")
CORE_SOCKET = os.environ.get("SCRAPERACK_CORE_SOCKET", "/tmp/scraperack-core.sock")
DATA = Path(os.environ.get("SCRAPERACK_PLUGIN_DATA", "data"))
GUEST_DISK = os.environ.get("SCRAPERACK_PLUGIN_GUEST_DISK") or None
HERE = Path(__file__).resolve().parent

VENV_MOUNT = "/tmp/environment"
GUEST_MOUNT = "/tmp/scraperack/python"
STATE = DATA / "environments.json"
MIGRATED = DATA / "migrated-environments.json"
FUNCTIONS = DATA / "execution-functions-v2.json"
FINISHED = {"succeeded", "failed", "timed_out"}

DEFAULTS = {
    "packages": [], "preload": [], "warm_instances": 0, "max_instances": None,
    "cpu_millis": 1000, "memory_mib": 512, "disk_mib": 1024, "timeout_secs": 300,
    "idle_timeout_secs": 30, "reuse": True, "network": True,
}
FIELDS = ("name", "image", *DEFAULTS)
lock = threading.RLock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# --- Core API over the plugin's private Unix socket -------------------------

class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def core(method, path, body=None, file=None, timeout=120):
    connection = UnixConnection(CORE_SOCKET, timeout=timeout)
    try:
        headers = {}
        if file is not None:
            headers["Content-Length"] = str(os.fstat(file.fileno()).st_size)
            headers["Content-Type"] = "application/octet-stream"
            payload = file
        elif body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        else:
            payload = None
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    if response.status >= 400:
        try:
            message = json.loads(data)["error"]
        except Exception:
            message = data.decode(errors="replace") or response.reason
        raise ApiError(response.status, message)
    return json.loads(data) if data else None


# --- State -------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {}


def save_state(state):
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    temporary.replace(STATE)


def clean_list(values):
    if isinstance(values, str):
        values = values.replace(",", "\n").splitlines()
    return sorted({str(value).strip() for value in values or [] if str(value).strip()})


def normalize(request, current=None):
    """Validate an environment definition from the API."""
    environment = {**DEFAULTS, **(current or {})}
    for field in FIELDS:
        if field in request:
            environment[field] = request[field]
    environment["name"] = str(environment.get("name") or "").strip()
    if not environment["name"]:
        raise ApiError(400, "environment name is required")
    if not re.fullmatch(r"[A-Za-z0-9._ -]{1,64}", environment["name"]):
        raise ApiError(400, "environment names may use letters, digits, spaces, '.', '_' and '-'")
    if not environment.get("image"):
        raise ApiError(400, "image is required")
    environment["packages"] = clean_list(environment["packages"])
    environment["preload"] = clean_list(environment["preload"])
    for field in ("warm_instances", "cpu_millis", "memory_mib", "disk_mib", "timeout_secs",
                  "idle_timeout_secs"):
        environment[field] = int(environment[field])
    if environment["max_instances"] in ("", None):
        environment["max_instances"] = None
    else:
        environment["max_instances"] = int(environment["max_instances"])
        if environment["max_instances"] < 1 or environment["warm_instances"] > environment["max_instances"]:
            raise ApiError(400, "invalid instance limits")
    environment["reuse"] = bool(environment["reuse"])
    environment["network"] = bool(environment["network"])
    return environment


def find(state, key):
    if key in state:
        return state[key]
    for environment in state.values():
        if environment["name"] == key:
            return environment
    raise ApiError(404, "environment not found")


# --- Building package disks ---------------------------------------------------

def run(*args, **kwargs):
    process = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if process.returncode:
        raise ApiError(400, (process.stderr or process.stdout).strip()[-4000:]
                       or f"{args[0]} failed")
    return process.stdout


def build_key(image, packages):
    digest = sha256(b"python-plugin-v1")
    digest.update(image["sha256"].encode())
    for package in ["cloudpickle", *packages]:
        digest.update(b"\0" + package.encode())
    return digest.hexdigest()


def build_venv_disk(image, packages):
    """Install packages into a virtualenv inside the image and return a core disk key."""
    source = image.get("docker_source")
    if not source:
        raise ApiError(400, "the image must be imported from Docker to build packages")
    key = build_key(image, packages)
    cache = DATA / "builds.json"
    with lock:
        cached = json.loads(cache.read_text()) if cache.exists() else {}
    disks = {disk["key"] for disk in core("GET", "/v1/disks")}
    if cached.get(key) in disks:
        return cached[key]
    script = ("/usr/bin/python3 -m venv /tmp/environment && "
              "/tmp/environment/bin/python -m pip install --no-cache-dir \"$@\"")
    container = run("docker", "create", "--entrypoint", "/bin/sh", source, "-c", script, "--",
                    "cloudpickle", *packages).strip()
    work = Path(tempfile.mkdtemp(prefix="python-plugin-", dir=DATA))
    try:
        run("docker", "start", "-a", container)
        root = work / "root"
        root.mkdir()
        run("docker", "cp", f"{container}:/tmp/environment/.", str(root))
        used = int(run("du", "-sb", str(root)).split()[0])
        step = 16 * 1024 * 1024
        size = -(-max(used + used // 5 + 8 * 1024 * 1024, 32 * 1024 * 1024) // step) * step
        disk = work / "environment.ext4"
        with disk.open("wb") as file:
            file.truncate(size)
        run("mkfs.ext4", "-q", "-F", "-d", str(root), str(disk))
        with disk.open("rb") as file:
            stored = core("POST", "/v1/disks", file=file, timeout=None)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    with lock:
        cached = json.loads(cache.read_text()) if cache.exists() else {}
        cached[key] = stored["key"]
        cache.write_text(json.dumps(cached, indent=2, sort_keys=True))
    return stored["key"]


def ensure_disk(environment, force=False):
    if environment.get("venv_disk") and not force:
        return environment["venv_disk"]
    images = {image["id"]: image for image in core("GET", "/v1/images")}
    image = images.get(environment["image"])
    if image is None:
        raise ApiError(400, "unknown image")
    return build_venv_disk(image, environment["packages"])


# --- Pools --------------------------------------------------------------------

def pool_spec(environment):
    disks = [{"key": environment["venv_disk"], "mount": VENV_MOUNT}]
    executor = [f"{VENV_MOUNT}/bin/python", "/opt/scraperack/executors/python"]
    if GUEST_DISK:
        disks.append({"key": GUEST_DISK, "mount": GUEST_MOUNT})
        executor = [f"{VENV_MOUNT}/bin/python", f"{GUEST_MOUNT}/executor.py"]
    return {
        "name": environment["pool"],
        "image": environment["image"],
        "warm_instances": environment["warm_instances"],
        "max_instances": environment["max_instances"],
        "cpu_millis": environment["cpu_millis"],
        "memory_mib": environment["memory_mib"],
        "disk_mib": environment["disk_mib"],
        "timeout_secs": environment["timeout_secs"],
        "idle_timeout_secs": environment["idle_timeout_secs"],
        "reuse": environment["reuse"],
        "network": environment["network"],
        "ports": [],
        "preload": environment["preload"],
        "disks": disks,
        "executor": executor,
        "aliases": [environment["id"], environment["name"]],
        "display_name": environment["name"],
    }


def apply_pool(environment):
    spec = pool_spec(environment)
    try:
        return core("PUT", f'/v1/pools/{environment["pool"]}', spec)
    except ApiError as error:
        if error.status != 404:
            raise
    return core("POST", "/v1/pools", spec)


def view(environment, pools):
    pool = pools.get(environment["pool"], {})
    return {**environment, "artifact": environment.get("venv_disk"),
            "ready": bool(pool.get("ready")), "revision": pool.get("revision")}


def pools_by_name():
    return {pool["name"]: pool for pool in core("GET", "/v1/pools")}


def unused_disk(key, state):
    if not key or any(environment.get("venv_disk") == key for environment in state.values()):
        return
    try:
        core("DELETE", f"/v1/disks/{key}")
    except ApiError:
        pass


# --- Environment operations -------------------------------------------------------

def create_environment(request):
    environment = normalize(request)
    with lock:
        state = load_state()
        if any(other["name"] == environment["name"] for other in state.values()):
            raise ApiError(409, "environment name already exists")
    environment["id"] = str(uuid.uuid4())
    environment["pool"] = f'python-{environment["id"]}'
    environment["venv_disk"] = ensure_disk(environment)
    apply_pool(environment)
    with lock:
        state = load_state()
        state[environment["id"]] = environment
        save_state(state)
    return view(environment, pools_by_name())


def update_environment(key, request):
    with lock:
        current = dict(find(load_state(), key))
    environment = normalize(request, current)
    with lock:
        if any(other["name"] == environment["name"] and other["id"] != current["id"]
               for other in load_state().values()):
            raise ApiError(409, "environment name already exists")
    rebuild = (environment["image"] != current["image"]
               or environment["packages"] != current["packages"])
    if rebuild:
        environment["venv_disk"] = ensure_disk(environment, force=True)
    apply_pool(environment)
    with lock:
        state = load_state()
        state[environment["id"]] = environment
        save_state(state)
        if rebuild:
            unused_disk(current.get("venv_disk"), state)
    return view(environment, pools_by_name())


def restart_environment(key):
    with lock:
        environment = find(load_state(), key)
    core("POST", f'/v1/pools/{environment["pool"]}/restart')
    return view(environment, pools_by_name())


def delete_environment(key):
    with lock:
        environment = find(load_state(), key)
    try:
        core("DELETE", f'/v1/pools/{environment["pool"]}')
    except ApiError as error:
        if error.status != 404:
            raise
    with lock:
        state = load_state()
        state.pop(environment["id"], None)
        save_state(state)
        unused_disk(environment.get("venv_disk"), state)


def adopt_migrated():
    """Take over environments the core created before plugins existed."""
    if not MIGRATED.exists():
        return
    with lock:
        state = load_state()
        for legacy in json.loads(MIGRATED.read_text()):
            if legacy["id"] in state:
                continue
            environment = normalize({**legacy, "image": legacy["image"]})
            environment.update(id=legacy["id"], pool=legacy["pool"],
                               venv_disk=legacy.get("artifact"))
            state[environment["id"]] = environment
        save_state(state)
        MIGRATED.replace(MIGRATED.with_suffix(".adopted.json"))


def reconcile():
    """Make the core's pools match this plugin's environments after a (re)start."""
    while True:
        try:
            core("GET", "/v1/pools")
            break
        except OSError:
            time.sleep(1)
    adopt_migrated()
    with lock:
        environments = list(load_state().values())
    disks = {disk["key"] for disk in core("GET", "/v1/disks")}
    for environment in environments:
        try:
            if environment.get("venv_disk") not in disks:
                environment["venv_disk"] = ensure_disk(environment, force=True)
                with lock:
                    state = load_state()
                    state[environment["id"]] = environment
                    save_state(state)
            apply_pool(environment)
        except Exception:
            print(f'reconcile {environment["name"]} failed', file=sys.stderr)
            traceback.print_exc()


# --- Executions ---------------------------------------------------------------------
#
# An execution is a core task on one of this plugin's pools. The core keeps the
# task (queue, VM, payload, result, logs, metrics); this plugin decides what it
# means as Python and which ones the user sees.

function_lock = threading.Lock()
function_cache = None


def pickle_strings(data):
    """Strings a pickle pushes, in order, with memo references resolved. Runs nothing."""
    strings, memo, last = [], {}, None
    for op, arg, _ in pickletools.genops(data):
        if op.name in ("GET", "BINGET", "LONG_BINGET"):
            last = memo.get(int(arg))
            if isinstance(last, str):
                strings.append(last)
        elif op.name == "MEMOIZE":
            memo[len(memo)] = last
        elif op.name in ("PUT", "BINPUT", "LONG_BINPUT"):
            memo[int(arg)] = last
        elif isinstance(arg, str) and "UNICODE" in op.name:
            strings.append(arg)
            last = arg
        else:
            last = None
    return strings


def describe_payload(payload):
    """Name the Python code in a payload without unpickling it."""
    encoding = (payload or {}).get("encoding")
    info = {"encoding": encoding, "function": None, "module": None, "file": None}
    if encoding == "python-source":
        match = re.search(r"^def\s+main\s*\(", payload.get("source", ""), re.M)
        info.update(function="main" if match else "<source>", module="<source>")
        return info
    if encoding == "files":
        command = payload.get("command")
        info["function"] = command if isinstance(command, str) else " ".join(map(str, command or []))
        return info
    if encoding not in ("python-function", "cloudpickle"):
        return info
    try:
        data = base64.b64decode(payload["function" if encoding == "python-function" else "data"])
        strings = pickle_strings(data)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return info
    if "_make_function" in strings:
        after = {}
        for index, key in enumerate(strings[:-1]):
            if key in ("__qualname__", "__module__", "__name__"):
                after.setdefault(key, strings[index + 1])
        info["function"] = after.get("__qualname__")
        info["module"] = after.get("__module__") or after.get("__name__")
        # A code object stores co_filename right before co_name; string constants
        # inside the function come earlier, so match on the pair.
        short = (info["function"] or "").rsplit(".", 1)[-1]
        info["file"] = next((strings[index - 1] for index, value in enumerate(strings)
                             if index and value == short and strings[index - 1].endswith(".py")),
                            None)
    elif len(strings) >= 2:
        info["module"], info["function"] = strings[0], strings[1]
    return info


def load_functions():
    global function_cache
    if function_cache is None:
        try:
            function_cache = json.loads(FUNCTIONS.read_text())
        except (FileNotFoundError, ValueError):
            function_cache = {}
    return function_cache


def save_functions(cache, keep):
    """Persist descriptions, dropping those of tasks the core no longer has."""
    trimmed = {key: value for key, value in cache.items() if key in keep}
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = FUNCTIONS.with_suffix(".tmp")
    temporary.write_text(json.dumps(trimmed))
    temporary.replace(FUNCTIONS)
    return trimmed


def function_of(task_id, cache):
    if task_id not in cache:
        try:
            cache[task_id] = describe_payload(core("GET", f"/v1/tasks/{task_id}/payload"))
        except ApiError:
            cache[task_id] = describe_payload(None)
    return cache[task_id]


def environments_by_pool():
    with lock:
        return {environment["pool"]: environment for environment in load_state().values()}


def execution_view(task, environments, function):
    environment = environments.get(task["pool"])
    end = task.get("finished_at_ms")
    start = task.get("started_at_ms")
    return {
        "id": task["id"],
        "environment": environment["name"] if environment else task["pool"],
        "environment_id": environment["id"] if environment else None,
        "status": task["status"],
        "function": function.get("function"),
        "module": function.get("module"),
        "file": function.get("file"),
        "encoding": function.get("encoding"),
        "node": task.get("node"),
        "error": task.get("error"),
        "created_at_ms": task["created_at_ms"],
        "started_at_ms": start,
        "finished_at_ms": end,
        "duration_ms": end - start if end and start else None,
        "metrics": task.get("metrics"),
    }


def owned_task(task_id):
    try:
        task = core("GET", f"/v1/tasks/{quote(task_id, safe='')}")
    except ApiError as error:
        if error.status in (400, 404):
            raise ApiError(404, "execution not found") from None
        raise
    if task.get("owner") != OWNER:
        raise ApiError(404, "execution not found")
    return task


def list_executions(query):
    global function_cache
    limit =min(int((query.get("limit") or ["200"])[0]), 1000)
    params = {"owner": OWNER, "limit": limit}
    environment = (query.get("environment") or [None])[0]
    if environment:
        with lock:
            params["pool"] = find(load_state(), environment)["pool"]
    tasks = core("GET", "/v1/tasks?" + urlencode(params))
    environments = environments_by_pool()
    with function_lock:
        cache = load_functions()
        before = len(cache)
        views = [execution_view(task, environments, function_of(task["id"], cache))
                 for task in tasks]
        if len(cache) != before and not environment:
            function_cache = save_functions(cache, {task["id"] for task in tasks})
    return views


def submit_execution(body):
    """Run Python code in an environment: the plugin's entry point for SDKs."""
    if "payload" not in body:
        raise ApiError(400, "payload is required")
    with lock:
        environment = find(load_state(), str(body.get("environment") or ""))
    task = core("POST", "/v1/tasks", {
        "pool": environment["pool"],
        "payload": body["payload"],
        "queue": bool(body.get("queue", True)),
        "variables": body.get("variables") or {},
        "secrets": body.get("secrets") or {},
    })
    function = describe_payload(body["payload"])
    with function_lock:
        load_functions()[task["id"]] = function
    return execution_view(task, {environment["pool"]: environment}, function)


def execution_details(task_id):
    task = owned_task(task_id)
    with function_lock:
        function = function_of(task["id"], load_functions())
    view = execution_view(task, environments_by_pool(), function)
    view["logs"] = core("GET", f"/v1/tasks/{task['id']}/logs")
    view["result"] = None
    if task["status"] in FINISHED:
        view["result"] = core("GET", f"/v1/tasks/{task['id']}/result").get("value")
    return view


def execution_route(method, parts, body, query):
    if parts == ["executions"]:
        if method == "GET":
            return 200, list_executions(query)
        if method == "POST":
            return 202, submit_execution(body)
    if len(parts) >= 2 and parts[0] == "executions" and method == "GET":
        task = owned_task(parts[1])
        if len(parts) == 2:
            return 200, execution_details(task["id"])
        if len(parts) == 3 and parts[2] in ("wait", "result", "logs", "payload"):
            return 200, core("GET", f"/v1/tasks/{task['id']}/{parts[2]}", timeout=60)
    return None


# --- HTTP -------------------------------------------------------------------------

def route(method, path, body, query=None):
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if parts[:1] == ["gui"] and parts[1:2] == ["api"]:
        parts = parts[1:]
    if parts == ["health"]:
        return 200, {"ok": True}
    if parts[:1] != ["api"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
    handled = execution_route(method, parts, body, query or {})
    if handled is not None:
        return handled
    if parts == ["images"] and method == "GET":
        return 200, core("GET", "/v1/images")
    if parts == ["environments"]:
        if method == "GET":
            pools = pools_by_name()
            with lock:
                state = load_state()
            return 200, sorted((view(item, pools) for item in state.values()),
                               key=lambda item: item["name"].lower())
        if method == "POST":
            return 201, create_environment(body)
    if len(parts) == 2 and parts[0] == "environments":
        if method == "GET":
            with lock:
                environment = find(load_state(), parts[1])
            return 200, view(environment, pools_by_name())
        if method == "PUT":
            return 200, update_environment(parts[1], body)
        if method == "DELETE":
            delete_environment(parts[1])
            return 204, None
    if len(parts) == 3 and parts[0] == "environments" and parts[2] == "restart" and method == "POST":
        return 200, restart_environment(parts[1])
    raise ApiError(404, "not found")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "core"

    def log_message(self, format, *args):
        print(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}", file=sys.stderr)

    def send(self, status, payload, content_type="application/json"):
        data = b"" if payload is None else (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if status != 204:
            self.wfile.write(data)

    def handle_method(self):
        url = urlsplit(self.path)
        path = url.path
        try:
            if self.command == "GET" and path.rstrip("/") in ("/gui", "/gui/index.html"):
                return self.send(200, (HERE / "gui" / "index.html").read_bytes(),
                                 "text/html; charset=utf-8")
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            status, payload = route(self.command, path, body, parse_qs(url.query))
            self.send(status, payload)
        except ApiError as error:
            self.send(error.status, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self.send(400, {"error": str(error)})
        except Exception as error:
            traceback.print_exc()
            self.send(500, {"error": str(error)})

    do_GET = do_POST = do_PUT = do_DELETE = handle_method


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    Path(PLUGIN_SOCKET).unlink(missing_ok=True)
    server = ThreadingUnixStreamServer(PLUGIN_SOCKET, Handler)
    server.daemon_threads = True
    threading.Thread(target=reconcile, daemon=True).start()
    print(f"{PLUGIN} plugin listening on {PLUGIN_SOCKET}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
