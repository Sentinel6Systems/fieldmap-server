#!/usr/bin/env bash
# The Fieldmap Android app, kept on the box: devices on its network download it from the Devices
# page and install it from there, without an app store. install.sh and install-pi.sh run "fetch".
#
# Usage:
#   ./scripts/app.sh fetch                     the newest published release, from GitHub, when it is newer
#   ./scripts/app.sh add <file.apk> [version]  a file of your own instead (fetch then leaves it alone)
#   ./scripts/app.sh status                    which app the box offers
#   ./scripts/app.sh remove                    offer none
#
# Settings, in .env, both optional:
#   APP_FETCH=off      never download the app (add one by hand, or offer none)
#   APP_REPO           <owner>/<repo> on GitHub whose latest release carries the app
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

setting() {
    local value=""
    if [ -f "$REPO_DIR/.env" ]; then
        value="$(grep -E "^$1=" "$REPO_DIR/.env" | tail -1 | cut -d= -f2- || true)"
        value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    fi
    echo "${value:-$2}"
}

# The files belong to the user the services run as (FIELDMAP_USER, which install-pi.sh may set),
# or to the owner of this folder, whoever runs the script. They are in data/app, or in app/ of
# FIELDMAP_DATA.
OWNER="$(setting FIELDMAP_USER "$(stat -c %U "$REPO_DIR")")"
APP_DIR="$(setting FIELDMAP_DATA "$REPO_DIR/data")/app"
as_owner() { if [ "$(id -un)" = "$OWNER" ]; then "$@"; else sudo -u "$OWNER" "$@"; fi; }
tool() { as_owner env APP_DIR="$APP_DIR" APP_REPO="$(setting APP_REPO "")" python3 "$REPO_DIR/tools/app-file.py" "$@"; }

case "${1:-}" in
fetch)
    if [ "$(setting APP_FETCH on)" = off ]; then echo "The app is not downloaded (APP_FETCH=off)."; exit 0; fi
    tool fetch
    ;;
add)
    [ -n "${2:-}" ] || { echo "Usage: $0 add <file.apk> [version]" >&2; exit 1; }
    tool add "$(realpath "$2")" "${@:3}"
    ;;
status|remove) tool "$1" ;;
*)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
