#!/usr/bin/env bash
# Install or update fieldmap-server: one command, on a 64-bit PC or a Raspberry Pi 4/5 (Raspberry
# Pi OS Lite 64-bit), and the tile server, the map, remote access for the Fieldmap app and the
# firewall are running, now and after every reboot.
#
# Usage:
#   ./scripts/install.sh [--no-builder] [--no-remote]
#
# Run it as the user who owns this folder, without sudo; it asks for sudo where it needs it.
# Run it again after every `git pull`: it is the update step too. A second run changes nothing
# and restarts nothing that did not change. It
#   1. installs what is missing on Debian 13, Ubuntu 24.04 or Raspberry Pi OS (Docker with
#      Docker Compose v2, Python 3, curl) and adds you to the docker group; on a Raspberry Pi it
#      turns on the kernel's memory cgroup, which the tile server's memory cap needs (from the
#      next reboot; it never reboots by itself);
#   2. writes empty data files for whatever has not been built yet, so that the tile
#      server starts (scripts/data-placeholders.sh);
#   3. chooses the tile server's memory cap from this machine's RAM (scripts/mem-limit.sh);
#   4. builds the web front (the map) and the route service when their sources changed, starts
#      the containers: tile server, web front, route service, magnetic model service, and
#      restarts the tile server when its config, styles or data changed;
#   5. installs the map builder page (scripts/admin.sh) as a systemd service. The page is then
#      at /mapadmin/. --no-builder leaves it out, and removes the service if an earlier run
#      installed it. On a machine with less than 8 GB of RAM (a Raspberry Pi) it builds in Pi
#      mode: at most 3.0 GB of RAM, the 30 m elevation data only (see PI_MODE in .env.example).
#      With FIELDMAP_PAUSE_UNITS in .env it stops those services during a build to free RAM and
#      starts them again afterwards (scripts/pause-setup.sh lets it, as root);
#   6. puts a firewall on the machine itself: nothing from outside the local network and VPNs
#      gets in, or gets an answer (scripts/firewall.sh; REMOTE_FIREWALL=off in .env leaves it out);
#   7. sets up remote access for the Fieldmap Android app (scripts/remote.sh setup): devices
#      paired on this network can then use the maps from anywhere, and the devices can share
#      positions, markers and messages with each other. Both are off until switched on (Admin,
#      Devices: step 2 for sharing and messages, step 3 for remote connectivity). Devices on this
#      network can install the app from the Devices page, step 1 (scripts/app.sh fetch puts it on
#      the server). --no-remote leaves all of it out;
#   8. checks the pages through the web front, and lists what it changed.
# Then build your maps: on the map builder page, or with build-region.sh.
#
# A machine whose own nginx already serves port 80 (another installer's, for example) uses
# scripts/install-nginx.sh instead.

set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
trap 'echo "install.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
die() { echo "Error: $*" >&2; exit 1; }

ARGS=("$@")
BUILDER=yes
REMOTE=yes
for arg in "$@"; do
    case "$arg" in
        --no-builder) BUILDER=no ;;
        --no-remote) REMOTE=no ;;
        *) echo "Usage: $0 [--no-builder] [--no-remote]" >&2; exit 1 ;;
    esac
done

# The owner of this folder runs everything, the map builder service included.
OWNER="$(stat -c %U "$REPO_DIR")"
if [ "$(id -un)" != "$OWNER" ]; then
    die "run this script as $OWNER, the owner of $REPO_DIR, without sudo. It asks for sudo itself where it needs it."
fi
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO=sudo

# What this run changed: printed at the end (remote.sh, firewall.sh add theirs, as root too: so
# in a folder of its own, not straight in /tmp, where root may not write to a user's file). Kept
# across the restart of this script in the docker group (below). FIELDMAP_CHANGES_DIR is the
# folder made here: the only one removed at the end, never one a caller's file is in.
if [ -z "${FIELDMAP_CHANGES:-}" ] || [ ! -f "$FIELDMAP_CHANGES" ]; then
    FIELDMAP_CHANGES_DIR="$(mktemp -d)"
    FIELDMAP_CHANGES="$FIELDMAP_CHANGES_DIR/changes"
    : > "$FIELDMAP_CHANGES"
fi
export FIELDMAP_CHANGES FIELDMAP_CHANGES_DIR="${FIELDMAP_CHANGES_DIR:-}"
changed() { echo "Changed: $1"; echo "$1" >> "$FIELDMAP_CHANGES"; }
# The last run's sums of what goes into each part, to tell whether it changed since.
STATE=/var/lib/fieldmap-server
stamp() { $SUDO cat "$STATE/$1" 2>/dev/null || true; }
stamp_save() { $SUDO mkdir -p "$STATE" && echo "$2" | $SUDO tee "$STATE/$1" >/dev/null; }
sum_of() { { { find "$@" -type f -print0 2>/dev/null || true; } | sort -z | xargs -0r sha256sum; } | sha256sum | cut -c1-64; }

# From .env, as Docker Compose and admin.sh read it. Arguments: NAME DEFAULT
setting() {
    local value=""
    if [ -f .env ]; then
        value="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    fi
    echo "${value:-$2}"
}
[ -z "$(setting FIELDMAP_NGINX_DIR "")" ] ||
    die ".env names FIELDMAP_NGINX_DIR: this folder was installed beside the machine's own nginx. Update it with: sudo ./scripts/install-nginx.sh"

if [ -z "${FIELDMAP_IN_DOCKER_GROUP:-}" ]; then
    echo "== What this machine needs"
    if command -v apt-get >/dev/null; then
        # Never asks; waits for the system's own package updates (a new machine runs them at first
        # start); Ubuntu's needrestart restarts no service after these installs.
        apt_get() { $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 apt-get -o DPkg::Lock::Timeout=300 "$@"; }
        need=()
        command -v python3 >/dev/null || need+=(python3)
        command -v curl >/dev/null || need+=(curl)
        command -v docker >/dev/null || need+=(docker.io)
        if [ ${#need[@]} -gt 0 ]; then
            apt_get update -qq
            apt_get install -y -qq "${need[@]}" >/dev/null
            changed "installed ${need[*]}"
        fi
        # Docker Compose v2 ("docker compose"): Debian's docker-compose package, or Ubuntu's
        # docker-compose-v2 (Ubuntu's docker-compose is the old v1). And buildx, for the builds.
        if ! $SUDO docker compose version >/dev/null 2>&1; then
            apt-cache show docker-compose-v2 >/dev/null 2>&1 && pkg=docker-compose-v2 || pkg=docker-compose
            apt_get install -y -qq "$pkg" >/dev/null
            changed "installed Docker Compose ($pkg)"
        fi
        if ! $SUDO docker buildx version >/dev/null 2>&1 && apt-cache show docker-buildx >/dev/null 2>&1; then
            apt_get install -y -qq docker-buildx >/dev/null
            changed "installed docker-buildx"
        fi
    fi
    for tool in docker python3 curl; do
        command -v "$tool" >/dev/null || die "$tool is not installed (this script installs it on Debian, Ubuntu and Raspberry Pi OS)."
    done
    $SUDO docker compose version >/dev/null 2>&1 ||
        die "'docker compose' (Docker Compose v2) does not work on this machine. Install Docker with Compose v2 (see docs.docker.com), then run this again."
    if command -v systemctl >/dev/null && [ -d /run/systemd/system ] && ! systemctl is-active -q docker; then
        $SUDO systemctl enable -q --now docker
        changed "started Docker"
    fi
    # Docker for $OWNER: the docker group. A new member gets it at the next login; until then this
    # script carries on in the group (sg).
    if [ "$OWNER" != root ] && ! id -nG "$OWNER" | tr ' ' '\n' | grep -qx docker; then
        $SUDO usermod -aG docker "$OWNER"
        changed "added $OWNER to the docker group (log out and in again to use docker yourself)"
    fi
    if ! docker info >/dev/null 2>&1 && [ "$OWNER" != root ] && command -v sg >/dev/null; then
        export FIELDMAP_IN_DOCKER_GROUP=1
        exec sg docker -c "$(printf '%q ' "$0" "${ARGS[@]}")"
    fi
fi
docker info >/dev/null 2>&1 || die "Docker is not running, or $OWNER may not use it (sudo usermod -aG docker $OWNER, then log out and in)."

# Raspberry Pi kernels ship with the memory cgroup off, and without it Docker drops the tile
# server's memory cap. Turn it on in cmdline.txt (from the next boot) and, because Docker keeps a
# container's dropped cap across restarts, recreate the tile server once after that boot.
NEED_REBOOT=no
if docker info 2>&1 | grep -qi 'no memory limit support'; then
    CMDLINE=""
    for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
        [ -f "$f" ] && { CMDLINE="$f"; break; }
    done
    if [ -z "$CMDLINE" ]; then
        echo "WARNING: this kernel has no memory cgroup, so the tile server's memory cap will not work." >&2
    else
        [ -f "$CMDLINE.fieldmap.bak" ] || $SUDO cp "$CMDLINE" "$CMDLINE.fieldmap.bak"
        for opt in cgroup_enable=memory cgroup_memory=1; do
            grep -qw -- "$opt" "$CMDLINE" || { $SUDO sed -i "1 s/\$/ $opt/" "$CMDLINE"; changed "$CMDLINE: $opt"; }
        done
        unit="$(cat <<EOF
# One-time job from fieldmap-server's install.sh: recreate the tile server so it
# gets its memory cap now that the memory cgroup is on, then remove this job.
[Unit]
Description=Apply the fieldmap-server tile server memory cap after reboot
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/docker compose up -d --force-recreate tileserver
ExecStartPost=/bin/systemctl disable fieldmap-memcap.service
ExecStartPost=/bin/rm -f /etc/systemd/system/fieldmap-memcap.service

[Install]
WantedBy=multi-user.target
EOF
)"
        if [ "$unit" != "$(cat /etc/systemd/system/fieldmap-memcap.service 2>/dev/null)" ]; then
            printf '%s\n' "$unit" | $SUDO tee /etc/systemd/system/fieldmap-memcap.service >/dev/null
            $SUDO systemctl daemon-reload
            $SUDO systemctl enable -q fieldmap-memcap.service
            changed "/etc/systemd/system/fieldmap-memcap.service"
        fi
        NEED_REBOOT=yes
    fi
fi

WEB_PORT="$(setting WEB_PORT 80)"
TILE_PORT="$(setting TILESERVER_PORT 8080)"
# Ports already taken by something else (another web server): say so
# now, rather than as a container that cannot start.
for pair in "$WEB_PORT:fieldmap-web" "$TILE_PORT:tileserver"; do
    port="${pair%%:*}"; name="${pair#*:}"
    if [ -n "$(ss -Hltn "sport = :$port" 2>/dev/null)" ] && [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != true ]; then
        die "port $port on this machine is taken by another program ($($SUDO ss -Hltnp "sport = :$port" | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2)). Stop it, or choose another port in .env (WEB_PORT / TILESERVER_PORT). A machine whose own nginx serves port 80 uses scripts/install-nginx.sh instead."
    fi
done

# An earlier install beside the machine's own nginx (install-nginx.sh, or install-pi.sh behind
# Internet-in-a-Box) in this same folder: take its containers, service and nginx snippets away, so
# that the two do not run side by side. Its data/ and .env stay as they are.
if [ -f docker-compose.pi.yml ]; then
    echo "== Leftovers of the install beside the machine's own nginx"
    docker compose -f docker-compose.pi.yml --profile remote --profile netbird down >/dev/null 2>&1 || true
    rm -f docker-compose.pi.yml
    if [ -f /etc/systemd/system/fieldmap-wmm.service ]; then
        $SUDO systemctl disable -q --now fieldmap-wmm 2>/dev/null || true
        $SUDO rm -f /etc/systemd/system/fieldmap-wmm.service
        $SUDO systemctl daemon-reload
    fi
    removed=""
    for f in mapprinter-nginx.conf maps-nginx.conf routing-nginx.conf mapremote-nginx.conf; do
        for dir in /etc/nginx/conf.d /etc/nginx/fieldmap.d; do
            if [ -f "$dir/$f" ]; then $SUDO rm -f "$dir/$f"; removed="yes"; fi
        done
    done
    if [ -n "$removed" ] && command -v nginx >/dev/null && systemctl is-active -q nginx 2>/dev/null; then
        $SUDO nginx -t -q && $SUDO systemctl reload nginx
    fi
    changed "removed the earlier install beside the machine's own nginx (its containers, fieldmap-wmm, nginx snippets)"
fi
# That install's Docker settings (pi/docker/daemon.json: no network of Docker's own) would stop
# the containers here, which have their own network. Only when the file is exactly that one.
if cmp -s pi/docker/daemon.json /etc/docker/daemon.json; then
    $SUDO rm -f /etc/docker/daemon.json
    $SUDO systemctl restart docker
    changed "removed /etc/docker/daemon.json of the install beside nginx, and restarted Docker"
fi

echo "== Data files"
before="$(ls data)"
./scripts/data-placeholders.sh
[ "$(ls data)" = "$before" ] || changed "placeholder files in data/"
# Docker makes a mounted folder that is missing, owned by root: an earlier `docker compose up`
# may have made data/admin that way, and the map builder could then not use it.
if [ -n "$(find data/admin -maxdepth 0 ! -user "$OWNER" 2>/dev/null)" ]; then
    $SUDO chown -R "$OWNER" data/admin
    changed "data/admin given back to $OWNER"
fi
# The web front mounts the folders of the remote access service and of the sharing service
# (scripts/remote.sh); made here so that they belong to $OWNER and not to root, as Docker
# would make them.
mkdir -p data/remote/run data/share/run

echo "== Tile server, map and route service"
./scripts/mem-limit.sh
# The web front (with the map) and the route service are built here, again only when their sources changed.
SUM="$(sum_of web print/package.json print/package-lock.json print/build.mjs print/src print/public ui guide)"
if [ "$SUM" != "$(stamp web-image)" ] || ! docker image inspect fieldmap-web:local >/dev/null 2>&1; then
    docker compose build -q web
    changed "built the web front and the map (fieldmap-web:local)"
fi
stamp_save web-image "$SUM"
SUM="$(sum_of brouter)"
if [ "$SUM" != "$(stamp brouter-image)" ] || ! docker image inspect fieldmap-brouter:local >/dev/null 2>&1; then
    docker compose build -q brouter
    changed "built the route service (fieldmap-brouter:local)"
fi
stamp_save brouter-image "$SUM"
ids() { docker inspect -f '{{.Name}} {{.Id}}' tileserver fieldmap-web fieldmap-brouter fieldmap-wmm 2>/dev/null || true; }
before="$(ids)"
docker compose up -d --quiet-pull
after="$(ids)"
if [ "$after" != "$before" ]; then
    changed "started or updated: $(diff <(echo "$before") <(echo "$after") | sed -n 's/^> \/\([^ ]*\) .*/\1/p' | paste -sd' ' -)"
fi
# TileServer GL reads config.json, the Maps page, the styles, sprites and the list of data files only
# when it starts. Restart it when any of them changed since the last run (not when `up` just started it).
SUM="$( { cat config.json ui/maps.tmpl styles/*.json sprites/*.json; ls -l --time-style=+%s data/*.mbtiles; } | sha256sum | cut -c1-64)"
if [ "$SUM" != "$(stamp tileserver-inputs)" ]; then
    if [ "$(echo "$after" | grep '^/tileserver ')" = "$(echo "$before" | grep '^/tileserver ')" ]; then
        docker restart tileserver >/dev/null
        changed "restarted the tile server for the current config, styles and data"
    fi
    stamp_save tileserver-inputs "$SUM"
fi

UNIT=/etc/systemd/system/fieldmap-admin.service
echo "== Map Builder page"
# With this file the web front answers "not here" for /mapadmin/, and the other pages'
# header leaves the map builder out instead of calling it "not running".
if [ "$BUILDER" = no ]; then
    [ -e data/admin/no-builder ] || touch data/admin/no-builder
    if [ -f "$UNIT" ]; then
        $SUDO systemctl disable --now fieldmap-admin >/dev/null 2>&1 || true
        $SUDO rm -f "$UNIT"
        $SUDO systemctl daemon-reload
        changed "removed the map builder service"
    else
        echo "Left out (--no-builder)."
    fi
elif ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
    BUILDER=manual
    rm -f data/admin/no-builder
    echo "This machine has no systemd: start the page yourself with ./scripts/admin.sh when you need it."
else
    rm -f data/admin/no-builder
    # Stopping and starting the services of FIELDMAP_PAUSE_UNITS during a build (root's part).
    $SUDO env FIELDMAP_CHANGES="$FIELDMAP_CHANGES" ./scripts/pause-setup.sh "$OWNER"
    # The unit in systemd/, with this folder and its owner filled in.
    unit="$(sed -e "s|/opt/tileserver|$REPO_DIR|g" -e "s|^User=.*|User=$OWNER|" systemd/fieldmap-admin.service)"
    if [ "$unit" != "$(cat "$UNIT" 2>/dev/null)" ]; then
        printf '%s\n' "$unit" | $SUDO tee "$UNIT" >/dev/null
        $SUDO systemctl daemon-reload
        changed "$UNIT"
    fi
    $SUDO systemctl enable -q fieldmap-admin
    SUM="$(sum_of "$UNIT" scripts/admin.sh tools/admin-server.py tools/pause-services.py .env)"
    if [ "$SUM" != "$(stamp fieldmap-admin)" ] || ! systemctl is-active -q fieldmap-admin; then
        $SUDO systemctl restart fieldmap-admin
        changed "restarted fieldmap-admin"
    fi
    stamp_save fieldmap-admin "$SUM"
    echo "The map builder runs as a service (fieldmap-admin), as user $OWNER. It starts at boot."
fi

echo
echo "== Remote access for the Fieldmap app"
if [ "$REMOTE" = yes ]; then
    if ! ./scripts/remote.sh setup; then
        echo "WARNING: remote access for the app is not set up (see above). Everything else works." >&2
        REMOTE=failed
        echo "== The firewall on this machine"
        $SUDO env FIELDMAP_CHANGES="$FIELDMAP_CHANGES" ./scripts/firewall.sh install
    fi
    echo "== The Fieldmap app, for devices to install from the Devices page"
    before="$(sum_of data/app)"
    ./scripts/app.sh fetch || echo "WARNING: the app could not be fetched (see above). Everything else works." >&2
    [ "$(sum_of data/app)" = "$before" ] || changed "the Fieldmap app on this server (data/app)"
elif [ -f /etc/systemd/system/fieldmap-remote.service ]; then
    ./scripts/remote.sh off               # keeps the firewall
    changed "remote access removed (--no-remote)"
else
    [ -e data/remote/run/no-remote ] || touch data/remote/run/no-remote      # no Devices page under Admin
    echo "Left out (--no-remote)."
    echo "== The firewall on this machine"
    $SUDO env FIELDMAP_CHANGES="$FIELDMAP_CHANGES" ./scripts/firewall.sh install
fi

echo "== Checks (through the web front on port $WEB_PORT)"
code() { curl -s -o /dev/null -m 20 -w '%{http_code}' "$1" || true; }
WEB="http://127.0.0.1:$WEB_PORT"
FAILED=()
# Arguments: address, the answers that are right, what it is.
check() {
    local got; got="$(code "$1")"
    if [[ " $2 " == *" $got "* ]]; then echo "  ok    $got  $1  ($3)"
    else echo "  FAIL  $got  $1  ($3; expected $2)" >&2; FAILED+=("$1"); fi
}
# The tile server needs a little while after a (re)start.
for _ in $(seq 60); do [ "$(code "$WEB/styles.json")" = 200 ] && break; sleep 2; done
check "$WEB/styles.json" 200 "tile server, through the web front"
check "http://127.0.0.1:$TILE_PORT/styles.json" 200 "tile server's own port (TAK clients)"
check "$WEB/map/" 200 "the map"
check "$WEB/guide/" 200 "User Guide"
check "$WEB/map/wmm-api/status" 200 "magnetic model service"
check "$WEB/routing/route?profile=car-vario&lonlats=0,0%7C0.001,0.001" "200 404" "route service (404: no routing data there)"
check "$WEB/routing/index.json" "200 404" "routing data (404: none built yet)"
[ "$BUILDER" = yes ] && check "$WEB/mapadmin/" 200 "Map Builder"
if [ "$REMOTE" = yes ]; then
    check "$WEB/phones/" 200 "Devices page"
    check "$WEB/mapremote/info" 200 "pairing, for the app"
    check "$WEB/mapshare/info" 200 "location sharing"
fi

ADDRESS="$(hostname -I 2>/dev/null | cut -d' ' -f1)"
ADDRESS="${ADDRESS:-localhost}"
WEB_PUBLIC="http://$ADDRESS$([ "$WEB_PORT" = 80 ] || echo ":$WEB_PORT")"
echo
if [ -s "$FIELDMAP_CHANGES" ]; then
    echo "What this run changed:"
    sed 's/^/  - /' "$FIELDMAP_CHANGES"
else
    echo "Nothing changed: everything was up to date."
fi
# Only the list and its folder made above, and the folder only if nothing else is in it.
if [ -n "$FIELDMAP_CHANGES_DIR" ] && [ "$FIELDMAP_CHANGES" = "$FIELDMAP_CHANGES_DIR/changes" ]; then
    rm -f "$FIELDMAP_CHANGES"
    rmdir "$FIELDMAP_CHANGES_DIR" 2>/dev/null || true
fi
echo
echo "Ready:"
echo "  Map                     $WEB_PUBLIC/map/"
echo "  Tile server (TAK)       http://$ADDRESS:$TILE_PORT/"
[ "$BUILDER" = yes ] && echo "  Map Builder             $WEB_PUBLIC/mapadmin/"
[ "$REMOTE" = yes ] && echo "  Devices                 $WEB_PUBLIC/phones/"
if [ -z "$(find data -maxdepth 1 -name 'basemap.mbtiles' -size +100k 2>/dev/null)" ]; then
    echo "No maps are installed yet: the map is blank until the first build."
    [ "$BUILDER" = yes ] && echo "Open the Map Builder to select regions and start a build."
fi
if [ "$NEED_REBOOT" = yes ]; then
    echo
    echo "Reboot once (sudo reboot) to turn on the tile server's memory cap. Everything works until then, without the cap."
fi

# A firewall is the owner's to change: say what it needs, do not touch it.
if command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q '^Status: active'; then
    echo
    echo "The ufw firewall is on. If a page does not open from another device, allow its port, for example:"
    for port in "$WEB_PORT" "$TILE_PORT"; do
        echo "  sudo ufw allow from <your-network>/24 to any port $port proto tcp"
    done
fi

[ "$REMOTE" = failed ] && FAILED+=("remote access setup")
if [ ${#FAILED[@]} -gt 0 ]; then
    echo >&2
    echo "Error: fieldmap-server is installed, but these checks failed: ${FAILED[*]}" >&2
    exit 1
fi
