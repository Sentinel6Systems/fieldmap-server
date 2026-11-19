#!/usr/bin/env bash
# Install or update fieldmap-server on a machine with Docker: one command, and the tile
# server, the print tool and the map builder page are running, now and after every reboot.
#
# Usage:
#   ./scripts/install.sh [--no-builder] [--no-remote]
#
# Run it again after every `git pull`: it is the update step too. It
#   1. writes empty data files for whatever has not been built yet, so that the tile
#      server starts (scripts/data-placeholders.sh);
#   2. chooses the tile server's memory cap from this machine's RAM (scripts/mem-limit.sh);
#   3. builds and starts the containers: tile server, print tool, magnetic model service
#      (docker compose up -d --build), and restarts the tile server so that it reads
#      the current config, styles and data;
#   4. installs the map builder page (scripts/admin.sh) as a systemd service, which
#      needs sudo. The page is then at /mapadmin/ beside the print tool. --no-builder
#      leaves this step out, and removes the service if an earlier run installed it;
#   5. puts a firewall on the box itself: nothing from outside the local network and VPNs gets
#      in, or gets an answer (scripts/firewall.sh; REMOTE_FIREWALL=off in .env leaves it out);
#   6. sets up remote access for the Fieldmap Android app (scripts/remote.sh setup): phones
#      paired on this network can then use the maps from anywhere, and the phones can share
#      positions and markers with each other. Both are off until switched on (Admin, Remote
#      access; and Admin, Devices). Devices on this network can install the app from the Devices page
#      (scripts/app.sh fetch puts it on the box). --no-remote leaves all of it out.
# Then build your maps: on the map builder page, or with build-region.sh.
#
# A Raspberry Pi behind Internet-in-a-Box uses scripts/install-pi.sh instead.

set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

BUILDER=yes
REMOTE=yes
for arg in "$@"; do
    case "$arg" in
        --no-builder) BUILDER=no ;;
        --no-remote) REMOTE=no ;;
        *) echo "Usage: $0 [--no-builder] [--no-remote]" >&2; exit 1 ;;
    esac
done

for tool in docker python3 curl; do
    command -v "$tool" >/dev/null || { echo "Error: $tool is not installed." >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "Error: the Docker Compose plugin is not installed (docker compose)." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Error: Docker is not running, or $(id -un) may not use it (add the user to the docker group)." >&2; exit 1; }

# From .env, as Docker Compose and admin.sh read it. Arguments: NAME DEFAULT
setting() {
    local value=""
    if [ -f .env ]; then
        value="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    fi
    echo "${value:-$2}"
}

# The owner of this folder runs everything, the map builder service included.
OWNER="$(stat -c %U "$REPO_DIR")"
if [ "$(id -un)" != "$OWNER" ]; then
    echo "Error: run this script as $OWNER, the owner of $REPO_DIR, without sudo. It asks for sudo itself where it needs it." >&2
    exit 1
fi

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO=sudo

echo "== Data files"
./scripts/data-placeholders.sh
# Docker makes a mounted folder that is missing, owned by root: an earlier `docker compose up`
# may have made data/admin that way, and the map builder could then not use it.
if [ -n "$(find data/admin -maxdepth 0 ! -user "$OWNER" 2>/dev/null)" ]; then
    echo "data/admin belongs to another user: giving it back to $OWNER."
    $SUDO chown -R "$OWNER" data/admin
fi

# The web front mounts the folders of the remote access service and of the sharing service
# (scripts/remote.sh); made here so that they belong to $OWNER and not to root, as Docker
# would make them.
mkdir -p data/remote/run data/share/run

echo "== Tile server and print tool"
./scripts/mem-limit.sh
docker compose up -d --build --quiet-pull
docker restart tileserver >/dev/null
echo "Started: tileserver, fieldmap-web (print tool), fieldmap-wmm."

UNIT=/etc/systemd/system/fieldmap-admin.service
echo "== Map builder page"
# With this file the web front answers "not here" for /mapadmin/, and the other pages'
# header leaves the map builder out instead of calling it "not running".
if [ "$BUILDER" = no ]; then touch data/admin/no-builder; else rm -f data/admin/no-builder; fi
if [ "$BUILDER" = no ]; then
    if [ -f "$UNIT" ]; then
        $SUDO systemctl disable --now fieldmap-admin >/dev/null 2>&1 || true
        $SUDO rm -f "$UNIT"
        $SUDO systemctl daemon-reload
        echo "Removed the map builder service."
    else
        echo "Left out (--no-builder)."
    fi
elif ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
    BUILDER=manual
    echo "This machine has no systemd: start the page yourself with ./scripts/admin.sh when you need it."
else
    # The unit in systemd/, with this folder and its owner filled in.
    sed -e "s|/opt/tileserver|$REPO_DIR|g" -e "s|^User=.*|User=$OWNER|" systemd/fieldmap-admin.service \
        | $SUDO tee "$UNIT" >/dev/null
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q fieldmap-admin
    $SUDO systemctl restart fieldmap-admin
    echo "Installed and started as a service (fieldmap-admin), as user $OWNER. It starts at boot."
    if ! id -nG "$OWNER" | tr ' ' '\n' | grep -qx docker && [ "$OWNER" != root ]; then
        echo "WARNING: $OWNER is not in the docker group, so builds started from the page will fail." >&2
        echo "         sudo usermod -aG docker $OWNER   (then log out and in, and run this script again)" >&2
    fi
fi

WEB_PORT="$(setting WEB_PORT 80)"
TILE_PORT="$(setting TILESERVER_PORT 8080)"
ADDRESS="$(hostname -I 2>/dev/null | cut -d' ' -f1)"
ADDRESS="${ADDRESS:-localhost}"
WEB="http://$ADDRESS$([ "$WEB_PORT" = 80 ] || echo ":$WEB_PORT")"
echo
echo "Ready:"
echo "  Maps                    $WEB/"
echo "  Print a map             $WEB/mapprinter/"
echo "  Tile server (TAK)       http://$ADDRESS:$TILE_PORT/"
[ "$BUILDER" = yes ] && echo "  Map builder             $WEB/mapadmin/"
[ "$REMOTE" = yes ] && echo "  Devices                 $WEB/phones/"
if [ -z "$(find data -maxdepth 1 -name 'basemap.mbtiles' -size +100k 2>/dev/null)" ]; then
    echo "There are no maps on this device yet: the Maps page is blank until the first build."
    [ "$BUILDER" = yes ] && echo "Open the map builder to choose your regions and start it."
fi

echo
echo "== Remote access for the Fieldmap app"
if [ "$REMOTE" = yes ]; then
    if ! ./scripts/remote.sh setup; then
        echo "WARNING: remote access for the app is not set up (see above). Everything else works." >&2
        echo "== The firewall on this box"
        $SUDO ./scripts/firewall.sh install
    fi
elif [ -f /etc/systemd/system/fieldmap-remote.service ]; then
    ./scripts/remote.sh off               # keeps the firewall
else
    touch data/remote/run/no-remote      # no Devices page under Admin
    echo "Left out (--no-remote)."
    echo "== The firewall on this box"
    $SUDO ./scripts/firewall.sh install
fi
if [ "$REMOTE" = yes ]; then
    echo "== The Fieldmap app, for devices to install from the Devices page"
    ./scripts/app.sh fetch || echo "WARNING: the app could not be fetched (see above). Everything else works." >&2
fi

# A firewall is the owner's to change: say what it needs, do not touch it.
if command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q '^Status: active'; then
    echo
    echo "The ufw firewall is on. If a page does not open from another device, allow its port, for example:"
    for port in "$WEB_PORT" "$TILE_PORT"; do
        echo "  sudo ufw allow from <your-network>/24 to any port $port proto tcp"
    done
fi
