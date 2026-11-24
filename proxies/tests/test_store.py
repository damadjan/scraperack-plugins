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

    def test_proven_source_gets_more_slack(self):
        store.add_sources(["https://big.example/list"])
        source = store.enabled_sources()[0]["id"]
        with store.write() as conn:
            conn.execute("UPDATE sources SET tested = 500, ever_alive = 20 WHERE id = ?", (source,))
        for _ in range(5):
            self.scrape(source, [], ok=False)
        self.assertEqual(store.refresh_source_stats(self.cfg), [])
        with store.write() as conn:
            conn.execute("UPDATE sources SET fail_streak = 48 WHERE id = ?", (source,))
        self.assertEqual([d[0] for d in store.refresh_source_stats(self.cfg)], [source])

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
        # Dead proxies that never worked are final and alive ones are not due yet.
        self.assertEqual(store.pick_for_test(self.cfg), ([], 0))
        found = store.query_proxies(protocol="https", country="us", limit=2)
        self.assertEqual([p["latency_ms"] for p in found], [100, 101])
        self.assertEqual(found[0]["url"], f"http://{found[0]['ip']}:8080")
        self.assertTrue(found[0]["last_checked"].endswith("Z"))
        self.assertEqual(store.stats()["proxies"], {"alive": 3, "new": 0, "dead": 7})
        with self.assertRaises(ValueError):
            store.query_proxies(protocol="ftp")

    def alive(self, specs):
        """Proxies made alive by a test round: specs are (ip, https, https_latency_ms, intercepts)."""
        store.add_sources(["https://t.example/list"])
        source = store.enabled_sources()[0]["id"]
        self.scrape(source, [[ip, 1080, "socks5"] for ip, *_ in specs])
        store.refresh_source_stats(self.cfg)
        picked = {p["ip"]: p["id"] for p in store.pick_for_test(self.cfg)[0]}
        store.store_test_results([{"id": picked[ip], "ip": ip, "ok": True, "protocol": "socks5", "https": https,
                                   "https_latency_ms": ms, "intercepts": forged, "anonymity": "elite",
                                   "latency_ms": 100, "exit_ip": ip} for ip, https, ms, forged in specs],
                                 lambda ip: "US")
        return picked

    def test_trust_filters_and_score(self):
        self.alive([("7.7.7.1", True, 900, False), ("7.7.7.2", True, 300, False),
                    ("7.7.7.3", False, None, True), ("7.7.7.4", False, None, False)])
        ips = lambda **kw: [p["ip"] for p in store.query_proxies(sort="score", **kw)]
        self.assertEqual(ips(trusted=True), ["7.7.7.2", "7.7.7.1"])
        self.assertEqual(ips(connect=True), ["7.7.7.2", "7.7.7.1"])
        self.assertEqual(ips(trusted=True, max_https_latency=500), ["7.7.7.2"])
        self.assertEqual(store.stats()["intercepting"], 1)
        self.assertEqual(store.capacity_counts(), {"alive": 4, "new": 0, "trusted": 2})
        # A later clean check never clears a forged certificate.
        forged = store.query_proxies(limit=10)
        row = next(p for p in forged if p["ip"] == "7.7.7.3")
        self.assertTrue(row["intercepts"])
        # Reported working moves a proxy to the front of sort=score.
        store.report_proxy("socks5h://7.7.7.1:1080", "worked")
        self.assertEqual(ips(trusted=True), ["7.7.7.1", "7.7.7.2"])
        self.assertEqual(store.query_proxies(trusted=True, sort="score")[0]["worked"], 1)

    def test_reports(self):
        picked = self.alive([("6.6.6.1", True, 200, False), ("6.6.6.2", True, 250, False)])
        self.assertIsNone(store.report_proxy("1.2.3.4:9", "dead"))
        with self.assertRaises(ValueError):
            store.report_proxy("6.6.6.1:1080", "nope")
        with self.assertRaises(ValueError):
            store.report_proxy("nonsense", "dead")
        # Dead: not handed out until re-tested, and re-tested first in the next round.
        self.assertEqual(store.report_proxy("socks5://6.6.6.1:1080", "dead"), picked["6.6.6.1"])
        self.assertEqual([p["ip"] for p in store.query_proxies()], ["6.6.6.2"])
        self.assertEqual(store.stats()["proxies"]["alive"], 2)   # one report never kills a proxy
        self.assertEqual([p["id"] for p in store.pick_for_test(self.cfg)[0]], [picked["6.6.6.1"]])
        # Intercepts: untrusted from then on.
        store.report_proxy("6.6.6.2:1080", "intercepts")
        self.assertEqual(store.query_proxies(trusted=True), [])

    def test_trusted_proxies_are_rechecked_sooner(self):
        picked = self.alive([("5.5.5.1", True, 200, False), ("5.5.5.2", False, None, False)])
        with store.write() as conn:
            conn.execute("UPDATE proxies SET last_checked = ?", (store.ago_ts(minutes=6),))
        due, alive_due = store.pick_for_test(self.cfg)
        self.assertEqual(([p["id"] for p in due], alive_due), ([picked["5.5.5.1"]], 1))
        store.save_settings({"recheck_trusted_min": 0})
        self.assertEqual(store.pick_for_test(store.get_settings()), ([], 0))

    def test_dead_that_worked_is_retested_with_backoff(self):
        store.add_sources(["https://a.example/list"])
        source = store.enabled_sources()[0]["id"]
        self.scrape(source, [["9.9.8.1", 8080, "http"], ["9.9.8.2", 8080, "http"]])
        store.refresh_source_stats(self.cfg)
        picked, _ = store.pick_for_test(self.cfg)
        ok = {"ok": True, "protocol": "socks5", "https": False, "anonymity": "elite", "latency_ms": 50}
        store.store_test_results([{**ok, "id": picked[0]["id"], "ip": picked[0]["ip"]},
                                  {"id": picked[1]["id"], "ip": picked[1]["ip"], "ok": False}], lambda ip: None)
        worked = picked[0]["id"]
        store.store_test_results([{"id": worked, "ip": "x", "ok": False}], lambda ip: None)
        self.assertEqual(store.pick_for_test(self.cfg), ([], 0))   # failed just now: not due yet

        def age(minutes):
            with store.write() as conn:
                conn.execute("UPDATE proxies SET last_checked = ?", (store.ago_ts(minutes=minutes),))

        age(61)
        picked, alive_due = store.pick_for_test(self.cfg)
        self.assertEqual(([p["id"] for p in picked], alive_due), ([worked], 0))
        self.assertEqual((picked[0]["status"], picked[0]["protocol"]), ("dead", "socks5"))
        store.store_test_results([{"id": worked, "ip": "x", "ok": False}], lambda ip: None)
        age(61)   # second failed re-test: now waits 120 minutes
        self.assertEqual(store.pick_for_test(self.cfg), ([], 0))
        age(121)
        self.assertEqual([p["id"] for p in store.pick_for_test(self.cfg)[0]], [worked])
        store.store_test_results([{**ok, "id": worked, "ip": "x"}], lambda ip: None)
        self.assertEqual(store.stats()["proxies"]["alive"], 1)
        self.assertEqual(store.list_sources()[0]["ever_alive"], 1)   # a comeback is not counted twice

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


if __name__ == "__main__":
    unittest.main()
