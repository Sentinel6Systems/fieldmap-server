# Raspberry Pi 4 behind Internet-in-a-Box

The layout used on the emergency box: Raspberry Pi OS Lite 64-bit, Internet-in-a-Box (IIAB) running the Wi-Fi
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
`http://<pi-host>/maps/styles/topo-light/{z}/{x}/{y}.jpg` (see the main README). To update later, see "Updating" in
the main README: `git pull && sudo ./scripts/install-pi.sh` in `<repo-dir>`.

The Maps page is at `http://<pi-host>/maps/` and the print tool at `http://<pi-host>/mapprinter/`; each has a
header with a link to the other. For a button back to the box's own start page, set `homeUrl` in
`print/public/config.json` (see "The pages and their header" in the main README) and run the script again.

Building the map data itself (Planetiler, GDAL) wants an x86 machine with plenty of RAM and disk; do that with
`scripts/build-region.sh <region-file>` elsewhere and copy the `.mbtiles` files over.

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
   Maps are now at `http://<pi-host>/maps/`.
4. **Print tool.** `cd print && npm ci && node build.mjs` on the Pi; nginx serves `print/dist` from the clone.
   (`scripts/deploy-print-pi.sh <user>@<pi-host> <repo-dir>` does the same build on a workstation and rsyncs the
   result, handy while developing.) Open `http://<pi-host>/mapprinter/`. `/print/` is left free, for example for CUPS.
5. **Magnetic model service.** `systemd/fieldmap-wmm.service` runs `tools/wmm-update.py serve` on `127.0.0.1:8091`
   as the repository's owner, allowed to write only `data/wmm/`; nginx passes `/mapprinter/wmm-api/` to it. It backs
   the print tool's "Update magnetic model" button (see "Magnetic model updates" in the main README).

## Remote access for the Android app

`install-pi.sh` also sets up remote access for the Fieldmap app (see "Remote access for the Android app" in the main
README): it adds `nginx/mapremote-nginx.conf` to `/etc/nginx/conf.d/`, the `fieldmap-remote` service, a second
container beside the tile server, and a firewall table of its own (`fieldmap_guard`) beside Internet-in-a-Box's.
`sudo REMOTE=no ./scripts/install-pi.sh` leaves it out. Internet-in-a-Box's hotspot is often open: set `REMOTE_PIN`
in `.env` if strangers could join it while pairing is open.

The same step installs the `fieldmap-share` service, for sharing positions and markers between phones (see "Sharing
positions and markers between phones" in the main README; `/mapshare/` in the same nginx file). It is off until
switched on on the Phones page. On an open hotspot, everyone who joins can see the shared positions while it is on.

## Notes

- The Pi runs the same full TileServer GL image and `config.json` as an x86 machine (the image is built for arm64),
  so browsers get vector tiles and TAK clients get raster tiles. Raster tiles are drawn on the Pi the first time
  they are asked for, which is slower than on a desktop; TAK clients cache them. Use 64-bit Raspberry Pi OS.
  The configs start no raster renderers and cap them at two per style (see "Use with TAK clients" in the main
  README). Memory grows with the number of styles TAK clients actually use, up to a few hundred MB if they use
  all six.
- The tile server container has a hard memory cap with no swap. `install-pi.sh` runs `scripts/mem-limit.sh`, which
  writes `TILESERVER_MEM_LIMIT` to `.env` in the repository root: `1g` on a Pi with under 8 GB, no cap on an 8 GB Pi.
  Edit that line to choose yourself, e.g. `TILESERVER_MEM_LIMIT=768m`. If a group of TAK clients pulls enough raster
  tiles to reach it, the kernel kills only the tile server and Docker restarts it within seconds; the rest of the
  Pi is never starved. Vector maps drop out for those seconds too. The cap needs the kernel's memory cgroup, which
  Raspberry Pi OS ships turned off: `install-pi.sh` turns it on (adds `cgroup_enable=memory cgroup_memory=1` to
  `/boot/firmware/cmdline.txt`, backup next to it as `cmdline.txt.fieldmap.bak`), adds a one-time job that recreates
  the tile server with its cap at the next boot, and offers to reboot. Until that reboot everything works, uncapped.
- The image in `docker-compose.pi.yml` is pinned by digest so a later pull cannot change it unnoticed.
- IIAB's nginx uses a limited MIME table: serve JavaScript as `.js`, not `.mjs` (the print tool's build already does).
- To keep the default map view on the recipient's area, edit `center` and `zoom` in the Pi's copies of the styles;
  leave the repository's styles generic.
