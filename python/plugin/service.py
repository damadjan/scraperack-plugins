"""ScrapeRack Python environments plugin.

Builds immutable virtualenv disks for Python package sets and keeps one
ScrapeRack pool per environment. Executions go through the core API
(``POST /v1/executions`` with the environment ID or name), so the plugin is
never on the data path.

Standard library only: the plugin installs without downloading anything.
"""
import http.client
import json
import os
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
from urllib.parse import unquote, urlsplit

PLUGIN = os.environ.get("SCRAPERACK_PLUGIN_NAME", "python")
PLUGIN_SOCKET = os.environ.get("SCRAPERACK_PLUGIN_SOCKET", "/tmp/scraperack-python.sock")
CORE_SOCKET = os.environ.get("SCRAPERACK_CORE_SOCKET", "/tmp/scraperack-core.sock")
DATA = Path(os.environ.get("SCRAPERACK_PLUGIN_DATA", "data"))
GUEST_DISK = os.environ.get("SCRAPERACK_PLUGIN_GUEST_DISK") or None
HERE = Path(__file__).resolve().parent

VENV_MOUNT = "/tmp/environment"
GUEST_MOUNT = "/tmp/scraperack/python"
STATE = DATA / "environments.json"
MIGRATED = DATA / "migrated-environments.json"

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


# --- HTTP -------------------------------------------------------------------------

def route(method, path, body):
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if parts[:1] == ["gui"] and parts[1:2] == ["api"]:
        parts = parts[1:]
    if parts == ["health"]:
        return 200, {"ok": True}
    if parts[:1] != ["api"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
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
        path = urlsplit(self.path).path
        try:
            if self.command == "GET" and path.rstrip("/") in ("/gui", "/gui/index.html"):
                return self.send(200, (HERE / "gui" / "index.html").read_bytes(),
                                 "text/html; charset=utf-8")
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            status, payload = route(self.command, path, body)
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
