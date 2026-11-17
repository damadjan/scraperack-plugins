"""Scraperack workspaces plugin: long-lived desktops in microVMs.

Templates: a Scraperack image (with XFCE and KasmVNC) plus VM size; the configuration
workspaces are made from. Each template has a pool whose snapshot holds a running desktop,
and a volume mount at /mnt/workspace.

Workspaces: copies of a template, one volume each, holding the home folder and changes to system folders. Start
runs one core task with the volume; the guest executor starts the desktop session.
Suspend saves the running microVM with the volume; the next start resumes it. When the
workspace's node is full, the core moves the volume to a node with room first.

Standard library only: the plugin installs without downloading anything.
"""
import http.client
import json
import os
import re
import secrets
import socket
import string
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

PLUGIN = os.environ.get("SCRAPERACK_PLUGIN_NAME", "workspaces")
PLUGIN_SOCKET = os.environ.get("SCRAPERACK_PLUGIN_SOCKET", "/tmp/scraperack-workspaces.sock")
CORE_SOCKET = os.environ.get("SCRAPERACK_CORE_SOCKET", "/tmp/scraperack-core.sock")
DATA = Path(os.environ.get("SCRAPERACK_PLUGIN_DATA", "data"))
GUEST_DISK = os.environ.get("SCRAPERACK_PLUGIN_GUEST_DISK") or None
HERE = Path(__file__).resolve().parent

GUEST_MOUNT = "/tmp/scraperack/workspaces"
VOLUME_MOUNT = "/mnt/workspace"
TEMPLATES = DATA / "templates.json"
WORKSPACES = DATA / "workspaces.json"
FINISHED = {"succeeded", "failed", "timed_out"}
ACTIVE = {"queued", "moving", "starting", "running", "stopping", "suspending"}
VNC_PORT, CONTROL_PORT = 6080, 9300
SESSION_TIMEOUT = 30 * 24 * 3600  # a running workspace ends after 30 days
STOP_GRACE = 20                   # seconds for a logout before the core stops the VM
MONITOR_INTERVAL = 2.0

TEMPLATE_DEFAULTS = {"cpu_millis": 2000, "memory_mib": 2048, "disk_gib": 10}
lock = threading.RLock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# --- Core API over the plugin's private Unix socket ---------------------------------------

class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def core(method, path, body=None, timeout=60):
    connection = UnixConnection(CORE_SOCKET, timeout=timeout)
    try:
        headers, payload = {}, None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
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


# --- State ----------------------------------------------------------------------------------

def load(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save(path, value):
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
    temporary.replace(path)


def now_ms():
    return int(time.time() * 1000)


def find(records, key, kind):
    if key in records:
        return records[key]
    for record in records.values():
        if record["name"] == key:
            return record
    raise ApiError(404, f"{kind} not found: {key}")


def check_name(name, kind):
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._ -]{1,64}", name):
        raise ApiError(400, f"{kind} names may use 1-64 letters, digits, spaces, '.', '_' and '-'")
    return name


# --- Templates: image and VM size, one pool each ----------------------------------------------

def normalize_template(request, current=None):
    template = {**TEMPLATE_DEFAULTS, **(current or {})}
    for field in ("name", "image", *TEMPLATE_DEFAULTS):
        if field in request:
            template[field] = request[field]
    template["name"] = check_name(template.get("name"), "template")
    if not template.get("image"):
        raise ApiError(400, "image (a Scraperack image id) is required")
    for field in TEMPLATE_DEFAULTS:
        template[field] = int(template[field])
        if template[field] < 1:
            raise ApiError(400, f"{field} must be positive")
    if template["memory_mib"] < 512:
        raise ApiError(400, "a template needs at least 512 MiB of memory")
    return template


def pool_spec(template):
    if not GUEST_DISK:
        raise ApiError(500, "the plugin's guest disk is missing")
    return {
        "name": template["pool"],
        "image": template["image"],
        "warm_instances": 0,
        "max_instances": None,
        "cpu_millis": template["cpu_millis"],
        "memory_mib": template["memory_mib"],
        "disk_mib": 1024,
        "timeout_secs": SESSION_TIMEOUT + 3600,
        "idle_timeout_secs": 0,
        "reuse": False,
        "network": True,
        "ports": [{"name": name, "guest_port": port, "host_port": None, "protocol": "tcp"}
                  for name, port in (("vnc", VNC_PORT), ("control", CONTROL_PORT))],
        "preload": ["golden-desktop"],  # the executor starts the display before the snapshot
        "disks": [{"key": GUEST_DISK, "mount": GUEST_MOUNT}],
        "executor": ["/usr/bin/python3", f"{GUEST_MOUNT}/executor.py"],
        "volume_mount": VOLUME_MOUNT,
        "aliases": [template["id"]],
        "display_name": f"Workspaces: {template['name']}",
    }


def apply_pool(template):
    spec = pool_spec(template)
    try:
        return core("PUT", f"/v1/pools/{template['pool']}", spec)
    except ApiError as error:
        if error.status != 404:
            raise
    return core("POST", "/v1/pools", spec)


def pools_by_name():
    return {pool["name"]: pool for pool in core("GET", "/v1/pools")}


def template_view(template, pools, workspaces=None):
    view = {**template, "ready": bool(pools.get(template["pool"], {}).get("ready"))}
    if workspaces is not None:
        view["workspaces"] = sum(w["template"] == template["id"] for w in workspaces.values())
    return view


def list_templates():
    pools = pools_by_name()
    with lock:
        templates, workspaces = load(TEMPLATES), load(WORKSPACES)
    return sorted((template_view(t, pools, workspaces) for t in templates.values()),
                  key=lambda t: t["name"].lower())


def get_template(key):
    with lock:
        template = find(load(TEMPLATES), key, "template")
        workspaces = load(WORKSPACES)
    return template_view(template, pools_by_name(), workspaces)


def save_template(key, request):
    with lock:
        templates = load(TEMPLATES)
        current = dict(find(templates, key, "template")) if key else None
    template = normalize_template(request, current)
    if any(other["name"] == template["name"] and other["id"] != template.get("id")
           for other in templates.values()):
        raise ApiError(409, "template name already exists")
    if not key:
        template["id"] = str(uuid.uuid4())
        template["pool"] = f"workspaces-{template['id']}"
    apply_pool(template)
    with lock:
        templates = load(TEMPLATES)
        templates[template["id"]] = template
        save(TEMPLATES, templates)
    return get_template(template["id"])


def delete_template(key):
    with lock:
        template = find(load(TEMPLATES), key, "template")
        if any(w["template"] == template["id"] for w in load(WORKSPACES).values()):
            raise ApiError(409, "workspaces still use this template; delete them or move them to "
                                "another template first")
    try:
        core("DELETE", f"/v1/pools/{template['pool']}")
    except ApiError as error:
        if error.status != 404:
            raise
    with lock:
        templates = load(TEMPLATES)
        templates.pop(template["id"], None)
        save(TEMPLATES, templates)


def migrate():
    """Data from before templates were called desktops (plugin 0.3 and older)."""
    old = DATA / "desktops.json"
    with lock:
        if old.exists() and not TEMPLATES.exists():
            old.replace(TEMPLATES)
        workspaces = load(WORKSPACES)
        if any("desktop" in w for w in workspaces.values()):
            for workspace in workspaces.values():
                if "desktop" in workspace:
                    workspace["template"] = workspace.pop("desktop")
            save(WORKSPACES, workspaces)


# --- Workspaces --------------------------------------------------------------------------------

def update_workspace(workspace_id, **changes):
    with lock:
        workspaces = load(WORKSPACES)
        if workspace_id not in workspaces:
            return None
        workspaces[workspace_id].update(changes)
        save(WORKSPACES, workspaces)
        return workspaces[workspace_id]


def get_record(key):
    with lock:
        return dict(find(load(WORKSPACES), key, "workspace"))


def vms_by_task():
    return {vm["task"]: vm for vm in core("GET", "/v1/vms") if vm.get("task")}


def volumes_by_name():
    return {volume["name"]: volume for volume in core("GET", "/v1/volumes")}


def nodes_by_id():
    try:
        return {node["id"]: node for node in core("GET", "/v1/nodes")}
    except Exception:
        return {}


def control(workspace, method, path, timeout=2):
    host, port = workspace.get("host"), (workspace.get("ports") or {}).get("control")
    if not host or not port:
        raise OSError("control port is not published")
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method,
                                     data=b"" if method == "POST" else None)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def workspace_view(workspace, templates, volumes=None, nodes=None):
    template = templates.get(workspace["template"])
    volume = (volumes or {}).get(workspace["volume"]) or {}
    view = {key: workspace.get(key) for key in (
        "id", "name", "status", "template", "timezone", "disk_gib", "task", "created_at_ms",
        "started_at_ms", "ready_at_ms", "stopped_at_ms", "last_reason", "error", "host",
        "node", "node_name", "volume", "resumed")}
    view["template_name"] = template["name"] if template else None
    view["volume_node"] = volume.get("node")
    view["volume_node_name"] = ((nodes or {}).get(volume.get("node")) or {}).get("name")
    view["migration"] = volume.get("migration")
    view["migration_error"] = volume.get("migration_error")
    ports = workspace.get("ports") or {}
    live = workspace["status"] in ("starting", "running") and workspace.get("host") and ports.get("vnc")
    password = quote(workspace.get("password") or "")
    base = f"http://{workspace['host']}:{ports['vnc']}" if live else None
    view["url"] = (f"{base}/vnc.html?autoconnect=1&resize=remote&reconnect=1&password={password}"
                   if live else None)
    # The portal's workspace page around KasmVNC's client; the password stays in the fragment.
    view["viewer_url"] = f"{base}/workspace.html#password={password}" if live else None
    return view


def list_workspaces():
    with lock:
        workspaces = list(load(WORKSPACES).values())
        templates = load(TEMPLATES)
    volumes, nodes = volumes_by_name(), nodes_by_id()
    views = [workspace_view(w, templates, volumes, nodes) for w in workspaces]
    return sorted(views, key=lambda w: w["name"].lower())


def get_workspace(key):
    workspace = get_record(key)
    with lock:
        templates = load(TEMPLATES)
    return workspace_view(workspace, templates, volumes_by_name(), nodes_by_id())


def create_workspace(request):
    name = check_name(request.get("name"), "workspace")
    with lock:
        templates = load(TEMPLATES)
        workspaces = load(WORKSPACES)
    if not templates:
        raise ApiError(400, "add a template first")
    key = request.get("template")
    if key:
        template = find(templates, str(key), "template")
    elif len(templates) == 1:
        template = next(iter(templates.values()))
    else:
        raise ApiError(400, "template is required when more than one exists")
    if any(w["name"] == name for w in workspaces.values()):
        raise ApiError(409, "workspace name already exists")
    disk_gib = int(request.get("disk_gib") or template["disk_gib"])
    if not 1 <= disk_gib <= 4096:
        raise ApiError(400, "disk_gib must be 1-4096")
    workspace_id = str(uuid.uuid4())
    workspace = {
        "id": workspace_id, "name": name, "template": template["id"],
        "volume": f"workspace-{workspace_id}", "disk_gib": disk_gib,
        "timezone": str(request.get("timezone") or "UTC"), "status": "stopped",
        "task": None, "created_at_ms": now_ms(),
    }
    core("POST", "/v1/volumes", {"name": workspace["volume"], "size_mib": disk_gib * 1024})
    with lock:
        workspaces = load(WORKSPACES)
        workspaces[workspace_id] = workspace
        save(WORKSPACES, workspaces)
    if request.get("start", True):
        return start_workspace(workspace_id)
    return get_workspace(workspace_id)


def edit_workspace(key, request):
    workspace = get_record(key)
    changes = {}
    if "name" in request:
        changes["name"] = check_name(request["name"], "workspace")
        with lock:
            if any(w["name"] == changes["name"] and w["id"] != workspace["id"]
                   for w in load(WORKSPACES).values()):
                raise ApiError(409, "workspace name already exists")
    if "timezone" in request:
        changes["timezone"] = str(request["timezone"] or "UTC")
    if "template" in request:
        with lock:
            template = find(load(TEMPLATES), str(request["template"]), "template")["id"]
        if template != workspace["template"]:
            if workspace["status"] in ACTIVE:
                raise ApiError(409, "shut the workspace down before changing its template")
            changes["template"] = template
    update_workspace(workspace["id"], **changes)
    return get_workspace(workspace["id"])


def new_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))  # VNC uses 8 characters


def start_workspace(key):
    workspace = get_record(key)
    if workspace["status"] in ACTIVE:
        return get_workspace(workspace["id"])
    with lock:
        template = load(TEMPLATES).get(workspace["template"])
    if template is None:
        raise ApiError(409, "the workspace's template was deleted; pick another one")
    password = new_password()
    task = core("POST", "/v1/tasks", {
        "pool": template["pool"], "volume": workspace["volume"], "queue": True,
        "secrets": {"VNC_PASSWORD": password},
        "payload": {"encoding": "workspace-session", "workspace": workspace["id"],
                    "settings": {"timezone": workspace.get("timezone") or "UTC"},
                    "timeout": SESSION_TIMEOUT},
    })
    update_workspace(workspace["id"], task=task["id"], status="queued", password=password,
                     error=None, last_reason=None, started_at_ms=None, ready_at_ms=None,
                     stopped_at_ms=None, stop_requested=None, host=None, ports=None,
                     node=None, node_name=None, resumed=None)
    return get_workspace(workspace["id"])


def suspend_workspace(key):
    workspace = get_record(key)
    if workspace["status"] not in ("starting", "running") or not workspace.get("task"):
        raise ApiError(409, "only a running workspace can be suspended")
    core("POST", f"/v1/tasks/{workspace['task']}/stop", {"suspend": True})
    update_workspace(workspace["id"], status="suspending", stop_requested="suspend")
    return get_workspace(workspace["id"])


def stop_workspace(key):
    workspace = get_record(key)
    if workspace["status"] not in ACTIVE or not workspace.get("task"):
        return get_workspace(workspace["id"])
    if workspace["status"] in ("queued", "moving"):
        core("POST", f"/v1/tasks/{workspace['task']}/stop", {})
    else:
        try:
            control(workspace, "POST", "/stop")  # logs the user out; the VM ends after
        except OSError:
            core("POST", f"/v1/tasks/{workspace['task']}/stop", {})
    update_workspace(workspace["id"], status="stopping", stop_requested="stop",
                     stop_requested_at_ms=now_ms())
    return get_workspace(workspace["id"])


def delete_workspace(key):
    workspace = get_record(key)
    if workspace["status"] in ACTIVE:
        raise ApiError(409, "stop the workspace first")
    try:
        core("DELETE", f"/v1/volumes/{workspace['volume']}")
    except ApiError as error:
        if error.status != 404:
            raise
    with lock:
        workspaces = load(WORKSPACES)
        workspaces.pop(workspace["id"], None)
        save(WORKSPACES, workspaces)


# --- Monitor ---------------------------------------------------------------------------------

def monitor_once():
    with lock:
        workspaces = list(load(WORKSPACES).values())
    if not workspaces:
        return
    volumes = volumes_by_name()
    active = [w for w in workspaces if w.get("task")]
    vms = vms_by_task() if active else {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda w: check_workspace(w, vms, volumes), workspaces))


def check_workspace(workspace, vms, volumes):
    volume = volumes.get(workspace["volume"]) or {}
    if not workspace.get("task"):
        status = "suspended" if volume.get("suspended") else "stopped"
        if workspace["status"] != status:
            update_workspace(workspace["id"], status=status)
        return
    try:
        task = core("GET", f"/v1/tasks/{workspace['task']}")
    except ApiError as error:
        if error.status == 404:
            update_workspace(workspace["id"], task=None, status="stopped", error="task is gone")
        return
    if task["status"] in FINISHED:
        return finish(workspace, task)
    vm = vms.get(task["id"])
    changes = {"node": task.get("node")}
    if vm is not None:
        changes["host"] = vm.get("node_address")
        changes["node_name"] = vm.get("node_name")
        changes["ports"] = {p["name"]: p["host_port"] for p in vm.get("ports", [])}
    if task.get("started_at_ms") and not workspace.get("started_at_ms"):
        changes["started_at_ms"] = task["started_at_ms"]
    requested = workspace.get("stop_requested")
    if task["status"] == "queued":
        changes["status"] = "moving" if volume.get("migration") else "queued"
        if requested:
            changes["status"] = "stopping"
    elif requested == "suspend":
        changes["status"] = "suspending"
    elif requested == "stop":
        changes["status"] = "stopping"
        waited = now_ms() - (workspace.get("stop_requested_at_ms") or now_ms())
        if waited > STOP_GRACE * 1000 and not workspace.get("forced"):
            try:
                core("POST", f"/v1/tasks/{task['id']}/stop", {})
                changes["forced"] = True
            except ApiError:
                pass
    elif vm is None or task["status"] != "running":
        changes["status"] = "starting"
    else:
        try:
            state = control({**workspace, **changes}, "GET", "/status").get("state")
        except OSError:
            state = None
        changes["status"] = "running" if state == "ready" else "starting"
        if state == "ready" and not workspace.get("ready_at_ms"):
            changes["ready_at_ms"] = now_ms()
    with lock:
        current = load(WORKSPACES).get(workspace["id"])
        if current is None or current.get("task") != workspace.get("task"):
            return
        if current.get("stop_requested") != workspace.get("stop_requested"):
            changes.pop("status", None)  # a stop or suspend arrived meanwhile
        update_workspace(workspace["id"], **changes)


def finish(workspace, task):
    value = {}
    if task["status"] == "succeeded":
        try:
            value = (core("GET", f"/v1/tasks/{task['id']}/result").get("value") or {})
        except ApiError:
            pass
        if value.get("encoding") == "json":  # the core's own results, such as a suspend
            value = value.get("data") or {}
    if value.get("suspended"):
        status, reason, error = "suspended", "suspended", None
    else:
        status = "stopped"
        reason = value.get("reason") or ("timeout" if task["status"] == "timed_out" else None)
        error = None
        if task["status"] == "failed" and workspace.get("stop_requested") != "stop":
            reason, error = "failed", task.get("error")
        elif task["status"] == "failed":
            reason = "stopped"
    update_workspace(workspace["id"], task=None, status=status, last_reason=reason, error=error,
                     stopped_at_ms=task.get("finished_at_ms") or now_ms(), host=None,
                     ports=None, stop_requested=None, stop_requested_at_ms=None, forced=None,
                     password=None)


def report(message=None):
    """Log the current exception. Never raises: with a full disk even writing the log fails,
    and an error escaping here would end the monitor for good."""
    try:
        if message:
            print(message, file=sys.stderr)
        traceback.print_exc()
    except Exception:
        pass


def monitor():
    while True:
        try:
            monitor_once()
        except Exception:
            report()
        time.sleep(MONITOR_INTERVAL)


def reconcile():
    """Make the core's pools match this plugin's templates after a (re)start."""
    while True:
        try:
            core("GET", "/v1/pools")
            break
        except Exception:
            time.sleep(1)
    with lock:
        templates = list(load(TEMPLATES).values())
    for template in templates:
        try:
            apply_pool(template)
        except Exception:
            report(f"reconcile {template['name']} failed")


def background():
    """Reconcile once, then monitor workspaces forever."""
    try:
        reconcile()
    except Exception:
        report("reconcile failed")
    monitor()


# --- HTTP ---------------------------------------------------------------------------------------

def route(method, path, body, query):
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if parts[:1] == ["gui"] and parts[1:2] == ["api"]:
        parts = parts[1:]
    if parts == ["health"]:
        return 200, {"ok": True}
    if parts[:1] != ["api"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
    if parts == ["workspaces"]:
        if method == "GET":
            return 200, list_workspaces()
        if method == "POST":
            return 201, create_workspace(body)
    if len(parts) == 2 and parts[0] == "workspaces":
        if method == "GET":
            return 200, get_workspace(parts[1])
        if method in ("PUT", "PATCH"):
            return 200, edit_workspace(parts[1], body)
        if method == "DELETE":
            delete_workspace(parts[1])
            return 204, None
    if len(parts) == 3 and parts[0] == "workspaces" and method == "POST":
        action = {"start": start_workspace, "suspend": suspend_workspace,
                  "stop": stop_workspace}.get(parts[2])
        if action:
            return 200, action(parts[1])
    if parts == ["images"] and method == "GET":
        return 200, core("GET", "/v1/images")
    if parts == ["templates"]:
        if method == "GET":
            return 200, list_templates()
        if method == "POST":
            return 201, save_template(None, body)
    if len(parts) == 2 and parts[0] == "templates":
        if method == "GET":
            return 200, get_template(parts[1])
        if method in ("PUT", "PATCH"):
            return 200, save_template(parts[1], body)
        if method == "DELETE":
            delete_template(parts[1])
            return 204, None
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
        try:
            if self.command == "GET" and url.path.rstrip("/") in ("/gui", "/gui/index.html"):
                return self.send(200, (HERE / "gui" / "index.html").read_bytes(),
                                 "text/html; charset=utf-8")
            if self.command == "GET" and url.path in ("/gui/portal", "/gui/portal/"):
                if url.path == "/gui/portal":  # relative links need the trailing slash
                    self.send_response(301)
                    self.send_header("Location", "portal/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self.send(200, (HERE / "gui" / "portal.html").read_bytes(),
                                 "text/html; charset=utf-8")
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(body, dict):
                raise ApiError(400, "the request body must be a JSON object")
            status, payload = route(self.command, url.path, body, parse_qs(url.query))
            self.send(status, payload)
        except ApiError as error:
            self.send(error.status, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self.send(400, {"error": str(error)})
        except Exception as error:
            report()
            self.send(500, {"error": str(error)})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_method


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    migrate()
    Path(PLUGIN_SOCKET).unlink(missing_ok=True)
    server = ThreadingUnixStreamServer(PLUGIN_SOCKET, Handler)
    server.daemon_threads = True
    threading.Thread(target=background, daemon=True).start()
    print(f"{PLUGIN} plugin listening on {PLUGIN_SOCKET}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
