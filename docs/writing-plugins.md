# Writing a plugin

A plugin adds a workload type to Scraperack without changing the core. It is a folder with
a manifest, a program that serves HTTP on a Unix socket, and optionally files for the
microVMs. Any language works. Plugins are trusted: they run with the controller's
permissions and can use the whole core API.

The [Python plugin](../python/plugin) is a complete, dependency-free example.

## Manifest

```toml
# scraperack-plugin.toml
name = "browser"                 # 1-32 lowercase letters, digits, dashes
version = "0.1.0"                # 1-32 letters, digits, . - _ +
description = "Headless browser fleet"
install = ["pip", "install", "--target", "vendor", "-r", "requirements.txt"]  # optional
run = ["python3", "service.py"]  # started in the plugin folder, restarted if it exits
gui = true                       # serve /gui/ to get a page in the Scraperack sidebar
guest = "guest"                  # optional folder, becomes a read-only disk for microVMs
```

## Process contract

The controller starts `run` in the plugin folder with:

| Variable | Meaning |
|---|---|
| `SCRAPERACK_PLUGIN_SOCKET` | Unix socket the plugin must serve HTTP on |
| `SCRAPERACK_CORE_SOCKET` | Unix socket of the core API; requests on it act as this plugin |
| `SCRAPERACK_PLUGIN_DATA` | Persistent directory kept across updates |
| `SCRAPERACK_PLUGIN_GUEST_DISK` | Disk key of the built `guest` folder, if any |
| `SCRAPERACK_PLUGIN_NAME`, `SCRAPERACK_PLUGIN_VERSION` | From the manifest |

The plugin must answer `GET /health` with a 2xx status. It may serve:

- `/gui/...`: shown in an iframe on its GUI page (`gui = true`)
- `/api/...`: public at `http://<controller>:7070/plugins/<name>/api/...`

Paths, methods, query strings, and bodies are proxied unchanged. Output goes to the
plugin's log on its card.

When the plugin starts it should read its state from the core and make its pools match,
so it recovers from crashes, restarts, and missed changes.

## Core API from a plugin

Requests on `SCRAPERACK_CORE_SOCKET` use the normal
[core API](https://github.com/damadjan/scraperack/blob/main/docs/api.md) and act as the
plugin:

- Pools it creates get `managed_by: "plugin:<name>"`. Only it can change or delete them, and
  they are removed with the plugin.
- Disks it uploads are owned by it and removed with it.
- Tasks on its pools record `owner: "plugin:<name>"`; list them with
  `GET /v1/tasks?owner=plugin:<name>`.

| Core API | Use |
|---|---|
| `/v1/pools` | Pools with `disks`, `executor`, `aliases`, `display_name`, `preload` |
| `/v1/disks` | Upload read-only ext4 disks; nodes fetch them on demand |
| `/v1/images` | Root filesystems imported from Docker images |
| `/v1/tasks` | Send payloads to a pool's executor and read results, logs, metrics |
| `/v1/vms`, `/v1/nodes` | Live inventory |

A plugin that turns tasks into something users recognize (executions, crawls, sessions)
should own that view: list its tasks, describe their payloads, and show them on its page.
The core GUI does not show tasks.

## Guest disk and executor

The `guest` folder is built into an ext4 disk. Attach it to a pool and point the pool's
`executor` at a program on it:

```json
{"disks": [{"key": "<SCRAPERACK_PLUGIN_GUEST_DISK>", "mount": "/tmp/scraperack/browser"}],
 "executor": ["/usr/bin/python3", "/tmp/scraperack/browser/executor.py"]}
```

The executor protocol (a Unix socket with length-prefixed JSON) is described in
[scraperack/docs/executors.md](https://github.com/damadjan/scraperack/blob/main/docs/executors.md).

## SDK

If users call the plugin from code, ship an SDK next to it (for example `python/sdk`) that
talks to `/plugins/<name>/api/` and builds on the core client
[`scraperack`](https://github.com/damadjan/scraperack/blob/main/docs/sdk.md).
