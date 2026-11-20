#!/usr/bin/env bash
# Build the map (print/) on this workstation and copy it to a Pi installed with install-pi.sh,
# which serves it from <repo-dir>/print/dist. Run from the repository root:
#   ./scripts/deploy-print-pi.sh <user>@<pi-host> <repo-dir>
set -euo pipefail
if [ $# -ne 2 ]; then echo "usage: $0 <user>@<pi-host> <repo-dir>" >&2; exit 1; fi
PI="$1" DIR="${2%/}"
cd "$(dirname "$0")/.."
( cd print && npm run build )
rsync -a --delete print/dist/ "$PI:$DIR/print/dist/"
echo "Deployed to $PI:$DIR/print/dist  ->  http://<pi-host>/map/"
