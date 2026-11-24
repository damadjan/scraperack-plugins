"""Scraperack queue plugin: more work than the cluster runs at once.

A queue is a lane of jobs with its own limits (concurrency, start rate, attempts). A job
names a runner, a plugin that runs work (the Python plugin today), and carries the input
that runner needs; the queue never looks inside it. When a queue has room, the dispatcher
hands the next job to its runner through the runner contract:

    POST /plugins/<runner>/api/runs {key, input}  -> {id, status, ...}
    GET  /plugins/<runner>/api/runs?ids=a,b        -> [{id, status, failure, error}]
    POST /plugins/<runner>/api/runs/<id>/stop

Jobs keep metadata only: status, reason, attempts and their run ids, timings. A job's
input is dropped when it ends; what the job produced goes wherever the job sends it.

Standard library only: the plugin installs without downloading anything.
"""
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from store import QUEUE_DEFAULTS, SETTING_DEFAULTS, STATUSES, Store, now_ms

PLUGIN = os.environ.get("SCRAPERACK_PLUGIN_NAME", "queue")
PLUGIN_SOCKET = os.environ.get("SCRAPERACK_PLUGIN_SOCKET", "/tmp/scraperack-queue.sock")
CORE_SOCKET = os.environ.get("SCRAPERACK_CORE_SOCKET", "/tmp/scraperack-core.sock")
DATA = Path(os.environ.get("SCRAPERACK_PLUGIN_DATA", "data"))
HERE = Path(__file__).resolve().parent

TICK_SECS = 0.5                 # how often the dispatcher looks for room
POLL_SECS = 1.0                 # how often running jobs are checked with their runners
SUBMIT_GIVE_UP_MS = 10 * 60_000  # a job whose runner refused it this long counts as failed
MAX_INPUT_BYTES = 1024 * 1024
MAX_BATCH = 1000
MAX_BODY_BYTES = 64 * 1024 * 1024
POLL_BATCH = 200
NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
RUNNER = re.compile(r"[a-z0-9-]{1,32}")
LIMITS = {"concurrency": (1, 10_000), "rate_per_minute": (1, 1_000_000), "max_attempts": (1, 100),
          "timeout_secs": (1, 7 * 86400), "max_queued": (1, 100_000_000)}
SETTING_LIMITS = {"retention_hours": (1, 720)}
store = None


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# --- Core API over the plugin's private Unix socket ------------------------------

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
        headers = {}
        payload = None
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


def runner_call(runner, method, path, body=None):
    """Call a runner plugin's API; plugins reach each other through the core."""
    return core(method, f"/plugins/{quote(runner, safe='')}/api{path}", body)


plugin_names = {"at": 0.0, "names": set()}


def known_runner(runner):
    """Whether a plugin of that name is installed (checked against the core, cached briefly)."""
    if time.monotonic() - plugin_names["at"] > 30 or runner not in plugin_names["names"]:
        try:
            plugin_names["names"] = {plugin["name"] for plugin in core("GET", "/v1/plugins")}
            plugin_names["at"] = time.monotonic()
        except (OSError, ApiError):
            return True     # can't tell right now; the dispatcher will find out
    return runner in plugin_names["names"]


# --- Validation ------------------------------------------------------------------

def bounded(field, value, limits):
    low, high = limits[field]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(400, f"{field} must be a number")
    try:
        value = int(value)
    except ValueError:
        raise ApiError(400, f"{field} must be a number") from None
    if not low <= value <= high:
        raise ApiError(400, f"{field} must be between {low} and {high}")
    return value


def normalize_queue(name, request, current=None):
    if not NAME.fullmatch(name):
        raise ApiError(400, "queue names use 1-64 letters, digits, '.', '_' and '-'")
    if not isinstance(request, dict):
        raise ApiError(400, "send the queue's settings as an object")
    unknown = set(request) - set(QUEUE_DEFAULTS) - {"name"}
    if unknown:
        raise ApiError(400, f"unknown queue settings: {', '.join(sorted(unknown))}")
    queue = {**QUEUE_DEFAULTS, **(current or {}), "name": name}
    for field, value in request.items():
        if field == "name":
            continue
        if field in ("retry_code_errors", "paused"):
            if not isinstance(value, bool):
                raise ApiError(400, f"{field} must be true or false")
            queue[field] = value
        elif field in ("rate_per_minute", "timeout_secs") and value in (None, "", 0):
            queue[field] = None
        else:
            queue[field] = bounded(field, value, LIMITS)
    return queue


def normalize_jobs(body):
    """[{runner, input}] from one job or {"jobs": [...]}."""
    if not isinstance(body, dict):
        raise ApiError(400, "send a job {runner, input} or {jobs: [...]}")
    jobs = body.get("jobs", [body])
    if not isinstance(jobs, list) or not jobs:
        raise ApiError(400, "jobs must be a non-empty list")
    if len(jobs) > MAX_BATCH:
        raise ApiError(400, f"send at most {MAX_BATCH} jobs at once")
    checked = []
    for job in jobs:
        if not isinstance(job, dict) or set(job) - {"runner", "input"}:
            raise ApiError(400, "a job is {runner, input}")
        runner = job.get("runner")
        if not isinstance(runner, str) or not RUNNER.fullmatch(runner):
            raise ApiError(400, "runner must be a plugin name, like \"python\"")
        if not isinstance(job.get("input"), dict):
            raise ApiError(400, "input must be an object; its runner defines what goes in it")
        if len(json.dumps(job["input"])) > MAX_INPUT_BYTES:
            raise ApiError(413, f"a job's input is limited to {MAX_INPUT_BYTES} bytes")
        checked.append({"runner": runner, "input": job["input"]})
    for runner in {job["runner"] for job in checked}:
        if runner == PLUGIN or not known_runner(runner):
            raise ApiError(400, f"runner {runner} is not an installed plugin")
    return checked


# --- Dispatcher ----------------------------------------------------------------------

def reason_of(failure, error):
    """A short reason: the failure kind and the last line of the error."""
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    return f"{failure}: {lines[-1][:300]}" if lines else failure


class Dispatcher:
    """Feeds queued jobs to their runners while each queue has room, and follows them."""

    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.allowance = {}             # queue -> starts allowed now under its rate limit
        self.last_refill = {}
        self.last_poll = 0.0
        self.submit_errors = {}         # job id -> last submit error
        self.pool = ThreadPoolExecutor(16, thread_name_prefix="submit")
        self.rotation = 0

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self.wake.wait(TICK_SECS)
            self.wake.clear()

    def tick(self):
        running = self.store.running()
        if time.monotonic() - self.last_poll >= POLL_SECS:
            self.last_poll = time.monotonic()
            self.follow([job for job in running if job["run_id"]])
            running = self.store.running()
        unsubmitted = [job for job in running if not job["run_id"]]
        if unsubmitted:
            self.submit_all(unsubmitted)
        self.start(running)

    # Starting jobs

    def start(self, running):
        active = {}
        for job in running:
            active[job["queue"]] = active.get(job["queue"], 0) + 1
        queues = [queue for queue in self.store.queues() if not queue["paused"]]
        if not queues:
            return
        # Rotate who goes first, so a busy queue never starves the others of submit time.
        self.rotation = (self.rotation + 1) % len(queues)
        claimed = []
        for queue in queues[self.rotation:] + queues[:self.rotation]:
            room = queue["concurrency"] - active.get(queue["name"], 0)
            room = min(room, self.rate_room(queue))
            if room <= 0:
                continue
            jobs = self.store.claim(queue["name"], min(room, 500))
            if queue["rate_per_minute"]:
                self.allowance[queue["name"]] -= len(jobs)
            claimed.extend(jobs)
        if claimed:
            self.submit_all(claimed)

    def rate_room(self, queue):
        rate = queue["rate_per_minute"]
        if not rate:
            return sys.maxsize
        name = queue["name"]
        now = time.monotonic()
        burst = max(1.0, rate / 60)     # at most one second's worth at once
        elapsed = now - self.last_refill.get(name, now - 1)
        self.allowance[name] = min(burst, self.allowance.get(name, burst) + elapsed * rate / 60)
        self.last_refill[name] = now
        return int(self.allowance[name])

    def submit_all(self, jobs):
        list(self.pool.map(self.submit, jobs))

    def submit(self, job):
        job_input = job.get("input")
        if job_input is None:
            job_input = self.store.job_input(job["id"])
            if job_input is None:
                return
        key = f'{job["id"]}/{job["attempt"]}'
        try:
            run = runner_call(job["runner"], "POST", "/runs", {"key": key, "input": job_input})
        except ApiError as error:
            if error.status in (400, 404, 409, 413, 422):
                self.store.finish(job["id"], job["attempt"], "failed",
                                  reason_of("rejected", str(error)))
                return
            self.submit_failed(job, f"runner answered {error.status}: {error}")
            return
        except OSError as error:
            self.submit_failed(job, f"runner unreachable: {error}")
            return
        self.submit_errors.pop(job["id"], None)
        if not self.store.set_run(job["id"], job["attempt"], run["id"]):
            self.stop(job["runner"], run["id"])     # cancelled while it was being submitted

    def submit_failed(self, job, message):
        """The runner could not take the job right now; it is tried again next tick."""
        self.submit_errors[job["id"]] = message
        started = job.get("started_ms") or now_ms()
        if now_ms() - started > SUBMIT_GIVE_UP_MS:
            self.submit_errors.pop(job["id"], None)
            self.failed(job, "infrastructure", message)

    # Following running jobs

    def follow(self, jobs):
        queues = {queue["name"]: queue for queue in self.store.queues()}
        by_runner = {}
        for job in jobs:
            by_runner.setdefault(job["runner"], []).append(job)
        for runner, runner_jobs in by_runner.items():
            for index in range(0, len(runner_jobs), POLL_BATCH):
                batch = runner_jobs[index:index + POLL_BATCH]
                ids = ",".join(job["run_id"] for job in batch)
                try:
                    runs = runner_call(runner, "GET", "/runs?" + urlencode({"ids": ids}))
                except ApiError as error:
                    if error.status != 404:
                        continue        # the runner is busy or restarting; ask again later
                    runs = []           # the runner is gone, and with it these runs
                except OSError:
                    continue
                views = {run["id"]: run for run in runs}
                for job in batch:
                    run = views.get(job["run_id"], {"status": "unknown"})
                    self.update(job, run, queues.get(job["queue"]))

    def update(self, job, run, queue):
        status = run.get("status")
        if status == "succeeded":
            self.store.finish(job["id"], job["attempt"], "done")
        elif status in ("failed", "timed_out"):
            failure = run.get("failure") or ("timeout" if status == "timed_out" else "infrastructure")
            self.failed(job, failure, run.get("error"), queue)
        elif status == "unknown":
            self.failed(job, "infrastructure", "the runner no longer knows this run", queue)
        elif queue and queue["timeout_secs"] and job["started_ms"] and \
                now_ms() - job["started_ms"] > queue["timeout_secs"] * 1000:
            self.stop(job["runner"], job["run_id"])
            self.failed(job, "timeout", f'ran longer than {queue["timeout_secs"]} s', queue)

    def failed(self, job, failure, error, queue=None):
        """Retry the job at the back of its queue, or fail it for good."""
        queue = queue or self.store.queue(job["queue"]) or QUEUE_DEFAULTS
        reason = reason_of(failure, error)
        retryable = failure == "infrastructure" or (
            failure in ("code", "timeout") and queue["retry_code_errors"])
        if retryable and job["attempt"] < queue["max_attempts"]:
            self.store.retry(job["id"], job["attempt"], reason)
        else:
            self.store.finish(job["id"], job["attempt"], "failed", reason)

    @staticmethod
    def stop(runner, run_id):
        try:
            runner_call(runner, "POST", f"/runs/{quote(run_id, safe='')}/stop", {})
        except (OSError, ApiError) as error:
            print(f"stopping run {run_id} on {runner} failed: {error}", file=sys.stderr)


dispatcher = None


def stop_runs(stopped):
    for _, runner, run_id in stopped:
        Dispatcher.stop(runner, run_id)


# --- Background upkeep ------------------------------------------------------------------

def upkeep_forever():
    """Forget old jobs and report the queues on the core's capacity page."""
    last_prune = 0.0
    while True:
        try:
            if time.monotonic() - last_prune > 600:
                last_prune = time.monotonic()
                store.prune(store.settings()["retention_hours"])
            report_capacity()
        except Exception:
            traceback.print_exc()
        time.sleep(5)


def report_capacity():
    queues = store.queues()
    counts = store.counts()
    running = store.running()
    try:
        core("PUT", "/v1/capacity", {
            "output": {"label": "Jobs running", "ready": len(running)},
            "dependencies": sorted({job["runner"] for job in running})[:12],
            "resources": [{"label": "Concurrency", "used": len(running),
                           "total": sum(queue["concurrency"] for queue in queues
                                        if not queue["paused"]) or None, "unit": "jobs",
                           "hint": "Raise a queue's concurrency"}],
            "queued": sum(count["queued"] for count in counts.values()),
        })
    except (OSError, ApiError):
        pass


# --- API --------------------------------------------------------------------------------

def queue_view(queue, counts):
    return {**queue, "counts": counts.get(queue["name"]) or dict.fromkeys(STATUSES, 0)}


def find_queue(name):
    queue = store.queue(name)
    if queue is None:
        raise ApiError(404, "queue not found")
    return queue


def find_job(job_id):
    job = store.job(job_id, attempts=True)
    if job is None:
        raise ApiError(404, "job not found")
    return job


def put_jobs(name, body):
    queue = find_queue(name)
    jobs = normalize_jobs(body)
    waiting = store.count(name, "queued")
    if waiting + len(jobs) > queue["max_queued"]:
        raise ApiError(429, f'queue {name} holds at most {queue["max_queued"]} waiting jobs',
                       retry_after=30)
    ids = store.add_jobs(name, jobs)
    dispatcher.wake.set()
    return {"ids": ids} if "jobs" in body else {"id": ids[0]}


def cancel_queue(name, body):
    find_queue(name)
    cancelled = store.cancel_queued(name)
    if body.get("running"):
        count, stopped = store.cancel(store.running_ids(name))
        stop_runs(stopped)
        cancelled += count
    return {"cancelled": cancelled}


def delete_queue(name):
    find_queue(name)
    store.cancel_queued(name)
    stop_runs(store.cancel(store.running_ids(name))[1])
    store.delete_queue(name)


def save_settings(body):
    if not isinstance(body, dict) or set(body) - set(SETTING_DEFAULTS):
        raise ApiError(400, f'settings are {", ".join(SETTING_DEFAULTS)}')
    return store.save_settings({field: bounded(field, value, SETTING_LIMITS)
                                for field, value in body.items()})


def route(method, path, body, query=None):
    query = query or {}
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if parts[:1] == ["gui"] and parts[1:2] == ["api"]:
        parts = parts[1:]
    if parts == ["health"]:
        return 200, {"ok": True}
    if parts[:1] != ["api"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
    if parts == ["queues"] and method == "GET":
        counts = store.counts()
        return 200, [queue_view(queue, counts) for queue in store.queues()]
    if len(parts) == 2 and parts[0] == "queues":
        name = parts[1]
        if method == "GET":
            return 200, queue_view(find_queue(name), store.counts())
        if method == "PUT":
            current = store.queue(name)
            saved = store.save_queue(normalize_queue(name, body, current))
            dispatcher.wake.set()
            return (200 if current else 201), queue_view(saved, store.counts())
        if method == "DELETE":
            delete_queue(name)
            return 204, None
    if len(parts) == 3 and parts[0] == "queues" and parts[2] == "jobs":
        if method == "POST":
            return 202, put_jobs(parts[1], body)
        if method == "GET":
            find_queue(parts[1])
            status = (query.get("status") or [None])[0]
            if status and status not in STATUSES:
                raise ApiError(400, f'status is one of {", ".join(STATUSES)}')
            limit = bounded("limit", (query.get("limit") or ["100"])[0], {"limit": (1, 1000)})
            return 200, store.jobs(parts[1], status, limit)
    if len(parts) == 3 and parts[0] == "queues" and parts[2] == "cancel" and method == "POST":
        return 200, cancel_queue(parts[1], body)
    if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
        return 200, find_job(parts[1])
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
        find_job(parts[1])
        stop_runs(store.cancel([parts[1]])[1])
        return 200, find_job(parts[1])
    if parts == ["settings"]:
        if method == "GET":
            return 200, store.settings()
        if method == "PUT":
            return 200, save_settings(body)
    raise ApiError(404, "not found")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "core"

    def log_message(self, format, *args):
        pass        # every dispatcher poll would be a line; errors are printed where they happen

    def send(self, status, payload, content_type="application/json", headers=None):
        data = b"" if payload is None else (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
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
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError(413, f"requests are limited to {MAX_BODY_BYTES} bytes")
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            status, payload = route(self.command, url.path, body, parse_qs(url.query))
            self.send(status, payload)
        except ApiError as error:
            headers = {}
            if "retry_after" in error.extra:
                headers["Retry-After"] = str(error.extra["retry_after"])
            self.send(error.status, {"error": str(error), **error.extra}, headers=headers)
        except (ValueError, TypeError) as error:
            self.send(400, {"error": str(error)})
        except Exception as error:
            traceback.print_exc()
            self.send(500, {"error": str(error)})

    do_GET = do_POST = do_PUT = do_DELETE = handle_method


def main():
    global store, dispatcher
    DATA.mkdir(parents=True, exist_ok=True)
    store = Store(DATA / "queue.db")
    dispatcher = Dispatcher(store)
    Path(PLUGIN_SOCKET).unlink(missing_ok=True)
    server = ThreadingUnixStreamServer(PLUGIN_SOCKET, Handler)
    server.daemon_threads = True
    threading.Thread(target=dispatcher.run_forever, daemon=True).start()
    threading.Thread(target=upkeep_forever, daemon=True).start()
    print(f"{PLUGIN} plugin listening on {PLUGIN_SOCKET}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
