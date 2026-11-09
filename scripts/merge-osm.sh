#!/usr/bin/env bash
# Download several Geofabrik extracts and merge them into one OSM file.
# Planetiler accepts only one OSM input file. Use this script when a region
# covers more than one Geofabrik area (for example, several US states).
#
# Usage:
#   ./scripts/merge-osm.sh <output-name> <geofabrik-path> [<geofabrik-path> ...]
#
# <geofabrik-path> is the path of the extract on download.geofabrik.de,
# without "-latest.osm.pbf". Examples:
#   north-america/us/new-jersey
#   europe/switzerland
#
# Output: data/sources/<output-name>.osm.pbf
# Downloads are kept in data/sources/extracts and reused on later runs.
# Delete that folder to force fresh downloads.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <output-name> <geofabrik-path> [<geofabrik-path> ...]" >&2
    exit 1
fi

reexec_in_tools "$@"

OUT_NAME="$1"
shift

BASE_URL="https://download.geofabrik.de"
EXTRACT_DIR="$REPO_DIR/data/sources/extracts"
OUT_FILE="$REPO_DIR/data/sources/$OUT_NAME.osm.pbf"
mkdir -p "$EXTRACT_DIR"

INPUTS=()
for path in "$@"; do
    name="$(basename "$path")"
    dest="$EXTRACT_DIR/$name.osm.pbf"
    INPUTS+=("$dest")
    if [ -f "$dest" ]; then
        echo "cached: $name"
        continue
    fi
    url="$BASE_URL/$path-latest.osm.pbf"
    echo "download: $url"
    if ! curl -fSL --retry 3 -C - -o "$dest.part" "$url"; then
        echo "Error: download failed: $url" >&2
        echo "Check the path at $BASE_URL" >&2
        exit 1
    fi
    mv "$dest.part" "$dest"
done

# Disk estimate: the merged copy plus Planetiler's work files and output
# come to roughly ten times the size of the extracts.
EXTRACT_GB="$(du -cBG "${INPUTS[@]}" | tail -1 | tr -dc '0-9')"
NEED_GB="$(python3 -c "print(max(6, int($EXTRACT_GB * 10 + 4)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "merging and processing $EXTRACT_GB GB of map extracts"

if [ ${#INPUTS[@]} -eq 1 ]; then
    echo "One extract only: copy it to the output file."
    cp "${INPUTS[0]}" "$OUT_FILE"
else
    echo "merge: ${#INPUTS[@]} extracts"
    osmium merge --overwrite -o "$OUT_FILE" "${INPUTS[@]}"
fi

echo "Data bounding box (this reads the whole file, allow a minute):"
osmium fileinfo -e -g data.bbox "$OUT_FILE" || true
echo "Done. Output: $OUT_FILE"
