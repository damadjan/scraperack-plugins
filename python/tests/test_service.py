import base64
import importlib
import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


class FakeCore:
    """In-memory stand-in for the controller's plugin API."""

    def __init__(self):
        self.pools = {}
        self.disks = {"venv-1": {"key": "venv-1"}}
        self.images = [{"id": "img", "name": "python", "sha256": "abc", "ready": True,
                        "docker_source": "sha256:1"}]
        self.calls = []
        self.tasks = {}

    def tasks_api(self, method, path, body):
        service = sys.modules["service"]
        url = urlsplit(path)
        parts = url.path.strip("/").split("/")[2:]
        if not parts and method == "POST":
            pool = self.pools[body["pool"]]
            task = {"id": f"task-{len(self.tasks) + 1}", "pool": pool["name"],
                    "owner": "plugin:python", "status": "queued", "node": None, "error": None,
                    "created_at_ms": len(self.tasks), "started_at_ms": None,
                    "finished_at_ms": None, "metrics": None, "payload": body["payload"]}
            self.tasks[task["id"]] = task
            return self.view(task)
        if not parts and method == "GET":
            query = parse_qs(url.query)
            pools = query.get("pool", [""])[0].split(",") if "pool" in query else None
            return [self.view(task) for task in reversed(self.tasks.values())
                    if task["owner"] == query["owner"][0]
                    and (pools is None or task["pool"] in pools)]
        task = self.tasks.get(parts[0])
        if task is None:
            raise service.ApiError(404, "task not found")
        if len(parts) == 1:
            return self.view(task)
        if parts[1] == "payload":
            return task["payload"]
        if parts[1] == "logs":
            return {"stdout": "hello\n", "stderr": ""}
        if parts[1] == "result":
            return {"status": task["status"], "value": {"encoding": "json", "data": 42}, "error": None}
        raise AssertionError(path)

    @staticmethod
    def view(task):
        return {key: value for key, value in task.items() if key != "payload"}

    def __call__(self, method, path, body=None, file=None, timeout=120):
        service = sys.modules["service"]
        self.calls.append((method, path))
        if path.startswith("/v1/tasks"):
            return self.tasks_api(method, path, body)
        if path == "/v1/pools" and method == "GET":
            return [{**pool, "ready": True} for pool in self.pools.values()]
        if path == "/v1/pools" and method == "POST":
            self.pools[body["name"]] = body
            return body
        if path.startswith("/v1/pools/"):
            name = path.split("/")[3]
            if method == "PUT":
                if name not in self.pools:
                    raise service.ApiError(404, "pool not found")
                self.pools[name] = body
                return body
            if method == "DELETE":
                self.pools.pop(name)
                return None
            if method == "POST" and path.endswith("/restart"):
                return self.pools[name]
        if path == "/v1/disks":
            return list(self.disks.values())
        if path.startswith("/v1/disks/") and method == "DELETE":
            self.disks.pop(path.rsplit("/", 1)[1], None)
            return None
        if path == "/v1/images":
            return self.images
        raise AssertionError(f"unexpected call {method} {path}")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        os.environ["SCRAPERACK_PLUGIN_DATA"] = self.directory.name
        os.environ["SCRAPERACK_PLUGIN_GUEST_DISK"] = "guest-1"
        sys.path.insert(0, str(PLUGIN))
        sys.modules.pop("service", None)
        self.service = importlib.import_module("service")
        self.core = FakeCore()
        self.service.core = self.core
        self.service.build_venv_disk = lambda image, packages: "venv-1"

    def tearDown(self):
        sys.path.remove(str(PLUGIN))
        self.directory.cleanup()

    def request(self, method, path, body=None, query=None):
        return self.service.route(method, path, body or {}, query)

    def test_executions_are_core_tasks_shown_as_python(self):
        _, environment = self.request("POST", "/api/environments", {"name": "jobs", "image": "img"})
        function = base64.b64encode(pickle.dumps(os.path.join)).decode()
        status, execution = self.request("POST", "/api/executions", {
            "environment": "jobs",
            "payload": {"encoding": "python-function", "function": function, "hash": "x",
                        "params": ""},
        })
        self.assertEqual(status, 202)
        self.assertEqual(execution["environment"], "jobs")
        self.assertEqual(execution["function"], "join")
        self.assertEqual(self.core.tasks[execution["id"]]["pool"], environment["pool"])

        self.core.tasks["foreign"] = {**self.core.tasks[execution["id"]], "id": "foreign",
                                      "owner": "plugin:other"}
        self.service.function_cache = None
        status, listed = self.request("GET", "/api/executions", query={"limit": ["10"]})
        self.assertEqual([item["id"] for item in listed], [execution["id"]])
        self.assertEqual(listed[0]["function"], "join")
        _, filtered = self.request("GET", "/api/executions",
                                   query={"environment": [environment["id"]]})
        self.assertEqual(len(filtered), 1)

        self.core.tasks[execution["id"]].update(status="succeeded", started_at_ms=10,
                                                 finished_at_ms=35)
        _, details = self.request("GET", f'/api/executions/{execution["id"]}')
        self.assertEqual(details["duration_ms"], 25)
        self.assertEqual(details["logs"]["stdout"], "hello\n")
        self.assertEqual(details["result"], {"encoding": "json", "data": 42})
        with self.assertRaises(self.service.ApiError) as hidden:
            self.request("GET", "/api/executions/foreign")
        self.assertEqual(hidden.exception.status, 404)

    def test_describes_functions_pickled_by_value(self):
        # Opcode strings as cloudpickle writes a function defined in __main__.
        strings = ["cloudpickle.cloudpickle", "_make_function", "_builtin_type", "CodeType",
                   "url", "/app/jobs.py", "scrape", "scrape", "__package__", "__name__",
                   "__main__", "__file__", "_function_setstate", "scrape", "__qualname__",
                   "Spider.scrape", "__module__"]
        data = pickle.dumps(strings)
        info = self.service.describe_payload(
            {"encoding": "python-function", "function": base64.b64encode(data).decode()})
        self.assertEqual(info["function"], "Spider.scrape")
        self.assertEqual(info["module"], "__main__")
        self.assertEqual(info["file"], "/app/jobs.py")
        self.assertIsNone(self.service.describe_payload({"encoding": "python-function",
                                                         "function": "!!"})["function"])

    def test_creates_environment_pool_with_executor_disks_and_aliases(self):
        status, environment = self.request("POST", "/api/environments", {
            "name": "jobs", "image": "img", "packages": "httpx==0.28.1, bs4", "max_instances": 4,
        })
        self.assertEqual(status, 201)
        pool = self.core.pools[environment["pool"]]
        self.assertEqual(pool["aliases"], [environment["id"], "jobs"])
        self.assertEqual(pool["disks"], [
            {"key": "venv-1", "mount": "/tmp/environment"},
            {"key": "guest-1", "mount": "/tmp/scraperack/python"},
        ])
        self.assertEqual(pool["executor"],
                         ["/tmp/environment/bin/python", "/tmp/scraperack/python/executor.py"])
        self.assertEqual(environment["packages"], ["bs4", "httpx==0.28.1"])
        self.assertTrue(environment["ready"])

    def test_gui_api_prefix_and_name_lookup(self):
        self.request("POST", "/api/environments", {"name": "jobs", "image": "img"})
        status, environments = self.request("GET", "/gui/api/environments")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in environments], ["jobs"])
        status, environment = self.request("GET", "/api/environments/jobs")
        self.assertEqual(environment["name"], "jobs")
        self.assertEqual(self.request("GET", "/health"), (200, {"ok": True}))

    def test_rejects_duplicate_names_and_bad_limits(self):
        self.request("POST", "/api/environments", {"name": "jobs", "image": "img"})
        with self.assertRaises(self.service.ApiError) as duplicate:
            self.request("POST", "/api/environments", {"name": "jobs", "image": "img"})
        self.assertEqual(duplicate.exception.status, 409)
        with self.assertRaises(self.service.ApiError):
            self.request("POST", "/api/environments",
                         {"name": "other", "image": "img", "warm_instances": 5, "max_instances": 2})

    def test_update_delete_and_disk_cleanup(self):
        _, environment = self.request("POST", "/api/environments", {"name": "jobs", "image": "img"})
        self.service.build_venv_disk = lambda image, packages: "venv-2"
        self.core.disks["venv-2"] = {"key": "venv-2"}
        _, updated = self.request("PUT", f'/api/environments/{environment["id"]}',
                                  {"packages": ["lxml==6.0.0"], "warm_instances": 1})
        self.assertEqual(updated["venv_disk"], "venv-2")
        self.assertNotIn("venv-1", self.core.disks)
        self.assertEqual(self.request("DELETE", "/api/environments/jobs"), (204, None))
        self.assertEqual(self.core.pools, {})
        self.assertNotIn("venv-2", self.core.disks)

    def test_adopts_environments_migrated_by_the_core(self):
        Path(self.directory.name, "migrated-environments.json").write_text(json.dumps([{
            "id": "legacy-id", "name": "jobsites", "image": "img", "packages": ["requests==2.0"],
            "preload": [], "artifact": "venv-1", "sha256": "x", "size": 1,
            "pool": "environment-legacy-id-abc", "warm_instances": 2, "max_instances": 30,
            "cpu_millis": 800, "memory_mib": 200, "disk_mib": 1024, "timeout_secs": 300,
            "idle_timeout_secs": 30, "reuse": True, "network": True,
        }]))
        self.core.pools["environment-legacy-id-abc"] = {"name": "environment-legacy-id-abc"}
        self.service.reconcile()
        pool = self.core.pools["environment-legacy-id-abc"]
        self.assertEqual(pool["aliases"], ["legacy-id", "jobsites"])
        self.assertEqual(pool["warm_instances"], 2)
        self.assertEqual(pool["disks"][0]["key"], "venv-1")
        self.assertTrue(Path(self.directory.name, "migrated-environments.adopted.json").exists())
        self.service.reconcile()
        self.assertEqual(len(self.service.load_state()), 1)


if __name__ == "__main__":
    unittest.main()
