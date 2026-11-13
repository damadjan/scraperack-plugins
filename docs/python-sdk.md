# Python SDK

`scraperack-python` runs Python functions through the [Python plugin](python-plugin.md). It
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


with Python("http://controller:7070") as py:
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

`run` and `call` take:

| Argument | Meaning |
|---|---|
| `environment` | Environment name or ID |
| `function`, `params` | Called as `function(*params)`; `call` takes the params directly |
| `variables`, `secrets` | Read inside with `scraperack_runtime.variable()` and `secret()` |
| `working_dir` | A local folder uploaded once (content-addressed), importable and the working directory during the call |
| `queue` | `False` fails at once instead of waiting when the environment is at `max_instances` |

Return values come back as JSON when possible, otherwise pickled.

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
