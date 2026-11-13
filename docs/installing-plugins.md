# Installing plugins

## Install

1. Pack the folder that contains `scraperack-plugin.toml` into a `.tar.gz` (Linux, macOS,
   or Windows 10+):

   ```sh
   tar -czf python-plugin.tar.gz -C python/plugin .
   ```

2. In the Scraperack GUI open **Plugins → Install plugin**, drop the archive in, review what
   it adds, and click **Install**.

Or with the API:

```sh
curl -X POST --data-binary @python-plugin.tar.gz http://controller:7070/v1/plugins/uploads
# {"upload": "<id>", "manifest": {...}, "installed_version": "0.2.0", ...}
curl -X POST http://controller:7070/v1/plugins/uploads/<id>/install
```

The controller unpacks the archive (at most 256 MiB), runs the manifest's `install`
command, builds the `guest` folder into a disk, starts the plugin, and waits up to
30 seconds for its `/health` to answer. The plugin's page then appears in the sidebar.

Archives contain only the plugin's own source. Dependencies are fetched on the controller
at install time from their original sources.

## Update and roll back

Installing an archive with a different `version` updates the plugin. The previous version
is kept:

- if the new version does not start, Scraperack switches back to the previous one
  automatically;
- **Roll back** on the plugin's card (or `POST /v1/plugins/{name}/rollback`) switches back
  by hand.

The plugin's data directory is kept across updates and rollbacks. If the `guest` folder did
not change, its disk is reused and the plugin's microVMs keep running.

## Disable and remove

**Disable** stops the plugin process; its pools and microVMs stay. **Remove** deletes the
plugin, its pools, and its disks, and is refused while its pools have active tasks.

Installing can be turned off on a controller with `SCRAPERACK_ALLOW_PLUGIN_INSTALL=false` in
`/etc/scraperack/controller.env`.
