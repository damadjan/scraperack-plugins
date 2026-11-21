# Proxies plugin

Free public proxies, collected from the lists you add, tested in microVMs, and served to your
programs through an API.

## Setup

1. Build the archive and install it from **Plugins → Install plugin**:

   ```sh
   tar -czf proxies-plugin.tar.gz -C proxies/plugin .
   ```

2. On the plugin's entry in Scraperack, click **Open Proxies**. Everything else happens on the
   plugin's own page:

   ```
   http://<controller>:7070/plugins/proxies/gui/app/
   ```

3. On **Sources**, paste the proxy lists you want scraped, one URL or domain per line. None
   ship with the plugin.
4. On **API keys**, create a key for your programs.

On first start the plugin builds a package disk (curl-cffi, aiohttp, aiohttp-socks) for the
image chosen in **Settings** (default `python-generic-v2`; any image with `/usr/bin/python3`
and its `venv` module): a short-lived `proxies-builder` microVM pip-installs them into a
virtualenv and the controller turns it into a disk. Then it creates two pools,
`proxies-scraper` and `proxies-tester`. The dashboard's top right shows when they are ready.

## The plugin's page

- **Dashboard:** alive, untested and dead counts, alive proxies by protocol and anonymity,
  the last scrape and test round, the tester's rate, and the activity log. **Scrape now** and
  **Test now** start a run within a few seconds.
- **Sources:** add, search, enable, disable and delete sources. Each one shows what its last
  scrape found, how many of its proxies were tested and worked (yield), and why it was
  disabled. **Disable failing/empty** turns off every source whose last scrape failed or
  found nothing.
- **Proxies:** working proxies with filters, exported as `ip:port` or `protocol://ip:port`.
- **API keys:** create, rotate (with a grace period for the old secret), disable and delete
  keys, and turn the key requirement off.
- **Settings:** schedule, scraper and tester sizes, source scoring, rate limiting, retention,
  judge URLs, and the pools' image and machine sizes.

## API

Programs use the API with `Authorization: Bearer <key>` (unless **Require a key** is off):

```sh
curl -H "Authorization: Bearer $KEY" \
  "http://<controller>:7070/plugins/proxies/api/proxies?protocol=socks5&limit=20&format=txt"
```

| Endpoint | Returns |
|---|---|
| `GET /plugins/proxies/api/proxies` | Working proxies, `{count, proxies}` or one per line with `format=txt` |
| `GET /plugins/proxies/api/proxies/random` | One working proxy among the 500 checked most recently |
| `GET /plugins/proxies/api/stats` | Counts by status, protocol and anonymity |
| `POST /plugins/proxies/api/reports` | `{proxy, reason}`: what a program learned using a proxy |

These four are the whole contract for programs, and other plugins are programs too: they call
the same endpoints with a key, through their core socket. There is no separate plugin API.

Filters for both proxy endpoints: `protocol` (`http,https,socks4,socks5`; `https` means HTTP
proxies that also do CONNECT), `anonymity` (`transparent,anonymous,elite`), `country` (ISO
codes, `US,DE`), `max_latency` (ms, to the HTTP judge), `max_https_latency` (ms, a full HTTPS
answer through the proxy), `checked_within` (minutes), `connect=1` (HTTPS works through it)
and `trusted=1` (HTTPS works with valid certificates, and the proxy never showed a forged one).
`/proxies` also takes `limit` (default 100, at most 50000), `sort` (`latency`, `checked`,
`uptime`, `score`) and, with `format=txt`, `scheme=1` for `protocol://ip:port` lines. `score`
puts proxies reported working in the last 6 hours first, then reliable ones (4 in 5 checks
pass), then the fastest over HTTPS.

Reports take `proxy` as `scheme://host:port` or `host:port` and one `reason`:

- `worked`: it carried a real request. Counts toward `sort=score`.
- `dead`: it failed for you. It isn't handed out again until its re-test, which comes first in
  the next round. One report never marks a proxy dead; only the tester does.
- `intercepts`: it showed a forged certificate. It is never trusted again.

An unknown proxy answers 404.

The page's own endpoints (`overview`, `sources`, `browse`, `settings`, `keys`, `run`) are
under the same `/plugins/proxies/api/` and need no key, like the rest of the controller's API.

## How it works

**Scraper.** Every `scrape_interval_min`, enabled sources are split into batches and fetched in
`proxies-scraper` microVMs with curl-cffi (Chrome impersonation). The parser handles `ip:port`
lists, `scheme://ip:port`, HTML tables, JSON APIs and base64-encoded IPs. A protocol hint is
taken from the URL (`socks5.txt`) or the list itself. Sources that fail to scrape
`source_disable_fail_streak` (3) times in a row, or yield nothing after their trial, are
disabled (the reason is shown; **Enable** resets it). A source that has yielded working proxies
is only disabled after `source_disable_fail_streak_proven` (48, about a day) errors in a row.

**Tester.** Each round re-checks stale alive proxies first, then fills the rest of
`test_round_max` with untested proxies split across sources: a new source gets a trial
(`source_trial_size` of its proxies), after which its share follows its yield, with a floor of
`source_min_share`. Within a source, what it lists right now goes first, then proxies listed by
several sources, then the most recently discovered. Leftover budget goes to dead proxies that
once worked (see below). Each check opens one plain TCP connection
first (most dead proxies stop there), then tries HTTP, SOCKS5 and SOCKS4 through a judge to
learn the protocol, latency, exit IP and anonymity, and an HTTPS request through it to one of
the `https_test_url` lines, picked at random, with certificates checked. That request gives
the HTTPS latency, and a forged certificate marks the proxy as intercepting for good. Picking
one URL per check costs no extra connections, yet a proxy that forges certificates for some
sites only meets several of them over its re-checks. Trusted proxies (clean HTTPS) are
re-checked every `recheck_trusted_min` (5) minutes instead of `recheck_alive_min` (15), at most
`recheck_trusted_max` (200) of them, best first: they are the ones programs get. The round's
connection rate stays capped either way (see below), so this reorders the budget rather than
adding to it. Countries come from the free DB-IP Lite database, downloaded monthly.

**Rate limiting.** A home internet link only tolerates a few new connections per second, so
the tester starts at `test_rate_min` connections/s across the cluster and ramps up about 8% a
minute to `test_rate_max`. Scraping and testing never run at the same time.

**Dead proxies.** A proxy that never worked and fails its test is not tested again while any
source still lists it, so a re-scrape cannot bring it back. Free proxies often fail one check and
come back, so a proxy that has worked before and is still listed is re-tested
`recheck_dead_min` (60) minutes after it fails, the wait doubling after each failed re-test up to
`recheck_dead_max_hours` (24). Because failures stick, a batch is thrown away
(nothing marked dead) if the microVM's own direct "canary" connections fail while it runs;
the tester then halves its rate, sets a learned ceiling at 80% of the rate that failed, and
pauses for `network_backoff_s`.

**Retention.** Hourly, the plugin forgets what the sources no longer list, so the database
stays about the size of the current lists:

- source listings not seen for `retention_unlisted_days` (3) are dropped, then dead proxies
  with no listing left are deleted; if one is listed again later it gets one fresh test
- untested proxies no source listed in `retention_new_unlisted_hours` (12) are deleted
- `retention_dead_retest_days` (off) also forgets dead proxies still listed, for one more test
- alive proxies are never deleted; they go once they stop working and drop off the lists

## Storage

One SQLite database, `proxies.db`, in the plugin's data directory on the controller
(`/var/lib/scraperack-controller/plugins/proxies/data/`), in WAL mode. It is kept across
plugin updates and removed with the plugin. With about a million listings it is around
250 MB. Copy it with `sqlite3 proxies.db ".backup copy.db"` while the plugin runs.
