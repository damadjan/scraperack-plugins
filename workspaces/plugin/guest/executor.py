#!/usr/bin/python3
"""Workspace executor: one task is one desktop session on the workspace's volume.

Before the pool snapshot (preload ``golden-desktop``) it starts the whole desktop: TigerVNC's
Xvnc, noVNC's websockify, a session D-Bus and XFCE as ``user``, with a fresh home folder
on a tmpfs. Every workspace restores with that desktop already running.

A session task then puts the workspace volume under it: a new workspace copies the fresh
home folder onto the volume; an existing one mounts its own home and the running XFCE
reloads its settings. Changes to system folders (installed software) are kept on the
volume too. The task sets the start's VNC password and replies when the user logs out,
the plugin stops it through the control port (9300), or it times out.

When a suspended workspace resumes, the request from before the suspend is abandoned (its
connection closes) and the new task attaches to the desktop that is still running.

Standard library only; runs on the image's /usr/bin/python3.
"""
import json
import os
import pwd
import secrets
import select
import shutil
import signal
import socket
import string
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOCKET = Path("/tmp/scraperack-executor.sock")
VOLUME = Path("/mnt/workspace")
LOGS = Path("/tmp/workspace")
CONTROL_PORT = 9300
VNC_PORT, WEB_PORT = 5901, 6080
DISPLAY = ":1"
X_SOCKET = Path("/tmp/.X11-unix/X1")
PASSWORD_FILE = Path("/run/vnc/passwd")
PREPARED = Path("/run/workspace-volume")
# Changes under these folders (installed packages, settings) are kept on the volume.
SYSTEM_DIRS = ("/etc", "/opt", "/root", "/usr", "/var")
GOLDEN_BUDGET = 160  # seconds; the core waits at most 180 for the pool snapshot
# The desktop is up once these run; xfdesktop draws the wallpaper and icons last.
DESKTOP_PROCESSES = ("xfwm4", "xfce4-panel", "xfdesktop")


def account():
    try:
        entry = pwd.getpwnam("user")
        return entry.pw_uid, entry.pw_gid, Path(entry.pw_dir)
    except KeyError:
        return 1000, 1000, Path("/home/user")


UID, GID, HOME = account()
BUS = f"unix:path=/run/user/{UID}/bus"


def log(message):
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "executor.log").open("a") as file:
        file.write(f"{time.strftime('%H:%M:%S')} {message}\n")


def run(*command, check=True):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(command)}: {result.stdout.strip()}")
    return result


def mounted(path):
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False
    return any(line.split()[1] == str(path) for line in lines if len(line.split()) > 1)


def reachable(url, timeout=0.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 400
    except Exception:
        return False


def processes(uid=None):
    """Names of running processes, optionally only those of one user."""
    names = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if uid is not None and entry.stat().st_uid != uid:
                continue
            names.add((entry / "comm").read_text().strip())
        except OSError:
            continue
    return names


def user_pids():
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid == UID:
                    pids.append(int(entry.name))
            except OSError:
                pass
    return pids


# --- System, display and desktop: prepared before the pool snapshot --------------------------

def prepare_system():
    """Kernel filesystems and writable runtime folders that a desktop expects."""
    mounts = (
        ("/sys", ["-t", "sysfs", "sysfs"]),
        ("/dev/pts", ["-t", "devpts", "-o", "gid=5,mode=620,ptmxmode=666", "devpts"]),
        ("/dev/shm", ["-t", "tmpfs", "-o", "mode=1777", "tmpfs"]),
        ("/run", ["-t", "tmpfs", "-o", "mode=755", "tmpfs"]),
        ("/var/tmp", ["-t", "tmpfs", "-o", "mode=1777", "tmpfs"]),
    )
    for target, arguments in mounts:
        if not mounted(target):
            Path(target).mkdir(parents=True, exist_ok=True)
            run("mount", *arguments, target)
    runtime = Path(f"/run/user/{UID}")
    if not runtime.exists():
        runtime.mkdir(parents=True)
        os.chown(runtime, UID, GID)
        runtime.chmod(0o700)
    ice = Path("/tmp/.ICE-unix")  # the session manager's sockets; only root may create it
    ice.mkdir(exist_ok=True)
    ice.chmod(0o1777)
    Path("/run/vnc").mkdir(parents=True, exist_ok=True)
    Path("/run/dbus").mkdir(parents=True, exist_ok=True)
    if not Path("/run/machine-id").exists():
        new_machine_id()
    if not PASSWORD_FILE.exists():
        set_password(new_password())


def new_machine_id():
    # /etc/machine-id links here.
    Path("/run/machine-id").write_text(secrets.token_hex(16) + "\n")


def new_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def set_password(password):
    """Xvnc reads the password file at each login, so this applies to new connections."""
    data = subprocess.run(["vncpasswd", "-f"], input=f"{password}\n".encode(),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
    temporary = PASSWORD_FILE.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(PASSWORD_FILE)


def start_display():
    LOGS.mkdir(parents=True, exist_ok=True)
    output = (LOGS / "display.log").open("ab")
    if "Xvnc" not in processes():
        for stale in ("/tmp/.X1-lock", str(X_SOCKET)):
            Path(stale).unlink(missing_ok=True)
        # Only websockify, on this VM, talks to Xvnc: never lock it out.
        subprocess.Popen(
            ["Xvnc", DISPLAY, "-geometry", "1280x800", "-depth", "24", "-rfbport", str(VNC_PORT),
             "-localhost", "-SecurityTypes", "VncAuth", "-rfbauth", str(PASSWORD_FILE),
             "-BlacklistThreshold", "1000000", "-AlwaysShared", "-ac", "-nolisten", "tcp",
             "-desktop", "Workspace"],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    if "websockify" not in processes():
        websockify = shutil.which("websockify") or "/usr/bin/websockify"
        subprocess.Popen(
            [websockify, "--web", str(web_root()), str(WEB_PORT), f"127.0.0.1:{VNC_PORT}"],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def web_root():
    """noVNC from the image plus the portal's toolbar-less viewer, served by websockify."""
    root = Path("/run/novnc-web")
    root.mkdir(parents=True, exist_ok=True)
    for entry in Path("/usr/share/novnc").iterdir():
        link = root / entry.name
        if not link.exists():
            link.symlink_to(entry)
    shutil.copyfile(Path(__file__).resolve().parent / "viewer.html", root / "workspace.html")
    return root


def display_ready():
    # The X socket, not a VNC connection: Xvnc counts half-open connections as failed logins.
    return X_SOCKET.exists() and reachable(f"http://127.0.0.1:{WEB_PORT}/vnc.html")


def desktop_up():
    running = processes(UID)
    return all(name in running for name in DESKTOP_PROCESSES)


def session_env():
    return {
        "HOME": str(HOME), "USER": HOME.name, "LOGNAME": HOME.name, "SHELL": "/bin/bash",
        "DISPLAY": DISPLAY, "XDG_RUNTIME_DIR": f"/run/user/{UID}", "LANG": "C.UTF-8",
        "DBUS_SESSION_BUS_ADDRESS": BUS,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }


def as_user(command, wait=False, output=None):
    output = output or (LOGS / "session.log").open("ab")
    groups = [group for group in (27, 44, 29) if group != GID]  # sudo, video, audio
    process = subprocess.Popen(command, cwd="/", env=session_env(), stdout=output,
                               stderr=subprocess.STDOUT, user=UID, group=GID,
                               extra_groups=groups, start_new_session=True)
    if wait:
        process.wait(30)
    return process


def start_session():
    """Session bus and XFCE for ``user``; returns the xfce4-session process."""
    if not HOME.exists():
        shutil.copytree("/etc/skel", HOME, symlinks=True)
        chown_tree(HOME)
    if not Path(f"/run/user/{UID}/bus").exists():
        as_user(["dbus-daemon", "--session", "--fork", f"--address={BUS}"], wait=True)
    return as_user(["xfce4-session"])


def chown_tree(path):
    for root, folders, files in os.walk(path):
        for name in [root, *(os.path.join(root, item) for item in folders + files)]:
            os.lchown(name, UID, GID)


def golden():
    """Preload: bring the whole desktop up so it is in the pool snapshot. Idempotent."""
    started = time.time()
    if desktop.session is not None:
        # Resent after every restore: the desktop is in the snapshot, answer at once.
        return {"restored": True}
    prepare_system()
    set_hostname()
    if not mounted("/home"):
        run("mount", "-t", "tmpfs", "-o", "mode=755", "tmpfs", "/home")
    start_display()
    while time.time() < started + GOLDEN_BUDGET and not display_ready():
        time.sleep(0.2)
    with desktop.lock:
        if desktop.session is None or desktop.session.poll() is not None:
            desktop.session = start_session()
    while time.time() < started + GOLDEN_BUDGET and not desktop_up():
        time.sleep(0.5)
    time.sleep(2)  # let the desktop finish drawing before the snapshot
    result = {"display_ready": display_ready(), "desktop_ready": desktop_up(),
              "golden_ms": int((time.time() - started) * 1000)}
    log(f"golden {result}")
    return result


# --- Workspace volume ---------------------------------------------------------------------

def prepare_volume(timezone):
    """Put the home folder and system changes on the volume, once per microVM.

    Returns whether the volume already had a home folder (the desktop must reload it).
    """
    if PREPARED.exists():
        return False
    existing = False
    if not mounted(VOLUME):
        log("no volume mounted; the session's files will not be kept")
    else:
        homes = VOLUME / "home"
        existing = (homes / HOME.name).exists()
        homes.mkdir(exist_ok=True)
        if not existing:
            # A new workspace starts with the golden desktop's fresh home folder.
            run("cp", "-a", str(HOME), str(homes / HOME.name))
        run("mount", "--bind", str(homes), "/home")
        for folder in SYSTEM_DIRS:
            base = VOLUME / "system" / folder.strip("/")
            upper, work = base / "upper", base / "work"
            upper.mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            result = run("mount", "-t", "overlay", "overlay", "-o",
                         f"lowerdir={folder},upperdir={upper},workdir={work}", folder, check=False)
            if result.returncode:
                log(f"overlay {folder} failed: {result.stdout.strip()}")
        # The overlays hide what was mounted inside them; mount those again.
        if Path("/tmp/resolv.conf").exists():
            run("mount", "--bind", "/tmp/resolv.conf", "/etc/resolv.conf", check=False)
        run("mount", "-t", "tmpfs", "-o", "mode=1777", "tmpfs", "/var/tmp", check=False)
    set_timezone(timezone)
    # The desktop was started in the snapshot from "/"; new shells should start at home.
    bashrc = Path("/etc/bash.bashrc")
    line = '[ "$PWD" = / ] && cd ~  # scraperack workspace\n'
    try:
        if line not in bashrc.read_text():
            with bashrc.open("a") as file:
                file.write(line)
    except OSError as error:
        log(f"bash.bashrc: {error}")
    PREPARED.touch()
    return existing


def set_hostname():
    """A fixed name, set before the desktop starts: its session manager's address includes
    the hostname, so renaming the machine later cuts every program off from the session."""
    try:
        socket.sethostname("workspace")
    except OSError as error:
        log(f"hostname: {error}")


def set_timezone(timezone):
    zone = Path("/usr/share/zoneinfo") / (timezone or "UTC")
    if not zone.is_file() or ".." in (timezone or ""):
        zone = Path("/usr/share/zoneinfo/UTC")
    try:
        Path("/etc/localtime").unlink(missing_ok=True)
        Path("/etc/localtime").symlink_to(zone)
        Path("/etc/timezone").write_text(f"{zone.relative_to('/usr/share/zoneinfo')}\n")
    except OSError as error:
        log(f"timezone: {error}")


def restart_session():
    """Start XFCE again on the workspace's own home folder; the display keeps running.

    Programs are already in memory from the snapshot's desktop, so this is quick.
    """
    session = desktop.session
    if session is not None and session.poll() is None:
        try:
            os.killpg(session.pid, signal.SIGTERM)
            session.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for pid in user_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for stale in Path(f"/run/user/{UID}").glob("*"):
        if stale.name == "bus" or stale.is_socket():
            stale.unlink(missing_ok=True)
    return start_session()


# --- Desktop session ----------------------------------------------------------------------

class Desktop:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.owner = None
        self.attached = None
        self.stop_reason = None
        self.stop_requested = threading.Event()

    def running(self):
        return self.session is not None and self.session.poll() is None

    def status(self):
        with self.lock:
            running = self.running()
        state = "idle"
        if running and self.attached:
            state = "ready" if desktop_up() and display_ready() else "starting"
        if self.stop_requested.is_set() and running:
            state = "stopping"
        return {"state": state, "session_running": running, "display_ready": display_ready(),
                "attached_ms": self.attached}

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop_requested.set()


desktop = Desktop()


def end_session():
    """Ask the session to end, then make sure every process of the user is gone."""
    session = desktop.session
    if session is not None and session.poll() is None:
        try:
            os.killpg(session.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            session.wait(10)
        except subprocess.TimeoutExpired:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = user_pids()
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if pids and sig == signal.SIGTERM:
            time.sleep(3)
    os.sync()


def connection_closed(conn):
    readable, _, _ = select.select([conn], [], [], 0)
    if not readable:
        return False
    try:
        return conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def run_session(request, conn):
    wrapped = request.get("encoding") == "scraperack-invocation"
    payload = dict((request.get("payload") if wrapped else request) or {})
    secrets_ = (request.get("secrets") if wrapped else None) or {}
    settings = payload.get("settings") or {}
    timeout = max(60, int(payload.get("timeout") or 30 * 24 * 3600))
    token = object()
    started = time.time()
    resumed = PREPARED.exists()
    with desktop.lock:
        desktop.owner = token
        if not resumed:
            desktop.stop_reason = None
            desktop.stop_requested.clear()
    try:
        prepare_system()
        if not display_ready():
            start_display()
        set_password(secrets_.get("VNC_PASSWORD") or new_password())
        existing = prepare_volume(settings.get("timezone"))
        with desktop.lock:
            if not desktop.running():
                log("no desktop from the snapshot; starting one")
                new_machine_id()
                desktop.session = start_session()
            elif existing:
                desktop.session = restart_session()
            desktop.attached = int(time.time() * 1000)
    except Exception as error:
        log(f"session failed to start: {error}")
        return {"result": {"value": {"reason": "failed"}, "stdout": tail(), "stderr": ""},
                "error": f"{type(error).__name__}: {error}", "timed_out": False}
    home = "" if resumed else f" ({'own' if existing else 'new'} home)"
    log(f"session {'resumed' if resumed else 'attached'} for {payload.get('workspace')}{home} "
        f"after {int((time.time() - started) * 1000)} ms")
    deadline = started + timeout
    while True:
        if desktop.owner is not token or connection_closed(conn):
            # A newer task took over (the workspace resumed): leave the desktop running.
            log("detached from the session")
            return None
        if desktop.session.poll() is not None:
            reason = desktop.stop_reason or "logged_out"
            break
        if desktop.stop_requested.is_set():
            reason = desktop.stop_reason or "stopped"
            break
        if time.time() > deadline:
            reason = "timeout"
            break
        time.sleep(0.2)
    end_session()
    log(f"session ended: {reason}")
    value = {"reason": reason, "resumed": resumed,
             "duration_ms": int((time.time() - started) * 1000)}
    return {"result": {"value": value, "stdout": tail(), "stderr": ""}, "error": None,
            "timed_out": False}


def checks():
    """Fixed, read-only facts for troubleshooting a desktop that misbehaves."""
    lines = [line for line in Path("/proc/mounts").read_text().splitlines()
             if " overlay " in line or "/home" in line or "/mnt/workspace" in line]
    for command in (["ls", "-la", "/usr/bin/xfce4-terminal", "/usr/share/icons/hicolor/index.theme"],
                    ["gtk-update-icon-cache", "--validate", "/usr/share/icons/hicolor"]):
        try:
            result = subprocess.run(command, env=session_env(), user=UID, group=GID, timeout=10,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines.append(f"$ {' '.join(command)} -> {result.returncode}: {result.stdout[-800:]}")
        except Exception as error:
            lines.append(f"$ {' '.join(command)} -> {error}")
    return "\n".join(lines)


def tail_of(path, size=20000):
    try:
        return path.read_bytes()[-size:].decode(errors="replace")
    except OSError:
        return ""


def tail(size=20000):
    return "\n".join(f"# {name}\n{tail_of(LOGS / name, size)}"
                     for name in ("executor.log", "session.log"))


# --- Control port and executor socket -------------------------------------------------------

class Control(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            return self.reply(200, desktop.status())
        if self.path == "/logs":
            logs = {name: tail_of(LOGS / name) for name in
                    ("executor.log", "display.log", "session.log")}
            logs["checks"] = checks()
            return self.reply(200, logs)
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/stop":
            desktop.stop("stopped")
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})


def read(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def serve(conn):
    with conn:
        while header := read(conn, 4):
            request = json.loads(read(conn, int.from_bytes(header, "big")))
            if request.get("encoding") == "scraperack-preload":
                reply = {"result": {"value": golden(), "stdout": "", "stderr": ""},
                         "error": None, "timed_out": False}
            else:
                try:
                    reply = run_session(request, conn)
                except Exception as error:
                    reply = {"result": None, "error": f"{type(error).__name__}: {error}",
                             "timed_out": False}
                if reply is None:
                    return
            data = json.dumps(reply).encode()
            try:
                conn.sendall(len(data).to_bytes(4, "big") + data)
            except OSError:
                return


def main():
    control = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    SOCKET.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    server.listen(4)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
