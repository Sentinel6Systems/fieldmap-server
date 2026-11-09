#!/usr/bin/env bash
# Choose the tile server's memory cap from this machine's RAM and write it to .env.
# Run from anywhere, before `docker compose up`:
#   ./scripts/mem-limit.sh
# Under 8 GB of RAM the cap is 1g; with 8 GB or more there is no cap (0).
# If .env already sets TILESERVER_MEM_LIMIT, that value is kept, so your own
# choice always wins. Delete the line and re-run to choose again.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"

if [ -f "$ENV_FILE" ] && grep -q '^TILESERVER_MEM_LIMIT=' "$ENV_FILE"; then
  echo "Tile server memory cap: $(grep '^TILESERVER_MEM_LIMIT=' "$ENV_FILE" | tail -1 | cut -d= -f2) (from .env)"
  exit 0
fi

# 8 GB machines report a little under 8 GiB, so the line is drawn at 7 GiB.
MEM_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
if [ "$MEM_KB" -ge $((7 * 1024 * 1024)) ]; then
  LIMIT=0
  NOTE="no cap, this machine has 8 GB or more"
else
  LIMIT=1g
  NOTE="this machine has under 8 GB"
fi

{
  echo
  echo "# Set by scripts/mem-limit.sh ($NOTE). 0 = no cap."
  echo "TILESERVER_MEM_LIMIT=$LIMIT"
} >> "$ENV_FILE"
echo "Tile server memory cap: $LIMIT ($NOTE), written to .env"
