"""Scraperack proxies plugin.

Collects free public proxies from the sources you add, tests them for protocol, speed, HTTPS
support and anonymity in microVMs, and serves the working ones through an API.

- Two pools, `proxies-scraper` and `proxies-tester`, run this plugin's executor (guest folder)
  with curl-cffi / aiohttp from a package disk the plugin builds from the chosen image.
- Everything is kept in one SQLite file in the plugin's data directory.
- The scheduler scrapes every source on an interval and tests proxies in rounds, never both at
  once (together they open too many connections for a home internet link).

Standard library only: the plugin installs without downloading anything.
"""
import base64
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from urllib.parse import parse_qs, unquote, urlsplit

import store
from geo import Geo

PLUGIN = os.environ.get("SCRAPERACK_PLUGIN_NAME", "proxies")
PLUGIN_SOCKET = os.environ.get("SCRAPERACK_PLUGIN_SOCKET", "/tmp/scraperack-proxies.sock")
CORE_SOCKET = os.environ.get("SCRAPERACK_CORE_SOCKET", "/tmp/scraperack-core.sock")
DATA = Path(os.environ.get("SCRAPERACK_PLUGIN_DATA", "data"))
GUEST_DISK = os.environ.get("SCRAPERACK_PLUGIN_GUEST_DISK") or None
HERE = Path(__file__).resolve().parent

VENV_MOUNT = "/tmp/environment"
GUEST_MOUNT = "/tmp/scraperack/proxies"
# cloudpickle: the Python executor baked into older images imports it with the package disk's Python.
PACKAGES = ["curl-cffi==0.17.0", "aiohttp==3.14.5", "aiohttp-socks==0.12.0", "cloudpickle==3.1.2"]
SCRAPER_POOL, TESTER_POOL, BUILDER_POOL = "proxies-scraper", "proxies-tester", "proxies-builder"
FINISHED = {"succeeded", "failed", "timed_out"}
RATE_RAMP_PER_MIN = 0.08
LOOP_SECONDS = 5


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


def guest_source(module, entry):
    """A `python-source` payload body: a guest module plus main(*params) calling its entry point.

    Both this plugin's executor and the Python executor baked into older images (which run
    their own executor whatever the pool says) run this format.
    """
    return (HERE / "guest" / f"{module}.py").read_text() + f"\n\ndef main(*params):\n    return {entry}(*params)\n"


SOURCES = {"scrape": guest_source("scrape", "scrape_batch"), "test": guest_source("check", "test_batch"),
           "build": guest_source("build", "build")}


def call(pool, name, *params):
    return run_task(pool, {"encoding": "python-source", "source": SOURCES[name], "params": list(params)})


def run_task(pool, payload):
    """Submit a task, wait for it, and return its value. Retries submits the core refuses for a while."""
    delay = 1
    for attempt in range(6):
        try:
            task = core("POST", "/v1/tasks", {"pool": pool, "payload": payload, "queue": True})
            break
        except (OSError, ApiError) as error:
            if isinstance(error, ApiError) and error.status < 500 or attempt == 5:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15)
    while task["status"] not in FINISHED:
        task = core("GET", f"/v1/tasks/{task['id']}/wait", timeout=90)
    outcome = core("GET", f"/v1/tasks/{task['id']}/result")
    if outcome["status"] != "succeeded":
        raise RuntimeError((outcome.get("error") or f"task {outcome['status']}").strip().splitlines()[-1][:500])
    value = outcome["value"]
    if not isinstance(value, dict) or value.get("encoding") != "json":
        raise RuntimeError("the executor returned something other than JSON")
    return value["data"]


# --- Package disk and pools -------------------------------------------------------

def run(*args):
    process = subprocess.run(args, capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout).strip()[-4000:] or f"{args[0]} failed")
    return process.stdout


def find_image(wanted):
    images = core("GET", "/v1/images")
    for image in images:
        if wanted in (image["id"], image["name"]):
            return image
    raise RuntimeError(f"Scraperack image {wanted!r} not found; pick one in Settings")


def package_disk(image):
    """A disk with a virtualenv of PACKAGES, built once per image in a microVM of that image.

    A short-lived `proxies-builder` pool runs the executor with the image's own Python, which
    pip-installs PACKAGES into /tmp/environment and returns it packed; the controller turns it
    into an ext4 disk mounted at the same path in the scraper and tester pools.
    """
    key = sha256(json.dumps([image["sha256"], PACKAGES]).encode()).hexdigest()
    cached = json.loads(store.get_value("status_packages", "{}"))
    disks = {disk["key"] for disk in core("GET", "/v1/disks")}
    if cached.get("key") == key and cached.get("disk") in disks:
        return cached["disk"]
    if not GUEST_DISK:
        raise RuntimeError("the plugin's guest disk is missing; reinstall the plugin")
    store.log("info", f"Installing {', '.join(PACKAGES)} for {image['name']} in a builder microVM")
    apply_pool({"name": BUILDER_POOL, "image": image["id"], "cpu_millis": 1000, "memory_mib": 1024,
                "disk_mib": 1024, "warm_instances": 0, "max_instances": 1, "timeout_secs": 900,
                "idle_timeout_secs": 5, "reuse": False, "network": True, "ports": [], "preload": [],
                "disks": [{"key": GUEST_DISK, "mount": GUEST_MOUNT}],
                "executor": ["/usr/bin/python3", f"{GUEST_MOUNT}/executor.py"],
                "aliases": [BUILDER_POOL], "display_name": "Proxy package builder"})
    try:
        built = call(BUILDER_POOL, "build", PACKAGES)
    finally:
        try:
            core("DELETE", f"/v1/pools/{BUILDER_POOL}")
        except (OSError, ApiError):
            pass
    work = Path(tempfile.mkdtemp(prefix="proxies-plugin-", dir=DATA))
    try:
        root = work / "root"
        root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(built["archive"])), mode="r:gz") as archive:
            archive.extractall(root, filter="tar")   # bin/python links to the image's /usr/bin/python3
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
        shutil.rmtree(work, ignore_errors=True)
    store.log("info", f"Package disk ready ({used // 1048576} MB, Python {built.get('python', '?')})")
    if cached.get("disk") and cached["disk"] != stored["key"]:
        try:
            core("DELETE", f"/v1/disks/{cached['disk']}")
        except ApiError:
            pass
    store.set_value("status_packages", json.dumps({"key": key, "disk": stored["key"]}))
    return stored["key"]


def pool_specs(cfg, image, disk):
    disks = [{"key": disk, "mount": VENV_MOUNT}]
    if GUEST_DISK:
        disks.append({"key": GUEST_DISK, "mount": GUEST_MOUNT})
    common = {"image": image["id"], "disk_mib": 1024, "idle_timeout_secs": 60, "reuse": True,
              "network": True, "ports": [], "disks": disks,
              "executor": [f"{VENV_MOUNT}/bin/python", f"{GUEST_MOUNT}/executor.py"]}
    return {
        SCRAPER_POOL: {**common, "name": SCRAPER_POOL, "aliases": [SCRAPER_POOL],
                       "display_name": "Proxy scraper", "preload": ["curl_cffi.requests"],
                       "cpu_millis": round(cfg["scraper_cpu"] * 1000), "memory_mib": cfg["scraper_memory_mib"],
                       "warm_instances": cfg["scraper_warm"], "max_instances": max(1, cfg["scraper_max_instances"]),
                       "timeout_secs": max(120, cfg["scrape_timeout_s"] * 3)},
        TESTER_POOL: {**common, "name": TESTER_POOL, "aliases": [TESTER_POOL],
                      "display_name": "Proxy tester", "preload": ["aiohttp", "aiohttp_socks"],
                      "cpu_millis": round(cfg["tester_cpu"] * 1000), "memory_mib": cfg["tester_memory_mib"],
                      "warm_instances": cfg["tester_warm"], "max_instances": max(1, cfg["tester_max_instances"]),
                      "timeout_secs": max(600, cfg["test_timeout_s"] * 30)},
    }


def apply_pool(spec):
    try:
        return core("PUT", f"/v1/pools/{spec['name']}", spec)
    except ApiError as error:
        if error.status != 404:
            raise
    return core("POST", "/v1/pools", spec)


def chunks(items, size):
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- Scheduler ---------------------------------------------------------------------

class Scheduler:
    """Scrapes on an interval and tests continuously, one at a time, on the plugin's pools."""

    def __init__(self, geo):
        self.geo = geo
        self.lock = threading.Lock()
        self.signature = None
        self.checked, self.checked_at = None, 0.0
        self.ready = False
        self.setup_error = None
        self.scrape_thread = self.test_thread = self.retention_thread = None
        self.last_scrape_start = 0.0
        self.last_test_end = 0.0
        self.last_housekeeping = 0.0
        self.test_pause_until = 0.0
        self.commands = set()
        self.rate = float(store.get_value("status_test_rate", 0) or 0)

    def command(self, name):
        with self.lock:
            self.commands.add(name)

    def take(self, name):
        with self.lock:
            present = name in self.commands
            self.commands.discard(name)
            return present

    def state(self):
        alive = lambda thread: bool(thread and thread.is_alive())
        return {"ready": self.ready, "setup_error": self.setup_error, "scraping": alive(self.scrape_thread),
                "testing": alive(self.test_thread), "rate": round(self.rate, 1),
                "pending_commands": sorted(self.commands)}

    def ensure_pools(self, cfg):
        """Build the package disk and create or update both pools; rechecked every few minutes."""
        wanted = json.dumps({k: v for k, v in cfg.items() if k == "image" or k.startswith(("scraper_", "tester_"))
                             or k in ("scrape_timeout_s", "test_timeout_s")}, sort_keys=True)
        if wanted == self.checked and time.monotonic() - self.checked_at < 300:
            return
        image = find_image(cfg["image"])
        disk = package_disk(image)
        specs = pool_specs(cfg, image, disk)
        signature = json.dumps(specs, sort_keys=True)
        if signature != self.signature:
            for spec in specs.values():
                apply_pool(spec)
            self.signature = signature
        self.checked, self.checked_at = wanted, time.monotonic()

    # -- scrape

    def scrape_round(self, cfg):
        run_id = store.start_run("scrape")
        started = time.monotonic()
        sources = store.enabled_sources()
        by_url = {s["url"]: s["id"] for s in sources}
        totals = {"sources": len(sources), "ok": 0, "errors": 0, "found": 0, "new": 0, "batch_failures": 0}
        count = threading.Lock()
        store.log("info", f"Scrape started: {len(sources)} sources")

        def batch(urls):
            try:
                results = call(SCRAPER_POOL, "scrape", urls, cfg["scrape_timeout_s"])
                failed = False
            except Exception as error:
                failed = True
                results = [{"url": u, "ok": False, "error": f"task failed: {error}"[:500], "proxies": []} for u in urls]
            for result in results:
                found, new = store.store_scrape_result(by_url[result["url"]], result)
                with count:
                    totals["found"] += found
                    totals["new"] += new
                    totals["ok" if result.get("ok") else "errors"] += 1
            if failed:
                with count:
                    totals["batch_failures"] += 1

        try:
            with ThreadPoolExecutor(max(1, cfg["scrape_parallel"])) as pool:
                list(pool.map(batch, chunks(list(by_url), cfg["scrape_batch_size"])))
            store.refresh_source_counts()
            self.update_source_stats(cfg)
            totals["seconds"] = round(time.monotonic() - started, 1)
            store.finish_run(run_id, "done", totals)
            store.log("info", f"Scrape done in {totals['seconds']}s: {totals['found']} proxies found, "
                              f"{totals['new']} new, {totals['errors']} sources failed")
        except Exception as error:
            traceback.print_exc()
            store.finish_run(run_id, "failed", {**totals, "error": str(error)})
            store.log("error", f"Scrape failed: {error}")

    def update_source_stats(self, cfg):
        disabled = store.refresh_source_stats(cfg)
        if disabled:
            examples = "; ".join(f"{url} ({reason})" for _, url, reason in disabled[:5])
            more = f" and {len(disabled) - 5} more" if len(disabled) > 5 else ""
            store.log("info", f"Auto-disabled {len(disabled)} sources: {examples}{more}")

    # -- test

    def test_round(self, cfg):
        picked, alive_due = store.pick_for_test(cfg)
        if not picked:
            return
        run_id = store.start_run("test")
        started = time.monotonic()
        judges = [j.strip() for j in cfg["judges"].splitlines() if j.strip()]
        items = [[p["id"], p["ip"], p["port"], p["hint"], p["protocol"] if p["status"] == "alive" else None]
                 for p in picked]
        totals = {"tested": 0, "alive": 0, "dead": 0, "batch_failures": 0, "inconclusive": 0, "real_ip": None}
        store.log("info", f"Test round started: {len(items)} proxies "
                          f"({alive_due} alive re-checks, {len(items) - alive_due} new)")
        abort = threading.Event()
        guard = threading.Lock()
        ceiling = cfg["test_rate_ceiling"] or cfg["test_rate_max"]
        upper = max(cfg["test_rate_min"], min(cfg["test_rate_max"], ceiling))
        self.rate = min(max(self.rate or cfg["test_rate_min"], cfg["test_rate_min"]), upper)
        queue = list(reversed(items))
        parallel = max(1, cfg["test_parallel"])

        def batch(chunk, per_vm):
            rate_used = self.rate
            batch_started = time.monotonic()
            try:
                response = call(TESTER_POOL, "test", chunk, judges, cfg["https_test_url"], cfg["test_timeout_s"],
                                cfg["test_concurrency"], per_vm)
            except Exception as error:
                with guard:
                    totals["batch_failures"] += 1
                    first = totals["batch_failures"] <= 3
                if first:
                    store.log("warn", f"Test batch failed: {str(error)[:300]}")
                return
            if response.get("inconclusive"):
                # Results from a microVM with network trouble would mark good proxies dead; drop them.
                with guard:
                    totals["inconclusive"] += 1
                    first = not abort.is_set()
                    abort.set()
                if first:
                    new_ceiling = max(cfg["test_rate_min"], int(rate_used * 0.8))
                    self.rate = max(cfg["test_rate_min"], rate_used / 2)
                    self.test_pause_until = time.monotonic() + cfg["network_backoff_s"]
                    store.set_value("test_rate_ceiling", new_ceiling)
                    store.log("error", f"Network trouble at {rate_used:.0f} connections/s ({response['inconclusive']}). "
                                       f"Batch discarded, nothing marked dead. Ceiling set to {new_ceiling}/s, "
                                       f"resuming at {self.rate:.0f}/s after {cfg['network_backoff_s']}s.")
                return
            if not abort.is_set():
                # Ramp up ~8%/min whatever the batch size or number of parallel tasks.
                minutes = (time.monotonic() - batch_started) / 60 / parallel
                with guard:
                    self.rate = min(upper, self.rate * (1 + RATE_RAMP_PER_MIN * minutes))
            alive, dead = store.store_test_results(response["results"], self.geo.country)
            with guard:
                totals["real_ip"] = totals["real_ip"] or response.get("real_ip")
                totals["tested"] += alive + dead
                totals["alive"] += alive
                totals["dead"] += dead

        def worker():
            # Each batch is sized to ~60 s of connections at the current rate, so it never hits
            # the task timeout and picks up rate changes quickly.
            while not abort.is_set():
                per_vm = max(0.5, self.rate / parallel)
                size = int(min(cfg["test_batch_size"], max(10, per_vm * 60)))
                with guard:
                    chunk = [queue.pop() for _ in range(min(size, len(queue)))]
                if not chunk:
                    return
                batch(chunk, per_vm)

        try:
            threads = [threading.Thread(target=worker, daemon=True) for _ in range(parallel)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.update_source_stats(cfg)
            totals["rate"] = round(self.rate)
            store.set_value("status_test_rate", round(self.rate))
            totals["seconds"] = round(time.monotonic() - started, 1)
            store.finish_run(run_id, "done", totals)
            if not totals["real_ip"] and totals["tested"]:
                store.log("warn", "No judge answered directly; transparent proxies cannot be detected. "
                                  "Check the judge URLs.")
            store.log("info", f"Test round done in {totals['seconds']}s: {totals['tested']} tested, "
                              f"{totals['alive']} alive, {totals['dead']} dead (final)"
                              + (f", {totals['inconclusive']} batches discarded" if totals["inconclusive"] else ""))
        except Exception as error:
            traceback.print_exc()
            store.finish_run(run_id, "failed", {**totals, "error": str(error)})
            store.log("error", f"Test round failed: {error}")

    # -- loop

    def retention(self, cfg):
        started = time.monotonic()
        try:
            out = store.apply_retention(cfg)
        except Exception as error:
            store.log("error", f"Retention failed: {str(error)[:300]}")
            return
        if any(out.values()):
            store.log("info", f"Retention ({time.monotonic() - started:.0f}s): forgot {out['dead']} dead and "
                              f"{out['untested']} untested proxies no source lists any more, "
                              f"{out['dead_retest']} dead for a re-test, {out['listings']} old source listings")

    @staticmethod
    def start(target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return thread

    def tick(self):
        cfg = store.get_settings()
        try:
            self.ensure_pools(cfg)
            if not self.ready:
                self.ready, self.setup_error = True, None
                store.log("info", "Pools ready: proxies-scraper and proxies-tester")
        except Exception as error:
            message = str(error)[:500]
            if message != self.setup_error:
                store.log("error", f"Pool setup failed: {message}")
            self.ready, self.setup_error = False, message
            time.sleep(30)
            return

        # Scraping and testing never overlap: together they open too many connections for a
        # home internet link. A due scrape waits for the current test round, and no new test
        # round starts while a scrape is due or running.
        mono = time.monotonic()
        scraping = bool(self.scrape_thread and self.scrape_thread.is_alive())
        testing = bool(self.test_thread and self.test_thread.is_alive())
        scrape_now = "scrape" in self.commands
        scrape_due = scrape_now or (cfg["scraping_enabled"] and (
            not self.last_scrape_start or mono - self.last_scrape_start >= cfg["scrape_interval_min"] * 60))
        if not scraping and scrape_due and not testing:
            self.take("scrape")
            self.last_scrape_start = mono
            self.scrape_thread = self.start(self.scrape_round, cfg)
            scraping = True

        if self.test_thread and not testing:
            self.test_thread = None
            self.last_test_end = mono
        if not testing and not scraping and not scrape_due:
            due = (cfg["testing_enabled"] and mono - self.last_test_end >= cfg["test_round_pause_s"]
                   and mono >= self.test_pause_until)
            if due or self.take("test"):
                self.test_thread = self.start(self.test_round, cfg)

        if not self.last_housekeeping or mono - self.last_housekeeping > 3600:
            self.last_housekeeping = mono
            store.trim_events()
            self.start(self.geo.refresh)
            if not (self.retention_thread and self.retention_thread.is_alive()):
                self.retention_thread = self.start(self.retention, cfg)

    def run(self):
        store.log("info", f"Proxies plugin {os.environ.get('SCRAPERACK_PLUGIN_VERSION', '')} started".replace("  ", " "))
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(LOOP_SECONDS)


# --- HTTP --------------------------------------------------------------------------

scheduler = None


def first(query, name, default=None):
    values = query.get(name)
    return values[0] if values and values[0] != "" else default


def number(query, name, low=None, high=None):
    value = first(query, name)
    if value is None:
        return None
    try:
        value = int(value)
    except ValueError:
        raise ApiError(422, f"{name} must be a whole number") from None
    if low is not None and value < low or high is not None and value > high:
        raise ApiError(422, f"{name} must be between {low} and {high}")
    return value


def proxy_filters(query):
    return {"protocol": first(query, "protocol"), "anonymity": first(query, "anonymity"),
            "country": first(query, "country"), "max_latency": number(query, "max_latency", 1),
            "checked_within": number(query, "checked_within", 1)}


def text_list(rows, scheme):
    lines = [row["url"] if scheme else f"{row['ip']}:{row['port']}" for row in rows]
    return "\n".join(lines) + ("\n" if lines else "")


def ids_of(body):
    ids = [int(item) for item in body.get("ids") or []]
    if not ids:
        raise ApiError(400, "no sources selected")
    return ids


def public_route(method, parts, query, headers):
    """The proxy API for programs; needs a key while api_auth_required is on."""
    if method != "GET" or parts not in (["proxies"], ["proxies", "random"], ["stats"]):
        return None
    if store.get_settings()["api_auth_required"]:
        auth = headers.get("Authorization") or ""
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        problem = store.check_key(key)
        if problem:
            raise ApiError(401, problem)
    if parts == ["stats"]:
        summary = store.stats()
        return 200, {"proxies": summary["proxies"], "alive": summary["alive"],
                     "enabled_sources": summary["sources"]["enabled"]}
    fmt = first(query, "format", "json")
    if fmt not in ("json", "txt"):
        raise ApiError(422, "format must be json or txt")
    if parts == ["proxies"]:
        rows = store.query_proxies(limit=number(query, "limit", 1, 50000) or 100,
                                   sort=first(query, "sort", "latency"), **proxy_filters(query))
        if fmt == "txt":
            return 200, text_list(rows, first(query, "scheme") in ("1", "true", "yes"))
        return 200, {"count": len(rows), "proxies": rows}
    rows = store.query_proxies(limit=500, sort="checked", **proxy_filters(query))
    if not rows:
        raise ApiError(404, "no proxy matches these filters")
    row = rows[int.from_bytes(os.urandom(4), "big") % len(rows)]
    return 200, (row["url"] + "\n" if fmt == "txt" else row)


def admin_route(method, parts, body, query):
    """What the plugin's page uses."""
    if parts == ["overview"] and method == "GET":
        cfg = store.get_settings()
        return 200, {"stats": store.stats(), "runs": store.last_runs(), "events": store.recent_events(),
                     "scheduler": scheduler.state() if scheduler else {},
                     "rate": {"max": cfg["test_rate_max"], "ceiling": cfg["test_rate_ceiling"]}}
    if parts == ["run"] and method == "POST":
        kind = body.get("kind")
        if kind not in ("scrape", "test"):
            raise ApiError(400, "kind must be scrape or test")
        scheduler.command(kind)
        return 202, {"queued": kind}
    if parts == ["sources"]:
        if method == "GET":
            return 200, store.list_sources(first(query, "search", ""))
        if method == "POST":
            urls = body.get("urls") or ""
            urls = urls.splitlines() if isinstance(urls, str) else urls
            return 201, {"added": store.add_sources(urls)}
    if len(parts) == 2 and parts[0] == "sources" and method == "POST":
        action = parts[1]
        if action == "enable":
            return 200, {"changed": store.set_sources_enabled(ids_of(body), True)}
        if action == "disable":
            return 200, {"changed": store.set_sources_enabled(ids_of(body), False)}
        if action == "delete":
            return 200, {"changed": store.delete_sources(ids_of(body))}
        if action == "disable-failing":
            return 200, {"changed": store.disable_failing_sources()}
    if parts == ["browse"] and method == "GET":
        rows = store.query_proxies(limit=number(query, "limit", 1, 50000) or 1000,
                                   sort=first(query, "sort", "latency"), **proxy_filters(query))
        if first(query, "format") == "txt":
            return 200, text_list(rows, first(query, "scheme") in ("1", "true", "yes"))
        return 200, rows
    if parts == ["settings"]:
        if method == "GET":
            return 200, store.settings_view()
        if method == "PUT":
            return 200, {"saved": store.save_settings(body)}
    if parts == ["images"] and method == "GET":
        return 200, [{"id": i["id"], "name": i["name"], "ready": i.get("ready")} for i in core("GET", "/v1/images")]
    if parts == ["keys"]:
        if method == "GET":
            return 200, {"keys": store.list_keys(), "auth_required": store.get_settings()["api_auth_required"]}
        if method == "POST":
            return 201, store.create_key(body.get("name"), int(body.get("days") or 0))
    if len(parts) >= 2 and parts[0] == "keys":
        try:
            key_id = int(parts[1])
            if len(parts) == 2 and method == "DELETE":
                store.delete_key(key_id)
                return 204, None
            if len(parts) == 2 and method == "PUT":
                store.set_key_enabled(key_id, bool(body.get("enabled")))
                return 200, {"ok": True}
            if len(parts) == 3 and parts[2] == "rotate" and method == "POST":
                return 200, store.rotate_key(key_id, int(body.get("grace_minutes") or 0))
        except KeyError as error:
            raise ApiError(404, error.args[0]) from None
    return None


def route(method, path, body, query, headers):
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if parts[:1] == ["gui"] and parts[1:2] == ["api"]:
        parts = parts[1:]
    if parts == ["health"]:
        return 200, {"ok": True}
    if parts[:1] != ["api"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
    handled = public_route(method, parts, query, headers) or admin_route(method, parts, body, query)
    if handled is None:
        raise ApiError(404, "not found")
    return handled


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "core"

    def log_message(self, format, *args):
        pass

    def send(self, status, payload, content_type=None):
        if isinstance(payload, str):
            data, content_type = payload.encode(), content_type or "text/plain; charset=utf-8"
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = b"" if payload is None else json.dumps(payload).encode()
            content_type = content_type or "application/json"
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status != 204:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if status != 204:
            self.wfile.write(data)

    def page(self, name):
        return self.send(200, (HERE / "gui" / name).read_bytes(), "text/html; charset=utf-8")

    def handle_method(self):
        url = urlsplit(self.path)
        path = url.path
        try:
            if self.command == "GET" and path.rstrip("/") in ("/gui", "/gui/index.html"):
                return self.page("index.html")
            if self.command == "GET" and path in ("/gui/app", "/gui/app/"):
                if path == "/gui/app":   # relative links need the trailing slash
                    self.send_response(301)
                    self.send_header("Location", "app/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self.page("app.html")
            if path.startswith("/gui/app/api/"):
                path = path[len("/gui/app"):]
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            status, payload = route(self.command, path, body, parse_qs(url.query), self.headers)
            self.send(status, payload)
        except ApiError as error:
            self.send(error.status, {"error": str(error)})
        except (ValueError, TypeError, KeyError) as error:
            self.send(400, {"error": str(error)})
        except Exception as error:
            traceback.print_exc()
            self.send(500, {"error": str(error)})

    do_GET = do_POST = do_PUT = do_DELETE = handle_method


def main():
    global scheduler
    DATA.mkdir(parents=True, exist_ok=True)
    store.open_db(DATA / "proxies.db")
    geo = Geo(str(DATA))
    scheduler = Scheduler(geo)
    Path(PLUGIN_SOCKET).unlink(missing_ok=True)
    server = ThreadingUnixStreamServer(PLUGIN_SOCKET, Handler)
    server.daemon_threads = True
    threading.Thread(target=geo.refresh, daemon=True).start()
    threading.Thread(target=scheduler.run, daemon=True).start()
    print(f"{PLUGIN} plugin listening on {PLUGIN_SOCKET}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
