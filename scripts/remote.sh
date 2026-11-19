#!/usr/bin/env bash
# Remote access for the Fieldmap Android app: a phone paired on this box's own network can use
# the maps from anywhere afterwards, and nobody else can. And live sharing between the apps:
# positions and shared markers, for the phones at home and the paired ones outside. install.sh
# and install-pi.sh run "setup" for you; nothing here needs an account or an outside service.
#
# Usage:
#   ./scripts/remote.sh setup          set it up, or bring it up to date (run by the installers)
#   ./scripts/remote.sh status         can phones reach the box from outside, and how
#   ./scripts/remote.sh pair           open pairing for ten minutes (same as the Devices page button)
#   ./scripts/remote.sh devices        the paired phones
#   ./scripts/remote.sh remove <id>    lock one out
#   ./scripts/remote.sh netbird-forget take NetBird off the box: its setup key and its place in the NetBird network
#   ./scripts/remote.sh off            remove it: the gateway, the services, the Devices page (the box's
#                                      firewall stays, closed to everything from outside)
#                                      (to switch remote access off and on: Admin, Remote access; sharing: Admin, Devices)
#
# On a Raspberry Pi behind Internet-in-a-Box, run it with sudo, like install-pi.sh.
# Elsewhere run it as the owner of this folder; it asks for sudo where it needs it.
#
# What "setup" does:
#   1. makes this box its own certificate authority and writes the gateway's configuration
#      (tools/remote-server.py init; data/remote/ stays on this box);
#   2. starts the service that pairs phones and keeps the router's port open
#      (fieldmap-remote, a systemd service): the Devices page at /phones/, and /mapremote/ for the app;
#   3. starts the service for live sharing (fieldmap-share, tools/share-server.py; /mapshare/
#      for the app), which takes and gives nothing until it is switched on on the Devices page;
#   4. starts the gateway, an nginx container that demands a certificate this box issued and
#      serves only what the app needs, and updates it every week (fieldmap-remote-update);
#   5. puts a firewall on the box itself that lets nothing in from outside the local network and
#      VPNs but the gateway, and answers nothing else (scripts/firewall.sh, fieldmap-firewall).
#
# NetBird (netbird.io) is the other way in, for a box whose router cannot open a port (Starlink,
# a router nobody can change). Nothing of it is on the box until the owner types a setup key on
# the Remote access page. Then "netbird-apply" (run as root by fieldmap-netbird.path, which
# "setup" installs) downloads NetBird's container and starts it; see tools/netbird-run.sh and
# the "NetBird" part of tools/remote-server.py.
#
# Settings, in .env (see .env.example), all optional:
#   REMOTE_PORT            the gateway's port on this machine (chosen at random by the first setup)
#   REMOTE_PIN             a code that pairing then asks for
#   REMOTE_ADDRESS         host:port of the gateway from outside that you set up yourself
#   REMOTE_DIRECT=off      do not ask the router to open a port
#   REMOTE_FIREWALL=off    no firewall on the box
#   REMOTE_FIREWALL_ALLOW  ports open from outside besides the gateway (none by default; 22 for SSH)
#   NETBIRD_IMAGE          NetBird's container image (netbirdio/netbird:latest)
#   REMOTE_RELAY=tailscale also make the gateway reachable through Tailscale Funnel (needs the box
#                          signed in to a Tailscale account: sudo tailscale up)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

setting() {
    local value=""
    if [ -f .env ]; then
        value="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    fi
    echo "${value:-$2}"
}

OWNER="$(stat -c %U "$REPO_DIR")"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO=sudo
# The files belong to the owner of this folder, whoever runs the script.
as_owner() { if [ "$(id -un)" = "$OWNER" ]; then "$@"; else sudo -u "$OWNER" "$@"; fi; }

# The gateway's port on this machine: chosen at random by the first "setup" and kept in .env
# (an uncommon port: nothing else on the box takes it, and it is not one scanners try first).
REMOTE_PORT="$(setting REMOTE_PORT "")"
SERVICE_PORT="$(setting REMOTE_SERVICE_PORT 8092)"
SHARE_PORT="$(setting SHARE_SERVICE_PORT 8093)"
RELAY="$(setting REMOTE_RELAY "")"
# A Raspberry Pi behind Internet-in-a-Box (install-pi.sh copied this file here): the tile
# server is on this machine's port 8080, the web server shows it at /maps/, and the gateway
# shares the machine's network.
if [ -f docker-compose.pi.yml ]; then
    PI=yes; COMPOSE=(docker compose -f docker-compose.pi.yml)
    INIT=(--prefix /maps/ --upstream http://127.0.0.1:8080/ --listen "$REMOTE_PORT" --ipv6)
    LOCAL="http://127.0.0.1/mapremote/info"
    LOCAL_SHARE="http://127.0.0.1/mapshare/info"
else
    PI=no; COMPOSE=(docker compose)
    INIT=(--prefix / --upstream http://tileserver:8080/ --listen 8443)
    LOCAL="http://127.0.0.1:$(setting WEB_PORT 80)/mapremote/info"
    LOCAL_SHARE="http://127.0.0.1:$(setting WEB_PORT 80)/mapshare/info"
fi
# Published on every address of this machine, IPv4 and IPv6; with only a relay, on this machine alone.
export REMOTE_PUBLISH="$REMOTE_PORT"
[ "$RELAY" = tailscale ] && [ "$(setting REMOTE_DIRECT on)" = off ] && REMOTE_PUBLISH="127.0.0.1:$REMOTE_PORT"

server() { as_owner env REMOTE_PIN="$(setting REMOTE_PIN "")" REMOTE_ADDRESS="$(setting REMOTE_ADDRESS "")" \
    REMOTE_PORT="$REMOTE_PORT" REMOTE_DIRECT="$(setting REMOTE_DIRECT on)" python3 tools/remote-server.py "$@"; }
# The running service, through its socket: the same requests the Maps page makes.
ask() { curl -fsS -m 10 --unix-socket data/remote/run/remote.sock "http://box/$1" "${@:2}"; }

# A systemd unit from systemd/, with this folder and its owner filled in.
install_unit() {
    sed -e "s|/opt/fieldmap-server|$REPO_DIR|g" -e "s|^User=fieldmap$|User=$OWNER|" "systemd/$1" | $SUDO tee "/etc/systemd/system/$1" >/dev/null
}

# NetBird, as the Remote access page left it (data/remote/netbird/client.env, written by the service).
NETBIRD_DIR=data/remote/netbird
netbird_setting() { { grep -m1 "^$1=" "$NETBIRD_DIR/client.env" 2>/dev/null || true; } | cut -d= -f2-; }

# Stops NetBird's client the slow way, so that it takes its interface down itself; an interface
# that a killed client left behind is taken away here.
netbird_stop() {
    docker stop -t 20 fieldmap-netbird >/dev/null 2>&1 || true
    docker rm -f fieldmap-netbird >/dev/null 2>&1 || true
    if ip link show nb-fieldmap >/dev/null 2>&1; then $SUDO ip link delete nb-fieldmap 2>/dev/null || true; fi
}

tailscale_self() {
    tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
    print(s.get("BackendState", "?"), (s.get("Self") or {}).get("DNSName", "").rstrip("."))
except Exception:
    print("? ")'
}

case "${1:-}" in
serve)
    # What the systemd service runs.
    export REMOTE_PIN="$(setting REMOTE_PIN "")" REMOTE_ADDRESS="$(setting REMOTE_ADDRESS "")" \
        REMOTE_PORT REMOTE_DIRECT="$(setting REMOTE_DIRECT on)"
    if [ "$PI" = yes ]; then exec python3 tools/remote-server.py serve --port "$SERVICE_PORT"; fi
    exec python3 tools/remote-server.py serve
    ;;

share-serve)
    # What the systemd service for live sharing runs. It gets the box's code and nothing else.
    export REMOTE_PIN="$(setting REMOTE_PIN "")"
    if [ "$PI" = yes ]; then exec python3 tools/share-server.py serve --port "$SHARE_PORT"; fi
    exec python3 tools/share-server.py serve
    ;;

setup)
    for tool in python3 openssl docker curl; do
        command -v "$tool" >/dev/null || { echo "Error: $tool is not installed." >&2; exit 1; }
    done
    if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
        echo "This machine has no systemd: remote access for the app needs it. Skipped." >&2
        exit 2
    fi
    if [ -z "$REMOTE_PORT" ]; then
        # A port between 20000 and 60999 that nothing on this machine listens on.
        REMOTE_PORT="$(python3 -c '
import random, socket, subprocess
used = subprocess.run(["ss", "-Hltn"], capture_output=True, text=True).stdout
for _ in range(200):
    port = random.randint(20000, 60999)
    if ":%d " % port in used:
        continue
    s = socket.socket()
    try:
        s.bind(("", port)); print(port); break
    except OSError:
        continue
    finally:
        s.close()')"
        [ -n "$REMOTE_PORT" ] || { echo "Error: found no free port for the gateway." >&2; exit 1; }
        echo "REMOTE_PORT=$REMOTE_PORT" | as_owner tee -a .env >/dev/null
        echo "The gateway's port on this box: $REMOTE_PORT (kept in .env as REMOTE_PORT)."
    fi
    export REMOTE_PUBLISH="$REMOTE_PORT"
    [ "$RELAY" = tailscale ] && [ "$(setting REMOTE_DIRECT on)" = off ] && REMOTE_PUBLISH="127.0.0.1:$REMOTE_PORT"
    if [ "$PI" = yes ]; then INIT=(--prefix /maps/ --upstream http://127.0.0.1:8080/ --listen "$REMOTE_PORT" --ipv6); fi

    echo "== Certificates and the gateway's configuration"
    server init "${INIT[@]}"
    as_owner rm -f data/remote/run/no-remote      # the Devices page is there (web/nginx.conf)

    echo "== The service that pairs phones and keeps the router's port open"
    install_unit fieldmap-remote.service
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q fieldmap-remote
    $SUDO systemctl restart fieldmap-remote

    echo "== The service for live sharing between the apps"
    # Its folder belongs to the owner of this one (Docker makes a mounted folder that is
    # missing, as root: give that back).
    if [ -n "$(find data/share -maxdepth 1 ! -user "$OWNER" 2>/dev/null)" ]; then $SUDO chown -R "$OWNER" data/share; fi
    as_owner mkdir -p data/share/run
    install_unit fieldmap-share.service
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q fieldmap-share
    # Remote access does not depend on it: if it does not start, the rest is still set up.
    $SUDO systemctl restart fieldmap-share || echo "WARNING: the sharing service did not start. See: journalctl -u fieldmap-share" >&2

    echo "== The gateway"
    "${COMPOSE[@]}" --profile remote pull -q remote >/dev/null 2>&1 || true
    "${COMPOSE[@]}" --profile remote up -d --quiet-pull remote
    docker restart fieldmap-remote >/dev/null      # read the current certificates and configuration
    if [ "$PI" = yes ]; then
        sed -e "s#127.0.0.1:8092#127.0.0.1:$SERVICE_PORT#" -e "s#127.0.0.1:8093#127.0.0.1:$SHARE_PORT#" pi/nginx/mapremote-nginx.conf | $SUDO tee /etc/nginx/conf.d/mapremote-nginx.conf >/dev/null
        $SUDO nginx -t -q && $SUDO systemctl reload nginx
    fi
    install_unit fieldmap-remote-update.service
    install_unit fieldmap-remote-update.timer
    install_unit fieldmap-remote-apply.service
    install_unit fieldmap-remote-apply.path
    as_owner mkdir -p "$NETBIRD_DIR" && as_owner chmod 700 "$NETBIRD_DIR"      # fieldmap-netbird.path watches a file in it
    install_unit fieldmap-netbird.service
    install_unit fieldmap-netbird.path
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q --now fieldmap-remote-update.timer fieldmap-remote-apply.path fieldmap-netbird.path

    # NetBird, where the owner set it up before: bring it up to date with the rest.
    if [ -f "$NETBIRD_DIR/client.env" ]; then
        echo "== NetBird"
        $SUDO "$0" netbird-apply || echo "WARNING: NetBird did not start (see above). Everything else works." >&2
    fi

    echo "== The firewall on this box"
    # The gateway's port is open only while remote access is switched on.
    $SUDO ./scripts/firewall.sh install

    sleep 3
    if ! curl -fs -m 5 "$LOCAL" | grep -q fieldmap-remote; then
        echo "WARNING: $LOCAL does not answer, so phones cannot pair yet." >&2
        [ "$PI" = yes ] || echo "         Run ./scripts/install.sh (it rebuilds the web front), then this again." >&2
    fi
    if ! curl -fs -m 5 "$LOCAL_SHARE" | grep -q '"enabled"'; then
        echo "WARNING: $LOCAL_SHARE does not answer, so phones cannot share positions and markers yet." >&2
        echo "         See: journalctl -u fieldmap-share" >&2
    fi
    code="$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "https://127.0.0.1:$REMOTE_PORT/" || true)"
    if [ "$code" = 400 ]; then echo "The gateway is up and turns away a visitor without a certificate."
    else echo "WARNING: the gateway on port $REMOTE_PORT answered '$code' to a visitor without a certificate (expected 400). See: docker logs fieldmap-remote" >&2; fi

    if [ "$RELAY" = tailscale ]; then
        echo "== Tailscale Funnel (REMOTE_RELAY=tailscale)"
        read -r state name <<< "$(tailscale_self)"
        if ! command -v tailscale >/dev/null || [ "$state" != Running ] || [ -z "$name" ]; then
            echo "This box is not signed in to Tailscale: run 'sudo tailscale up', then this again." >&2
        else
            $SUDO tailscale funnel --bg --tcp=443 "tcp://localhost:$REMOTE_PORT"
            echo "$name:443" | as_owner tee data/remote/address >/dev/null
        fi
    else
        as_owner rm -f data/remote/address
    fi

    echo
    "$0" status || true
    echo
    if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("enabled") is True else 1)' \
        data/remote/settings.json 2>/dev/null; then
        echo "Remote access is on. To pair another device, open Admin, Devices on this box's network (in the"
        echo "header of the Maps page); to switch it off, Admin, Remote access."
    else
        echo "Remote access is off until someone switches it on: on this box's network open Admin, Remote access"
        echo "(in the header of the Maps page) and press Switch on. That also opens pairing for ten minutes:"
        echo "then, in the Fieldmap app on the phone, Menu, Box, Pair this device."
    fi
    echo "Sharing positions and markers between the devices has its own switch, on Admin, Devices; it is off"
    echo "until someone switches it on."
    ;;

status)
    server status
    as_owner python3 tools/share-server.py status
    echo "Service:        $(systemctl is-active fieldmap-remote 2>/dev/null || true)"
    echo "Share service:  $(systemctl is-active fieldmap-share 2>/dev/null || true)"
    echo "Gateway:        $(docker inspect -f '{{.State.Status}}' fieldmap-remote 2>/dev/null || echo 'not started')"
    echo "NetBird client: $(docker inspect -f '{{.State.Status}}' fieldmap-netbird 2>/dev/null | grep . || echo 'not on this box')"
    echo "Firewall:       $(systemctl is-active fieldmap-firewall.timer 2>/dev/null | sed 's/^active$/on/;s/^inactive$/off/' || true)"
    ;;

pair)
    ask pairing -H 'Content-Type: application/json' -d '{"open": true}' >/dev/null
    echo "Pairing is open for ten minutes. In the Fieldmap app, on this network: Menu, Box, Pair this device."
    ;;

devices) server devices ;;

remove)
    [ -n "${2:-}" ] || { echo "Usage: $0 remove <id>   (the ids are in: $0 devices)" >&2; exit 1; }
    server remove "$2"
    ;;

netbird-apply)
    # Makes the NetBird container what the Remote access page asked for: running (downloaded
    # first, the first time) or gone. Run as root by fieldmap-netbird.path when the service
    # touches data/remote/netbird/apply-request, and by "setup".
    want="$(netbird_setting FIELDMAP_NETBIRD)"
    management="$(netbird_setting NB_MANAGEMENT_URL)"
    if [ "$want" = on ] && [ -n "$management" ]; then
        # Another server than the one this box joined: its place there is of no use here.
        if [ -f "$NETBIRD_DIR/state/.management" ] && [ "$(cat "$NETBIRD_DIR/state/.management")" != "$management" ]; then
            netbird_stop
            $SUDO rm -rf "$NETBIRD_DIR/state"
        fi
        $SUDO mkdir -p "$NETBIRD_DIR/state"
        echo "$management" | $SUDO tee "$NETBIRD_DIR/state/.management" >/dev/null
        $SUDO rm -f "$NETBIRD_DIR/problem"          # what was wrong last time may be right now
        if ! "${COMPOSE[@]}" --profile netbird pull -q netbird >/dev/null 2>&1 &&
           ! docker image inspect "$(setting NETBIRD_IMAGE netbirdio/netbird:latest)" >/dev/null 2>&1; then
            printf 'download\n' | $SUDO tee "$NETBIRD_DIR/problem" >/dev/null
            echo "Error: NetBird could not be downloaded. Check the internet connection of this box." >&2
            exit 1
        fi
        docker stop -t 20 fieldmap-netbird >/dev/null 2>&1 || true
        $SUDO rm -f "$NETBIRD_DIR/status.json" "$NETBIRD_DIR/status.for" "$NETBIRD_DIR/key-check"
        # Docker's own complaint goes to the Remote access page at once (it says "did not start"
        # with the reason), not only after the page stops waiting.
        if ! out="$("${COMPOSE[@]}" --profile netbird up -d --force-recreate netbird 2>&1)"; then
            printf 'start\n%s\n' "$(printf '%s' "$out" | grep -v '^ *Container ' | tail -2 | tr '\n' ' ' | cut -c1-300)" |
                $SUDO tee "$NETBIRD_DIR/problem" >/dev/null
            echo "$out" >&2
            echo "Error: NetBird did not start." >&2
            exit 1
        fi
        echo "NetBird is started. The Remote access page says if it is connected."
    else
        netbird_stop
        $SUDO rm -f "$NETBIRD_DIR/status.json" "$NETBIRD_DIR/status.for" "$NETBIRD_DIR/problem"
        echo "NetBird is not running on this box."
    fi
    # The firewall opens, or closes, the gateway's port on NetBird's interface.
    if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then $SUDO ./scripts/firewall.sh apply >/dev/null || true; fi
    ;;

netbird-forget)
    # Takes NetBird off the box for good: the container, the setup key, the box's place in the
    # NetBird network. The box's name stays in the NetBird dashboard until you delete it there.
    netbird_stop
    $SUDO rm -rf "$NETBIRD_DIR"
    server netbird-forget
    echo "NetBird is off this box. The setup key is deleted from the box."
    echo "To remove the box from the NetBird network too, delete fieldmap-box in the NetBird dashboard (Peers)."
    ;;

update-gateway)
    # Weekly (fieldmap-remote-update.timer): the gateway is what faces the internet, so it takes
    # nginx's security fixes as they come. Recreated only when the image changed.
    before="$(docker inspect -f '{{.Image}}' fieldmap-remote 2>/dev/null || true)"
    "${COMPOSE[@]}" --profile remote pull -q remote
    "${COMPOSE[@]}" --profile remote up -d remote
    after="$(docker inspect -f '{{.Image}}' fieldmap-remote 2>/dev/null || true)"
    [ "$before" = "$after" ] && echo "The gateway is up to date." || echo "The gateway was updated."
    # NetBird's client too, where it runs: it is the other part of the box that talks to the internet.
    if docker inspect fieldmap-netbird >/dev/null 2>&1; then
        before="$(docker inspect -f '{{.Image}}' fieldmap-netbird 2>/dev/null || true)"
        "${COMPOSE[@]}" --profile netbird pull -q netbird || true
        "${COMPOSE[@]}" --profile netbird up -d netbird
        after="$(docker inspect -f '{{.Image}}' fieldmap-netbird 2>/dev/null || true)"
        [ "$before" = "$after" ] && echo "NetBird is up to date." || echo "NetBird was updated."
    fi
    docker image prune -f >/dev/null 2>&1 || true
    ;;

off)
    if [ -f data/remote/address ] && command -v tailscale >/dev/null; then
        $SUDO tailscale funnel --tcp=443 "tcp://localhost:$REMOTE_PORT" off || true
    fi
    as_owner rm -f data/remote/address
    "${COMPOSE[@]}" --profile remote stop remote >/dev/null 2>&1 || true
    netbird_stop                    # NetBird, the other way in, goes with the gateway
    $SUDO systemctl disable -q --now fieldmap-remote fieldmap-share fieldmap-remote-update.timer fieldmap-remote-apply.path fieldmap-netbird.path 2>/dev/null || true
    as_owner mkdir -p data/remote/run && as_owner touch data/remote/run/no-remote   # no Devices tab, no gateway port
    # The firewall stays: with no gateway, nothing from outside gets in or gets an answer.
    $SUDO ./scripts/firewall.sh install
    echo "Remote access is off: the gateway and the services (sharing between the phones too) are stopped."
    echo "The router lets go of its port within two hours. The paired phones stay on the list:"
    echo "'./scripts/remote.sh setup' lets them in again."
    ;;

*)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
