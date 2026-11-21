# Raspberry Pi 4 behind Internet-in-a-Box

The layout used on an emergency kit: Raspberry Pi OS Lite 64-bit, Internet-in-a-Box (IIAB) running the Wi-Fi
hotspot and nginx on port 80, TileServer GL in Docker on localhost, the print tool as static files.
Nothing here contains credentials; put your own in IIAB's `local_vars.yml`.

## Quick install

```bash
sudo apt install -y git
sudo git clone https://github.com/Sentinel6Systems/fieldmap-server.git <repo-dir>
sudo chown -R "$USER:$USER" <repo-dir>
cd <repo-dir>
cp <path>/*.mbtiles data/      # basemap, contours, hillshade (and optional extras, usdata), built elsewhere
sudo ./scripts/install-pi.sh
```

`<repo-dir>` is any folder on a drive with room for the map data.

That installs Docker if needed, starts TileServer GL on localhost, builds the print tool on the Pi with the
Node.js that IIAB installs, and adds the nginx snippets. Open `http://<pi-host>/mapprinter/`; TAK clients use
`http://<pi-host>/maps/styles/topo-light/{z}/{x}/{y}.jpg` (see [Tile addresses](../docs/reference.md#tile-addresses-for-tak-and-other-apps)).
To update later, see [Update the server](../docs/user-guide.md#update-the-server): `git pull && sudo ./scripts/install-pi.sh` in `<repo-dir>`.

The Maps page is at `http://<pi-host>/maps/`, the print tool at `http://<pi-host>/mapprinter/` and the User Guide at
`http://<pi-host>/guide/`; every page has the same header. For a button back to the device's own start page, set
`homeUrl` in `print/public/config.json` (see [Pages and addresses](../docs/reference.md#pages-and-addresses)) and run
the script again.

Building the map data itself (Planetiler, GDAL) wants an x86 machine with plenty of RAM and disk; do that with
`scripts/build-region.sh <region-file>` elsewhere and copy the `.mbtiles` files over.

## Without Internet-in-a-Box: beside a web server of the machine's own

`install-pi.sh` also installs on Raspberry Pi OS Lite 64-bit (Debian 13), Debian 13 or Ubuntu 24.04 (arm64 or amd64)
without Internet-in-a-Box, where the machine's own nginx serves port 80 and another installer may call this one as a
step. The settings come from the environment and are kept in `.env`, so later runs (and `git pull` updates) need not
name them again:

```bash
sudo FIELDMAP_DATA=/srv/maps \
     FIELDMAP_NGINX_DIR=/etc/nginx/fieldmap.d \
     FIELDMAP_PORTS_FILE=/etc/<host-firewall>/fieldmap.ports \
     FIELDMAP_SERVER_NAME=<server-name> \
     /opt/fieldmap-server/scripts/install-pi.sh
```

| Setting | What it is |
|---|---|
| `FIELDMAP_DATA` | The data folder, outside the repository (on a disk of its own, for example): the `.mbtiles` files and the services' folders `wmm/`, `remote/`, `share/`, `app/`, `routing/`. Every container and service reads it directly (a symbolic link at `data/` would not work inside the containers). The services start only once it is mounted (`RequiresMountsFor`). |
| `FIELDMAP_NGINX_DIR` | Where the nginx snippets (`pi/nginx/*.conf`, location blocks) go: a folder that the server block for port 80 includes, `include /etc/nginx/fieldmap.d/*.conf;` **inside** `server { }`. Not `/etc/nginx/conf.d` on Debian: that one is included at http level, where location blocks fail. If `nginx -t` fails, the snippets are put back as they were and the script stops. |
| `FIELDMAP_PORTS_FILE` | For a machine whose own firewall drops everything from outside: what remote access needs from outside, now, one line each, rewritten when it changes (see [Firewall](../docs/reference.md#firewall)): `wan tcp <port>` (the gateway, while the router's port is the way in: from anywhere, on any interface but a Wi-Fi hotspot's) and `iface nb-fieldmap tcp <port>` (the gateway, while NetBird is the way in: on NetBird's interface only). Comments only: nothing. |
| `FIELDMAP_SERVER_NAME` | The host name of that server block, for the checks at the end (default `127.0.0.1`). |
| `FIELDMAP_USER` | The user the services run as. With `FIELDMAP_DATA` the default is a system user `fieldmap`, made by the script; otherwise the owner of the repository. |

Put the repository where nginx (`www-data`) can read it, such as `/opt/fieldmap-server` owned by the admin user; not
in a home folder, which is often closed to other users (mode 700). The script installs what is missing: Docker
(Debian's `docker.io`, with `docker-compose` on Debian or `docker-compose-v2` on Ubuntu, and `docker-buildx`;
`pi/docker/daemon.json` is written before Docker's first start, so Docker never makes a network or firewall rules of
its own) and Node.js (`nodejs`, `npm`; 18 or newer) to build the map. It needs these ports on 127.0.0.1 and stops if
something else has one: 8080 (tile server), 8091 (magnetic model), 8092 (remote access), 8093 (location sharing) and
17777 (route service). It never asks anything and never reboots; it ends with checks through nginx and a list of what
it changed. A second run changes nothing and restarts nothing that did not change. It exits with an error and says why
when something fails.

## What the script does, step by step

1. **Docker.** `sudo apt install docker.io docker-compose`. Copy `docker/daemon.json` to `/etc/docker/daemon.json`
   first (no iptables, no bridge) so Docker never touches IIAB's firewall; the container uses host networking.
2. **Data.** Clone this repository to `<repo-dir>` and copy your `.mbtiles` files into its `data/`; any optional file
   you did not build is written empty by `scripts/data-placeholders.sh`. Copy `tileserver/docker-compose.pi.yml`
   to the repository root on the Pi, then
   `sudo docker compose -f docker-compose.pi.yml up -d`. The server listens on `127.0.0.1:8080` with
   `--public_url /maps/`, so URLs work under whatever hostname the client used.
3. **nginx.** Copy `nginx/maps-nginx.conf` and `nginx/mapprinter-nginx.conf` to `/etc/nginx/conf.d/` (IIAB includes that
   folder and does not overwrite files it did not create), `sudo nginx -t && sudo systemctl reload nginx`.
   Maps are now at `http://<pi-host>/maps/`, and the User Guide at `http://<pi-host>/guide/` (the second file serves
   `guide/` from the clone).
4. **Print tool.** `cd print && npm ci && node build.mjs` on the Pi; nginx serves `print/dist` from the clone.
   (`scripts/deploy-print-pi.sh <user>@<pi-host> <repo-dir>` does the same build on a workstation and rsyncs the
   result, handy while developing.) Open `http://<pi-host>/mapprinter/`. `/print/` is left free, for example for CUPS.
5. **Magnetic model service.** `systemd/fieldmap-wmm.service` runs `tools/wmm-update.py serve` on `127.0.0.1:8091`
   as the repository's owner, allowed to write only `data/wmm/`; nginx passes `/mapprinter/wmm-api/` to it. It backs
   the print tool's **Update Magnetic Model** button (see [Magnetic model updates](../docs/how-it-works.md#magnetic-model-updates)).

## Remote access for the Android app

`install-pi.sh` also sets up remote access for the Fieldmap app (see [Remote access](../docs/user-guide.md#remote-access)): it adds `nginx/mapremote-nginx.conf` to `/etc/nginx/conf.d/`, the `fieldmap-remote` service, a second
container beside the tile server, and a firewall table of its own (`fieldmap_guard`) beside Internet-in-a-Box's.
`sudo REMOTE=no ./scripts/install-pi.sh` leaves remote access out, and still puts up the firewall (see [Firewall](../docs/reference.md#firewall)). Internet-in-a-Box's hotspot is often open: set `REMOTE_PIN`
in `.env` if strangers could join it while pairing is open.

The same step installs the `fieldmap-share` service, for sharing positions and markers between devices (see
[Location sharing](../docs/user-guide.md#location-sharing); `/mapshare/` in the same nginx file). It is off until
enabled on the Devices page. On an open hotspot, everyone who joins can see the shared positions while it is on.

## Notes

- The Pi runs the same full TileServer GL image and `config.json` as an x86 machine (the image is built for arm64),
  so browsers get vector tiles and TAK clients get raster tiles. Raster tiles are drawn on the Pi the first time
  they are asked for, which is slower than on a desktop; TAK clients cache them. Use 64-bit Raspberry Pi OS.
  The configs start no raster renderers and cap them at two per style (see [Tile addresses](../docs/reference.md#tile-addresses-for-tak-and-other-apps)). Memory grows with the number of styles TAK clients actually use, up to a few hundred MB if they use
  all six.
- The tile server container has a hard memory cap with no swap. `install-pi.sh` runs `scripts/mem-limit.sh`, which
  writes `TILESERVER_MEM_LIMIT` to `.env` in the repository root: `1g` on a Pi with under 8 GB, no cap on an 8 GB Pi.
  Edit that line to choose yourself, e.g. `TILESERVER_MEM_LIMIT=768m`. If a group of TAK clients pulls enough raster
  tiles to reach it, the kernel kills only the tile server and Docker restarts it within seconds; the rest of the
  Pi is never starved. Vector maps drop out for those seconds too. The cap needs the kernel's memory cgroup, which
  Raspberry Pi OS ships turned off: `install-pi.sh` turns it on (adds `cgroup_enable=memory cgroup_memory=1` to
  `/boot/firmware/cmdline.txt`, backup next to it as `cmdline.txt.fieldmap.bak`), adds a one-time job that recreates
  the tile server with its cap at the next boot, and offers to reboot. Until that reboot everything works, uncapped.
- The images in `docker-compose.pi.yml` (TileServer GL, the gateway's nginx, NetBird, and the route service's Java
  base image in `brouter/Dockerfile`) are pinned by digest so a later pull cannot change them unnoticed. Newer versions
  come with an update of this repository.
- IIAB's nginx uses a limited MIME table: serve JavaScript as `.js`, not `.mjs` (the print tool's build already does).
- To keep the default map view on the recipient's area, edit `center` and `zoom` in the Pi's copies of the styles;
  leave the repository's styles generic.
