# Queue plugin

Puts more work on Scraperack than it runs at once. A queue holds jobs and feeds them to
their runner, a plugin that runs work (the [Python plugin](python-plugin.md) today), as long
as the queue has room. Jobs start as soon as there is room, never at a chosen time: the
queue does no scheduling.

```sh
tar -czf queue-plugin.tar.gz -C queue/plugin .
```

## How it fits

| Piece | Its job |
|---|---|
| Core | Runs microVMs now |
| Runner plugins (Python, later others) | Run code of one language, through the [runner contract](#runner-contract) |
| Queue plugin | Holds the backlog and decides how much runs: concurrency, start rate, attempts |
| Language SDKs | Build a call, then run it now or put it in a queue |

The queue never looks inside a job. A job is `{runner, input}`: the runner's plugin name and
what that runner needs to start it. The same queue can hold jobs of different runners.

Jobs keep metadata only: status, reason, attempts with their run ids, and timings. A job's
input is kept until the job ends, then dropped. What a job produces (rows, files, uploads)
goes wherever the job sends it, never back to the queue. Logs stay with the runner (for
Python, on the Executions tab, for the controller's task retention).

## Big crawls and long work

A run has a time limit (the Python environment's `timeout_secs`). For a big crawl or long
work, don't start one long run: split it into many small jobs, for example one per URL or per
page of results, and put them in a queue. The queue keeps `concurrency` of them running,
retries the ones that fail, and carries on after restarts. Jobs run at least once, so make
them safe to repeat.

Jobs keep no results. Write them to your own storage from the job, or read them from the
job's runs: `GET /jobs/{id}` lists its attempts with their run ids, and the runner keeps each
run's result (for Python, `GET /plugins/python/api/executions/{run id}`) as long as the
controller keeps finished tasks. Keep such results small.

## From Python

```python
from scraperack_python import Python


def scrape(url):
    import httpx
    ...  # write the result to your own storage


with Python("http://controller:7070", "sr_...") as py:
    py.queue("upwork").configure(concurrency=20, rate_per_minute=300)
    call = py.bind("scrapers", scrape, "https://example.com")   # built, not started
    call.put("upwork")                                           # or call.run() to start it now
    py.queue("upwork").put_many(py.bind("scrapers", scrape, url) for url in urls)
```

See [the Python SDK](python-sdk.md#queues).

## Queues

| Setting | Default | Meaning |
|---|---|---|
| `concurrency` | 10 | Most jobs running at once |
| `rate_per_minute` | none | Most job starts a minute (bursts of at most one second's worth) |
| `max_attempts` | 3 | Tries per job, the first included |
| `retry_code_errors` | false | Also retry when the job's own code fails or times out |
| `timeout_secs` | none | Longest one try may run; the run is stopped. The runner's own limit (the environment's timeout) still applies |
| `max_queued` | 1,000,000 | Most waiting jobs; more are refused with 429 and `retry_after` |
| `paused` | false | Start nothing new; running jobs finish |

The dispatcher looks for room twice a second. Queues take turns going first, and within a
queue jobs start in order. The core still caps each environment at its `max_instances`, so
several queues on one environment can't overload it.

## Jobs

`queued` → `running` → `done` | `failed` | `cancelled`

| Failure | Retried |
|---|---|
| `infrastructure`: microVM or node lost, runner unreachable for 10 minutes, run lost | Always, up to `max_attempts` |
| `code`: the job's code raised | Only with `retry_code_errors` |
| `timeout`: the runner's limit or `timeout_secs` | Only with `retry_code_errors` |
| `rejected`: the runner refused the input (for example an unknown environment) | Never |
| `stopped`: the run was stopped outside the queue | Never |

A retried job goes to the back of its queue, right away. The `reason` is the failure and the
last line of its error, for example `code: ValueError: boom`.

Delivery is at least once. Each attempt is submitted with the key `<job>/<attempt>`, and a
runner returns the same run for the same key, so a queue that restarts mid-submit doesn't
start the attempt twice. A job can still run twice if the runner crashes between starting
the run and storing its key, so make jobs safe to repeat.

Finished jobs are forgotten after `retention_hours` (setting, default 72).

## API

Served at `http://<controller>:7070/plugins/queue/api/`:

| Endpoint | |
|---|---|
| `GET /queues` | Queues with their settings and counts |
| `PUT /queues/{name}` | Create, or change some settings |
| `GET /queues/{name}` | One queue |
| `DELETE /queues/{name}` | Drop waiting jobs, stop running ones, delete the queue |
| `POST /queues/{name}/jobs` | `{runner, input}` → `{id}`, or `{jobs: [...]}` (at most 1000) → `{ids}` |
| `GET /queues/{name}/jobs?status=&limit=` | Newest first |
| `POST /queues/{name}/cancel` | Cancel waiting jobs; `{running: true}` stops running ones too |
| `GET /jobs/{id}` | A job with `attempts_detail` |
| `POST /jobs/{id}/cancel` | Cancel; a running job is stopped |
| `GET/PUT /settings` | `retention_hours` |

Counts per queue: `queued`, `running`, `done`, `failed`, `cancelled`, and
`finished_last_minute`. The plugin also reports running and waiting jobs on the core's
capacity page.

A job's `input` is at most 1 MiB. It is stored in SQLite on the controller until the job
ends, secrets included.

## Runner contract

A plugin becomes a runner by serving these endpoints under its `/api/`. The queue calls
them through the core (`/plugins/<runner>/api/...`).

| Endpoint | |
|---|---|
| `POST /runs` | `{key, input}` → a run. The same `key` must return the same run |
| `GET /runs?ids=a,b` | Runs in that order (at most 500); an unknown id has status `unknown` |
| `GET /runs/{id}` | One run |
| `POST /runs/{id}/stop` | Stop it |

A run is `{id, status, failure, error, started_at_ms, finished_at_ms}`:

- `status`: `queued`, `running`, `succeeded`, `failed`, `timed_out` or `unknown`.
- `failure` (when failed): `code`, `timeout`, `stopped` or `infrastructure`.
- `error`: the end of the error text.

`POST /runs` answers 4xx for input it will never run, which fails the job, and 5xx or 429
when it can't take work right now, which makes the queue try again.

Data: `queue.db` (SQLite) in the plugin's data directory.
