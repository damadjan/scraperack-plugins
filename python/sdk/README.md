# scraperack-python

Run Python functions on a self-hosted Scraperack cluster. This is the SDK of the
[Python plugin](..): it manages environments (package sets with warm microVMs) and runs
functions in them as executions. It builds on [`scraperack`](https://github.com/damadjan/scraperack/tree/main/sdk),
the core client, which stays available as `py.cluster`.

> Scraperack is alpha software. Use it on trusted infrastructure and expect breaking changes.

```sh
pip install scraperack-python
```

## Run a function

```python
from scraperack_python import Python


def add(a, b):
    from scraperack_runtime import secret, variable

    print(variable("LOG_LEVEL", "INFO"))
    database_url = secret("DATABASE_URL")
    return a + b


with Python("http://controller:7070") as py:
    execution = py.run(
        "scrapers",                     # environment name or ID
        add,
        (2, 3),
        working_dir="./project",
        variables={"LOG_LEVEL": "DEBUG"},
        secrets={"DATABASE_URL": "postgresql://..."},
    )
    print(py.result(execution["id"]))   # 5, or RemoteError with the traceback
    print(py.logs(execution["id"])["stdout"])

    print(py.call("scrapers", add, 2, 3))   # run and wait in one step
```

`working_dir` uploads project modules and files once, content-addressed. It is made
importable and becomes the current directory for the execution. Third-party packages
belong in the environment.

## Environments

```python
image = next(i for i in py.cluster.images() if i["name"] == "python-runtime")
py.create_environment("scrapers", image["id"], packages=["httpx==0.28.1"], warm=2)
py.update_environment("scrapers", warm=4)
py.environments()
py.delete_environment("scrapers")
```

## Executions

```python
for execution in py.executions(environment="scrapers", limit=20):
    print(execution["function"], execution["status"], execution["duration_ms"])
details = py.execution(execution_id)   # adds logs and the encoded result
```

`AsyncPython` has the same methods for `asyncio`.
