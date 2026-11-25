# Python plugin

Warm Python environments. Install it from [`python/plugin`](../python/plugin)
([how](installing-plugins.md)) and call it with the [Python SDK](python-sdk.md). It has no
dependencies of its own.

## How an environment stays warm

1. **Packages are built once.** They are installed into a virtualenv in a short-lived
   `python-build-…` microVM of the image (2 vCPUs, 2 GiB, network on), and the virtualenv
   becomes a read-only disk that every node caches. The same image and packages reuse it.
2. **The interpreter is started before the snapshot.** Each environment is one Scraperack
   pool. When a node first prepares the pool, it boots a microVM, mounts the virtualenv,
   starts the plugin's executor with the virtualenv's Python, and imports the `preload`
   modules. Then it snapshots that microVM.
3. **Every microVM is restored from that snapshot.** A call never waits for Python to
   start or for its imports: it lands in an interpreter that already has them loaded.
   `warm_instances` additionally keeps restored microVMs waiting, so a call does not even
   wait for the restore; with the default of 0, each call restores one on demand.
4. **MicroVMs are reused.** With `reuse` on, a microVM keeps running further calls of the
   same environment instead of being replaced after one, and a function it has seen is not
   unpickled again.

An environment's name and ID both work wherever an environment is expected.

| Setting | Default | Meaning |
|---|---|---|
| `name` | | Letters, digits, spaces, `.`, `_`, `-` (up to 64) |
| `image` | | An image with `/usr/bin/python3` and its `venv` module |
| `packages` | `[]` | pip requirements, for example `httpx==0.28.1` |
| `preload` | `[]` | Modules imported before the snapshot, so calls find them loaded |
| `cpu_millis`, `memory_mib`, `disk_mib` | 1000, 512, 1024 | Size of each microVM |
| `timeout_secs` | 300 | Hard limit per execution |
| `warm_instances` | 0 | Restored microVMs kept waiting for calls |
| `max_instances` | none | Cap; extra executions queue |
| `idle_timeout_secs` | 30 | How long extra idle microVMs are kept |
| `reuse` | true | Run further executions in the same microVM |
| `network` | true | Internet access |

Changing packages or the image rebuilds the disk; changing `preload` or the microVM size
takes a new snapshot; other changes apply right away. Restart replaces the environment's
microVMs.

## Executions

An execution is one function call in an environment, kept with its result and output. The **Executions** tab lists them
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

## Source code

Without the SDK, send Python source as the payload. If it defines `main`, the call returns
`main(*params, **kwargs)`; otherwise it runs as a script and returns its `result` variable
(or `null`). Output, secrets and variables work as for functions, and the warm microVM is
the same. Compiled source is cached while a microVM is reused.

```sh
curl -H "Authorization: Bearer sr_..." -H "Content-Type: application/json"   http://controller:7070/plugins/python/api/executions -d '{"environment": "scrapers",
  "payload": {"encoding": "python-source", "params": ["https://example.com"],
    "source": "import httpx
def main(url):
    return httpx.get(url).status_code
"}}'
```

The result is JSON when the value fits in JSON, otherwise cloudpickled.

## Python versions

Functions are pickled by value with cloudpickle, so the calling program must run the same
Python minor version as the environment's image (for example 3.10 with 3.10). A mismatch
fails with errors like `code expected at most 16 arguments, got 18`.

Both sides must also have the same cloudpickle. Every environment gets `cloudpickle==3.1.2`
(the SDK requires 3.1.x) unless its `packages` name cloudpickle themselves.

Image build contexts (Python 3.10): [`python-runtime`](../python/images/python-runtime)
and [`python-requests-runtime`](../python/images/python-requests-runtime) (adds `requests`).

## API

Served at `http://<controller>:7070/plugins/python/api/`:

| Endpoint | |
|---|---|
| `GET /environments` | Environments and whether their disks reached every node |
| `POST /environments` | Create; blocks until packages are built |
| `GET/PUT/DELETE /environments/{id or name}` | Read, update, delete |
| `POST /environments/{id or name}/restart` | Replace the environment's microVMs |
| `GET /executions?environment=&limit=` | Newest first, with function names |
| `POST /executions` | `{environment, payload, queue, variables, secrets}`; `payload` is a `python-function` (from the SDK) or `python-source` `{source, params?, kwargs?}` |
| `GET /executions/{id}` | One execution with logs and result |
| `GET /executions/{id}/wait`, `/result`, `/logs`, `/payload` | As the core task endpoints |
| `POST /runs`, `GET /runs?ids=`, `GET /runs/{id}`, `POST /runs/{id}/stop` | The [runner contract](queue-plugin.md#runner-contract), used by the queue plugin; `input` is the `POST /executions` body |

Each execution is a core task on the environment's pool; IDs are the same. Runs are the same
executions: a run's id is its execution's id. A run fails with `code` when the function
raised, `timeout`, `stopped`, or otherwise `infrastructure`. Run keys are kept for 48 hours
in `runs.db`.
