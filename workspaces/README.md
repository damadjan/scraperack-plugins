# Workspaces plugin

Long-lived desktops in Scraperack microVMs, used from the browser over KasmVNC. Each
workspace keeps its home folder and installed software on its own volume, can be suspended
with its windows open and resumed in a few seconds, and moves to a node with room when its
own node is full. No SDK: everything is on the plugin's page and its HTTP API.

```sh
tar -czf workspaces-plugin.tar.gz -C plugin .
```

Install the archive from **Plugins → Install plugin**, import the
[`xfce-desktop`](images/xfce-desktop) image on the **Images** page, click **Open Workspaces**
on the plugin's entry, add a template (image and machine size) on **Templates**, then create a
workspace from it.

Needs a Scraperack core with volumes (see its `docs/volumes.md`).

See [docs/workspaces-plugin.md](../docs/workspaces-plugin.md).
