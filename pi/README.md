# Raspberry Pi, and installing beside a web server of the machine's own

A Raspberry Pi installs like any other machine, with `scripts/install.sh`. This folder (`pi/`, named after the first
servers it was made for) holds what `scripts/install-nginx.sh` uses on a machine whose own nginx already serves port 80:
a compose file, nginx snippets, Docker settings and a systemd unit.

## Raspberry Pi

A Raspberry Pi 4 or 5 with 4 GB or more, Raspberry Pi OS Lite **64-bit** (Debian 13), ideally booting from an SSD.
Internet during the install.

```bash
sudo apt install -y git
git clone https://github.com/Sentinel6Systems/fieldmap-server.git
cd fieldmap-server
./scripts/install.sh
sudo reboot                    # once, when the script asks for it (the memory caps)
```

Run `install.sh` as yourself, without sudo; it asks for sudo where it needs it. It installs Docker (Debian's
`docker.io`, `docker-compose` and `docker-buildx`) and adds you to the docker group, then installs the same containers
and services as on a PC: the map at `http://<pi-host>/map/`, the Map Builder at `http://<pi-host>/mapadmin/`, the tile
server for TAK clients on port 8080, remote access for the Fieldmap app, and the firewall. To update:
`git pull && ./scripts/install.sh`.

### Building maps on the Pi

The Map Builder builds on the Pi itself, in **Pi mode** (on by itself below 8 GB of RAM; see
[Builds on a Raspberry Pi](../docs/reference.md#builds-on-a-raspberry-pi)): every build container stays within 3.0 GB
of RAM, Planetiler keeps its working data on disk, the contour lines are computed one cell at a time, the terrain is
the 30 m data only, and a build leaves 5 GB of the drive free. The page refuses a selection too large for that, before
the build starts. A US state or a small country is fine; for several large states or a large country, build on a
bigger machine (`scripts/build-region.sh <region-file>`, or its Map Builder) and copy the `.mbtiles` files into
`data/`. `--no-builder` leaves the Map Builder out.

- **Use an SSD.** A build writes many gigabytes: on an SD card it is slow and wears the card out. The page warns when
  the maps are on one.
- **Other services.** On a Pi that also runs other services, list the large ones in `.env` to stop them during a build
  and free their RAM: `FIELDMAP_PAUSE_UNITS="jellyfin calibre-web docker:<container>"` (systemd services by name,
  Docker containers as `docker:<name>`). The page names them before the build and while it runs, and they start
  again when it ends, also when it fails, is canceled, or a reboot cut it short. The list is read at every build:
  a change needs no new install.
- **Access Code.** Everyone who can reach the server can open the Map Builder and replace the maps. Set
  `ADMIN_PIN=<code>` in `.env` (then run the installer again) on a network others use, such as a hotspot.

The Pi itself joins a network: a router, or the Wi-Fi of another device. fieldmap-server does not run a Wi-Fi hotspot.

### Moving a server off Internet-in-a-Box

Older versions installed into Internet-in-a-Box's nginx. To move such a Pi to the install above, keep two things from
the old clone: `data/` (the maps, and in `data/remote/` the server's certificate authority and paired devices) and `.env`
(the gateway's port, so paired devices keep working). Then, on a freshly installed Raspberry Pi OS Lite 64-bit (or with
Internet-in-a-Box's nginx stopped and disabled, so that port 80 is free): clone, copy `data/` and `.env` back into the
clone, and run `./scripts/install.sh`. `install.sh` stops and says so while another program holds port 80.

## Beside a machine's own nginx

`scripts/install-nginx.sh` installs on Raspberry Pi OS Lite 64-bit, Debian 13 or Ubuntu 24.04 (arm64 or amd64) whose
own nginx serves port 80, typically when another installer puts fieldmap-server in as one step. The settings come from
the environment and are kept in `.env`, so later runs (and `git pull` updates) need not name them again:

```bash
sudo FIELDMAP_NGINX_DIR=/etc/nginx/fieldmap.d \
     FIELDMAP_DATA=/srv/maps \
     FIELDMAP_PORTS_FILE=/etc/<host-firewall>/fieldmap.ports \
     FIELDMAP_SERVER_NAME=<server-name> \
     /opt/fieldmap-server/scripts/install-nginx.sh
```

| Setting | What it is |
|---|---|
| `FIELDMAP_NGINX_DIR` | Needed. Where the nginx snippets (`pi/nginx/*.conf`, location blocks) go: a folder that the server block for port 80 includes, `include /etc/nginx/fieldmap.d/*.conf;` **inside** `server { }`. Not `/etc/nginx/conf.d` on Debian: that one is included at http level, where location blocks fail. If `nginx -t` fails, the snippets are put back as they were and the script stops. |
| `FIELDMAP_DATA` | The data folder, outside the repository (on a disk of its own, for example): the `.mbtiles` files and the services' folders `wmm/`, `remote/`, `share/`, `app/`, `routing/`. Every container and service reads it directly (a symbolic link at `data/` would not work inside the containers). The services start only once it is mounted (`RequiresMountsFor`). Default: `data/` in the repository. |
| `FIELDMAP_PORTS_FILE` | For a machine whose own firewall drops everything from outside: what remote access needs from outside, now, one line each, rewritten when it changes (see [Firewall](../docs/reference.md#firewall)): `wan tcp <port>` (the gateway, while the router's port is the way in: from anywhere, on any interface but a Wi-Fi hotspot's) and `iface nb-fieldmap tcp <port>` (the gateway, while NetBird is the way in: on NetBird's interface only). Comments only: nothing. |
| `FIELDMAP_SERVER_NAME` | The host name of that server block, for the checks at the end (default `127.0.0.1`). |
| `FIELDMAP_USER` | The user the services run as. With `FIELDMAP_DATA` the default is a system user `fieldmap`, made by the script; otherwise the owner of the repository. |
| `FIELDMAP_BUILD_USER` | The user the Map Builder runs as: it writes the maps into the data folder and starts the build's Docker containers, so it is added to the docker group. Default: the owner of the data folder (when that is not root). |
| `FIELDMAP_PAUSE_UNITS` | Services the Map Builder stops during a build to free RAM, and starts again afterwards (see [Building maps on the Pi](#building-maps-on-the-pi)). |

Put the repository where nginx (`www-data`) can read it, such as `/opt/fieldmap-server` owned by the admin user; not
in a home folder, which is often closed to other users (mode 700): the script stops when nginx cannot read it. The
script installs what is missing: Docker (Debian's `docker.io`, with `docker-compose` on Debian or `docker-compose-v2` on
Ubuntu, and `docker-buildx`; `pi/docker/daemon.json` is written before Docker's first start, so Docker never makes a
network or firewall rules of its own) and Node.js (`nodejs`, `npm`; 18 or newer) to build the map. It needs these ports
on 127.0.0.1 and stops if something else has one: 8080 (tile server), 8091 (magnetic model), 8092 (remote access), 8093
(location sharing) and 17777 (route service). It never asks anything and never reboots; it ends with checks through
nginx and a list of what it changed. A second run changes nothing and restarts nothing that did not change. It exits
with an error and says why when something fails. (`scripts/install-pi.sh`, its old name, still runs it.)

The pages are then at `/map/` (the map; `/maps/` and `/mapprinter/` lead there), `/guide/`, `/phones/` (Devices),
`/routing/` and `/maps/` (the tile server; TAK clients use `http://<server>/maps/styles/topo-light/{z}/{x}/{y}.jpg`,
see [Tile addresses](../docs/reference.md#tile-addresses-for-tak-and-other-apps)).

### What install-nginx.sh does, step by step

1. **Docker.** `docker.io` and Docker Compose v2, with `pi/docker/daemon.json` (no iptables, no bridge) so Docker never
   touches the machine's firewall; the containers use the machine's network, on 127.0.0.1.
2. **Data.** Any optional `.mbtiles` file you did not build is written empty by `scripts/data-placeholders.sh`.
   `pi/tileserver/docker-compose.pi.yml` is copied to the repository root and started: the tile server on
   `127.0.0.1:8080` with `--public_url /maps/`, and the route service (built here) on `127.0.0.1:17777`.
3. **Map.** `cd print && npm ci && node build.mjs`; nginx serves `print/dist` from the clone.
   (`scripts/deploy-print-pi.sh <user>@<server> <repo-dir>` does the same build on a workstation and rsyncs the
   result, handy while developing.)
4. **Magnetic model service.** `pi/systemd/fieldmap-wmm.service` runs `tools/wmm-update.py serve` on `127.0.0.1:8091`,
   allowed to write only `wmm/` in the data folder; nginx passes `/map/wmm-api/` to it. It backs the map's **Update
   Magnetic Model** button (see [Magnetic model updates](../docs/how-it-works.md#magnetic-model-updates)).
5. **Map Builder.** `systemd/fieldmap-admin.service` runs `scripts/admin.sh` as `FIELDMAP_BUILD_USER`, which joins the
   docker group; `scripts/pause-setup.sh` adds the root helper, its one sudoers rule and the boot unit that stop and
   start the services of `FIELDMAP_PAUSE_UNITS` (see [Builds on a Raspberry Pi](../docs/reference.md#builds-on-a-raspberry-pi)).
   Without the three `.mbtiles` files the install still runs: the map is blank until the first build.
6. **nginx.** `pi/nginx/maps-nginx.conf`, `mapprinter-nginx.conf`, `routing-nginx.conf` and `mapadmin-nginx.conf`
   (`/mapadmin/`) into `FIELDMAP_NGINX_DIR`, then `nginx -t` and a reload.
7. **Remote access** for the Fieldmap app (see [Remote access](../docs/user-guide.md#remote-access)): `scripts/remote.sh setup`
   adds `pi/nginx/mapremote-nginx.conf`, the `fieldmap-remote` and `fieldmap-share` services, the gateway container, and a
   firewall table of its own (`fieldmap_guard`). `sudo REMOTE=no ./scripts/install-nginx.sh` leaves remote access out,
   and still puts up the firewall (see [Firewall](../docs/reference.md#firewall)). If strangers could join the network
   while pairing is open (an open hotspot), set `REMOTE_PIN` in `.env`.

## Notes

- A Raspberry Pi runs the same full TileServer GL image and `config.json` as an x86 machine (the image is built for
  arm64), so browsers get vector tiles and TAK clients get raster tiles. Raster tiles are drawn on the Pi the first
  time they are asked for, which is slower than on a desktop; TAK clients cache them. The configs start no raster
  renderers and cap them at two per style (see [Tile addresses](../docs/reference.md#tile-addresses-for-tak-and-other-apps)).
  Memory grows with the number of styles TAK clients actually use, up to a few hundred MB if they use all six.
- The tile server container has a hard memory cap with no swap. Both installers run `scripts/mem-limit.sh`, which
  writes `TILESERVER_MEM_LIMIT` to `.env`: `1g` on a machine with under 8 GB, no cap with 8 GB or more. Edit that line to
  choose yourself, e.g. `TILESERVER_MEM_LIMIT=768m`. If a group of TAK clients pulls enough raster tiles to reach it,
  the kernel kills only the tile server and Docker restarts it within seconds; the rest of the machine is never
  starved. The cap needs the kernel's memory cgroup, which some Raspberry Pi OS kernels ship turned off: the installers
  then turn it on (`cgroup_enable=memory cgroup_memory=1` in `/boot/firmware/cmdline.txt`, backup next to it as
  `cmdline.txt.fieldmap.bak`), add a one-time job that recreates the tile server with its cap at the next boot, and say
  that a reboot is needed. Until that reboot everything works, uncapped.
- The images (TileServer GL, the gateway's nginx, NetBird, and the route service's Java base image in
  `brouter/Dockerfile`) are pinned by digest so a later pull cannot change them unnoticed. Newer versions come with an
  update of this repository.
- To keep the default map view on your area, edit `center` and `zoom` in your copies of the styles; leave the
  repository's styles generic.
