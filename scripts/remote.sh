#!/usr/bin/env bash
# Remote access for the Fieldmap Android app: a phone paired on this box's own network can use
# the maps from anywhere afterwards, and nobody else can. install.sh and install-pi.sh run
# "setup" for you; nothing here needs an account or an outside service.
#
# Usage:
#   ./scripts/remote.sh setup          set it up, or bring it up to date (run by the installers)
#   ./scripts/remote.sh status         can phones reach the box from outside, and how
#   ./scripts/remote.sh pair           open pairing for ten minutes (same as the Phones page button)
#   ./scripts/remote.sh devices        the paired phones
#   ./scripts/remote.sh remove <id>    lock one out
#   ./scripts/remote.sh off            remove it: the gateway, the service, the firewall, the Phones tab
#                                      (to switch it off and on, use the switch on the Phones page)
#
# On a Raspberry Pi behind Internet-in-a-Box, run it with sudo, like install-pi.sh.
# Elsewhere run it as the owner of this folder; it asks for sudo where it needs it.
#
# What "setup" does:
#   1. makes this box its own certificate authority and writes the gateway's configuration
#      (tools/remote-server.py init; data/remote/ stays on this box);
#   2. starts the service that pairs phones and keeps the router's port open
#      (fieldmap-remote, a systemd service): the Phones page at /phones/, and /mapremote/ for the app;
#   3. starts the gateway, an nginx container that demands a certificate this box issued and
#      serves only what the app needs, and updates it every week (fieldmap-remote-update);
#   4. puts a firewall on the box itself that lets nothing in from outside the local network
#      but the gateway and SSH (scripts/firewall.sh, fieldmap-firewall).
#
# Settings, in .env (see .env.example), all optional:
#   REMOTE_PORT            the gateway's port on this machine (default 8443)
#   REMOTE_PIN             a code that pairing then asks for
#   REMOTE_ADDRESS         host:port of the gateway from outside that you set up yourself
#   REMOTE_DIRECT=off      do not ask the router to open a port
#   REMOTE_FIREWALL=off    no firewall on the box
#   REMOTE_FIREWALL_ALLOW  ports open from outside besides the gateway (default 22, for SSH)
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

REMOTE_PORT="$(setting REMOTE_PORT 8443)"
SERVICE_PORT="$(setting REMOTE_SERVICE_PORT 8092)"
RELAY="$(setting REMOTE_RELAY "")"
# A Raspberry Pi behind Internet-in-a-Box (install-pi.sh copied this file here): the tile
# server is on this machine's port 8080, the web server shows it at /maps/, and the gateway
# shares the machine's network.
if [ -f docker-compose.pi.yml ]; then
    PI=yes; COMPOSE=(docker compose -f docker-compose.pi.yml)
    INIT=(--prefix /maps/ --upstream http://127.0.0.1:8080/ --listen "$REMOTE_PORT" --ipv6)
    LOCAL="http://127.0.0.1/mapremote/info"
else
    PI=no; COMPOSE=(docker compose)
    INIT=(--prefix / --upstream http://tileserver:8080/ --listen 8443)
    LOCAL="http://127.0.0.1:$(setting WEB_PORT 80)/mapremote/info"
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

setup)
    for tool in python3 openssl docker curl; do
        command -v "$tool" >/dev/null || { echo "Error: $tool is not installed." >&2; exit 1; }
    done
    if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
        echo "This machine has no systemd: remote access for the app needs it. Skipped." >&2
        exit 2
    fi

    echo "== Certificates and the gateway's configuration"
    server init "${INIT[@]}"
    as_owner rm -f data/remote/run/no-remote      # the Phones page is there (web/nginx.conf)

    echo "== The service that pairs phones and keeps the router's port open"
    install_unit fieldmap-remote.service
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q fieldmap-remote
    $SUDO systemctl restart fieldmap-remote

    echo "== The gateway"
    "${COMPOSE[@]}" --profile remote pull -q remote >/dev/null 2>&1 || true
    "${COMPOSE[@]}" --profile remote up -d --quiet-pull remote
    docker restart fieldmap-remote >/dev/null      # read the current certificates and configuration
    if [ "$PI" = yes ]; then
        sed "s#127.0.0.1:8092#127.0.0.1:$SERVICE_PORT#" pi/nginx/mapremote-nginx.conf | $SUDO tee /etc/nginx/conf.d/mapremote-nginx.conf >/dev/null
        $SUDO nginx -t -q && $SUDO systemctl reload nginx
    fi
    install_unit fieldmap-remote-update.service
    install_unit fieldmap-remote-update.timer
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable -q --now fieldmap-remote-update.timer

    echo "== The firewall on this box"
    if [ "$(setting REMOTE_FIREWALL on)" = off ]; then
        $SUDO systemctl disable -q --now fieldmap-firewall.timer fieldmap-firewall.path 2>/dev/null || true
        $SUDO ./scripts/firewall.sh off
    else
        install_unit fieldmap-firewall.service
        install_unit fieldmap-firewall.timer
        install_unit fieldmap-firewall.path
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable -q --now fieldmap-firewall.timer fieldmap-firewall.path
        $SUDO systemctl start fieldmap-firewall.service
        echo "From outside the local network only the gateway (port $REMOTE_PORT) and $(setting REMOTE_FIREWALL_ALLOW 22) get in."
    fi

    sleep 3
    if ! curl -fs -m 5 "$LOCAL" | grep -q fieldmap-remote; then
        echo "WARNING: $LOCAL does not answer, so phones cannot pair yet." >&2
        [ "$PI" = yes ] || echo "         Run ./scripts/install.sh (it rebuilds the web front), then this again." >&2
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
    echo "Remote access is off until someone switches it on: on this box's network open the Phones page"
    echo "(in the header of the Maps page) and press Switch on. That also opens pairing for ten minutes:"
    echo "then, in the Fieldmap app on the phone, Menu, Server, Pair this phone."
    ;;

status)
    server status
    echo "Service:        $(systemctl is-active fieldmap-remote 2>/dev/null || true)"
    echo "Gateway:        $(docker inspect -f '{{.State.Status}}' fieldmap-remote 2>/dev/null || echo 'not started')"
    echo "Firewall:       $(systemctl is-active fieldmap-firewall.timer 2>/dev/null | sed 's/^active$/on/;s/^inactive$/off/' || true)"
    ;;

pair)
    ask pairing -H 'Content-Type: application/json' -d '{"open": true}' >/dev/null
    echo "Pairing is open for ten minutes. In the Fieldmap app, on this network: Menu, Server, Pair this phone."
    ;;

devices) server devices ;;

remove)
    [ -n "${2:-}" ] || { echo "Usage: $0 remove <id>   (the ids are in: $0 devices)" >&2; exit 1; }
    server remove "$2"
    ;;

update-gateway)
    # Weekly (fieldmap-remote-update.timer): the gateway is what faces the internet, so it takes
    # nginx's security fixes as they come. Recreated only when the image changed.
    before="$(docker inspect -f '{{.Image}}' fieldmap-remote 2>/dev/null || true)"
    "${COMPOSE[@]}" --profile remote pull -q remote
    "${COMPOSE[@]}" --profile remote up -d remote
    after="$(docker inspect -f '{{.Image}}' fieldmap-remote 2>/dev/null || true)"
    [ "$before" = "$after" ] && echo "The gateway is up to date." || echo "The gateway was updated."
    docker image prune -f >/dev/null 2>&1 || true
    ;;

off)
    if [ -f data/remote/address ] && command -v tailscale >/dev/null; then
        $SUDO tailscale funnel --tcp=443 "tcp://localhost:$REMOTE_PORT" off || true
    fi
    as_owner rm -f data/remote/address
    "${COMPOSE[@]}" --profile remote stop remote >/dev/null 2>&1 || true
    $SUDO systemctl disable -q --now fieldmap-remote fieldmap-remote-update.timer fieldmap-firewall.timer fieldmap-firewall.path 2>/dev/null || true
    as_owner mkdir -p data/remote/run && as_owner touch data/remote/run/no-remote   # no Phones tab
    $SUDO ./scripts/firewall.sh off
    echo "Remote access is off: the gateway, the service and the box's firewall are stopped."
    echo "The router lets go of its port within two hours. The paired phones stay on the list:"
    echo "'./scripts/remote.sh setup' lets them in again."
    ;;

*)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
