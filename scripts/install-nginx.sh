#!/usr/bin/env bash
# Install fieldmap-server beside a web server of the machine's own: a Raspberry Pi OS / Debian 13 /
# Ubuntu 24.04 machine (arm64 or amd64) whose nginx already serves port 80, typically because
# another installer puts fieldmap-server in as one step. Everywhere else, use scripts/install.sh
# (it brings its own web front, in Docker).
# Run it as root, from anywhere:
#   sudo FIELDMAP_NGINX_DIR=<folder> ./scripts/install-nginx.sh
# It installs Docker if needed, starts TileServer GL and the route service in Docker on
# localhost, builds the map (print/) here with Node.js, installs the Map Builder (/mapadmin/),
# adds its nginx snippets (pi/nginx), and sets up remote access for the Fieldmap app and the
# firewall. Re-run it after `git pull` to
# update. A second run changes nothing and restarts nothing that did not change; every run
# ends with its own checks through nginx and a list of what it changed. It never asks
# anything and never reboots; when something fails it says what, and exits with an error.
#
# Settings, from the environment. They are kept in .env, where the services, Docker Compose and
# the other scripts read them, so later runs need not name them again:
#   FIELDMAP_NGINX_DIR=/path    (needed) where the nginx snippets go: a folder that nginx includes
#                               INSIDE the server block for port 80 (not Debian's /etc/nginx/conf.d,
#                               which is included at http level)
#   FIELDMAP_DATA=/path         the data folder (the .mbtiles files, and the services' folders
#                               wmm/ remote/ share/ app/ routing/), outside this repository;
#                               default data/ in it. Every container and service reads it directly.
#   FIELDMAP_PORTS_FILE=/path   a file for this machine's own firewall, which this one cannot open:
#                               what remote access needs from outside, now (scripts/firewall.sh)
#   FIELDMAP_SERVER_NAME=name   the host name the final checks ask nginx for (default 127.0.0.1)
#   FIELDMAP_USER=name          the user the services run as. Default: the owner of this folder;
#                               with FIELDMAP_DATA, the system user "fieldmap" (made here)
#   FIELDMAP_BUILD_USER=name    the user the Map Builder runs as: it writes the maps into the data
#                               folder and starts Docker containers (so it joins the docker group).
#                               Default: the owner of the data folder (when that is not root)
#   FIELDMAP_PAUSE_UNITS="..."  services the Map Builder stops during a build to free RAM, and
#                               starts again afterwards: systemd services by name, Docker
#                               containers as docker:<name> (see .env.example)
#   REMOTE=no                   leave remote access for the app out (the firewall stays)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
trap 'echo "install-nginx.sh: FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
die() { echo "Error: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run this as root: sudo $0"
export DEBIAN_FRONTEND=noninteractive
# Ubuntu's needrestart restarts no service after a package install of this script.
export NEEDRESTART_SUSPEND=1
# apt waits for the system's own package updates (a new machine runs them at first start).
apt-get() { command apt-get -o DPkg::Lock::Timeout=300 "$@"; }

# What this run changed: printed at the end (remote.sh, firewall.sh and nginx-snippets.sh add theirs).
FIELDMAP_CHANGES="$(mktemp)"
export FIELDMAP_CHANGES
trap 'rm -f "$FIELDMAP_CHANGES"' EXIT
changed() { echo "Changed: $1"; echo "$1" >> "$FIELDMAP_CHANGES"; }
# The last run's sums of what goes into each part, to tell whether it changed since.
STATE=/var/lib/fieldmap-server
mkdir -p "$STATE"
stamp() { cat "$STATE/$1" 2>/dev/null || true; }
stamp_save() { echo "$2" > "$STATE/$1"; }
sum_of() { { { find "$@" -type f -print0 2>/dev/null || true; } | sort -z | xargs -0r sha256sum; } | sha256sum | cut -c1-64; }

# From .env; the default when it is not there, or empty.
setting() {
    local value=""
    if [ -f .env ]; then
        value="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    fi
    echo "${value:-$2}"
}
REPO_OWNER="$(stat -c %U "$REPO")"
as_user() { local user="$1"; shift; if [ "$(id -un)" = "$user" ]; then "$@"; else sudo -u "$user" "$@"; fi; }
# Writes NAME=VALUE into .env (which stays its owner's), in place of the line there.
env_set() {
    [ "$(setting "$1" "")" = "$2" ] && return 0
    { grep -v "^$1=" .env 2>/dev/null || true; echo "$1=$2"; } > "$STATE/env.new"
    as_user "$REPO_OWNER" tee .env < "$STATE/env.new" >/dev/null
    rm -f "$STATE/env.new"
    changed ".env: $1=$2"
}

echo "== Settings"
for name in FIELDMAP_DATA FIELDMAP_NGINX_DIR FIELDMAP_PORTS_FILE FIELDMAP_SERVER_NAME FIELDMAP_USER FIELDMAP_BUILD_USER FIELDMAP_PAUSE_UNITS; do
    if [ -n "${!name:-}" ]; then env_set "$name" "${!name}"; fi
done
DATA="$(setting FIELDMAP_DATA "$REPO/data")"
NGINX_DIR="$(setting FIELDMAP_NGINX_DIR "")"
[ -n "$NGINX_DIR" ] || die "FIELDMAP_NGINX_DIR is not set. This installer is for a machine whose own nginx serves port 80: name the folder that its server block includes. Everywhere else (a PC, a Raspberry Pi), run ./scripts/install.sh as the owner of this folder, without sudo."
PORTS_FILE="$(setting FIELDMAP_PORTS_FILE "")"
SERVER_NAME="$(setting FIELDMAP_SERVER_NAME 127.0.0.1)"
# A data folder of its own: the services run as their own user, "fieldmap", unless .env says otherwise.
if [ -n "$(setting FIELDMAP_DATA "")" ] && [ -z "$(setting FIELDMAP_USER "")" ]; then env_set FIELDMAP_USER fieldmap; fi
SVC_USER="$(setting FIELDMAP_USER "$REPO_OWNER")"

case "$DATA" in /*) ;; *) die "FIELDMAP_DATA must be a full path (it is: $DATA)." ;; esac
[ -d "$DATA" ] || die "the data folder $DATA does not exist."
[ -d "$NGINX_DIR" ] || die "the nginx folder $NGINX_DIR does not exist (FIELDMAP_NGINX_DIR)."
command -v nginx >/dev/null || die "nginx is not installed. This installer adds its pages to the nginx that serves port 80."
if [ -n "$PORTS_FILE" ]; then
    case "$PORTS_FILE" in /*) ;; *) die "FIELDMAP_PORTS_FILE must be a full path (it is: $PORTS_FILE)." ;; esac
fi
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
    changed "made the system user $SVC_USER (the services run as it)"
fi
# nginx serves files from this folder (the map, the guide) and from the data folder (the magnetic
# model, the routing data): its user must be able to get to them. A home folder often cannot.
WEB_USER="$(ps -o user= -C nginx 2>/dev/null | grep -vx root | head -1 || true)"
WEB_USER="${WEB_USER:-www-data}"
if id "$WEB_USER" >/dev/null 2>&1; then
    for dir in "$REPO" "$DATA"; do
        if ! sudo -u "$WEB_USER" test -x "$dir"; then
            msg="nginx (user $WEB_USER) cannot get into $dir: a folder above it is closed (a home folder is often mode 700)."
            die "$msg Put it somewhere else, for example /opt/fieldmap-server."
        fi
    done
fi
echo "Data: $DATA. nginx snippets: $NGINX_DIR. Services run as: $SVC_USER."

echo "== Checking data"
# No maps yet: the map is blank until they are built on the Map Builder page, or copied in.
for f in basemap contours hillshade; do
    [ -f "$DATA/$f.mbtiles" ] || echo "No $f.mbtiles in $DATA yet: build it at /mapadmin/ (Map Builder), or copy it in."
done
# Files not built yet (and the optional ones: build-extras.sh, build-usdata.sh); empty ones keep the tile
# server starting without them.
before="$(ls "$DATA")"
owner="$(stat -c %U "$DATA")"; id "$owner" >/dev/null 2>&1 || owner=root
as_user "$owner" ./scripts/data-placeholders.sh
[ "$(ls "$DATA")" = "$before" ] || changed "placeholder files in $DATA"
# The services' folders belong to the user they run as. NetBird's container keeps its own (root's) files.
for dir in wmm remote share app; do
    [ -d "$DATA/$dir" ] || { install -d -o "$SVC_USER" -g "$(id -gn "$SVC_USER")" "$DATA/$dir"; changed "made $DATA/$dir"; }
    if [ -n "$(find "$DATA/$dir" ! -user "$SVC_USER" \( ! -path "$DATA/remote/netbird/*" -o ! -user root \) -print -quit)" ]; then
        find "$DATA/$dir" ! -user "$SVC_USER" \( ! -path "$DATA/remote/netbird/*" -o ! -user root \) -exec chown "$SVC_USER": {} +
        changed "owner of $DATA/$dir: $SVC_USER"
    fi
done

echo "== Ports on 127.0.0.1"
# Taken by something else: say so now, not as a service that cannot start later.
listening="$(ss -Hltn 2>/dev/null | awk '{print $4}')"
ours() {
    case "$1" in
        8080) command -v docker >/dev/null && docker inspect tileserver >/dev/null 2>&1 ;;
        17777) command -v docker >/dev/null && docker inspect fieldmap-brouter >/dev/null 2>&1 ;;
        8091) systemctl is-active -q fieldmap-wmm ;;
        8092) systemctl is-active -q fieldmap-remote ;;
        8093) systemctl is-active -q fieldmap-share ;;
    esac
}
for port in 8080 8091 8092 8093 17777; do
    if echo "$listening" | grep -q ":$port\$" && ! ours "$port"; then
        die "port $port on this machine is taken by: $(ss -Hltnp "sport = :$port" | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2). fieldmap-server needs 8080, 8091, 8092, 8093 and 17777 on 127.0.0.1."
    fi
done
echo "Free, or fieldmap-server's own: 8080 (tile server), 8091 (magnetic model), 8092 (remote access), 8093 (sharing), 17777 (routes)."

echo "== Docker"
# Docker's settings before Docker itself: no network of its own, no firewall rules (the containers
# use the machine's network, and the machine's firewall stays as it is), not even at its first start.
mkdir -p /etc/docker
NEW_DAEMON=no
if [ ! -f /etc/docker/daemon.json ]; then
    install -m 644 pi/docker/daemon.json /etc/docker/daemon.json
    NEW_DAEMON=yes
    changed "/etc/docker/daemon.json"
fi
if ! command -v docker >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq docker.io >/dev/null
    changed "installed Docker (docker.io)"
elif [ "$NEW_DAEMON" = yes ]; then
    systemctl restart docker
fi
# Docker Compose v2 ("docker compose"): Debian's docker-compose package, or Ubuntu's
# docker-compose-v2 (Ubuntu's docker-compose is the old v1). And buildx, for the route service.
if ! docker compose version >/dev/null 2>&1; then
    apt-cache show docker-compose-v2 >/dev/null 2>&1 && pkg=docker-compose-v2 || pkg=docker-compose
    apt-get install -y -qq "$pkg" >/dev/null
    changed "installed Docker Compose ($pkg)"
fi
if ! docker buildx version >/dev/null 2>&1 && apt-cache show docker-buildx >/dev/null 2>&1; then
    apt-get install -y -qq docker-buildx >/dev/null
    changed "installed docker-buildx"
fi
docker compose version >/dev/null 2>&1 || die "'docker compose' (Docker Compose v2) does not work on this machine."
systemctl enable -q --now docker
# A data folder on a disk of its own: Docker starts the containers once it is mounted.
DROPIN=/etc/systemd/system/docker.service.d/fieldmap-data.conf
if [ "$DATA" != "$REPO/data" ]; then
    want="$(printf '%s\n' "# fieldmap-server (scripts/install-nginx.sh): the containers read the data folder." "[Unit]" "RequiresMountsFor=$DATA")"
    if [ "$want" != "$(cat "$DROPIN" 2>/dev/null)" ]; then
        mkdir -p "$(dirname "$DROPIN")"
        printf '%s\n' "$want" > "$DROPIN"
        systemctl daemon-reload
        changed "$DROPIN"
    fi
elif [ -f "$DROPIN" ]; then
    rm -f "$DROPIN"; systemctl daemon-reload; changed "removed $DROPIN"
fi

echo "== Tile server"
as_user "$REPO_OWNER" ./scripts/mem-limit.sh
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
            grep -qw -- "$opt" "$CMDLINE" || { sed -i "1 s/\$/ $opt/" "$CMDLINE"; changed "$CMDLINE: $opt"; }
        done
        unit="$(cat <<EOF
# One-time job from fieldmap-server's install-nginx.sh: recreate the tile server so it
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
)"
        if [ "$unit" != "$(cat /etc/systemd/system/fieldmap-memcap.service 2>/dev/null)" ]; then
            printf '%s\n' "$unit" > /etc/systemd/system/fieldmap-memcap.service
            systemctl daemon-reload
            systemctl enable -q fieldmap-memcap.service
            changed "/etc/systemd/system/fieldmap-memcap.service"
        fi
        NEED_REBOOT=1
        echo "The memory cgroup is on in $CMDLINE (backup: $CMDLINE.fieldmap.bak); the cap starts after a reboot."
    fi
fi
cmp -s pi/tileserver/docker-compose.pi.yml docker-compose.pi.yml || { cp pi/tileserver/docker-compose.pi.yml .; changed "docker-compose.pi.yml"; }
rm -f config-pi.json   # left by older versions, which ran the light image
COMPOSE=(docker compose -f docker-compose.pi.yml)
# The route service is built here (brouter/), again only when its Dockerfile changed; with the
# machine's network, as Docker has none of its own. (Not "docker compose build": newer Compose
# versions stop there to ask whether the build may use the machine's network.)
SUM="$(sum_of brouter)"
if [ "$SUM" != "$(stamp brouter-image)" ] || ! docker image inspect fieldmap-brouter:local >/dev/null 2>&1; then
    docker build -q --network host -t fieldmap-brouter:local brouter >/dev/null
    changed "built the route service's image (fieldmap-brouter:local)"
fi
stamp_save brouter-image "$SUM"
ids() { docker inspect -f '{{.Id}}' tileserver fieldmap-brouter 2>/dev/null || true; }
before="$(ids)"
"${COMPOSE[@]}" up -d --quiet-pull tileserver brouter
[ "$(ids)" = "$before" ] || changed "started the tile server and the route service (tileserver, fieldmap-brouter)"
# TileServer GL reads config.json, the Maps page, the styles, sprites and the list of data files only when
# it starts, and `up -d` leaves a running container alone. Restart it when any of them changed
# since the last run of this script (not when `up` just started it).
STAMP="$DATA/.tileserver-inputs.sha256"
SUM="$( { cat config.json ui/maps.tmpl styles/*.json sprites/*.json; ls -l --time-style=+%s "$DATA"/*.mbtiles; } | sha256sum | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$SUM" ]; then
    if [ "$(ids)" = "$before" ]; then
        docker restart tileserver >/dev/null
        changed "restarted the tile server for the current config, styles and data"
    fi
    echo "$SUM" > "$STAMP"
fi

echo "== Map (print/)"
if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
    apt-get install -y -qq nodejs npm >/dev/null
    changed "installed Node.js and npm"
fi
[ "$(node -p 'process.versions.node.split(".")[0]')" -ge 18 ] || die "Node.js $(node --version) is too old for the map: it needs 18 or newer."
# Built again only when its sources, the shared look (ui/) or Node.js changed.
SUM="$( { node --version; sum_of print/package.json print/package-lock.json print/build.mjs print/src print/public ui/fieldmap.css ui/nav.js; } | sha256sum | cut -c1-64)"
if [ "$SUM" != "$(stamp print)" ] || [ ! -f print/dist/index.html ]; then
    ( cd print && sudo -u "$REPO_OWNER" npm ci --no-audit --no-fund --silent && sudo -u "$REPO_OWNER" node build.mjs )
    changed "built the map (print/dist)"
    stamp_save print "$SUM"
else
    echo "The map is up to date."
fi

echo "== Magnetic model service"
# tools/wmm-update.py serve: the map's "Update magnetic model" button. Runs as $SVC_USER and may
# write only wmm/ in the data folder.
command -v python3 >/dev/null || apt-get install -y -qq python3 >/dev/null
unit="$(sed -e "s#/opt/fieldmap-server/data#$DATA#g" -e "s#/opt/fieldmap-server/#$REPO/#g" -e "s#^User=.*#User=$SVC_USER#" pi/systemd/fieldmap-wmm.service)"
if [ "$unit" != "$(cat /etc/systemd/system/fieldmap-wmm.service 2>/dev/null)" ]; then
    printf '%s\n' "$unit" > /etc/systemd/system/fieldmap-wmm.service
    systemctl daemon-reload
    changed "/etc/systemd/system/fieldmap-wmm.service"
fi
systemctl enable -q fieldmap-wmm.service
SUM="$(sum_of /etc/systemd/system/fieldmap-wmm.service tools/wmm-update.py)"
if [ "$SUM" != "$(stamp fieldmap-wmm)" ] || ! systemctl is-active -q fieldmap-wmm; then
    systemctl restart fieldmap-wmm.service
    changed "restarted fieldmap-wmm"
fi
stamp_save fieldmap-wmm "$SUM"

echo "== Map Builder"
# The page at /mapadmin/ (tools/admin-server.py, run by scripts/admin.sh): it chooses regions and
# layers and runs the builds, which write the maps into the data folder and start Docker
# containers. So it runs as a user who owns the data folder, in the docker group.
BUILDER=yes
data_owner="$(stat -c %U "$DATA")"
BUILD_USER="$(setting FIELDMAP_BUILD_USER "")"
if [ -z "$BUILD_USER" ]; then
    if [ "$data_owner" != root ] && id "$data_owner" >/dev/null 2>&1; then BUILD_USER="$data_owner"; else BUILD_USER="$SVC_USER"; fi
fi
if ! sudo -u "$BUILD_USER" test -w "$DATA"; then
    echo "WARNING: the Map Builder is not installed: its user, $BUILD_USER, cannot write to $DATA (owner: $data_owner)." >&2
    echo "         Set FIELDMAP_BUILD_USER to the owner of the data folder, or give $BUILD_USER that folder." >&2
    BUILDER=failed
else
    if ! id -nG "$BUILD_USER" | tr ' ' '\n' | grep -qx docker; then
        usermod -aG docker "$BUILD_USER"
        changed "added $BUILD_USER to the docker group (the Map Builder starts the build's containers)"
    fi
    [ -d "$DATA/admin" ] || { install -d -m 755 -o "$BUILD_USER" -g "$(id -gn "$BUILD_USER")" "$DATA/admin"; changed "made $DATA/admin"; }
    if [ -n "$(find "$DATA/admin" -maxdepth 0 ! -user "$BUILD_USER")" ]; then
        chown -R "$BUILD_USER": "$DATA/admin"
        changed "owner of $DATA/admin: $BUILD_USER"
    fi
    # Stopping and starting the services of FIELDMAP_PAUSE_UNITS during a build (root's part).
    ./scripts/pause-setup.sh "$BUILD_USER"
    unit="$(sed -e "s#/opt/tileserver/data#$DATA#g" -e "s#/opt/tileserver#$REPO#g" -e "s#^User=.*#User=$BUILD_USER#" systemd/fieldmap-admin.service)"
    if [ "$unit" != "$(cat /etc/systemd/system/fieldmap-admin.service 2>/dev/null)" ]; then
        printf '%s\n' "$unit" > /etc/systemd/system/fieldmap-admin.service
        systemctl daemon-reload
        changed "/etc/systemd/system/fieldmap-admin.service"
    fi
    systemctl enable -q fieldmap-admin.service
    SUM="$(sum_of /etc/systemd/system/fieldmap-admin.service scripts/admin.sh tools/admin-server.py tools/pause-services.py .env)"
    if [ "$SUM" != "$(stamp fieldmap-admin)" ] || ! systemctl is-active -q fieldmap-admin; then
        systemctl restart fieldmap-admin.service
        changed "restarted fieldmap-admin"
    fi
    stamp_save fieldmap-admin "$SUM"
    echo "The Map Builder runs as a service (fieldmap-admin), as user $BUILD_USER."
fi

echo "== nginx ($NGINX_DIR)"
SNIPPETS="$(mktemp -d)"
SNIPPET_FILES=(mapprinter-nginx.conf maps-nginx.conf routing-nginx.conf)
[ "$BUILDER" != yes ] || SNIPPET_FILES+=(mapadmin-nginx.conf)
for f in "${SNIPPET_FILES[@]}"; do
    sed -e "s#/opt/fieldmap-server/data/#$DATA/#g" -e "s#/opt/fieldmap-server/#$REPO/#g" "pi/nginx/$f" > "$SNIPPETS/$f"
done
./scripts/nginx-snippets.sh "$SNIPPETS" "$NGINX_DIR" || { rm -rf "$SNIPPETS"; die "the nginx snippets were not installed (see above)."; }
rm -rf "$SNIPPETS"

if [ "${REMOTE:-yes}" != no ]; then
    echo "== Remote access for the Fieldmap app"
    # Phones paired on this network can then use the maps from anywhere, and phones can share
    # positions and markers through the box (scripts/remote.sh). Both are off until switched on.
    if ! ./scripts/remote.sh setup; then
        echo "WARNING: remote access for the app is not set up (see above). Everything else works." >&2
        REMOTE=failed
        echo "== The firewall on this box"
        ./scripts/firewall.sh install
    fi
    echo "== The Fieldmap app, for devices to install from the Devices page"
    before="$(sum_of "$DATA/app")"
    ./scripts/app.sh fetch || echo "WARNING: the app could not be fetched (see above). Everything else works." >&2
    [ "$(sum_of "$DATA/app")" = "$before" ] || changed "the Fieldmap app on this server ($DATA/app)"
else
    if [ ! -e "$DATA/remote/run/no-remote" ]; then
        sudo -u "$SVC_USER" mkdir -p "$DATA/remote/run" && sudo -u "$SVC_USER" touch "$DATA/remote/run/no-remote"
        changed "remote access left out (REMOTE=no)"
    fi
    echo "== The firewall on this box"
    # Nothing from outside the local network and VPNs gets in, or gets an answer.
    ./scripts/firewall.sh install
fi

echo "== Checks (through nginx on 127.0.0.1, as $SERVER_NAME)"
code() { curl -s -o /dev/null -m 20 -w '%{http_code}' -H "Host: $SERVER_NAME" "http://127.0.0.1$1" || true; }
FAILED=()
# Arguments: address, the answers that are right, what it is.
check() {
    local got; got="$(code "$1")"
    if [[ " $2 " == *" $got "* ]]; then echo "  ok    $got  $1  ($3)"
    else echo "  FAIL  $got  $1  ($3; expected $2)" >&2; FAILED+=("$1"); fi
}
# The tile server needs a little while after a (re)start.
for _ in $(seq 60); do [ "$(code /maps/styles.json)" = 200 ] && break; sleep 2; done
check /maps/styles.json 200 "tile server"
check /maps/styles/topo-light/ 200 "a style's own page"
check /map/ 200 "the map"
check /mapprinter/ 301 "the old map printer address, sent on to /map/"
check /guide/ 200 "User Guide"
check /map/wmm-api/status 200 "magnetic model service"
# The page answers once its service is up (a moment after a restart).
if [ "$BUILDER" = yes ]; then
    for _ in $(seq 15); do [ "$(code /mapadmin/)" = 200 ] && break; sleep 1; done
    check /mapadmin/ 200 "Map Builder"
fi
check "/routing/route?profile=car-vario&lonlats=0,0%7C0.001,0.001" "200 404" "route service"
got="$(code /routing/index.json)"
case "$got" in
    200) echo "  ok    200  /routing/index.json  (routing data)" ;;
    404) echo "  ok    404  /routing/index.json  (no routing data in $DATA/routing yet: scripts/build-routing.sh)" ;;
    *) echo "  FAIL  $got  /routing/index.json  (routing data; expected 200, or 404 before a build)" >&2; FAILED+=(/routing/index.json) ;;
esac
if [ "${REMOTE:-yes}" = yes ]; then
    check /phones/ 200 "Devices page"
    check /mapremote/info 200 "pairing, for the app"
    check /mapshare/info 200 "location sharing"
fi

echo
if [ -s "$FIELDMAP_CHANGES" ]; then
    echo "What this run changed:"
    sed 's/^/  - /' "$FIELDMAP_CHANGES"
else
    echo "Nothing changed: everything was up to date."
fi
if [ "${REMOTE:-yes}" = failed ]; then FAILED+=("remote access setup"); fi
if [ "$BUILDER" = failed ]; then FAILED+=("Map Builder setup"); fi
if [ ${#FAILED[@]} -gt 0 ]; then
    echo >&2
    echo "Error: fieldmap-server is installed, but these checks failed: ${FAILED[*]}" >&2
    exit 1
fi
echo
echo "Done. Map: http://<server>/map/"
if [ "$BUILDER" = yes ]; then
    echo "Map Builder: http://<server>/mapadmin/"
    if [ -z "$(setting ADMIN_PIN "")" ]; then
        echo "Note: everyone who can reach this server can open the Map Builder and replace the maps. To make it ask"
        echo "      for an Access Code, set ADMIN_PIN=<code> in $REPO/.env and run this script again."
    fi
fi
echo "TAK clients: http://<server>/maps/styles/topo-light/{z}/{x}/{y}.jpg"

if [ "$NEED_REBOOT" = 1 ]; then
    echo
    echo "Reboot once (sudo reboot) to turn on the tile server's memory cap. Everything works until then, without the cap."
fi
