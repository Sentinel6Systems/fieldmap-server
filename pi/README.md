# Raspberry Pi 4 behind Internet-in-a-Box

The layout used on the emergency box: Raspberry Pi OS Lite 64-bit, Internet-in-a-Box (IIAB) running the Wi-Fi
hotspot and nginx on port 80, TileServer GL light in Docker on localhost, the print tool as static files.
Nothing here contains credentials; put your own in IIAB's `local_vars.yml`.

## Steps

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
4. **Print tool.** From your workstation: `cd print && npm install`, then `./scripts/deploy-print-pi.sh user@<pi>`.
   It builds `print/dist` and rsyncs it to `/opt/fieldmap-server/print/dist` (change the path in
   `nginx/print-nginx.conf` if you prefer another). Open `http://<pi>/print/`.

## Notes

- The light image does no server-side rendering, so `serve_rendered` is false for every style in `config-pi.json`.
  Browsers render the vector tiles themselves; the Pi only serves files. Docker plus the tile server use about 140 MB
  of RAM on a Pi 4.
- The image in `docker-compose.pi.yml` is pinned by digest so a later pull cannot change it unnoticed.
- IIAB's nginx uses a limited MIME table: serve JavaScript as `.js`, not `.mjs` (the print tool's build already does).
- To keep the default map view on the recipient's area, edit `center` and `zoom` in the Pi's copies of the styles;
  leave the repository's styles generic.
