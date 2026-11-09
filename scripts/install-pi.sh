#!/usr/bin/env bash
# Install fieldmap-server on a Raspberry Pi that already runs Internet-in-a-Box.
# Run from the repository root on the Pi, as a user with sudo:
#   sudo ./scripts/install-pi.sh
# It builds the print tool here (Node is installed by IIAB), starts TileServer GL
# in Docker on localhost, and adds the two nginx snippets. Re-run it after
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
sudo -u "${SUDO_USER:-$(id -un)}" ./scripts/mem-limit.sh
# Raspberry Pi kernels ship with the memory cgroup off, and without it Docker drops the cap.
# Turn it on in cmdline.txt (takes effect at the next boot) and, because Docker keeps a
# container's dropped cap across restarts, recreate the tile server once after that boot.
NEED_REBOOT=0
if docker info 2>&1 | grep -qi 'no memory limit support'; then
  CMDLINE=""
  for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [ -f "$f" ] && { CMDLINE="$f"; break; }
  done
  if [ -z "$CMDLINE" ]; then
    echo "WARNING: this kernel has no memory cgroup, so the tile server's memory cap will not work." >&2
    echo "         Turn on the kernel's memory cgroup and reboot, then run this script again." >&2
  else
    [ -f "$CMDLINE.fieldmap.bak" ] || cp "$CMDLINE" "$CMDLINE.fieldmap.bak"
    for opt in cgroup_enable=memory cgroup_memory=1; do
      grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
    done
    cat > /etc/systemd/system/fieldmap-memcap.service <<EOF
# One-time job from fieldmap-server's install-pi.sh: recreate the tile server so it
# gets its memory cap now that the memory cgroup is on, then remove this job.
[Unit]
Description=Apply the fieldmap-server tile server memory cap after reboot
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=/usr/bin/docker compose -f docker-compose.pi.yml up -d --force-recreate tileserver
ExecStartPost=/bin/systemctl disable fieldmap-memcap.service
ExecStartPost=/bin/rm -f /etc/systemd/system/fieldmap-memcap.service

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable -q fieldmap-memcap.service
    NEED_REBOOT=1
    echo "Memory cgroup turned on in $CMDLINE (backup: $CMDLINE.fieldmap.bak); the cap starts after a reboot."
  fi
fi
cp pi/tileserver/docker-compose.pi.yml .
rm -f config-pi.json   # left by older versions, which ran the light image
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
echo "TAK clients: http://<this-pi>/maps/styles/topo-light/{z}/{x}/{y}.png"

if [ "$NEED_REBOOT" = 1 ]; then
  echo
  echo "A reboot is needed to turn on the tile server's memory cap. Everything works until then, without the cap."
  if [ -t 0 ]; then
    read -r -p "Reboot now? [Y/n] " answer
    case "$answer" in [nN]*) echo "Reboot later with: sudo reboot" ;; *) reboot ;; esac
  else
    echo "Reboot with: sudo reboot"
  fi
fi
