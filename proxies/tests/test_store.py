import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        store.open_db(Path(self.directory.name) / "proxies.db")
        self.cfg = store.get_settings()

    def tearDown(self):
        store.db().close()
        store._local.__dict__.clear()
        self.directory.cleanup()

    def scrape(self, source_id, proxies, ok=True):
        return store.store_scrape_result(source_id, {"ok": ok, "status": 200, "ms": 5, "proxies": proxies,
                                                     "error": None if ok else "HTTP 500"})

    def test_settings_defaults_and_save(self):
        self.assertTrue(self.cfg["scraping_enabled"])
        self.assertEqual(self.cfg["test_round_max"], 3000)
        store.save_settings({"test_round_max": "50", "testing_enabled": False, "image": " python-generic-v2 "})
        cfg = store.get_settings()
        self.assertEqual((cfg["test_round_max"], cfg["testing_enabled"], cfg["image"]), (50, False, "python-generic-v2"))
        with self.assertRaises(ValueError):
            store.save_settings({"test_round_max": "lots"})
        with self.assertRaises(ValueError):
            store.save_settings({"nope": 1})

    def test_sources_scrape_and_counts(self):
        self.assertEqual(store.add_sources(["example.com/a.txt", "https://example.com/a.txt", "# comment",
                                            "https://other.example/socks5"]), 2)
        self.assertEqual(store.add_sources(["https://example.com/a.txt"]), 0)
        first, second = [s["id"] for s in store.enabled_sources()]
        self.assertEqual(self.scrape(first, [["1.2.3.4", 80, "http"], ["5.6.7.8", 1080, None]]), (2, 2))
        self.assertEqual(self.scrape(second, [["1.2.3.4", 80, "http"]]), (1, 0))
        store.refresh_source_counts()
        self.assertEqual(store.scalar("SELECT source_count FROM proxies WHERE ip = '1.2.3.4'"), 2)
        store.refresh_source_stats(self.cfg)
        listed = {s["id"]: (s["listed"], s["pending"]) for s in store.list_sources()}
        self.assertEqual(listed, {first: (2, 2), second: (1, 1)})

    def test_failing_source_is_disabled(self):
        store.add_sources(["https://broken.example/list"])
        source = store.enabled_sources()[0]["id"]
        for _ in range(3):
            self.scrape(source, [], ok=False)
        disabled = store.refresh_source_stats(self.cfg)
        self.assertEqual([d[0] for d in disabled], [source])
        self.assertEqual(store.enabled_sources(), [])
        store.set_sources_enabled([source], True)
        self.assertEqual(len(store.enabled_sources()), 1)

    def test_round_tests_and_yield(self):
        store.add_sources(["https://a.example/list"])
        source = store.enabled_sources()[0]["id"]
        self.scrape(source, [[f"9.9.9.{i}", 8080, "http"] for i in range(1, 11)])
        store.refresh_source_stats(self.cfg)
        picked, alive_due = store.pick_for_test(self.cfg)
        self.assertEqual((len(picked), alive_due), (10, 0))
        results = [{"id": p["id"], "ip": p["ip"], "ok": i < 3, "protocol": "http", "https": True,
                    "anonymity": "elite", "latency_ms": 100 + i, "exit_ip": p["ip"]} for i, p in enumerate(picked)]
        self.assertEqual(store.store_test_results(results, lambda ip: "US"), (3, 7))
        store.refresh_source_stats(self.cfg)
        row = store.list_sources()[0]
        self.assertEqual((row["tested"], row["ever_alive"], row["alive_now"], row["yield_pct"]), (10, 3, 3, 30.0))
        # Dead proxies are final and alive ones are not due yet: nothing left to test.
        self.assertEqual(store.pick_for_test(self.cfg), ([], 0))
        found = store.query_proxies(protocol="https", country="us", limit=2)
        self.assertEqual([p["latency_ms"] for p in found], [100, 101])
        self.assertEqual(found[0]["url"], f"http://{found[0]['ip']}:8080")
        self.assertTrue(found[0]["last_checked"].endswith("Z"))
        self.assertEqual(store.stats()["proxies"], {"alive": 3, "new": 0, "dead": 7})
        with self.assertRaises(ValueError):
            store.query_proxies(protocol="ftp")

    def test_retention_forgets_unlisted_dead(self):
        store.add_sources(["https://a.example/list"])
        source = store.enabled_sources()[0]["id"]
        self.scrape(source, [["8.8.4.4", 3128, None], ["8.8.4.5", 3128, None]])
        store.refresh_source_stats(self.cfg)
        ids = [p["id"] for p in store.pick_for_test(self.cfg)[0]]
        self.assertEqual(len(ids), 2)
        store.store_test_results([{"id": i, "ip": "x", "ok": False} for i in ids], lambda ip: None)
        with store.write() as conn:
            conn.execute("UPDATE proxy_sources SET last_seen = '2000-01-01 00:00:00'")
        out = store.apply_retention(self.cfg)
        self.assertEqual((out["listings"], out["dead"]), (2, 2))
        self.assertEqual(store.scalar("SELECT COUNT(*) FROM proxies"), 0)

    def test_api_keys(self):
        created = store.create_key("bot", 0)
        self.assertIsNone(store.check_key(created["key"]))
        self.assertEqual(store.check_key("psx_wrong"), "invalid API key")
        rotated = store.rotate_key(created["id"], 60)
        self.assertIsNone(store.check_key(created["key"]))   # old key still in its grace period
        self.assertIsNone(store.check_key(rotated["key"]))
        with store.write() as conn:   # the grace period ends
            conn.execute("UPDATE api_keys SET previous_expires_at = '2000-01-01 00:00:00'")
        self.assertEqual(store.check_key(created["key"]), "this API key was rotated; use the new key")
        store.rotate_key(created["id"], 0)
        self.assertEqual(store.check_key(rotated["key"]), "invalid API key")
        store.set_key_enabled(created["id"], False)
        self.assertEqual(store.list_keys()[0]["status"], "disabled")
        store.delete_key(created["id"])
        with self.assertRaises(KeyError):
            store.delete_key(created["id"])


if __name__ == "__main__":
    unittest.main()
