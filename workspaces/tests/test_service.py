import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"


class FakeCore:
    """In-memory stand-in for the controller's plugin API."""

    def __init__(self):
        self.pools, self.volumes, self.tasks, self.vms = {}, {}, {}, []
        self.calls = []

    def __call__(self, method, path, body=None, timeout=60):
        service = sys.modules["service"]
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")[1:]
        if parts[0] == "pools":
            if method == "GET":
                return [{**pool, "ready": True} for pool in self.pools.values()]
            if method == "PUT" and parts[1] not in self.pools:
                raise service.ApiError(404, "pool not found")
            if method in ("PUT", "POST"):
                self.pools[body["name"]] = body
                return body
            if method == "DELETE":
                del self.pools[parts[1]]
                return None
        if parts[0] == "volumes":
            if method == "GET":
                return list(self.volumes.values())
            if method == "POST":
                self.volumes[body["name"]] = {**body, "node": None, "suspended": False,
                                              "migration": None, "migration_error": None}
                return self.volumes[body["name"]]
            if method == "DELETE":
                del self.volumes[parts[1]]
                return None
        if parts[0] == "tasks":
            if method == "POST" and len(parts) == 1:
                task = {"id": f"task-{len(self.tasks) + 1}", "status": "queued", "node": None,
                        "error": None, "started_at_ms": None, "finished_at_ms": None,
                        "request": body}
                self.tasks[task["id"]] = task
                return task
            task = self.tasks[parts[1]]
            if len(parts) == 2:
                return task
            if parts[2] == "result":
                return {"status": task["status"], "value": task.get("value"), "error": None}
            if parts[2] == "stop":
                task["stop"] = body
                return task
        if parts[0] == "vms":
            return self.vms
        if parts[0] == "nodes":
            return [{"id": "node-1", "name": "worker1"}]
        if parts[0] == "images":
            return []
        raise AssertionError(f"unexpected core call {method} {path}")


class WorkspacesTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        os.environ["SCRAPERACK_PLUGIN_DATA"] = self.data.name
        os.environ["SCRAPERACK_PLUGIN_GUEST_DISK"] = "guest-disk"
        sys.path.insert(0, str(PLUGIN))
        sys.modules.pop("service", None)
        self.service = importlib.import_module("service")
        self.core = FakeCore()
        self.service.core = self.core
        self.status = {"state": "ready"}
        self.stops = []

        def control(workspace, method, path, timeout=2):
            if path == "/stop":
                self.stops.append(workspace["id"])
            return self.status

        self.service.control = control

    def tearDown(self):
        sys.path.remove(str(PLUGIN))
        self.data.cleanup()

    def route(self, method, path, body=None):
        return self.service.route(method, f"/api/{path}", body or {}, {})[1]

    def run_task(self, task_id):
        task = self.core.tasks[task_id]
        task.update(status="running", node="node-1", started_at_ms=1)
        self.core.vms = [{"task": task_id, "node_address": "10.0.0.5", "node_name": "worker1",
                          "ports": [{"name": "vnc", "host_port": 31000},
                                    {"name": "control", "host_port": 31001}]}]

    def test_workspace_lifecycle(self):
        desktop = self.route("POST", "desktops", {"name": "xfce", "image": "img"})
        pool = self.core.pools[desktop["pool"]]
        self.assertEqual(pool["volume_mount"], "/mnt/workspace")
        self.assertEqual(pool["preload"], ["golden-desktop"])
        self.assertFalse(pool["reuse"])

        workspace = self.route("POST", "workspaces", {"name": "dev", "disk_gib": 5})
        self.assertEqual(workspace["status"], "queued")
        self.assertEqual(self.core.volumes[workspace["volume"]]["size_mib"], 5120)
        request = self.core.tasks[workspace["task"]]["request"]
        self.assertEqual(request["volume"], workspace["volume"])
        password = request["secrets"]["VNC_PASSWORD"]
        self.assertEqual(len(password), 8)

        self.core.volumes[workspace["volume"]]["migration"] = {"from": "a", "to": "b"}
        self.service.monitor_once()
        self.assertEqual(self.route("GET", "workspaces/dev")["status"], "moving")
        self.core.volumes[workspace["volume"]]["migration"] = None

        self.run_task(workspace["task"])
        self.status = {"state": "starting"}
        self.service.monitor_once()
        self.assertEqual(self.route("GET", "workspaces/dev")["status"], "starting")
        self.status = {"state": "ready"}
        self.service.monitor_once()
        view = self.route("GET", "workspaces/dev")
        self.assertEqual(view["status"], "running")
        self.assertIn(f"password={password}", view["url"])
        self.assertIn("http://10.0.0.5:31000/vnc.html", view["url"])
        with self.assertRaises(self.service.ApiError):
            self.route("DELETE", f"workspaces/{workspace['id']}")

        self.route("POST", f"workspaces/{workspace['id']}/suspend")
        self.assertEqual(self.core.tasks[workspace["task"]]["stop"], {"suspend": True})
        task = self.core.tasks[workspace["task"]]
        task.update(status="succeeded", finished_at_ms=2, value={"suspended": True})
        self.core.volumes[workspace["volume"]]["suspended"] = True
        self.service.monitor_once()
        view = self.route("GET", "workspaces/dev")
        self.assertEqual(view["status"], "suspended")
        self.assertIsNone(view["url"])

        view = self.route("POST", f"workspaces/{workspace['id']}/start")
        self.assertEqual(view["status"], "queued")
        self.assertNotEqual(view["task"], workspace["task"])
        self.run_task(view["task"])
        self.service.monitor_once()
        self.route("POST", f"workspaces/{workspace['id']}/stop")
        self.assertEqual(self.stops, [workspace["id"]])
        self.core.tasks[view["task"]].update(status="succeeded", finished_at_ms=3,
                                             value={"reason": "stopped"})
        self.core.volumes[workspace["volume"]]["suspended"] = False
        self.service.monitor_once()
        view = self.route("GET", "workspaces/dev")
        self.assertEqual((view["status"], view["last_reason"]), ("stopped", "stopped"))

        with self.assertRaises(self.service.ApiError):
            self.route("DELETE", f"desktops/{desktop['id']}")
        self.route("DELETE", f"workspaces/{workspace['id']}")
        self.assertNotIn(workspace["volume"], self.core.volumes)
        self.route("DELETE", f"desktops/{desktop['id']}")
        self.assertFalse(self.core.pools)


if __name__ == "__main__":
    unittest.main()
