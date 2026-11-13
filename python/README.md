# Python plugin

Runs Python functions in ScrapeRack microVMs. Each environment combines an imported image,
a pinned set of pip packages, resource limits, and scaling rules. Packages are installed
once into a virtualenv disk that every node caches; the plugin needs no dependencies of
its own.

The plugin page has two tabs: **Environments** to manage them, and **Executions**
to see every function sent to them, with its status, duration, result, stdout, stderr,
and errors.

```
plugin/         the installable plugin (pack this folder)
sdk/            `scraperack-python`, the SDK for this plugin (builds on the core `scraperack` client)
verification/   end-to-end checks and benchmarks
tests/          plugin unit tests
```

## Install

```sh
tar -czf python-plugin.tar.gz -C python/plugin .
```

Then **Plugins → Install plugin** in the ScrapeRack GUI. The plugin needs Docker on the
controller to build package disks, which the ScrapeRack installer already sets up.
Images used for environments must be imported from Docker and contain `/usr/bin/python3`.

Environments created before plugins existed are adopted automatically: they keep their
IDs, names, and warm microVMs.

## Use

```sh
pip install scraperack-python
```

```python
from scraperack_python import Python

py = Python("http://controller:7070")
image = next(i for i in py.cluster.images() if i["name"] == "python-runtime")
py.create_environment("scrapers", image["id"], packages=["httpx==0.28.1"], warm=2)

def fetch(url):
    import httpx
    return httpx.get(url).status_code

execution = py.run("scrapers", fetch, ("https://example.com",))
print(py.result(execution["id"]))
print(py.call("scrapers", fetch, "https://example.com"))  # run and wait
```

See [`sdk/README.md`](sdk/README.md) for executions, async use, and working directories.

Code can read invocation-scoped values with `scraperack_runtime.variable("NAME")` and
`scraperack_runtime.secret("NAME")`.

## API

Served by the plugin at `http://<controller>:7070/plugins/python/api/`:

| Endpoint | |
|---|---|
| `GET /environments` | List environments and whether their disks reached every node |
| `POST /environments` | Create; blocks until packages are built |
| `GET/PUT/DELETE /environments/{id or name}` | Read, update (rebuilds packages if they changed), delete |
| `POST /environments/{id or name}/restart` | Replace the environment's microVMs |
| `GET /executions?environment=&limit=` | Newest executions first, with the function name |
| `POST /executions` | Run code: `{"environment", "payload", "queue", "variables", "secrets"}` |
| `GET /executions/{id}` | One execution with logs and result |
| `GET /executions/{id}/wait`, `/result`, `/logs`, `/payload` | Same as the core task endpoints |

Each execution is a core task on the environment's pool (`/v1/tasks`). The core stores
and schedules it; this plugin decides what it means and shows only its own.

## Develop

```sh
python -m unittest discover -s python/tests
pip install ../scraperack/sdk ./python/sdk   # the core client, then this SDK
(cd python/sdk && python -m unittest discover -s tests)
```
