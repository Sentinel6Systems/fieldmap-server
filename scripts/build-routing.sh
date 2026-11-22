#!/usr/bin/env bash
# Download the routing data for the Fieldmap app: BRouter's .rd5 files, one per 5 x 5 degree
# square (W80_N35.rd5: longitude -80 to -75, latitude 35 to 40), published every week by
# brouter.de. The app plans routes along roads and trails with them, without a connection.
# The files go to data/routing/ with an index.json (tools/routing-files.py), and the web
# server serves them at /routing/ (also to paired devices through the remote gateway).
#
# Usage:
#   ./scripts/build-routing.sh <min-lon> <min-lat> <max-lon> <max-lat>
#
# Every square that overlaps the box is downloaded whole; a box whose min-lon is larger than
# its max-lon crosses the 180 degree line and gets both sides. Squares with no land have no
# file and are skipped. A file on hand that the server has no newer copy of is kept. Squares
# outside the box are deleted: one folder holds the routing data of one region, as each
# .mbtiles file holds one region's layer. build-region.sh runs this as its "routing" step.
#
# Size: up to about 240 MB a square, mostly much less; roughly a quarter to a half of the
# region's OpenStreetMap extract, more for a small region (a square is downloaded whole).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -ne 4 ]; then
    echo "Usage: $0 <min-lon> <min-lat> <max-lon> <max-lat>" >&2
    exit 1
fi
ROUTING_URL="https://brouter.de/brouter/segments4/"
TOOL="$REPO_DIR/tools/routing-files.py"
OUT="$OUT_DIR/routing"
LIVE="$DATA_DIR/routing"
trap 'disk_full_hint "$DATA_DIR"' ERR

LIST="$(python3 "$TOOL" squares "$@")"
mapfile -t SQUARES <<< "$LIST"
mkdir -p "$OUT"

# A staged build starts from the files in use (hard links: no copy, no space), so that a
# square the server has no newer copy of is not downloaded again.
todo=0
for name in "${SQUARES[@]}"; do
    if [ "$OUT" != "$LIVE" ] && [ ! -e "$OUT/$name" ] && [ -s "$LIVE/$name" ]; then
        ln "$LIVE/$name" "$OUT/$name" 2>/dev/null || cp -p "$LIVE/$name" "$OUT/$name"
    fi
    [ -s "$OUT/$name" ] || todo=$((todo + 1))
done
# Up to 240 MB a square, most far less; squares without land take nothing.
require_free_space "$DATA_DIR" "$(( todo * 15 / 100 + 1 ))" "downloading up to $todo routing files"

# Download one square, with retries. With a copy on hand, only when the server's is newer
# (curl -R keeps the server's date on the file, -z compares with it). Returns 1 when the
# server has no such file (it answers 404 or 403); stops the build after four failed tries.
# Argument: NAME
fetch_square() {
    local name="$1" dest="$OUT/$1" code tries=0 since=()
    [ -s "$dest" ] && since=(-z "$dest")
    while :; do
        rm -f "$dest.part"
        code="$(curl -sSL -R "${since[@]}" --connect-timeout 30 -o "$dest.part" -w '%{http_code}' "$ROUTING_URL$name")" \
            || code="${code:-000}, the connection broke off"
        case "$code" in
            200)
                mv -f "$dest.part" "$dest"
                echo "download: $name ($(du -h "$dest" | cut -f1))"
                return 0 ;;
            304)
                rm -f "$dest.part"
                echo "up to date: $name"
                return 0 ;;
            403|404)
                rm -f "$dest.part"
                return 1 ;;
            *)
                tries=$((tries + 1))
                if [ "$tries" -ge 4 ]; then
                    rm -f "$dest.part"
                    echo "Error: could not download $name (HTTP ${code:-none}). Check the connection and run the same command again; finished downloads are kept." >&2
                    exit 1
                fi
                echo "retry $tries: $name (HTTP ${code:-none})"
                sleep $((tries * 5)) ;;
        esac
    done
}

echo "Routing data: ${#SQUARES[@]} square$([ ${#SQUARES[@]} -eq 1 ] || echo s) from $ROUTING_URL"
none=0
for name in "${SQUARES[@]}"; do
    if ! fetch_square "$name"; then
        if [ -s "$OUT/$name" ]; then
            echo "no file on the server now: $name (the copy on hand is kept)"
        else
            echo "no file (no land there): $name, skipped"
            none=$((none + 1))
        fi
    fi
done
[ "$none" -eq 0 ] || echo "$none square$([ "$none" -eq 1 ] || echo s) without a file."

python3 "$TOOL" index "$OUT" "$@"
echo "Done. Output: $OUT"
