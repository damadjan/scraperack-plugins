#!/usr/bin/python3
"""Workspace executor: one task is one desktop session on the workspace's volume.

Before the pool snapshot (preload ``golden-desktop``) it starts the whole desktop: KasmVNC's
Xvnc (it serves its web client and the WebSocket on port 6080), a session D-Bus and XFCE as
``user``, with a fresh home folder on a tmpfs. Every workspace restores with that desktop already running.

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
WEB_PORT = 6080
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
    temporary = PASSWORD_FILE.with_suffix(".tmp")
    temporary.write_bytes(vnc_obfuscate(password))
    temporary.chmod(0o600)
    temporary.replace(PASSWORD_FILE)


# --- VNC password file --------------------------------------------------------------------
# A VncAuth password file holds the password DES-encrypted with VNC's fixed key (what
# ``vncpasswd -f`` writes). KasmVNC ships no such tool and Python has no DES, so this is
# plain DES: encryption only, one block.

_PC1 = (57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3,
        60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
        29, 21, 13, 5, 28, 20, 12, 4)
_PC2 = (14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
_E = (32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
_S = (
    "e4d12fb83a6c5907 0f74e2d1a6cb9538 41e8d62bfc973a50 fc8249175b3ea06d",
    "f18e6b34972dc05a 3d47f28ec01a69b5 0e7ba4d158c6932f d8a13f42b67c05e9",
    "a09e63f51dc7b428 d709346a285ecbf1 d6498f30b12c5ae7 1ad069874fe3b52c",
    "7de3069a1285bc4f d8b56f03472c1ae9 a690cb7df13e5284 3f06a1d8945bc72e",
    "2c417ab6853fd0e9 eb2c47d150fa3986 421bad78f9c5630e b8c71e2d6f09a453",
    "c1af92680d34e75b af427c9561de0b38 9ef528c3704a1db6 432c95fabe17608d",
    "4b2ef08d3c975a61 d0b7491ae35c2f86 14bdc37eaf680592 6bd814a7950fe23c",
    "d2846fb1a93e50c7 1fd8a374c56b0e92 7b419ce206adf358 21e74a8dfc90356b",
)
_SBOX = tuple(tuple(int(c, 16) for c in rows.replace(" ", "")) for rows in _S)


def _permute(value, table, width):
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def _des_encrypt(key, block):
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    subkeys = []
    for shift in _SHIFTS:
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for subkey in subkeys:
        x = _permute(right, _E, 32) ^ subkey
        s = 0
        for i in range(8):
            chunk = (x >> (42 - 6 * i)) & 0x3F
            row = ((chunk & 0x20) >> 4) | (chunk & 1)
            s = (s << 4) | _SBOX[i][row * 16 + ((chunk >> 1) & 0xF)]
        left, right = right, left ^ _permute(s, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


# VNC's fixed key, each byte's bits reversed as VNC's DES variant expects.
_VNC_KEY = bytes(int(f"{b:08b}"[::-1], 2) for b in (23, 82, 107, 6, 35, 78, 88, 7))


def vnc_obfuscate(password):
    """Contents of a VncAuth password file; VNC uses the first 8 characters."""
    return _des_encrypt(_VNC_KEY, password.encode()[:8].ljust(8, b"\0"))


def start_display():
    LOGS.mkdir(parents=True, exist_ok=True)
    output = (LOGS / "display.log").open("ab")
    if "Xkasmvnc" not in processes():
        for stale in ("/tmp/.X1-lock", str(X_SOCKET)):
            Path(stale).unlink(missing_ok=True)
        # KasmVNC serves its web client and the VNC WebSocket itself; browsers connect to it
        # directly. VncAuth with the start's password: no HTTP basic auth (a page embedded
        # in another cannot send it) and no TLS (the portal and the nodes use plain HTTP).
        # A viewer reconnecting with an earlier start's password must not lock its user
        # out, so no blacklist. A fixed public IP keeps it from asking STUN servers on the
        # internet for one at start (that is only for its optional WebRTC transport).
        subprocess.Popen(
            ["Xkasmvnc", DISPLAY, "-geometry", "1280x800", "-depth", "24",
             "-websocketPort", str(WEB_PORT), "-interface", "0.0.0.0",
             "-httpd", str(web_root()), "-DisableBasicAuth", "1", "-sslOnly", "0",
             "-SecurityTypes", "VncAuth", "-rfbauth", str(PASSWORD_FILE), "-publicIP", "127.0.0.1",
             "-BlacklistThreshold", "1000000", "-AlwaysShared", "-ac", "-nolisten", "tcp",
             "-desktop", "Workspace"],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


KASMVNC_WEB = Path("/usr/share/kasmvnc/www")
# KasmVNC's client answers VncAuth with an empty password (Kasm uses HTTP basic auth
# instead); this makes it answer with the password it was given.
CLIENT_PATCH = (b'this._rfbCredentials.password="";',
                b'this._rfbCredentials.password=this._rfbCredentials.password||"";')


def web_root():
    """KasmVNC's web client plus the portal's workspace page, served by Xvnc.

    Links to the image's files, except the client script, which is patched for VncAuth.
    """
    root = Path("/run/kasmvnc-web")
    (root / "assets").mkdir(parents=True, exist_ok=True)
    for source, target in ((KASMVNC_WEB, root), (KASMVNC_WEB / "assets", root / "assets")):
        for entry in source.iterdir():
            link = target / entry.name
            if entry.name == "assets" or link.exists() or link.is_symlink():
                continue
            if entry.suffix == ".js" and CLIENT_PATCH[0] in entry.read_bytes():
                link.write_bytes(entry.read_bytes().replace(*CLIENT_PATCH))
            else:
                link.symlink_to(entry)
    if not any(CLIENT_PATCH[1] in path.read_bytes() for path in (root / "assets").glob("*.js")
               if not path.is_symlink()):
        log("KasmVNC client not patched for VncAuth: logins will fail")
    shutil.copyfile(Path(__file__).resolve().parent / "viewer.html", root / "workspace.html")
    return root


def display_ready():
    # The X socket and the web client, not a VNC connection (that would count as a login).
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
