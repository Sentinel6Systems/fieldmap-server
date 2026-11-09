#!/usr/bin/env bash
# Build the print tool and push it to the Pi. Run from the repo root on your workstation.
#   ./scripts/deploy-print.sh user@pi-host
set -euo pipefail
PI="${1:-}"
if [ -z "$PI" ]; then echo "usage: $0 user@pi-host" >&2; exit 1; fi
cd "$(dirname "$0")/.."
( cd print && npm run build )
# First run only: create the target folder (asks for the Pi's sudo password).
ssh -t "$PI" 'test -d /opt/fieldmap-server/print || { sudo mkdir -p /opt/fieldmap-server/print && sudo chown -R "$(id -un):$(id -un)" /opt/fieldmap-server; }'
rsync -a --delete print/dist/ "$PI:/opt/fieldmap-server/print/dist/"
echo "Deployed to $PI:/opt/fieldmap-server/print/dist  ->  http://<pi-host>/mapprinter/"
