# Raspberry Pi 4 behind Internet-in-a-Box

The layout used on the emergency box: Raspberry Pi OS Lite 64-bit, Internet-in-a-Box (IIAB) running the Wi-Fi
hotspot and nginx on port 80, TileServer GL light in Docker on localhost, the print tool as static files.
Nothing here contains credentials; put your own in IIAB's `local_vars.yml`.

## Quick install

```bash
sudo apt install -y git
sudo git clone https://github.com/Sentinel6Systems/fieldmap-server.git /library/maps/fieldmap-server
sudo chown -R "$USER:$USER" /library/maps/fieldmap-server
cd /library/maps/fieldmap-server
cp /path/to/your/*.mbtiles data/      # basemap, contours, hillshade, built elsewhere with scripts/build-region.sh
sudo ./scripts/install-pi.sh
```

That installs Docker if needed, starts TileServer GL light on localhost, builds the print tool on the Pi with the
Node.js that IIAB installs, and adds the nginx snippets. Open `http://<pi>/print/`. To update later:
`git pull && sudo ./scripts/install-pi.sh`.

Building the map data itself (Planetiler, GDAL) wants an x86 machine with plenty of RAM and disk; do that with
`scripts/build-region.sh` elsewhere and copy the three `.mbtiles` files over.

## What the script does, step by step

1. **Docker.** `sudo apt install docker.io docker-compose`. Copy `docker/daemon.json` to `/etc/docker/daemon.json`
   first (no iptables, no bridge) so Docker never touches IIAB's firewall; the container uses host networking.
2. **Data.** Clone this repository to `/library/maps/fieldmap-server` (IIAB's `/library` is where bulk content lives)
   and copy your three `.mbtiles` files into its `data/`. Copy `tileserver/config-pi.json` and
   `tileserver/docker-compose.pi.yml` to the repository root on the Pi, then
   `sudo docker compose -f docker-compose.pi.yml up -d`. The server listens on `127.0.0.1:8080` with
   `--public_url /maps/`, so URLs work under whatever hostname the client used.
3. **nginx.** Copy `nginx/maps-nginx.conf` and `nginx/print-nginx.conf` to `/etc/nginx/conf.d/` (IIAB includes that
   folder and does not overwrite files it did not create), `sudo nginx -t && sudo systemctl reload nginx`.
   Maps are now at `http://<pi>/maps/`.
4. **Print tool.** `cd print && npm ci && node build.mjs` on the Pi; nginx serves `print/dist` from the clone.
   (`scripts/deploy-print-pi.sh user@<pi>` does the same build on a workstation and rsyncs the result, handy while
   developing.) Open `http://<pi>/print/`.

## Notes

- The light image does no server-side rendering, so `serve_rendered` is false for every style in `config-pi.json`.
  Browsers render the vector tiles themselves; the Pi only serves files. Docker plus the tile server use about 140 MB
  of RAM on a Pi 4.
- The image in `docker-compose.pi.yml` is pinned by digest so a later pull cannot change it unnoticed.
- IIAB's nginx uses a limited MIME table: serve JavaScript as `.js`, not `.mjs` (the print tool's build already does).
- To keep the default map view on the recipient's area, edit `center` and `zoom` in the Pi's copies of the styles;
  leave the repository's styles generic.
