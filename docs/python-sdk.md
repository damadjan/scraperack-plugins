# Python SDK

`scraperack-python` calls Python functions in the [Python plugin](python-plugin.md)'s warm
environments. It
builds on the core client [`scraperack`](https://github.com/damadjan/scraperack/blob/main/docs/sdk.md),
which is installed with it and available as `py.cluster`.

```sh
pip install scraperack-python
```

The calling program must use the same Python minor version as the environment
([why](python-plugin.md#python-versions)).

## Run a function

```python
from scraperack_python import Python, RemoteError


def fetch(url):
    import httpx
    from scraperack_runtime import secret, variable

    print("fetching", url, variable("MODE"))
    return httpx.get(url, headers={"Authorization": secret("TOKEN")}).status_code


with Python("http://controller:7070", api_key="sr_...") as py:
    # Run and wait in one step; raises RemoteError with the traceback on failure.
    print(py.call("scrapers", fetch, "https://example.com",
                  variables={"MODE": "fast"}, secrets={"TOKEN": "..."}))

    # Or start it and collect later.
    execution = py.run("scrapers", fetch, ("https://example.com",))
    try:
        print(py.result(execution["id"]))
    except RemoteError as error:
        print("failed:", error)
    print(py.logs(execution["id"])["stdout"])
```

`api_key` is a Scraperack API key that opens the python and queue plugins (and `core`, for
`.cluster`); without it the client reads `SCRAPERACK_API_KEY`.

`run` and `call` take:

| Argument | Meaning |
|---|---|
| `environment` | Environment name or ID |
| `function`, `params` | Called as `function(*params, **kwargs)`; `call` takes them directly |
| `kwargs` | `run` only: keyword arguments for the function |
| `variables`, `secrets` | Read inside with `scraperack_runtime.variable()` and `secret()` |
| `working_dir` | A local folder uploaded once (content-addressed), importable and the working directory during the call |
| `queue` | `False` fails at once instead of waiting when the environment is at `max_instances` |

`call(environment, function, *args, **kwargs)` passes every keyword to the function except
its own options `queue`, `variables`, `secrets` and `working_dir`. For a function argument
with one of those names, use `run(..., kwargs={...})`.

Return values come back as JSON when possible, otherwise pickled.

## Queues

With the [queue plugin](queue-plugin.md), a call can wait in a queue instead of starting
now. `bind` builds the call without starting it; then `run()` starts it now, or `put()`
hands it to a queue, which starts it when the queue has room.

```python
with Python("http://controller:7070") as py:
    upwork = py.queue("upwork")
    upwork.configure(concurrency=20, rate_per_minute=300, max_attempts=3)

    call = py.bind("scrapers", fetch, "https://example.com", secrets={"TOKEN": "..."})
    call.run()                      # start now: an execution
    job_id = call.put("upwork")     # or queue it: a job id

    ids = upwork.put_many(py.bind("scrapers", fetch, url) for url in urls)   # 500 per request
    print(upwork.info()["counts"])  # queued, running, done, failed, cancelled
    print(upwork.job(job_id)["reason"])
```

`bind(environment, function, *args, **kwargs)` takes the same options as `call`
(`variables`, `secrets`, `working_dir`); every other keyword goes to the function. A working
directory is uploaded when the call is bound.

Queued jobs return nothing: the function sends its results where they belong (a database,
files, an upload). A job only has a status and a reason.

| `py.queue(name)` | |
|---|---|
| `configure(**settings)` | Create the queue or change settings: `concurrency`, `rate_per_minute`, `max_attempts`, `retry_code_errors`, `timeout_secs`, `max_queued`, `paused` |
| `info()` | Settings and counts |
| `put(call)`, `put_many(calls)` | Add jobs; a full queue raises `scraperack.APIError` with status 429 |
| `jobs(status=None, limit=100)`, `job(id)` | Job metadata; `job` adds the attempts and their execution ids |
| `cancel(id)`, `cancel_waiting(running=False)` | Cancel one job, or every waiting one |
| `delete()` | Delete the queue |

`put` also takes a raw job `{"runner": ..., "input": {...}}`, so a Python program can queue
work for other runners. `py.queues()` lists every queue. `AsyncPython` has the same methods;
`await py.bind(...)` and `await call.put(...)`.

## Executions

```python
for execution in py.executions(environment="scrapers", limit=20):
    print(execution["function"], execution["status"], execution["duration_ms"])

details = py.execution(execution_id)   # adds "logs" and the encoded "result"
py.wait(execution_id)                  # block until finished
```

## Environments

```python
image = next(i for i in py.cluster.images() if i["name"] == "python-runtime")
py.create_environment("scrapers", image["id"], packages=["httpx==0.28.1"],
                      preload=["httpx"], cpu=0.5, memory_mib=512, warm=2, max_instances=20)
py.update_environment("scrapers", warm=4)        # packages are rebuilt only if they change
py.restart_environment("scrapers")
py.environments()
py.delete_environment("scrapers")
```

Settings use the SDK's short names: `cpu` (cores), `warm`, `timeout`, `idle_timeout`, and
otherwise the plugin's field names (`memory_mib`, `max_instances`, `network`, `reuse`, …).

## Async

`AsyncPython` has the same methods as coroutines:

```python
import asyncio
from scraperack_python import AsyncPython

async def main():
    async with AsyncPython("http://controller:7070") as py:
        results = await asyncio.gather(*(py.call("scrapers", fetch, url) for url in urls))

asyncio.run(main())
```

## Sharing a core client

Pass an existing `scraperack.Cluster` (or `AsyncCluster`) to reuse its connection:

```python
from scraperack import Cluster
from scraperack_python import Python

cluster = Cluster("http://controller:7070")
py = Python(cluster)
```

Runnable examples, one concept per file: [python/examples](../python/examples/).
