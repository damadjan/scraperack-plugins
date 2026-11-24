"""SQLite storage: sources, proxies, who lists what, settings, runs and events.

Proxy lifecycle: new -> alive <-> (re-checked) ... -> dead. A dead proxy that never worked is
not tested again while sources keep listing it: its row stays so a re-scrape cannot bring it
back as new. One that did work is re-tested with a doubling backoff (recheck_dead_min up to
recheck_dead_max_hours), since free proxies often fail a single check and come back. Once no
source has listed it for `retention_unlisted_days` the row is deleted (see apply_retention), so
the tables stay about as big as what the sources list now instead of growing forever.

One database file in the plugin's data directory, in WAL mode: readers never wait, and writers
take `write_lock` so the scheduler's threads and the API never fight over SQLite's own lock.
Timestamps are UTC text ('YYYY-MM-DD HH:MM:SS'), so they sort and compare as strings.
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, UTC

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    disabled_reason TEXT,
    created_at TEXT NOT NULL,
    last_scraped_at TEXT,
    last_status TEXT,                       -- ok / empty / error
    last_error TEXT,
    last_http_status INTEGER,
    last_found INTEGER NOT NULL DEFAULT 0,
    last_new INTEGER NOT NULL DEFAULT 0,
    last_duration_ms INTEGER,
    scrapes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0, -- scrape errors in a row
    listed INTEGER NOT NULL DEFAULT 0,      -- proxies this source lists
    tested INTEGER NOT NULL DEFAULT 0,      -- running total: its proxies tested at least once
    ever_alive INTEGER NOT NULL DEFAULT 0,  -- running total: its proxies that worked at least once
    alive_now INTEGER NOT NULL DEFAULT 0,
    pending INTEGER NOT NULL DEFAULT 0,     -- listed, not tested yet
    yield_pct REAL,
    last_budget INTEGER NOT NULL DEFAULT 0  -- proxies picked in the last test round
);
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    hint TEXT,                              -- protocol suggested by the source
    protocol TEXT,                          -- http / socks4 / socks5 (detected)
    https INTEGER NOT NULL DEFAULT 0,
    anonymity TEXT,                         -- transparent / anonymous / elite
    latency_ms INTEGER,
    exit_ip TEXT,
    country TEXT,
    status TEXT NOT NULL DEFAULT 'new',     -- new / alive / dead (final)
    source_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_checked TEXT,
    last_alive TEXT,
    checks INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0,
    https_latency_ms INTEGER,               -- time to a full HTTPS answer through it (CONNECT/TLS)
    intercepts INTEGER NOT NULL DEFAULT 0,  -- showed a forged certificate once: never trusted again
    worked INTEGER NOT NULL DEFAULT 0,      -- programs reported it worked for them
    last_worked TEXT,
    UNIQUE (ip, port)
);
CREATE INDEX IF NOT EXISTS ix_proxies_status_checked ON proxies (status, last_checked);
CREATE INDEX IF NOT EXISTS ix_proxies_alive ON proxies (status, protocol, latency_ms);
CREATE TABLE IF NOT EXISTS proxy_sources (
    source_id INTEGER NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (source_id, ip, port)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_proxy_sources_ipport ON proxy_sources (ip, port);
CREATE INDEX IF NOT EXISTS ix_proxy_sources_seen ON proxy_sources (last_seen);
CREATE INDEX IF NOT EXISTS ix_proxy_sources_source_seen ON proxy_sources (source_id, last_seen);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                     -- scrape / test
    status TEXT NOT NULL,                   -- running / done / failed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
-- API keys moved to Scraperack itself in 0.2.0.
DROP TABLE IF EXISTS api_keys;
"""

DEFAULT_JUDGES = 'http://httpbin.org/get\nhttp://azenv.net/\nhttp://proxyjudge.us/azenv.php'
# Well-known sites with valid certificates. One per check, picked at random: no more
# connections than a single URL, but a proxy that forges certificates for some sites only
# meets several of them over its re-checks.
DEFAULT_HTTPS_URLS = ('https://httpbin.org/ip\nhttps://www.cloudflare.com/cdn-cgi/trace\n'
                      'https://www.google.com/generate_204\nhttps://www.apple.com/library/test/success.html\n'
                      'https://detectportal.firefox.com/success.txt')
OLD_DEFAULTS = {'https_test_url': ('https://httpbin.org/ip',)}   # replaced by the new default
# Columns added after the first release: (name, definition), added on startup when missing.
MIGRATIONS = (('https_latency_ms', 'INTEGER'), ('intercepts', 'INTEGER NOT NULL DEFAULT 0'),
              ('worked', 'INTEGER NOT NULL DEFAULT 0'), ('last_worked', 'TEXT'))
REPORT_REASONS = ('worked', 'dead', 'intercepts')
WORKED_RECENT_HOURS = 6   # sort=score puts proxies reported working this recently first

# key: (default, type, description)
DEFAULTS = {
    'scraping_enabled': ('1', bool, 'Run the scraper on its schedule'),
    'testing_enabled': ('1', bool, 'Run the tester continuously'),
    'scrape_interval_min': ('30', int, 'Minutes between full scrapes of all sources'),
    'scrape_batch_size': ('5', int, 'Source URLs per task'),
    'scrape_parallel': ('6', int, 'Scrape tasks in flight at once'),
    'scrape_timeout_s': ('30', int, 'Per-URL fetch timeout'),
    'source_trial_size': ('300', int, 'Proxies tested from a new source before its yield counts'),
    'source_min_share': ('0.02', float, 'Smallest share of the test budget an enabled source gets'),
    'source_auto_disable': ('1', bool, 'Disable sources that keep failing or never yield a working proxy'),
    'source_disable_fail_streak': ('3', int, 'Scrape errors in a row before a source is disabled'),
    'source_disable_fail_streak_proven': ('48', int, 'Scrape errors in a row before a source that has yielded working proxies is disabled'),
    'test_round_pause_s': ('10', int, 'Pause between test rounds'),
    'test_round_max': ('3000', int, 'Proxies tested per round (alive re-checks first, then new by source yield)'),
    'test_batch_size': ('300', int, 'Max proxies per test task'),
    'test_concurrency': ('120', int, 'Concurrent checks inside one microVM'),
    'test_rate_min': ('2', int, 'Cluster-wide new connections/s the tester starts at (and never goes below)'),
    'test_rate_max': ('8', int, 'Cluster-wide new connections/s the tester may ramp up to'),
    'test_rate_ceiling': ('0', int, 'Learned limit: 80% of the rate that caused network trouble (0 = none)'),
    'test_parallel': ('2', int, 'Test tasks in flight at once (one microVM each)'),
    'network_backoff_s': ('300', int, 'Pause testing this long after network trouble'),
    'test_timeout_s': ('10', int, 'Per-check timeout'),
    'recheck_alive_min': ('15', int, 'Re-test alive proxies after this many minutes'),
    'recheck_trusted_min': ('5', int, 'Re-test trusted proxies (clean HTTPS) sooner: after this many minutes (0 = like other alive proxies)'),
    'recheck_trusted_max': ('200', int, 'At most this many trusted proxies get the sooner re-test, best first'),
    'recheck_dead_min': ('60', int, 'Re-test dead proxies that once worked (and are still listed) after this many minutes, doubling after each failure (0 = never)'),
    'recheck_dead_max_hours': ('24', int, 'Longest wait between re-tests of a dead proxy that once worked'),
    'retention_unlisted_days': ('3', int, 'Forget dead proxies (and source listings) no source has listed for this many days'),
    'retention_new_unlisted_hours': ('12', int, 'Forget untested proxies no source has listed for this many hours'),
    'retention_dead_retest_days': ('0', int, 'Forget dead proxies after this many days even if still listed (0 = never)'),
    'judges': (DEFAULT_JUDGES, str, 'Proxy judge URLs (plain HTTP, one per line, httpbin or azenv format)'),
    'https_test_url': (DEFAULT_HTTPS_URLS, str, 'HTTPS URLs for the CONNECT/TLS check, one per line; each check picks one, so a proxy that intercepts only some sites is caught over a few rounds'),
    'image': ('python-generic-v2', str, 'Scraperack image (name or id) with Python 3 for both pools'),
    'scraper_cpu': ('0.5', float, 'Scraper microVM CPU cores'),
    'scraper_memory_mib': ('256', int, 'Scraper microVM memory (MiB)'),
    'scraper_warm': ('0', int, 'Scraper warm microVMs'),
    'scraper_max_instances': ('6', int, 'Scraper max microVMs'),
    'tester_cpu': ('0.5', float, 'Tester microVM CPU cores'),
    'tester_memory_mib': ('160', int, 'Tester microVM memory (MiB)'),
    'tester_warm': ('0', int, 'Tester warm microVMs'),
    'tester_max_instances': ('2', int, 'Tester max microVMs'),
}
STATUS_KEYS = ('status_test_rate', 'status_packages')   # kept by the service, not shown as settings
PRIOR_YIELD = 0.05   # assumed yield for a source still in its trial
PROTOCOLS = {'http', 'https', 'socks4', 'socks5'}
ANONYMITY = {'transparent', 'anonymous', 'elite'}

write_lock = threading.RLock()
_local = threading.local()
_path = None


def now():
    return datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')


def ago_ts(**delta):
    return (datetime.now(UTC) - timedelta(**delta)).strftime('%Y-%m-%d %H:%M:%S')


def url_hash(url):
    return hashlib.sha256(url.encode()).hexdigest()


def open_db(path):
    """Set the database file and create the schema. Call once at startup."""
    global _path
    _path = str(path)
    _local.__dict__.clear()
    conn = db()
    conn.execute('PRAGMA journal_mode=WAL')
    with write_lock:   # executescript commits on its own
        columns = {row[1] for row in conn.execute('PRAGMA table_info(proxies)')}
        for name, definition in MIGRATIONS:
            if columns and name not in columns:
                conn.execute(f'ALTER TABLE proxies ADD COLUMN {name} {definition}')
        conn.executescript(SCHEMA)
    seed()
    with write() as conn:   # nothing runs before the scheduler starts: these were cut off
        conn.execute("UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'", (now(),))


def db():
    """This thread's connection (autocommit; use write() for transactions)."""
    conn = getattr(_local, 'conn', None)
    if conn is None or getattr(_local, 'path', None) != _path:
        conn = sqlite3.connect(_path, timeout=60, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=60000')
        conn.execute('PRAGMA cache_size=-32000')
        _local.conn, _local.path = conn, _path
    return conn


@contextmanager
def write():
    """One write transaction at a time, across all threads."""
    with write_lock:
        conn = db()
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')


def rows(sql, params=()):
    return [dict(row) for row in db().execute(sql, params)]


def scalar(sql, params=()):
    row = db().execute(sql, params).fetchone()
    return row[0] if row else None


def marks(values):
    return ','.join('?' * len(values))


# ---------------------------------------------------------------- settings / events / runs

def seed():
    with write() as conn:
        existing = {row[0] for row in conn.execute('SELECT key FROM settings')}
        known = set(DEFAULTS) | set(STATUS_KEYS)
        for key in existing - known:
            conn.execute('DELETE FROM settings WHERE key = ?', (key,))
        for key, old in OLD_DEFAULTS.items():   # untouched old defaults follow the new one
            conn.execute(f'UPDATE settings SET value = ? WHERE key = ? AND value IN ({marks(old)})',
                         (DEFAULTS[key][0], key, *old))
        conn.executemany('INSERT INTO settings (key, value) VALUES (?, ?)',
                         [(k, v[0]) for k, v in DEFAULTS.items() if k not in existing]
                         + [(k, '') for k in STATUS_KEYS if k not in existing])


def parse_setting(key, value):
    default, kind, _ = DEFAULTS[key]
    try:
        if kind is bool:
            return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
        return kind(value)
    except (TypeError, ValueError):
        return parse_setting(key, default) if value != default else None


def get_settings():
    raw = {row['key']: row['value'] for row in rows('SELECT key, value FROM settings')}
    return {key: parse_setting(key, raw.get(key, default)) for key, (default, _, _) in DEFAULTS.items()}


def settings_view():
    values = get_settings()
    return [{'key': key, 'value': values[key], 'type': kind.__name__, 'description': text}
            for key, (_, kind, text) in DEFAULTS.items()]


def save_settings(values):
    clean = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f'unknown setting: {key}')
        kind = DEFAULTS[key][1]
        if kind is bool:
            clean[key] = '1' if (value is True or str(value).strip().lower() in ('1', 'true', 'yes', 'on')) else '0'
        else:
            try:
                parsed = kind(value)
            except (TypeError, ValueError):
                raise ValueError(f'{key} needs a {kind.__name__}') from None
            if kind in (int, float) and parsed < 0:
                raise ValueError(f'{key} cannot be negative')
            clean[key] = str(parsed) if kind is not str else str(value).strip()
    with write() as conn:
        conn.executemany('INSERT INTO settings (key, value) VALUES (?, ?) '
                         'ON CONFLICT (key) DO UPDATE SET value = excluded.value', list(clean.items()))
    return len(clean)


def get_value(key, default=None):
    value = scalar('SELECT value FROM settings WHERE key = ?', (key,))
    return default if value in (None, '') else value


def set_value(key, value):
    with write() as conn:
        conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                     'ON CONFLICT (key) DO UPDATE SET value = excluded.value', (key, str(value)))


def log(level, message):
    print(f'[{level}] {message}', flush=True)
    with write() as conn:
        conn.execute('INSERT INTO events (ts, level, message) VALUES (?, ?, ?)', (now(), level, message[:4000]))


def trim_events(keep=3000):
    with write() as conn:
        cutoff = conn.execute('SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?', (keep,)).fetchone()
        if cutoff:
            conn.execute('DELETE FROM events WHERE id <= ?', (cutoff[0],))
        old_run = conn.execute('SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET 500').fetchone()
        if old_run:
            conn.execute('DELETE FROM runs WHERE id <= ?', (old_run[0],))


def start_run(kind):
    with write() as conn:
        # Anything left 'running' from a previous process is dead.
        conn.execute("UPDATE runs SET status = 'failed', finished_at = ? WHERE kind = ? AND status = 'running'",
                     (now(), kind))
        return conn.execute("INSERT INTO runs (kind, status, started_at) VALUES (?, 'running', ?)",
                            (kind, now())).lastrowid


def finish_run(run_id, status, summary):
    with write() as conn:
        conn.execute('UPDATE runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?',
                     (status, now(), json.dumps(summary), run_id))


# ---------------------------------------------------------------- sources

def add_sources(urls):
    new, seen = [], set()
    for url in urls:
        url = url.strip()
        if not url or url.startswith('#'):
            continue
        if '://' not in url:
            url = 'https://' + url
        if url not in seen:
            seen.add(url)
            new.append((url, url_hash(url), now()))
    if not new:
        return 0
    with write() as conn:
        before = conn.total_changes
        conn.executemany('INSERT OR IGNORE INTO sources (url, url_hash, created_at) VALUES (?, ?, ?)', new)
        return conn.total_changes - before


def list_sources(search=''):
    where, params = '', ()
    if search:
        where, params = 'WHERE url LIKE ?', (f'%{search}%',)
    return rows('SELECT id, url, enabled, disabled_reason, last_scraped_at, last_status, last_error, '
                'last_http_status, last_found, last_new, last_duration_ms, scrapes, failures, listed, tested, '
                f'ever_alive, alive_now, pending, yield_pct, last_budget FROM sources {where} ORDER BY id', params)


def set_sources_enabled(ids, enabled):
    with write() as conn:
        return conn.execute(
            f'UPDATE sources SET enabled = ?, disabled_reason = NULL, fail_streak = 0 WHERE id IN ({marks(ids)})',
            (1 if enabled else 0, *ids)).rowcount


def delete_sources(ids):
    with write() as conn:
        conn.execute(f'DELETE FROM proxy_sources WHERE source_id IN ({marks(ids)})', ids)
        return conn.execute(f'DELETE FROM sources WHERE id IN ({marks(ids)})', ids).rowcount


def disable_failing_sources():
    with write() as conn:
        return conn.execute("UPDATE sources SET enabled = 0, disabled_reason = 'disabled by hand: failing or empty' "
                            "WHERE enabled = 1 AND last_status IN ('error', 'empty')").rowcount


def enabled_sources():
    return rows('SELECT id, url FROM sources WHERE enabled = 1 ORDER BY id')


def store_scrape_result(source_id, result):
    """Save one URL's scrape result; returns (found, new)."""
    ts = now()
    found = result.get('proxies') or []
    new = 0
    with write() as conn:
        for start in range(0, len(found), 5000):
            part = found[start:start + 5000]
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO proxies (ip, port, hint, first_seen) VALUES (?, ?, ?, ?)",
                             [(ip, int(port), hint, ts) for ip, port, hint in part])
            new += conn.total_changes - before
            conn.executemany('INSERT INTO proxy_sources (source_id, ip, port, last_seen) VALUES (?, ?, ?, ?) '
                             'ON CONFLICT (source_id, ip, port) DO UPDATE SET last_seen = excluded.last_seen',
                             [(source_id, ip, int(port), ts) for ip, port, _ in part])
        status = 'error' if not result.get('ok') else ('ok' if found else 'empty')
        conn.execute(
            'UPDATE sources SET last_scraped_at = ?, last_status = ?, last_error = ?, last_http_status = ?, '
            'last_found = ?, last_new = ?, last_duration_ms = ?, scrapes = scrapes + 1, '
            'fail_streak = CASE WHEN ? THEN fail_streak + 1 ELSE 0 END, '
            'failures = failures + ? WHERE id = ?',
            (ts, status, result.get('error') or None, result.get('status'), len(found), new, result.get('ms'),
             status == 'error', 1 if status == 'error' else 0, source_id))
    return len(found), new


def refresh_source_counts():
    """How many sources list each untested proxy; proxies on several lists are tested first."""
    with write() as conn:
        conn.execute("UPDATE proxies SET source_count = (SELECT COUNT(*) FROM proxy_sources ps "
                     "WHERE ps.ip = proxies.ip AND ps.port = proxies.port) WHERE status = 'new'")


def refresh_source_stats(cfg):
    """Refresh per-source counts and auto-disable sources that fail or never yield.

    `tested` and `ever_alive` are running totals kept by store_test_results, so they survive
    retention deleting the proxies behind them; only the current counts are recomputed here.
    """
    stats = rows("SELECT ps.source_id AS sid, COUNT(*) AS listed, "
                 "SUM(p.status = 'alive') AS alive_now, SUM(p.status = 'new') AS pending "
                 "FROM proxy_sources ps JOIN proxies p ON p.ip = ps.ip AND p.port = ps.port "
                 "GROUP BY ps.source_id")
    disabled = []
    with write() as conn:
        conn.execute('UPDATE sources SET listed = 0, alive_now = 0, pending = 0')
        conn.executemany('UPDATE sources SET listed = ?, alive_now = ?, pending = ? WHERE id = ?',
                         [(r['listed'], r['alive_now'] or 0, r['pending'] or 0, r['sid']) for r in stats])
        conn.execute('UPDATE sources SET yield_pct = CASE WHEN tested > 0 '
                     'THEN ROUND(100.0 * ever_alive / tested, 2) ELSE NULL END')
        if cfg['source_auto_disable']:
            streak, trial = cfg['source_disable_fail_streak'], cfg['source_trial_size']
            # A source that has yielded working proxies gets far more slack: big lists time out
            # and hosts have bad hours, and losing a proven source costs much more than a scrape.
            proven = max(streak, cfg['source_disable_fail_streak_proven'])
            for row in conn.execute('SELECT id, url, ever_alive > 0 FROM sources WHERE enabled = 1 AND '
                                    'fail_streak >= CASE WHEN ever_alive > 0 THEN ? ELSE ? END', (proven, streak)):
                disabled.append((row[0], row[1], f'{proven if row[2] else streak}+ scrape errors in a row'))
            for row in conn.execute('SELECT id, url FROM sources WHERE enabled = 1 AND tested >= ? AND ever_alive = 0',
                                    (trial,)):
                disabled.append((row[0], row[1], f'0 working proxies out of {trial}+ tested'))
            conn.executemany('UPDATE sources SET enabled = 0, disabled_reason = ? WHERE id = ?',
                             [(reason, sid) for sid, _, reason in disabled])
    return disabled


def pick_for_test(cfg):
    """Build one test round: stale alive proxies first, then new ones split across sources by yield,
    then dead proxies that once worked and are due for a re-test.

    Dead proxies that never worked are never picked. A source still in its trial (fewer than source_trial_size of
    its proxies tested) gets enough to finish the trial; the rest of the budget is shared in
    proportion to each source's yield, with a floor of source_min_share so weak sources still trickle.
    """
    budget = cfg['test_round_max']
    cols = 'p.id, p.ip, p.port, p.hint, p.protocol, p.status'
    picked = {}
    if cfg['recheck_trusted_min'] > 0 and cfg['recheck_trusted_max'] > 0:
        # The proxies programs actually get (clean HTTPS) are re-checked sooner, so a dead one
        # stops being handed out within minutes. Few of them, and the round's pacer caps the
        # connection rate either way: this reorders the budget, it doesn't add to it.
        for row in rows(f"SELECT {cols} FROM proxies p WHERE p.status = 'alive' AND p.https = 1 "
                        "AND p.intercepts = 0 AND (p.last_checked IS NULL OR p.last_checked < ?) "
                        "ORDER BY p.last_checked IS NULL DESC, p.last_worked IS NULL, "
                        "COALESCE(p.https_latency_ms, 999999) LIMIT ?",
                        (ago_ts(minutes=cfg['recheck_trusted_min']), min(cfg['recheck_trusted_max'], budget))):
            picked[row['id']] = row
    alive_due = rows(f"SELECT {cols} FROM proxies p WHERE p.status = 'alive' AND "
                     "(p.last_checked IS NULL OR p.last_checked < ?) ORDER BY p.last_checked LIMIT ?",
                     (ago_ts(minutes=cfg['recheck_alive_min']), budget))
    for row in alive_due:
        picked.setdefault(row['id'], row)
    remaining = budget - len(picked)

    source_rows = rows('SELECT id, tested, ever_alive, pending FROM sources WHERE enabled = 1 AND pending > 0')
    trial = cfg['source_trial_size']
    allocation = {}
    for s in source_rows:
        if s['tested'] < trial:
            allocation[s['id']] = min(trial - s['tested'], s['pending'])
    trial_total = sum(allocation.values())
    if trial_total > remaining:   # more trials than budget: share it out evenly this round
        scale = remaining / trial_total
        allocation = {k: max(1, int(v * scale)) for k, v in allocation.items()}
    left = max(0, remaining - sum(allocation.values()))
    capacity = {s['id']: s['pending'] - allocation.get(s['id'], 0) for s in source_rows}
    # Proven sources are weighted by their yield; sources still in trial share leftovers at a
    # neutral prior so the budget is never left unused.
    weights = {s['id']: max(cfg['source_min_share'],
                            s['ever_alive'] / s['tested'] if s['tested'] >= trial else PRIOR_YIELD)
               for s in source_rows if capacity[s['id']] > 0}
    while left > 0 and weights:   # capacity left by small sources flows to the others
        total = sum(weights.values())
        handed = 0
        for sid, weight in list(weights.items()):
            share = min(capacity[sid], max(1, int(left * weight / total)))
            allocation[sid] = allocation.get(sid, 0) + share
            capacity[sid] -= share
            handed += share
            if capacity[sid] <= 0:
                del weights[sid]
        if handed == 0:
            break
        left -= handed

    budgets = {}
    for sid, count in sorted(allocation.items(), key=lambda kv: -kv[1]):
        count = min(count, budget - len(picked))
        if count <= 0:
            continue
        # Lists overlap heavily, so another source may already have picked this source's top
        # proxies; over-fetch and skip those so each source really gets its share. Free proxies
        # die within hours: what the source lists now comes first, then proxies on several
        # lists, then the most recently discovered.
        candidates = rows(f"SELECT {cols} FROM proxy_sources ps JOIN proxies p ON p.ip = ps.ip AND p.port = ps.port "
                          "WHERE ps.source_id = ? AND p.status = 'new' "
                          "ORDER BY ps.last_seen DESC, p.source_count DESC, p.id DESC LIMIT ?",
                          (sid, count + len(picked)))
        taken = 0
        for row in candidates:
            if taken >= count:
                break
            if row['id'] not in picked:
                picked[row['id']] = row
                taken += 1
        budgets[sid] = taken
    if cfg['recheck_dead_min'] > 0 and len(picked) < budget:
        # Wait recheck_dead_min after the first failure, doubling per failed re-test up to the cap.
        due = rows(f"SELECT {cols} FROM proxies p WHERE p.status = 'dead' AND p.successes > 0 AND "
                   "p.last_checked < datetime('now', '-' || min(? * (1 << min(max(p.fail_streak, 1) - 1, 12)), ?) "
                   "|| ' minutes') AND EXISTS (SELECT 1 FROM proxy_sources ps WHERE ps.ip = p.ip AND ps.port = p.port) "
                   "ORDER BY p.last_alive DESC LIMIT ?",
                   (cfg['recheck_dead_min'], cfg['recheck_dead_max_hours'] * 60, budget - len(picked)))
        for row in due:
            picked.setdefault(row['id'], row)
    with write() as conn:
        conn.execute('UPDATE sources SET last_budget = 0 WHERE last_budget != 0')
        conn.executemany('UPDATE sources SET last_budget = ? WHERE id = ?', [(n, sid) for sid, n in budgets.items()])
    return list(picked.values()), sum(row['status'] == 'alive' for row in picked.values())


def store_test_results(results, country_of):
    ts = now()
    ok_rows, bad_rows = [], []
    for r in results:
        if r.get('ok'):
            ok_rows.append((r['protocol'], 1 if r.get('https') else 0, r.get('anonymity'), r.get('latency_ms'),
                            (r.get('exit_ip') or '')[:45] or None, country_of(r['ip']), ts, ts,
                            r.get('https_latency_ms') if r.get('https') else None,
                            1 if r.get('intercepts') else 0, r['id']))
        else:
            bad_rows.append((ts, r['id']))
    with write() as conn:
        _count_source_yield(conn, results)
        conn.executemany("UPDATE proxies SET status = 'alive', protocol = ?, https = ?, anonymity = ?, latency_ms = ?, "
                         "exit_ip = ?, country = ?, last_checked = ?, last_alive = ?, checks = checks + 1, "
                         "successes = successes + 1, fail_streak = 0, https_latency_ms = ?, "
                         "intercepts = MAX(intercepts, ?) WHERE id = ?", ok_rows)
        # Dead proxies that once worked are re-tested with a backoff (see pick_for_test); the
        # rest are not picked again, and retention forgets them once no source lists them.
        conn.executemany("UPDATE proxies SET status = 'dead', last_checked = ?, checks = checks + 1, "
                         "fail_streak = fail_streak + 1 WHERE id = ?", bad_rows)
    return len(ok_rows), len(bad_rows)


def _count_source_yield(conn, results):
    """Add a batch to each listing source's running `tested` / `ever_alive` totals.

    A proxy counts once per source: when it is first tested, and when it first works. Runs
    before the batch's own updates, while the proxies still show their previous state.
    """
    ok = {r['id']: bool(r.get('ok')) for r in results}
    if not ok:
        return
    tested, alive = {}, {}
    ids = list(ok)
    for start in range(0, len(ids), 900):
        part = ids[start:start + 900]
        for sid, pid, status, successes in conn.execute(
                'SELECT ps.source_id, p.id, p.status, p.successes FROM proxies p '
                f'JOIN proxy_sources ps ON ps.ip = p.ip AND ps.port = p.port WHERE p.id IN ({marks(part)})', part):
            if status == 'new':
                tested[sid] = tested.get(sid, 0) + 1
            if ok[pid] and not successes:
                alive[sid] = alive.get(sid, 0) + 1
    conn.executemany('UPDATE sources SET tested = tested + ?, ever_alive = ever_alive + ? WHERE id = ?',
                     [(tested.get(sid, 0), alive.get(sid, 0), sid) for sid in set(tested) | set(alive)])


# ---------------------------------------------------------------- retention

def apply_retention(cfg, chunk=20000):
    """Forget what the sources no longer list. Returns counts of deleted rows.

    1. Source listings not seen for `retention_unlisted_days` are dropped.
    2. Dead proxies left with no listing are deleted; if a source lists one again later it comes
       back as new and gets one fresh test.
    3. Untested proxies no source listed in `retention_new_unlisted_hours` are deleted: free
       proxies that drop off every list are almost always gone.
    4. With `retention_dead_retest_days`, dead proxies older than that are deleted even if still
       listed, so they get one more test.
    Alive proxies are never deleted here; they become dead (and then go) when they stop working.
    Work is done in chunks so the write lock is never held for long.
    """
    out = {'listings': 0, 'dead': 0, 'untested': 0, 'dead_retest': 0}
    days = cfg['retention_unlisted_days']
    if days > 0:
        cutoff = ago_ts(days=days)
        while True:
            with write() as conn:
                deleted = _delete_listings(conn, cutoff, chunk)
            out['listings'] += deleted
            if deleted < chunk:
                break
        out['dead'] = _delete_where("p.status = 'dead' AND NOT EXISTS (SELECT 1 FROM proxy_sources ps "
                                    "WHERE ps.ip = p.ip AND ps.port = p.port)", (), chunk)
    hours = cfg['retention_new_unlisted_hours']
    if hours > 0:
        cutoff = ago_ts(hours=hours)
        out['untested'] = _delete_where("p.status = 'new' AND p.first_seen < ? AND NOT EXISTS (SELECT 1 FROM "
                                        "proxy_sources ps WHERE ps.ip = p.ip AND ps.port = p.port AND ps.last_seen >= ?)",
                                        (cutoff, cutoff), chunk)
    retest = cfg['retention_dead_retest_days']
    if retest > 0:
        out['dead_retest'] = _delete_where("p.status = 'dead' AND p.last_checked < ?", (ago_ts(days=retest),), chunk)
    return out


def _delete_listings(conn, cutoff, chunk):
    # proxy_sources has no rowid; delete by key, oldest first.
    keys = conn.execute('SELECT source_id, ip, port FROM proxy_sources WHERE last_seen < ? '
                        'ORDER BY last_seen LIMIT ?', (cutoff, chunk)).fetchall()
    conn.executemany('DELETE FROM proxy_sources WHERE source_id = ? AND ip = ? AND port = ?', keys)
    return len(keys)


def _delete_where(condition, params, chunk):
    """Delete matching proxies, walking the table by id so each transaction stays small."""
    deleted, last_id = 0, 0
    top = scalar('SELECT MAX(id) FROM proxies') or 0
    while last_id < top:
        upper = last_id + chunk * 5
        with write() as conn:
            deleted += conn.execute(f'DELETE FROM proxies WHERE id IN (SELECT p.id FROM proxies p '
                                    f'WHERE p.id > ? AND p.id <= ? AND {condition})',
                                    (last_id, upper, *params)).rowcount
        last_id = upper
    return deleted


# ---------------------------------------------------------------- reading

def split(value, allowed, name):
    if not value:
        return []
    items = [v.strip().lower() for v in str(value).split(',') if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise ValueError(f'unknown {name}: {", ".join(bad)}; allowed: {", ".join(sorted(allowed))}')
    return items


def query_proxies(protocol=None, anonymity=None, country=None, max_latency=None, checked_within=None,
                  limit=100, sort='latency', trusted=False, connect=False, max_https_latency=None):
    """Working proxies. A proxy reported dead waits for its re-test (``last_checked`` NULL).

    ``connect``: HTTPS works through it (CONNECT for HTTP proxies, a TLS tunnel for SOCKS).
    ``trusted``: that, with valid certificates, and it never forged one.
    """
    where, params = ["status = 'alive'", 'last_checked IS NOT NULL'], []
    if connect or trusted:
        where.append('https = 1')
    if trusted:
        where.append('intercepts = 0')
    if max_https_latency:
        where.append('https_latency_ms <= ?')
        params.append(int(max_https_latency))
    protocols = split(protocol, PROTOCOLS, 'protocol')
    if protocols:
        parts = []
        plain = [p for p in protocols if p != 'https']
        if plain:
            parts.append(f'protocol IN ({marks(plain)})')
            params += plain
        if 'https' in protocols:
            parts.append("(protocol = 'http' AND https = 1)")
        where.append('(' + ' OR '.join(parts) + ')')
    levels = split(anonymity, ANONYMITY, 'anonymity')
    if levels:
        where.append(f'anonymity IN ({marks(levels)})')
        params += levels
    countries = [c.strip().upper() for c in str(country or '').split(',') if c.strip()]
    if countries:
        where.append(f'country IN ({marks(countries)})')
        params += countries
    if max_latency:
        where.append('latency_ms <= ?')
        params.append(int(max_latency))
    if checked_within:
        where.append('last_checked >= ?')
        params.append(ago_ts(minutes=int(checked_within)))
    order = {'latency': 'latency_ms ASC', 'checked': 'last_checked DESC',
             'uptime': 'successes DESC, latency_ms ASC',
             # Reported working lately, then reliable (4 in 5 checks pass), then fast over HTTPS.
             'score': 'COALESCE(last_worked >= ?, 0) DESC, successes * 5 >= checks * 4 DESC, '
                      'COALESCE(https_latency_ms, 999999) ASC, latency_ms ASC'}.get(sort)
    if order is None:
        raise ValueError('sort must be latency, checked, uptime or score')
    order_params = (ago_ts(hours=WORKED_RECENT_HOURS),) if sort == 'score' else ()
    result = rows('SELECT ip, port, protocol, https, anonymity, latency_ms, https_latency_ms, intercepts, country, '
                  'exit_ip, last_checked, checks, successes, worked, last_worked '
                  f'FROM proxies WHERE {" AND ".join(where)} ORDER BY {order} LIMIT ?',
                  (*params, *order_params, max(1, min(int(limit), 50000))))
    for row in result:
        row['https'] = bool(row['https'])
        row['intercepts'] = bool(row['intercepts'])
        row['last_worked'] = row['last_worked'].replace(' ', 'T') + 'Z' if row['last_worked'] else None
        row['url'] = f"{row['protocol']}://{row['ip']}:{row['port']}"
        row['last_checked'] = row['last_checked'].replace(' ', 'T') + 'Z' if row['last_checked'] else None
    return result


def report_proxy(proxy, reason):
    """What a program learned using a proxy. ``proxy``: ``scheme://host:port`` or ``host:port``.

    ``worked``: it carried a real request (counts toward ``sort=score``). ``dead``: it failed
    for the program; it is not handed out again until its re-test, which comes first in the
    next round (one report alone never marks it dead). ``intercepts``: it showed a forged
    certificate; it is never trusted again. Returns the proxy's id, or None if it is unknown.
    """
    if reason not in REPORT_REASONS:
        raise ValueError(f'reason must be one of {", ".join(REPORT_REASONS)}')
    text = str(proxy or '').strip()
    host_port = text.split('://', 1)[-1].rsplit('@', 1)[-1].strip('/')
    host, _, port = host_port.rpartition(':')
    host = host.strip('[]')
    if not host or not port.isdigit():
        raise ValueError('proxy must look like scheme://host:port or host:port')
    row = db().execute('SELECT id FROM proxies WHERE ip = ? AND port = ?', (host, int(port))).fetchone()
    if row is None:
        return None
    statement = {
        'worked': ('UPDATE proxies SET worked = worked + 1, last_worked = ? WHERE id = ?', (now(), row['id'])),
        'dead': ("UPDATE proxies SET last_checked = NULL WHERE status = 'alive' AND id = ?", (row['id'],)),
        'intercepts': ('UPDATE proxies SET intercepts = 1, https = 0 WHERE id = ?', (row['id'],)),
    }[reason]
    with write() as conn:
        conn.execute(*statement)
    return row['id']


def stats():
    by_status = {r['status']: r['n'] for r in rows('SELECT status, COUNT(*) AS n FROM proxies GROUP BY status')}
    alive = {'http': 0, 'https': 0, 'socks4': 0, 'socks5': 0, 'elite': 0, 'anonymous': 0, 'transparent': 0}
    for row in rows("SELECT protocol, anonymity, https, COUNT(*) AS n FROM proxies WHERE status = 'alive' "
                    'GROUP BY protocol, anonymity, https'):
        for key in (row['protocol'], row['anonymity']):
            if key in alive:
                alive[key] += row['n']
        if row['https']:
            alive['https'] += row['n']
    source = rows('SELECT COUNT(*) AS total, COALESCE(SUM(enabled), 0) AS enabled FROM sources')[0]
    return {
        'proxies': {'alive': by_status.get('alive', 0), 'new': by_status.get('new', 0), 'dead': by_status.get('dead', 0)},
        'alive': alive,
        'fast': scalar("SELECT COUNT(*) FROM proxies WHERE status = 'alive' AND latency_ms <= 2000"),
        'trusted': scalar("SELECT COUNT(*) FROM proxies WHERE status = 'alive' AND https = 1 AND intercepts = 0"),
        'intercepting': scalar('SELECT COUNT(*) FROM proxies WHERE intercepts = 1'),
        'sources': source,
    }


def last_runs():
    out = {}
    for kind in ('scrape', 'test'):
        found = rows('SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT 1', (kind,))
        run = found[0] if found else None
        if run and run['summary']:
            run['summary'] = json.loads(run['summary'])
        out[kind] = run
    return out


def recent_events(limit=150):
    return rows('SELECT id, ts, level, message FROM events ORDER BY id DESC LIMIT ?', (limit,))
