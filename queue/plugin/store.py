"""SQLite storage for the queue plugin: queues, jobs and their attempts.

Jobs hold metadata only. A job's input (what its runner needs to start it) is kept until
the job ends, then dropped; what the job produced never comes back here.
"""
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS queues (
    name TEXT PRIMARY KEY,
    concurrency INTEGER NOT NULL,
    rate_per_minute INTEGER,            -- NULL: no limit
    max_attempts INTEGER NOT NULL,
    retry_code_errors INTEGER NOT NULL,
    timeout_secs INTEGER,               -- NULL: only the runner's own limit
    max_queued INTEGER NOT NULL,
    paused INTEGER NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL,
    runner TEXT NOT NULL,
    input TEXT,                         -- dropped when the job ends
    status TEXT NOT NULL,               -- queued, running, done, failed, cancelled
    reason TEXT,
    turn INTEGER NOT NULL,              -- place in line; a retried job goes to the back
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,                        -- the current attempt's run; NULL while it is submitted
    created_ms INTEGER NOT NULL,
    started_ms INTEGER,
    finished_ms INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_line ON jobs (queue, status, turn);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (queue, created_ms);
CREATE INDEX IF NOT EXISTS jobs_finished ON jobs (finished_ms) WHERE finished_ms IS NOT NULL;
CREATE TABLE IF NOT EXISTS attempts (
    job TEXT NOT NULL,
    number INTEGER NOT NULL,
    run_id TEXT,
    started_ms INTEGER NOT NULL,
    finished_ms INTEGER,
    outcome TEXT,                       -- done, failed, cancelled
    reason TEXT,
    PRIMARY KEY (job, number)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

QUEUE_DEFAULTS = {"concurrency": 10, "rate_per_minute": None, "max_attempts": 3,
                  "retry_code_errors": False, "timeout_secs": None, "max_queued": 1_000_000,
                  "paused": False}
SETTING_DEFAULTS = {"retention_hours": 72}
STATUSES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "running")


def now_ms():
    return int(time.time() * 1000)


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.next_turn = (self.db.execute("SELECT max(turn) FROM jobs").fetchone()[0] or 0) + 1

    def transaction(self):
        return Transaction(self)

    def _turns(self, count):
        first = self.next_turn
        self.next_turn += count
        return range(first, first + count)

    # --- Queues ---------------------------------------------------------------

    def queues(self):
        with self.lock:
            return [queue_row(row) for row in
                    self.db.execute("SELECT * FROM queues ORDER BY name")]

    def queue(self, name):
        with self.lock:
            row = self.db.execute("SELECT * FROM queues WHERE name = ?", (name,)).fetchone()
        return queue_row(row) if row else None

    def save_queue(self, queue):
        with self.lock:
            self.db.execute(
                "INSERT INTO queues (name, concurrency, rate_per_minute, max_attempts, "
                "retry_code_errors, timeout_secs, max_queued, paused, created_ms) "
                "VALUES (:name, :concurrency, :rate_per_minute, :max_attempts, "
                ":retry_code_errors, :timeout_secs, :max_queued, :paused, :created_ms) "
                "ON CONFLICT (name) DO UPDATE SET concurrency = excluded.concurrency, "
                "rate_per_minute = excluded.rate_per_minute, "
                "max_attempts = excluded.max_attempts, "
                "retry_code_errors = excluded.retry_code_errors, "
                "timeout_secs = excluded.timeout_secs, max_queued = excluded.max_queued, "
                "paused = excluded.paused",
                {**queue, "created_ms": queue.get("created_ms") or now_ms()})
        return self.queue(queue["name"])

    def delete_queue(self, name):
        with self.transaction() as db:
            db.execute("DELETE FROM attempts WHERE job IN (SELECT id FROM jobs WHERE queue = ?)",
                       (name,))
            db.execute("DELETE FROM jobs WHERE queue = ?", (name,))
            db.execute("DELETE FROM queues WHERE name = ?", (name,))

    def counts(self):
        """{queue: {status: count}} for every queue, plus jobs finished in the last minute."""
        with self.lock:
            counts = {queue["name"]: {**dict.fromkeys(STATUSES, 0), "finished_last_minute": 0}
                      for queue in self.queues()}
            for row in self.db.execute(
                    "SELECT queue, status, count(*) FROM jobs GROUP BY queue, status"):
                counts.setdefault(row[0], {**dict.fromkeys(STATUSES, 0),
                                           "finished_last_minute": 0})[row[1]] = row[2]
            for row in self.db.execute(
                    "SELECT queue, count(*) FROM jobs WHERE finished_ms > ? GROUP BY queue",
                    (now_ms() - 60_000,)):
                if row[0] in counts:
                    counts[row[0]]["finished_last_minute"] = row[1]
        return counts

    def count(self, queue, status):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM jobs WHERE queue = ? AND status = ?",
                                   (queue, status)).fetchone()[0]

    # --- Jobs -------------------------------------------------------------------

    def add_jobs(self, queue, jobs):
        """Append jobs ({runner, input}) to the back of a queue; returns their ids."""
        created = now_ms()
        with self.transaction() as db:
            ids = [str(uuid.uuid4()) for _ in jobs]
            db.executemany(
                "INSERT INTO jobs (id, queue, runner, input, status, turn, created_ms) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                [(job_id, queue, job["runner"], json.dumps(job["input"]), turn, created)
                 for job_id, job, turn in zip(ids, jobs, self._turns(len(jobs)))])
        return ids

    def job(self, job_id, attempts=False):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            view = job_row(row)
            if attempts:
                view["attempts_detail"] = [dict(attempt) for attempt in self.db.execute(
                    "SELECT number, run_id, started_ms, finished_ms, outcome, reason "
                    "FROM attempts WHERE job = ? ORDER BY number", (job_id,))]
        return view

    def jobs(self, queue, status=None, limit=100):
        with self.lock:
            if status:
                rows = self.db.execute(
                    "SELECT * FROM jobs WHERE queue = ? AND status = ? ORDER BY turn DESC "
                    "LIMIT ?", (queue, status, limit))
            else:
                rows = self.db.execute(
                    "SELECT * FROM jobs WHERE queue = ? ORDER BY created_ms DESC, turn DESC "
                    "LIMIT ?", (queue, limit))
            return [job_row(row) for row in rows]

    def claim(self, queue, count):
        """Move the next `count` queued jobs of a queue to running, opening an attempt each."""
        started = now_ms()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id, runner, input, attempts FROM jobs "
                "WHERE queue = ? AND status = 'queued' ORDER BY turn LIMIT ?",
                (queue, count)).fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                           "run_id = NULL, started_ms = ? WHERE id = ?", (started, row["id"]))
                db.execute("INSERT OR REPLACE INTO attempts (job, number, started_ms) "
                           "VALUES (?, ?, ?)", (row["id"], row["attempts"] + 1, started))
        return [{"id": row["id"], "queue": queue, "runner": row["runner"],
                 "input": json.loads(row["input"]), "attempt": row["attempts"] + 1}
                for row in rows]

    def running(self):
        """Every running job, submitted or not (without its input)."""
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT id, queue, runner, attempts AS attempt, run_id, started_ms "
                "FROM jobs WHERE status = 'running'")]

    def job_input(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT input FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def set_run(self, job_id, attempt, run_id):
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE jobs SET run_id = ? WHERE id = ? AND attempts = ? AND status = 'running'",
                (run_id, job_id, attempt)).rowcount
            db.execute("UPDATE attempts SET run_id = ? WHERE job = ? AND number = ?",
                       (run_id, job_id, attempt))
        return bool(changed)

    def finish(self, job_id, attempt, status, reason=None):
        """End a running attempt: the job is done, failed or cancelled for good."""
        finished = now_ms()
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE jobs SET status = ?, reason = ?, input = NULL, finished_ms = ? "
                "WHERE id = ? AND attempts = ? AND status = 'running'",
                (status, reason, finished, job_id, attempt)).rowcount
            if changed:
                close_attempt(db, job_id, attempt, status, reason, finished)
        return bool(changed)

    def retry(self, job_id, attempt, reason):
        """End a failed attempt and put the job at the back of its queue."""
        with self.transaction() as db:
            changed = db.execute(
                "UPDATE jobs SET status = 'queued', reason = ?, run_id = NULL, turn = ? "
                "WHERE id = ? AND attempts = ? AND status = 'running'",
                (reason, self._turns(1)[0], job_id, attempt)).rowcount
            if changed:
                close_attempt(db, job_id, attempt, "failed", reason, now_ms())
        return bool(changed)

    def cancel(self, job_ids):
        """Cancel active jobs; returns how many, and the started runs as (job, runner, run_id)."""
        finished = now_ms()
        cancelled, stopped = 0, []
        with self.transaction() as db:
            for job_id in job_ids:
                row = db.execute("SELECT status, runner, run_id, attempts FROM jobs WHERE id = ?",
                                 (job_id,)).fetchone()
                if row is None or row["status"] not in ACTIVE:
                    continue
                db.execute("UPDATE jobs SET status = 'cancelled', reason = 'cancelled', "
                           "input = NULL, finished_ms = ? WHERE id = ?", (finished, job_id))
                cancelled += 1
                if row["status"] == "running":
                    close_attempt(db, job_id, row["attempts"], "cancelled", "cancelled", finished)
                    if row["run_id"]:
                        stopped.append((job_id, row["runner"], row["run_id"]))
        return cancelled, stopped

    def cancel_queued(self, queue):
        with self.transaction() as db:
            return db.execute(
                "UPDATE jobs SET status = 'cancelled', reason = 'cancelled', input = NULL, "
                "finished_ms = ? WHERE queue = ? AND status = 'queued'",
                (now_ms(), queue)).rowcount

    def running_ids(self, queue):
        with self.lock:
            return [row[0] for row in self.db.execute(
                "SELECT id FROM jobs WHERE queue = ? AND status = 'running'", (queue,))]

    def prune(self, retention_hours):
        """Forget jobs that ended longer ago than the retention, in small batches."""
        cutoff = now_ms() - retention_hours * 3600 * 1000
        removed = 0
        while True:
            with self.transaction() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM jobs WHERE finished_ms < ? LIMIT 5000", (cutoff,))]
                if not ids:
                    return removed
                marks = ",".join("?" * len(ids))
                db.execute(f"DELETE FROM attempts WHERE job IN ({marks})", ids)
                db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
            removed += len(ids)

    # --- Settings ---------------------------------------------------------------

    def settings(self):
        with self.lock:
            stored = {row[0]: json.loads(row[1])
                      for row in self.db.execute("SELECT key, value FROM settings")}
        return {**SETTING_DEFAULTS, **{key: value for key, value in stored.items()
                                       if key in SETTING_DEFAULTS}}

    def save_settings(self, settings):
        with self.transaction() as db:
            for key, value in settings.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                           (key, json.dumps(value)))
        return self.settings()


class Transaction:
    """The store's lock plus one SQLite transaction."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.db.execute("BEGIN IMMEDIATE")
        return self.store.db

    def __exit__(self, kind, *_):
        try:
            self.store.db.execute("ROLLBACK" if kind else "COMMIT")
        finally:
            self.store.lock.release()


def close_attempt(db, job_id, attempt, outcome, reason, finished):
    db.execute("UPDATE attempts SET finished_ms = ?, outcome = ?, reason = ? "
               "WHERE job = ? AND number = ?", (finished, outcome, reason, job_id, attempt))


def queue_row(row):
    queue = dict(row)
    queue["retry_code_errors"] = bool(queue["retry_code_errors"])
    queue["paused"] = bool(queue["paused"])
    return queue


def job_row(row):
    job = dict(row)
    job.pop("input", None)
    job.pop("turn", None)
    return job
