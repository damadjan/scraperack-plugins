import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


class FakeCore:
    """The controller's plugin API, with one runner plugin ("python") behind it."""

    def __init__(self):
        self.runs = {}          # run id -> {status, failure, error, key, input}
        self.keys = {}
        self.submits = []
        self.stops = []
        self.down = False       # the runner answers 503
        self.reject = None      # the runner answers 400 with this message
        self.capacity = None

    def __call__(self, method, path, body=None, timeout=60):
        service = sys.modules["service"]
        url = urlsplit(path)
        if path == "/v1/plugins":
            return [{"name": "python"}, {"name": "queue"}]
        if path == "/v1/capacity":
            self.capacity = body
            return None
        if not url.path.startswith("/plugins/python/api/runs"):
            raise service.ApiError(404, "plugin not found")
        if self.down:
            raise service.ApiError(503, "plugin python is disabled")
        parts = url.path.split("/")[5:]
        if not parts and method == "POST":
            if self.reject:
                raise service.ApiError(400, self.reject)
            self.submits.append(body["key"])
            if body["key"] in self.keys:
                return self.runs[self.keys[body["key"]]]
            run = {"id": f"run-{len(self.runs) + 1}", "status": "queued", "failure": None,
                   "error": None, "input": body["input"]}
            self.runs[run["id"]] = run
            self.keys[body["key"]] = run["id"]
            return run
        if not parts and method == "GET":
            ids = parse_qs(url.query)["ids"][0].split(",")
            return [self.runs.get(run_id, {"id": run_id, "status": "unknown"}) for run_id in ids]
        if len(parts) == 2 and parts[1] == "stop":
            self.stops.append(parts[0])
            self.runs[parts[0]].update(status="failed", failure="stopped", error="task was stopped")
            return self.runs[parts[0]]
        raise AssertionError(f"unexpected {method} {path}")

    def finish(self, run_id, status="succeeded", failure=None, error=None):
        self.runs[run_id].update(status=status, failure=failure, error=error)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        os.environ["SCRAPERACK_PLUGIN_DATA"] = self.directory.name
        sys.path.insert(0, str(PLUGIN))
        for module in ("service", "store"):
            sys.modules.pop(module, None)
        self.service = importlib.import_module("service")
        self.core = FakeCore()
        self.service.core = self.core
        self.restart()

    def restart(self):
        """A fresh store and dispatcher on the same database, as after a plugin restart."""
        if self.service.store:
            self.service.store.db.close()
        self.service.store = self.service.Store(Path(self.directory.name) / "queue.db")
        self.service.dispatcher = self.service.Dispatcher(self.service.store)

    def tearDown(self):
        self.service.dispatcher.pool.shutdown()
        self.service.store.db.close()
        sys.path.remove(str(PLUGIN))
        self.directory.cleanup()

    def request(self, method, path, body=None, query=None):
        return self.service.route(method, path, body if body is not None else {}, query)

    def tick(self):
        self.service.dispatcher.last_poll = 0
        self.service.dispatcher.tick()

    def put(self, queue, count=1, **job_input):
        jobs = [{"runner": "python", "input": {"environment": "jobs", "n": index, **job_input}}
                for index in range(count)]
        _, body = self.request("POST", f"/api/queues/{queue}/jobs", {"jobs": jobs})
        return body["ids"]

    def job(self, job_id):
        return self.request("GET", f"/api/jobs/{job_id}")[1]

    def test_runs_at_most_concurrency_and_keeps_only_metadata(self):
        status, queue = self.request("PUT", "/api/queues/scrape", {"concurrency": 2})
        self.assertEqual((status, queue["concurrency"], queue["max_attempts"]), (201, 2, 3))
        ids = self.put("scrape", 5)
        self.tick()
        self.assertEqual(len(self.core.runs), 2)
        self.assertEqual([self.job(job_id)["status"] for job_id in ids],
                         ["running", "running", "queued", "queued", "queued"])
        self.assertEqual(self.core.runs["run-1"]["input"], {"environment": "jobs", "n": 0})

        self.core.finish("run-1")
        self.tick()
        self.assertEqual(self.job(ids[0])["status"], "done")
        self.assertEqual(len(self.core.runs), 3, "the freed slot went to the next job")
        self.assertIsNone(self.service.store.job_input(ids[0]), "input is dropped when a job ends")
        _, queue = self.request("GET", "/api/queues/scrape")
        self.assertEqual((queue["counts"]["done"], queue["counts"]["running"],
                          queue["counts"]["queued"]), (1, 2, 2))
        details = self.job(ids[0])
        self.assertEqual(details["attempts_detail"][0]["run_id"], "run-1")
        self.assertNotIn("input", details)

    def test_infrastructure_failures_retry_at_the_back_code_errors_do_not(self):
        self.request("PUT", "/api/queues/scrape", {"concurrency": 1, "max_attempts": 2})
        first, second = self.put("scrape", 2)
        self.tick()
        self.core.finish("run-1", "failed", "infrastructure",
                         "the node lost the task: its microVM ended without a result")
        self.tick()
        job = self.job(first)
        self.assertEqual((job["status"], job["attempts"]), ("queued", 1))
        self.assertTrue(job["reason"].startswith("infrastructure: the node lost the task"))
        self.assertEqual(self.job(second)["status"], "running", "the retry went to the back")

        self.core.finish("run-2", "failed", "code",
                         "Traceback (most recent call last):\n  File ...\nValueError: boom")
        self.tick()
        self.assertEqual(self.job(second)["status"], "failed")
        self.assertEqual(self.job(second)["reason"], "code: ValueError: boom")
        self.core.finish("run-3", "failed", "infrastructure", "lost")
        self.tick()
        job = self.job(first)
        self.assertEqual((job["status"], job["attempts"]), ("failed", 2), "attempts ran out")
        self.assertEqual([attempt["outcome"] for attempt in job["attempts_detail"]],
                         ["failed", "failed"])

    def test_code_errors_retry_when_the_queue_says_so(self):
        self.request("PUT", "/api/queues/scrape", {"retry_code_errors": True, "max_attempts": 3})
        (job_id,) = self.put("scrape")
        self.tick()
        self.core.finish("run-1", "timed_out", "timeout", "task timed out")
        self.tick()
        self.assertEqual(self.job(job_id)["status"], "running")
        self.assertEqual(self.job(job_id)["attempts"], 2)

    def test_a_restart_resubmits_with_the_same_key(self):
        self.request("PUT", "/api/queues/scrape", {})
        (job_id,) = self.put("scrape")
        self.core.down = True
        self.tick()
        self.assertEqual(self.job(job_id)["status"], "running")
        self.assertIsNone(self.job(job_id)["run_id"], "not submitted while the runner is down")
        self.core.down = False
        self.restart()
        self.tick()
        self.assertEqual(self.job(job_id)["run_id"], "run-1")
        # Submitted, but the answer was lost: the same key gets the same run.
        self.service.store.db.execute("UPDATE jobs SET run_id = NULL")
        self.tick()
        self.assertEqual(self.core.submits, [f"{job_id}/1", f"{job_id}/1"])
        self.assertEqual(len(self.core.runs), 1)
        self.assertEqual(self.job(job_id)["run_id"], "run-1")

    def test_rejected_jobs_fail_without_retries(self):
        self.request("PUT", "/api/queues/scrape", {})
        (job_id,) = self.put("scrape")
        self.core.reject = "environment not found"
        self.tick()
        job = self.job(job_id)
        self.assertEqual((job["status"], job["reason"]), ("failed", "rejected: environment not found"))

    def test_lost_runs_count_as_infrastructure(self):
        self.request("PUT", "/api/queues/scrape", {"max_attempts": 1})
        (job_id,) = self.put("scrape")
        self.tick()
        del self.core.runs["run-1"]
        self.tick()
        self.assertEqual(self.job(job_id)["reason"],
                         "infrastructure: the runner no longer knows this run")

    def test_cancel_stops_running_jobs_and_drops_waiting_ones(self):
        self.request("PUT", "/api/queues/scrape", {"concurrency": 1})
        first, *rest = self.put("scrape", 3)
        self.tick()
        _, job = self.request("POST", f"/api/jobs/{first}/cancel")
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self.core.stops, ["run-1"])
        _, cancelled = self.request("POST", "/api/queues/scrape/cancel")
        self.assertEqual(cancelled, {"cancelled": 2})
        self.tick()
        self.assertEqual(len(self.core.runs), 1)
        self.assertEqual({self.job(job_id)["status"] for job_id in rest}, {"cancelled"})

    def test_paused_queues_start_nothing_and_timeouts_stop_runs(self):
        self.request("PUT", "/api/queues/scrape", {"paused": True, "timeout_secs": 5,
                                                   "max_attempts": 1})
        (job_id,) = self.put("scrape")
        self.tick()
        self.assertEqual(self.core.runs, {})
        self.request("PUT", "/api/queues/scrape", {"paused": False})
        self.tick()
        self.service.store.db.execute("UPDATE jobs SET started_ms = started_ms - 6000")
        self.tick()
        self.assertEqual(self.core.stops, ["run-1"])
        self.assertEqual(self.job(job_id)["reason"], "timeout: ran longer than 5 s")

    def test_rate_limits_starts(self):
        self.request("PUT", "/api/queues/slow", {"concurrency": 100, "rate_per_minute": 60})
        self.put("slow", 10)
        self.tick()
        self.tick()
        self.assertEqual(len(self.core.runs), 1, "60 a minute is one a second")

    def test_full_queues_answer_429_and_inputs_are_checked(self):
        self.request("PUT", "/api/queues/small", {"max_queued": 2})
        self.put("small", 2)
        with self.assertRaises(self.service.ApiError) as full:
            self.put("small")
        self.assertEqual((full.exception.status, full.exception.extra), (429, {"retry_after": 30}))
        for body, status in (({"runner": "nope", "input": {}}, 400),
                             ({"runner": "queue", "input": {}}, 400),
                             ({"runner": "python", "input": "x"}, 400),
                             ({"runner": "python", "input": {}, "extra": 1}, 400)):
            with self.assertRaises(self.service.ApiError) as bad:
                self.request("POST", "/api/queues/small/jobs", body)
            self.assertEqual(bad.exception.status, status, body)
        with self.assertRaises(self.service.ApiError) as missing:
            self.put("nope")
        self.assertEqual(missing.exception.status, 404)
        with self.assertRaises(self.service.ApiError):
            self.request("PUT", "/api/queues/small", {"concurrency": 0})
        with self.assertRaises(self.service.ApiError):
            self.request("PUT", "/api/queues/small", {"schedule": "daily"})

    def test_delete_queue_and_prune_old_jobs(self):
        self.request("PUT", "/api/queues/scrape", {})
        (job_id,) = self.put("scrape")
        self.tick()
        self.core.finish("run-1")
        self.tick()
        self.service.store.db.execute("UPDATE jobs SET finished_ms = 1")
        self.assertEqual(self.service.store.prune(72), 1)
        with self.assertRaises(self.service.ApiError):
            self.job(job_id)
        self.put("scrape")
        self.tick()
        self.assertEqual(self.request("DELETE", "/api/queues/scrape"), (204, None))
        self.assertEqual(self.core.stops, ["run-2"])
        self.assertEqual(self.request("GET", "/api/queues")[1], [])

    def test_settings(self):
        self.assertEqual(self.request("PUT", "/api/settings", {"retention_hours": 24})[1],
                         {"retention_hours": 24})
        with self.assertRaises(self.service.ApiError):
            self.request("PUT", "/api/settings", {"retention_hours": 0})
        self.assertEqual(self.request("GET", "/gui/api/settings")[1], {"retention_hours": 24})


if __name__ == "__main__":
    unittest.main()
