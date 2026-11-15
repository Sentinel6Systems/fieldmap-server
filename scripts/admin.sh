#!/usr/bin/env bash
# Start the map builder: a web page to choose regions and layers on a map, check the disk
# space, and start the build (tools/admin-server.py). Optional: everything it does can
# also be done with build-region.sh and a region file.
#
# Usage:
#   ./scripts/admin.sh
#
# Then open http://<this device's address>/mapadmin/ in a browser: the web front (the
# fieldmap-web container) passes that address on to this page. Stop it with Ctrl-C; a
# build that is running carries on. scripts/install.sh runs this as a service.
#
# Settings, in the environment or in .env (see .env.example):
# ADMIN_PORT   also listen on this port, for a machine that runs no web front
#              (http://<address>:<port>/). Not 8090, which other software often holds.
# ADMIN_HOST   address for that port (default 0.0.0.0: every network the device is on;
#              127.0.0.1 for this device only)
# ADMIN_PIN    a code the page then asks for. Without it the page is open to everyone who
#              can reach the device, and whoever can open it can replace the maps and fill
#              the disk: set one on a network you do not trust.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Settings from .env, unless the environment already has them.
for var in ADMIN_PORT ADMIN_HOST ADMIN_PIN; do
    if [ -z "${!var:-}" ] && [ -f "$REPO_DIR/.env" ]; then
        value="$(grep -E "^$var=" "$REPO_DIR/.env" | tail -1 | cut -d= -f2- || true)"
        if [ -n "$value" ]; then export "$var=$value"; fi
    fi
done

command -v python3 >/dev/null || { echo "Error: python3 is not installed." >&2; exit 1; }
command -v docker >/dev/null || echo "Warning: docker is not installed. The page will open, but a build needs it." >&2
mkdir -p "$REPO_DIR/data/admin"
exec python3 "$REPO_DIR/tools/admin-server.py" serve
