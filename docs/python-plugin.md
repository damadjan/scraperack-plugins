# Python plugin

Runs Python functions in Scraperack microVMs. Install it from
[`python/plugin`](../python/plugin) ([how](installing-plugins.md)) and call it with the
[Python SDK](python-sdk.md). It needs Docker on the controller to build package disks,
which the Scraperack installer sets up, and has no dependencies of its own.

## Environments

An environment is an imported image, a pinned set of pip packages, resource limits, and
scaling rules. Packages are installed once into a virtualenv, stored as a read-only disk
that every node caches. Each environment is one Scraperack pool; its name and ID both work
wherever an environment is expected.

| Setting | Default | Meaning |
|---|---|---|
| `name` | | Letters, digits, spaces, `.`, `_`, `-` (up to 64) |
| `image` | | An image imported from Docker that has `/usr/bin/python3` |
| `packages` | `[]` | pip requirements, for example `httpx==0.28.1` |
| `preload` | `[]` | Modules imported before the snapshot, so calls start warm |
| `cpu_millis`, `memory_mib`, `disk_mib` | 1000, 512, 1024 | Size of each microVM |
| `timeout_secs` | 300 | Hard limit per execution |
| `warm_instances` | 0 | MicroVMs kept ready |
| `max_instances` | none | Cap; extra executions queue |
| `idle_timeout_secs` | 30 | How long extra idle microVMs are kept |
| `reuse` | true | Run further executions in the same microVM |
| `network` | true | Internet access |

Changing packages or the image rebuilds the disk; other changes apply right away. Restart
replaces the environment's microVMs.

## Executions

An execution is one function call in an environment. The **Executions** tab lists them
newest first with function name, environment, status, start time, duration, and node, and
filters by environment. Click one for its result, stdout, stderr, traceback, timings, and
CPU and memory use.

Function names are read from the submitted payload without unpickling it. Executions are
kept as long as the controller's task storage allows (24 hours by default).

Inside a call, read values passed with the call:

```python
from scraperack_runtime import secret, variable

token = secret("TOKEN")          # encrypted at rest, redacted from output
mode = variable("MODE", "fast")
```

## Python versions

Functions are pickled by value with cloudpickle, so the calling program must run the same
Python minor version as the environment's image (for example 3.10 with 3.10). A mismatch
fails with errors like `code expected at most 16 arguments, got 18`.

## API

Served at `http://<controller>:7070/plugins/python/api/`:

| Endpoint | |
|---|---|
| `GET /environments` | Environments and whether their disks reached every node |
| `POST /environments` | Create; blocks until packages are built |
| `GET/PUT/DELETE /environments/{id or name}` | Read, update, delete |
| `POST /environments/{id or name}/restart` | Replace the environment's microVMs |
| `GET /executions?environment=&limit=` | Newest first, with function names |
| `POST /executions` | `{environment, payload, queue, variables, secrets}` |
| `GET /executions/{id}` | One execution with logs and result |
| `GET /executions/{id}/wait`, `/result`, `/logs`, `/payload` | As the core task endpoints |

Each execution is a core task on the environment's pool; IDs are the same.

## Environments from before plugins

Environments created when Python support was built into the core are adopted
automatically, keeping their IDs, names, and warm microVMs.
