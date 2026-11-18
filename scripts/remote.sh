#!/usr/bin/env bash
# Remote access for the Fieldmap Android app: a phone that has been on this box's own
# network can use the maps from anywhere afterwards, and nobody else can.
#
# Usage:
#   ./scripts/remote.sh setup [--port 443|8443|10000]   set it up (again after every update)
#   ./scripts/remote.sh status                          what is set up and whether it runs
#   ./scripts/remote.sh devices                         the phones that are let in
#   ./scripts/remote.sh remove <id>                     lock one out
#   ./scripts/remote.sh off                             stop being reachable from outside
#
# On a Raspberry Pi behind Internet-in-a-Box, run it with sudo, like install-pi.sh.
# Elsewhere run it as the owner of this folder; it asks for sudo where it needs it.
#
# What "setup" does:
#   1. makes this box its own certificate authority and writes the gateway's configuration
#      (tools/remote-server.py init; the files are in data/remote/ and stay on this box);
#   2. starts the service that lets phones in and keeps the list of them (a systemd
#      service, fieldmap-remote), reached on the local network at /mapremote/;
#   3. starts the gateway, an nginx container that demands a certificate this box issued
#      and serves only the tile server. It listens on this machine only;
#   4. makes the gateway reachable from the internet through Tailscale Funnel: Tailscale's
#      relays pass the encrypted connection on and cannot read it. The box must be signed in
#      to a Tailscale account first (sudo tailscale up); the phones need no Tailscale.
#
# Settings, in .env (see .env.example):
#   REMOTE_PORT      the gateway's port on this machine (default 8443)
#   REMOTE_PIN       a code the app must give to be let in. Without it, being on the local
#                    network is enough.
#   REMOTE_ADDRESS   host:port at which the gateway is reached from outside. Set it when you
#                    forward a port yourself instead of using Tailscale; then also set
#                    REMOTE_BIND=0.0.0.0 so that the gateway listens on the network.
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
REMOTE_BIND="$(setting REMOTE_BIND 127.0.0.1)"
SERVICE_PORT="$(setting REMOTE_SERVICE_PORT 8092)"
# A Raspberry Pi behind Internet-in-a-Box (install-pi.sh copied this file here): the tile
# server is on this machine's port 8080 and the web server shows it at /maps/.
if [ -f docker-compose.pi.yml ]; then
    PI=yes; COMPOSE=(docker compose -f docker-compose.pi.yml)
    PREFIX=/maps/; UPSTREAM=http://127.0.0.1:8080/; LISTEN="$REMOTE_BIND:$REMOTE_PORT"
    LOCAL="http://127.0.0.1/mapremote/info"
else
    PI=no; COMPOSE=(docker compose)
    PREFIX=/; UPSTREAM=http://tileserver:8080/; LISTEN=8443
    LOCAL="http://127.0.0.1:$(setting WEB_PORT 80)/mapremote/info"
fi
UNIT=/etc/systemd/system/fieldmap-remote.service

server() { as_owner env REMOTE_PIN="$(setting REMOTE_PIN "")" REMOTE_ADDRESS="$(setting REMOTE_ADDRESS "")" python3 tools/remote-server.py "$@"; }

# What Tailscale says about this machine: "<state> <name>", e.g. "Running box.example.ts.net".
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
    # What the systemd service runs: the settings from .env, then the service itself.
    export REMOTE_PIN="$(setting REMOTE_PIN "")" REMOTE_ADDRESS="$(setting REMOTE_ADDRESS "")"
    if [ "$PI" = yes ]; then exec python3 tools/remote-server.py serve --port "$SERVICE_PORT"; fi
    exec python3 tools/remote-server.py serve
    ;;

setup)
    FUNNEL_PORT=443
    if [ "${2:-}" = --port ]; then FUNNEL_PORT="${3:-}"; fi
    case "$FUNNEL_PORT" in 443|8443|10000) ;; *) echo "Error: Tailscale Funnel has the ports 443, 8443 and 10000." >&2; exit 1 ;; esac
    for tool in python3 openssl docker curl; do
        command -v "$tool" >/dev/null || { echo "Error: $tool is not installed." >&2; exit 1; }
    done

    echo "== Certificates and the gateway's configuration"
    server init --prefix "$PREFIX" --upstream "$UPSTREAM" --listen "$LISTEN"

    echo "== The service that lets phones in"
    if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
        sed -e "s|/opt/fieldmap-server|$REPO_DIR|g" -e "s|^User=.*|User=$OWNER|" systemd/fieldmap-remote.service | $SUDO tee "$UNIT" >/dev/null
        $SUDO systemctl daemon-reload
        $SUDO systemctl enable -q fieldmap-remote
        $SUDO systemctl restart fieldmap-remote
        echo "Installed and started as a service (fieldmap-remote), as user $OWNER. It starts at boot."
    else
        echo "This machine has no systemd: keep './scripts/remote.sh serve' running yourself."
    fi

    echo "== The gateway"
    "${COMPOSE[@]}" --profile remote up -d --quiet-pull remote
    docker restart fieldmap-remote >/dev/null      # read the current certificates and configuration
    if [ "$PI" = yes ]; then
        sed "s#127.0.0.1:8092#127.0.0.1:$SERVICE_PORT#" pi/nginx/mapremote-nginx.conf | $SUDO tee /etc/nginx/conf.d/mapremote-nginx.conf >/dev/null
        $SUDO nginx -t && $SUDO systemctl reload nginx
    fi
    sleep 2
    if ! curl -fs -m 5 "$LOCAL" | grep -q fieldmap-remote; then
        echo "WARNING: $LOCAL does not answer, so phones cannot be let in yet." >&2
        [ "$PI" = yes ] || echo "         Run ./scripts/install.sh first (it rebuilds the web front), then this again." >&2
    fi
    code="$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "https://127.0.0.1:$REMOTE_PORT$PREFIX" || true)"
    if [ "$code" = 400 ]; then echo "The gateway is up and turns away a visitor without a certificate."
    else echo "WARNING: the gateway on port $REMOTE_PORT answered '$code' to a visitor without a certificate (expected 400). See: docker logs fieldmap-remote" >&2; fi

    echo "== Reaching it from the internet"
    if [ -n "$(setting REMOTE_ADDRESS "")" ]; then
        echo "REMOTE_ADDRESS is set: phones are told $(setting REMOTE_ADDRESS ""). Forwarding that to this machine's port $REMOTE_PORT is yours to set up."
        as_owner rm -f data/remote/address
    elif ! command -v tailscale >/dev/null; then
        echo "Tailscale is not installed on this machine. Install it (https://tailscale.com/download/linux),"
        echo "sign this box in with 'sudo tailscale up', and run this script again."
        echo "Until then phones can be let in on the local network, but have no way in from outside."
        exit 2
    else
        read -r state name <<< "$(tailscale_self)"
        if [ "$state" != Running ] || [ -z "$name" ]; then
            echo "This box is not signed in to Tailscale yet. Run"
            echo "    sudo tailscale up"
            echo "open the link it prints, sign in, and run this script again."
            exit 2
        fi
        # The first time, Tailscale prints a link to switch Funnel on for the account: open it,
        # approve, and run this script again if it stopped here.
        $SUDO tailscale funnel --bg --tcp="$FUNNEL_PORT" "tcp://localhost:$REMOTE_PORT"
        echo "$name:$FUNNEL_PORT" | as_owner tee data/remote/address >/dev/null
        echo "Reachable from the internet at $name:$FUNNEL_PORT, through Tailscale Funnel."
        echo "A new name can take about ten minutes to be known everywhere."
    fi
    echo
    echo "Done. On a phone, on this box's network: Fieldmap, Menu, Server, Set up remote access."
    ;;

status)
    server status
    if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
        echo "Service:        $(systemctl is-active fieldmap-remote 2>/dev/null || true)"
    fi
    echo "Gateway:        $(docker inspect -f '{{.State.Status}}' fieldmap-remote 2>/dev/null || echo 'not started')"
    if command -v tailscale >/dev/null; then
        read -r state name <<< "$(tailscale_self)"
        echo "Tailscale:      $state ${name:+as $name}"
    fi
    ;;

devices) server devices ;;

remove)
    [ -n "${2:-}" ] || { echo "Usage: $0 remove <id>   (the ids are in: $0 devices)" >&2; exit 1; }
    server remove "$2"
    ;;

off)
    if [ -f data/remote/address ] && command -v tailscale >/dev/null; then
        port="$(sed 's/.*://' data/remote/address)"
        $SUDO tailscale funnel --tcp="$port" "tcp://localhost:$REMOTE_PORT" off || true
    fi
    as_owner rm -f data/remote/address
    "${COMPOSE[@]}" --profile remote stop remote >/dev/null 2>&1 || true
    echo "The gateway is stopped and the box is no longer reachable from outside."
    echo "The phones stay on the list: './scripts/remote.sh setup' lets them in again."
    ;;

*)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
