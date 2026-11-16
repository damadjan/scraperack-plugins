# Workspaces plugin

Desktops that last: each workspace is an XFCE desktop in its own microVM, opened in the
browser with noVNC, with a persistent [volume](https://github.com/damadjan/scraperack/blob/main/docs/volumes.md)
for its files.

## Setup

1. Build the plugin archive and install it from **Plugins → Install plugin**:

   ```sh
   tar -czf workspaces-plugin.tar.gz -C workspaces/plugin .
   ```

2. Build [`workspaces/images/xfce-desktop`](../workspaces/images/xfce-desktop), push it to a
   registry, and import it on the **Images** page.
3. On the plugin's **Desktops** tab, add a desktop: the image, CPU, memory, and the default
   disk size for new workspaces.
4. On **Workspaces**, create a workspace and click **Open** once it is running.

## Portal

People use their workspaces from the portal, not the Scraperack admin page:

```
http://<controller>:7070/plugins/workspaces/gui/portal/
```

(**Open portal** on the plugin's page links to it.)

- **Your workspaces:** a card per workspace with its status, desktop and node; one click
  launches, resumes or opens it. Suspend, shut down and delete are in its menu.
- **New workspace:** pick a desktop from the catalog, name it, choose disk size and
  timezone; it opens as soon as it is ready.
- **Session:** while the workspace starts (queued, moving to another node, starting) the
  portal shows where it is; then the browser opens the desktop page, served from inside the
  workspace's microVM. The desktop fills the window and follows its size. The handle on
  the left edge opens the controls: back to the portal, fullscreen, clipboard both ways,
  Ctrl+Alt+Del, Esc and Super, picture quality, view only, suspend and shut down.

The desktop page (`guest/viewer.html`) is noVNC's client without its toolbar; websockify
serves it from the microVM, on the same origin as the VNC connection. The portal hands it
the password, its own address and the workspace in the URL fragment; Back, Suspend and Shut
down return to the portal, which does the work. Plain HTML, CSS and JavaScript, no framework.

## How it works

- **Desktop:** one pool per desktop. Before the pool's snapshot is taken, the executor
  starts the whole desktop: TigerVNC's `Xvnc`, noVNC's `websockify` on port 6080, and XFCE
  as `user` with a fresh home folder. Every workspace restores with it already running
  (about 5 seconds to a usable desktop on the test cluster).
- **Workspace:** one volume (`workspace-<id>`) mounted at `/mnt/workspace`. On start, the
  executor puts `/home` on the volume and overlays `/etc`, `/opt`, `/root`, `/usr`, and
  `/var` with folders on it, so files, settings, and software installed with
  `sudo apt install` are kept. A new workspace keeps the snapshot's desktop and copies its
  fresh home folder onto the volume; a workspace that already has a home starts XFCE again
  on it (a few seconds, the programs are already in memory). Each start sets a new VNC
  password and the workspace's timezone.
- **Open** links to noVNC on the workspace's node with that start's password; the
  desktop follows the browser window's size.
- **Suspend** saves the running microVM with its volume: as much disk as the desktop's
  memory, written at the node's disk speed (20 s to 2 minutes for 2 GiB here). **Resume**
  brings it back with every window open in about 5 seconds; a new password is set.
- **Stop** logs the user out and ends the microVM; the core forces it after 20 seconds.
  Logging out from the desktop does the same.
- **Moving:** when the workspace's node has no free memory for it and another node does,
  the core copies the volume (and a suspended state) there before it starts. The workspace
  shows **moving** meanwhile.
- A running workspace ends after 30 days.

Changing a desktop's image, CPU, or memory makes its suspended workspaces start fresh next
time; their files are kept. Software installed on top of one image version stays on the
volume when the image changes.

## Images

A desktop image needs, besides `/usr/bin/python3`: `Xvnc` and `vncpasswd` (TigerVNC),
`websockify` and noVNC in `/usr/share/novnc`, `dbus-daemon`, `xfce4-session`, a `user`
account, `/mnt/workspace`, and `/etc/machine-id` linked to `/run/machine-id`. The microVM's
hostname is `workspace`. The
[`xfce-desktop`](../workspaces/images/xfce-desktop/Dockerfile) image is a small Debian
example with Firefox.

## API

`http://<controller>:7070/plugins/workspaces/api/...`

| Method and path | Body | Result |
|---|---|---|
| `GET /workspaces` | | Workspaces with `status`, `url`, node, and any move |
| `POST /workspaces` | `{name, desktop?, disk_gib?, timezone?, start?}` | 201, workspace (started unless `start` is false) |
| `GET /workspaces/{id or name}` | | Workspace |
| `PATCH /workspaces/{id or name}` | `{name?, timezone?, desktop?}` | Workspace |
| `POST /workspaces/{id}/start` | | Starts or resumes |
| `POST /workspaces/{id}/suspend` | | Saves the running desktop |
| `POST /workspaces/{id}/stop` | | Logs out and stops |
| `DELETE /workspaces/{id}` | | 204; deletes the volume (only when stopped or suspended) |
| `GET /desktops`, `POST /desktops` | `{name, image, cpu_millis?, memory_mib?, disk_gib?}` | Desktops |
| `GET`, `PUT`, `DELETE /desktops/{id or name}` | | Desktop; delete only without workspaces |

Statuses: `stopped`, `queued`, `moving`, `starting`, `running`, `suspending`, `suspended`,
`stopping`.

The noVNC link carries the VNC password, so share it only with whoever should use the
workspace. Ports are published on the node without other authentication, like the rest of
Scraperack: keep the nodes on a trusted network.
