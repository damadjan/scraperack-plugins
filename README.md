# ScrapeRack plugins

Public plugins for [ScrapeRack](https://github.com/damadjan/scraperack). A plugin adds a
workload type to a ScrapeRack cluster without changing the core. The core only runs
microVMs; a plugin owns what runs in them and how users see it.

| Plugin | What it adds |
|---|---|
| [`python`](python/) | Python environments and executions: package sets built once into cached disks, functions run with the `scraperack` SDK and listed with their output |

## Install a plugin

1. Pack the plugin folder (the one containing `scraperack-plugin.toml`) into a `.tar.gz`.
   On Linux, macOS, or Windows 10+:

   ```sh
   tar -czf python-plugin.tar.gz -C python/plugin .
   ```

2. Open the ScrapeRack GUI → **Plugins** → **Install plugin**, drop the archive in,
   review what it adds, and click **Install**.

Installing the same plugin again with a higher `version` updates it. If the new version
does not start, ScrapeRack rolls back to the previous one automatically. A **Roll back**
button stays available after updates.

Archives contain only the plugin's own source. Dependencies are fetched on the controller
at install time from their original sources, so nothing third-party is redistributed.

## Write a plugin

A plugin is a folder with a manifest, a program that serves HTTP on a Unix socket, and
optionally files for the microVMs. Any language works.

```toml
# scraperack-plugin.toml
name = "browser"                 # lowercase letters, digits, dashes
version = "0.1.0"
description = "Headless browser fleet"
install = ["pip", "install", "--target", "vendor", "-r", "requirements.txt"]  # optional
run = ["python3", "service.py"]  # started in the plugin folder, restarted if it exits
gui = true                       # serve /gui/ to get a page in the ScrapeRack sidebar
guest = "guest"                  # optional folder, becomes a read-only disk for microVMs
```

The controller starts `run` with these environment variables:

| Variable | Meaning |
|---|---|
| `SCRAPERACK_PLUGIN_SOCKET` | Unix socket the plugin must serve HTTP on |
| `SCRAPERACK_CORE_SOCKET` | Unix socket of the core API; requests on it act as this plugin |
| `SCRAPERACK_PLUGIN_DATA` | Persistent directory kept across updates |
| `SCRAPERACK_PLUGIN_GUEST_DISK` | Disk key of the built `guest` folder, if any |
| `SCRAPERACK_PLUGIN_NAME`, `SCRAPERACK_PLUGIN_VERSION` | From the manifest |

The plugin must answer `GET /health`. It may serve `/gui/...` (shown in the GUI) and
`/api/...` (public at `http://<controller>:7070/plugins/<name>/api/...`).

Through the core socket a plugin uses the normal ScrapeRack API. Pools it creates are
owned by it: only it can change or delete them, and they are removed with the plugin.

| Core API | Use |
|---|---|
| `GET/POST /v1/pools`, `GET/PUT/DELETE /v1/pools/{name}`, `POST /v1/pools/{name}/restart` | Pools of microVMs, with `disks`, `executor`, `aliases`, `display_name` |
| `GET/POST /v1/disks`, `DELETE /v1/disks/{key}` | Upload read-only ext4 disks; nodes download them automatically |
| `GET /v1/images`, `POST /v1/images/import` | Root filesystems imported from Docker images |
| `POST /v1/tasks`, `GET /v1/tasks?owner=plugin:<name>`, `GET /v1/tasks/{id}[/wait\|/result\|/logs\|/payload]` | Send an opaque payload to a pool's executor on a warm microVM; tasks on a plugin's pools record it as `owner` |
| `GET /v1/vms`, `GET /v1/nodes` | Live inventory |

Inside a microVM the guest agent mounts the pool's `disks` (in order) at their `mount`
paths, then starts the pool's `executor` command. Executors accept length-prefixed JSON
requests on `/tmp/scraperack-executor.sock`; see
[`python/plugin/guest/executor.py`](python/plugin/guest/executor.py).

When a plugin starts it should read its own state from the core and make its pools match,
so it recovers from crashes and missed changes.
