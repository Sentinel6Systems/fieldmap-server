#!/usr/bin/env bash
# The old name of scripts/install-nginx.sh, kept so that `git pull && sudo ./scripts/install-pi.sh`
# on a server installed beside its own nginx still updates it. On a Raspberry Pi of its own, use
# ./scripts/install.sh (as the owner of this folder, without sudo), like on a PC.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${FIELDMAP_NGINX_DIR:-}" ] && ! grep -qE '^FIELDMAP_NGINX_DIR=.' "$REPO/.env" 2>/dev/null; then
    echo "install-pi.sh is now scripts/install-nginx.sh, for a machine whose own nginx serves port 80 (FIELDMAP_NGINX_DIR)." >&2
    echo "Fieldmap no longer installs into Internet-in-a-Box. On a Raspberry Pi, as on a PC, run:" >&2
    echo "  ./scripts/install.sh        (as $(stat -c %U "$REPO"), without sudo)" >&2
    echo "Moving a server off Internet-in-a-Box: see pi/README.md." >&2
    exit 1
fi
echo "install-pi.sh is now scripts/install-nginx.sh; running that."
exec "$REPO/scripts/install-nginx.sh" "$@"
