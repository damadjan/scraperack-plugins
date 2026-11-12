# Python environments plugin

Runs Python functions in ScrapeRack microVMs. Each environment combines an imported image,
a pinned set of pip packages, resource limits, and scaling rules. Packages are installed
once into a virtualenv disk that every node caches; the plugin needs no dependencies of
its own.

```
plugin/         the installable plugin (pack this folder)
sdk/            the `scraperack` Python client
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

```python
from scraperack import Cluster

cluster = Cluster("http://controller:7070")
image = next(i for i in cluster.images() if i["name"] == "python-runtime")
cluster.create_environment("scrapers", image["id"], packages=["httpx==0.28.1"], warm=2)

def fetch(url):
    import httpx
    return httpx.get(url).status_code

execution = cluster.run("scrapers", fetch, ("https://example.com",))
print(cluster.result(execution["id"]))
```

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

Executions go straight to the core: `POST /v1/executions` with `"environment": "<id or name>"`.

## Develop

```sh
python -m unittest discover -s python/tests
(cd python/sdk && python -m unittest discover -s tests)
```
