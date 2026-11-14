#!/usr/bin/env bash
# Build the world layer: the whole world at zoom 0-8, from a filtered copy of
# the OpenStreetMap planet. A region built with WORLD=yes (build-region.sh) or
# --world (build-vector.sh) uses it for zoom 0-8 and its own data from zoom 9.
# Those scripts run this one when the layer is missing, so you rarely need to.
#
# Usage:
#   ./scripts/build-world.sh [--language <code>]
#
# The first run downloads about 96 GB and needs about 160 GB free; it takes
# hours. Later runs reuse data/sources/planet-z0-8.osm.pbf. To rebuild from a
# newer planet, delete that file and data/world-z0-8*.mbtiles.
#
# Output: data/world-z0-8.mbtiles (data/world-z0-8-<code>.mbtiles with --language)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

while [ $# -gt 0 ]; do
    case "$1" in
        --language) LANGUAGE="${2:-}"; shift 2 ;;
        *) echo "Usage: $0 [--language <code>]   (codes: $BUILD_LANGUAGES)" >&2; exit 1 ;;
    esac
done
LANGUAGE="${LANGUAGE:-}"
check_language
MEMORY="${PLANETILER_MEMORY:-8g}"

if [ ! -s "$REPO_DIR/data/sources/planet-z0-8.osm.pbf" ]; then
    require_free_space "$REPO_DIR/data" 160 "downloading and filtering the OpenStreetMap planet"
    "$REPO_DIR/scripts/filter-planet.sh"
fi

fetch_shared_sources "$REPO_DIR/data/sources"

OUT="$(world_file)"
echo "Planetiler: build data/$OUT (zoom 0-8, Java heap $MEMORY${LANGUAGE:+, language $LANGUAGE})"
run_planetiler planet-z0-8.osm.pbf "$OUT" "$MEMORY" --maxzoom=8 --render_maxzoom=8
echo "Done. Output: $REPO_DIR/data/$OUT"
