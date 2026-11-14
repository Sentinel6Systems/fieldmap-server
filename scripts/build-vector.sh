#!/usr/bin/env bash
# Build the base map vector tiles for one area.
# The OpenStreetMap extract comes from the fastest of several mirrors whose
# copy is at most 30 days old (see merge-osm.sh); Planetiler runs in Docker.
# The output file is data/basemap.mbtiles.
#
# Usage:
#   ./scripts/build-vector.sh [--world] [--language <code>] <area>
#
# <area> is a Geofabrik path, such as europe/switzerland or
# north-america/us/delaware, or just its last part (switzerland), which is
# looked up in Geofabrik's index. Find areas at https://download.geofabrik.de
#
# --world and --language work as in build-region.sh: zoom 0-8 from the world
# layer (build-world.sh), and labels in one language only
# (en zh hi es ar fr bn pt ru id).
#
# Large areas need more memory. Set PLANETILER_MEMORY to change the Java
# heap size. Example: PLANETILER_MEMORY=8g ./scripts/build-vector.sh germany

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY="${PLANETILER_MEMORY:-4g}"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

usage() {
    echo "Usage: $0 [--world] [--language <code>] <geofabrik-path-or-area>" >&2
    exit 1
}
WORLD=no LANGUAGE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --world) WORLD=yes; shift ;;
        --language) [ $# -ge 2 ] || usage; LANGUAGE="$2"; shift 2 ;;
        -*) usage ;;
        *) break ;;
    esac
done
[ $# -eq 1 ] || usage
AREA="$1"
check_language
mkdir -p "$REPO_DIR/data"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
# The extract size is unknown until it is downloaded. Ask for a
# baseline that covers a mid-sized country or a US state.
require_free_space "$REPO_DIR/data" 15 "the base map build"

if [[ "$AREA" != */* ]]; then
    echo "Looking up $AREA in Geofabrik's index"
    INDEX_URL="https://download.geofabrik.de/index-v1-nogeom.json"
    AREA_PATH="$(curl -fsSL --retry 3 --connect-timeout 30 "$INDEX_URL" | python3 -c '
import json, sys
area = sys.argv[1]
for f in json.load(sys.stdin)["features"]:
    p = f["properties"]
    if p["id"] == area:
        print(p["urls"]["pbf"].split("download.geofabrik.de/", 1)[1].removesuffix("-latest.osm.pbf"))
        break
' "$AREA")" || AREA_PATH=""
    if [ -z "$AREA_PATH" ]; then
        echo "Error: could not find $AREA in Geofabrik's index. Give the full path instead," >&2
        echo "for example europe/switzerland (see https://download.geofabrik.de)." >&2
        exit 1
    fi
    echo "  -> $AREA_PATH"
else
    AREA_PATH="$AREA"
fi
NAME="$(basename "$AREA_PATH")"

"$REPO_DIR/scripts/merge-osm.sh" "$NAME" "$AREA_PATH"

# Fetch Planetiler's shared inputs here, with retries and fallback
# mirrors, so Planetiler itself makes no network calls.
fetch_shared_sources "$REPO_DIR/data/sources"

# Take the bounds from the data: some mirrors' files have none in their header,
# and Planetiler would then build the whole world.
build_basemap "$NAME.osm.pbf" "$MEMORY" --osm_parse_node_bounds=true

echo "Done. Output: $REPO_DIR/data/basemap.mbtiles"
