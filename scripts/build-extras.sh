#!/usr/bin/env bash
# Build data/extras.mbtiles: OpenStreetMap layers the base map leaves out (power lines
# and towers, substations and plants, pipelines, fences, walls and gates, towers and
# masts with height, springs, waterfalls and other landmarks). The layers are defined
# in planetiler/extras.yml and drawn by the print styles.
#
# Usage:
#   ./scripts/build-extras.sh [--bounds=<min-lon>,<min-lat>,<max-lon>,<max-lat>] <osm-file>
#
# <osm-file> is an OSM extract in data/sources: <region-name>.osm.pbf (made by
# build-region.sh or merge-osm.sh) or <area>.osm.pbf (made by build-vector.sh).
# build-region.sh runs this as its optional "extras" step.
#
# Size: about 1 MB per 2,000 square miles of US countryside; roughly 70 MB for a
# six-state region and 1-2 GB for the whole US. Most of it is z14 detail (fences,
# gates, power towers). Set PLANETILER_MEMORY to change the Java heap (default 4g).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY="${PLANETILER_MEMORY:-4g}"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

usage() {
    echo "Usage: $0 [--bounds=<min-lon>,<min-lat>,<max-lon>,<max-lat>] <osm-file in data/sources>" >&2
    exit 1
}
BOUNDS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --bounds=*) BOUNDS=("$1"); shift ;;
        -*) usage ;;
        *) break ;;
    esac
done
[ $# -eq 1 ] || usage
OSM="$(basename "$1")"
if [ ! -s "$REPO_DIR/data/sources/$OSM" ]; then
    echo "Error: data/sources/$OSM not found. Build the base map first, or run merge-osm.sh." >&2
    exit 1
fi
trap 'disk_full_hint "$REPO_DIR/data"' ERR
OSM_GB="$(du -BG "$REPO_DIR/data/sources/$OSM" | cut -f1 | tr -dc '0-9')"
require_free_space "$REPO_DIR/data" "$(( OSM_GB * 3 + 2 ))" "the extras build"

echo "Planetiler: build data/extras.mbtiles from $OSM (Java heap $MEMORY)"
docker run --rm --label "$BUILD_LABEL" \
    --user "$(id -u):$(id -g)" \
    -e JAVA_TOOL_OPTIONS="-Xmx$MEMORY" \
    -v "$REPO_DIR/data":/data \
    -v "$REPO_DIR/planetiler":/schema:ro \
    "$PLANETILER_IMAGE" \
    generate-custom \
    --schema=/schema/extras.yml \
    --osm_path="/data/sources/$OSM" \
    --tmpdir=/data/tmp \
    --output=/data/extras.tmp.mbtiles \
    --force \
    "${BOUNDS[@]}"
mv "$REPO_DIR/data/extras.tmp.mbtiles" "$OUT_DIR/extras.mbtiles"
rm -f "$REPO_DIR/data/extras.tmp.mbtiles.layerstats.tsv.gz"
echo "Done. Output: $OUT_DIR/extras.mbtiles ($(du -h "$OUT_DIR/extras.mbtiles" | cut -f1))"
echo "Restart the tile server to load it: docker restart tileserver"
