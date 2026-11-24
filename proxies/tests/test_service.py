import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "guest"))
import executor
import service
import store


class FakeCore:
    """In-memory stand-in for the controller's plugin API."""

    def __init__(self):
        self.pools, self.tasks, self.calls = {}, {}, []
        self.images = [{"id": "img1", "name": "python-generic-v2", "sha256": "abc", "docker_source": "sha256:abc"}]
        self.disks = [{"key": "disk1"}]
        self.reply = lambda kind, params: {"ok": True}

    def __call__(self, method, path, body=None, file=None, timeout=120):
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")[1:]
        if parts == ["images"]:
            return self.images
        if parts == ["disks"]:
            return self.disks
        if parts[0] == "pools":
            if method == "PUT" and parts[1] not in self.pools:
                raise service.ApiError(404, "pool not found")
            self.pools[body["name"]] = body
            return body
        if parts == ["tasks"] and method == "POST":
            task = {"id": f"t{len(self.tasks) + 1}", "status": "queued", "payload": body["payload"]}
            self.tasks[task["id"]] = task
            return task
        task = self.tasks[parts[1]]
        if parts[2] == "wait":
            task["status"] = "succeeded"
            return task
        if parts[2] == "result":
            payload = task["payload"]
            assert payload["encoding"] == "python-source" and "def main(*params)" in payload["source"]
            kind = "scrape" if "def scrape_batch" in payload["source"] else "test"
            return {"status": "succeeded", "value": {"encoding": "json", "data": self.reply(kind, payload["params"])},
                    "error": None}
        raise AssertionError(path)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        store.open_db(Path(self.directory.name) / "proxies.db")
        self.core = FakeCore()
        self.original_core = service.core
        service.core = self.core
        service.GUEST_DISK = "guest1"
        store.set_value("status_packages", json.dumps({
            "key": service.sha256(json.dumps(["abc", service.PACKAGES]).encode()).hexdigest(), "disk": "disk1"}))
        service.scheduler = service.Scheduler(geo=type("Geo", (), {"country": staticmethod(lambda ip: "DE")})())

    def tearDown(self):
        service.core = self.original_core
        store.db().close()
        store._local.__dict__.clear()
        self.directory.cleanup()

    def call(self, method, path, body=None, query="", headers=None):
        from urllib.parse import parse_qs
        return service.route(method, path, body or {}, parse_qs(query), headers or {})

    def test_pools_use_cached_package_disk_and_guest(self):
        service.scheduler.ensure_pools(store.get_settings())
        scraper = self.core.pools["proxies-scraper"]
        self.assertEqual([d["key"] for d in scraper["disks"]], ["disk1", "guest1"])
        self.assertEqual(scraper["executor"], ["/tmp/environment/bin/python", "/tmp/scraperack/proxies/executor.py"])
        self.assertEqual(self.core.pools["proxies-tester"]["memory_mib"], 160)
        writes = len([c for c in self.core.calls if c[0] in ("PUT", "POST")])
        service.scheduler.ensure_pools(store.get_settings())   # unchanged settings: nothing sent
        self.assertEqual(len([c for c in self.core.calls if c[0] in ("PUT", "POST")]), writes)

    def test_scrape_and_test_rounds(self):
        store.add_sources(["https://a.example/list"])
        self.core.reply = lambda kind, params: (
            [{"url": u, "ok": True, "status": 200, "ms": 3, "proxies": [["8.8.8.8", 3128, "http"], ["8.8.4.4", 1080, None]]}
             for u in params[0]] if kind == "scrape" else
            {"real_ip": "1.1.1.1", "results": [{"id": i[0], "ip": i[1], "ok": i[2] == 3128, "protocol": "http",
                                               "https": False, "anonymity": "elite", "latency_ms": 50, "exit_ip": i[1]}
                                              for i in params[0]]})
        cfg = store.get_settings()
        service.scheduler.scrape_round(cfg)
        self.assertEqual(store.stats()["proxies"]["new"], 2)
        service.scheduler.test_round(cfg)
        self.assertEqual(store.stats()["proxies"], {"alive": 1, "new": 0, "dead": 1})
        self.assertEqual(store.query_proxies()[0]["country"], "DE")
        self.assertEqual(store.last_runs()["test"]["summary"]["alive"], 1)

    def test_inconclusive_batch_marks_nothing_dead(self):
        store.add_sources(["https://a.example/list"])
        source = store.enabled_sources()[0]["id"]
        store.store_scrape_result(source, {"ok": True, "proxies": [["8.8.8.8", 3128, None]]})
        store.refresh_source_stats(store.get_settings())
        self.core.reply = lambda kind, params: {"real_ip": None, "results": [], "inconclusive": "canary failed"}
        service.scheduler.test_round(store.get_settings())
        self.assertEqual(store.stats()["proxies"]["new"], 1)
        self.assertGreater(store.get_settings()["test_rate_ceiling"], 0)

    def test_public_api_leaves_keys_to_scraperack(self):
        status, body = self.call("GET", "/api/proxies")
        self.assertEqual((status, body), (200, {"count": 0, "proxies": []}))
        self.assertEqual(self.call("GET", "/api/proxies", query="format=txt")[1], "")
        with self.assertRaises(service.ApiError):   # the plugin no longer manages keys
            self.call("GET", "/api/keys")
        with self.assertRaises(service.ApiError):
            self.call("GET", "/api/proxies/random")

    def test_reports_route(self):
        with self.assertRaises(service.ApiError) as caught:
            self.call("POST", "/api/reports", {"proxy": "socks5://1.2.3.4:1080", "reason": "dead"})
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ValueError):
            self.call("POST", "/api/reports", {"proxy": "1.2.3.4:1080", "reason": "maybe"})
        with self.assertRaises(service.ApiError):   # reports are POST only
            self.call("GET", "/api/reports")

    def test_admin_routes(self):
        self.assertEqual(self.call("POST", "/gui/api/sources", {"urls": "a.example\nb.example"})[1], {"added": 2})
        ids = [s["id"] for s in self.call("GET", "/api/sources")[1]]
        self.assertEqual(self.call("POST", "/api/sources/disable", {"ids": ids[:1]})[1], {"changed": 1})
        self.assertEqual(len(store.enabled_sources()), 1)
        self.assertEqual(self.call("PUT", "/api/settings", {"scrape_parallel": 3})[1], {"saved": 1})
        self.assertEqual(self.call("POST", "/api/run", {"kind": "scrape"})[0], 202)
        overview = self.call("GET", "/api/overview")[1]
        self.assertEqual(overview["scheduler"]["pending_commands"], ["scrape"])
        with self.assertRaises(service.ApiError):
            self.call("GET", "/api/nothing")


class ExecutorTest(unittest.TestCase):
    def test_python_source(self):
        for source in service.SOURCES.values():   # each defines main() and imports only the stdlib at load
            namespace = {}
            exec(compile(source, "<test>", "exec"), namespace)
            self.assertTrue(callable(namespace["main"]))
        source = "def main(a, b):\n    return a + b\n"
        reply = executor.handle({"encoding": "python-source", "source": source, "params": [2, 3]})
        self.assertEqual(reply["result"]["value"], {"encoding": "json", "data": 5})
        self.assertIn("unknown payload encoding", executor.handle({"encoding": "nope"})["error"])
        self.assertEqual(executor.handle({"encoding": "scraperack-preload", "preload": ["json"]})["result"]["value"],
                         {"encoding": "json", "data": ["json"]})

    def test_extract_formats(self):
        import scrape
        text = "socks5://9.9.9.9:1080\n<td>8.8.8.8</td><td>3128</td>\n1.2.3.4:80 10.0.0.1:80"
        found = scrape.extract_proxies(text, "https://x.example/http.txt")
        self.assertEqual(found, [["1.2.3.4", 80, "http"], ["8.8.8.8", 3128, "http"], ["9.9.9.9", 1080, "socks5"]])


if __name__ == "__main__":
    unittest.main()
