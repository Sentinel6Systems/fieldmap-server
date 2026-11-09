#!/usr/bin/env bash
# Install fieldmap-server on a Raspberry Pi that already runs Internet-in-a-Box.
# Run from the repository root on the Pi, as a user with sudo:
#   sudo ./scripts/install-pi.sh
# It builds the print tool here (Node is installed by IIAB), starts TileServer GL
# light in Docker on localhost, and adds the two nginx snippets. Re-run it after
# `git pull` to update; it is safe to run more than once.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "== Checking data"
for f in basemap contours hillshade; do
  [ -f "data/$f.mbtiles" ] || { echo "data/$f.mbtiles is missing. Copy your three .mbtiles files into data/ first." >&2; exit 1; }
done

echo "== Docker"
if ! command -v docker >/dev/null; then
  apt-get update && apt-get install -y docker.io docker-compose
fi
if [ ! -f /etc/docker/daemon.json ]; then
  install -m 644 pi/docker/daemon.json /etc/docker/daemon.json
  systemctl restart docker
fi

echo "== Tile server"
cp pi/tileserver/config-pi.json pi/tileserver/docker-compose.pi.yml .
docker compose -f docker-compose.pi.yml pull -q
docker compose -f docker-compose.pi.yml up -d

echo "== Print tool"
command -v node >/dev/null || { echo "Node.js is not installed. IIAB installs it; otherwise: apt-get install nodejs npm" >&2; exit 1; }
OWNER="${SUDO_USER:-$(id -un)}"
( cd print && sudo -u "$OWNER" npm ci --no-audit --no-fund --silent && sudo -u "$OWNER" node build.mjs )

echo "== nginx"
sed "s#/opt/fieldmap-server/print/dist/#$REPO/print/dist/#" pi/nginx/print-nginx.conf > /etc/nginx/conf.d/print-nginx.conf
install -m 644 pi/nginx/maps-nginx.conf /etc/nginx/conf.d/maps-nginx.conf
nginx -t && systemctl reload nginx

sleep 5
echo "== Styles served:"
curl -s http://127.0.0.1/maps/styles.json | grep -o '"id":"[^"]*"' || echo "(tile server not answering yet; try again in a minute)"
echo
echo "Done. Maps: http://<this-pi>/maps/   Print: http://<this-pi>/print/"
