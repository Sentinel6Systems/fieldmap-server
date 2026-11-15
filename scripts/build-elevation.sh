#!/usr/bin/env bash
# Build elevation raster tiles for one region: heights in metres as Mapbox terrain-RGB
# (0.1 m steps) in lossless WebP, for the print tool's terrain analysis (viewshed and radio coverage).
# Same Copernicus GLO-30 elevation data as build-hillshade.sh and build-contours.sh.
# The output file is data/elevation.mbtiles.
#
# Usage:
#   ./scripts/build-elevation.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> [maxzoom]
#
# With --areas (Geofabrik extract paths, as in a region file) the tiles cover only those
# areas' outlines inside the box; without it, the whole box. A box whose min-lon is larger
# than its max-lon crosses the 180° line. See tools/region-outline.py.
#
# maxzoom (or ELEVATION_MAXZOOM): 12 by default, about 30 m per pixel at US latitudes.
# 11 halves the resolution and makes the file about 4 times smaller. The tiles start
# at zoom 8 (or maxzoom, if lower), so a large radius needs only a few tiles.
# ELEVATION_ENCODING (terrarium or mapbox) and ELEVATION_FORMAT (png or webp) choose
# the tile encoding; the defaults are mapbox and webp (the smallest of the four, about
# 30 % below PNG; both encodings keep the heights to 0.1 m or better).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

AREAS=""
if [ "${1:-}" = "--areas" ]; then
    AREAS="${2:-}"
    shift 2 || true
fi
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
    echo "Usage: $0 [--areas \"<geofabrik paths>\"] <min-lon> <min-lat> <max-lon> <max-lat> [maxzoom]" >&2
    exit 1
fi

MAXZOOM="${5:-${ELEVATION_MAXZOOM:-12}}"
case "$MAXZOOM" in
    ''|*[!0-9]*) echo "Error: maxzoom must be a whole number (for example 11 or 12)." >&2; exit 1 ;;
esac
if [ "$MAXZOOM" -lt 6 ] || [ "$MAXZOOM" -gt 14 ]; then
    echo "Error: maxzoom must be between 6 and 14." >&2
    exit 1
fi
export ELEVATION_ENCODING="${ELEVATION_ENCODING:-mapbox}" ELEVATION_FORMAT="${ELEVATION_FORMAT:-webp}"

reexec_in_tools ${AREAS:+--areas "$AREAS"} "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"
MINZOOM=$(( MAXZOOM < 8 ? MAXZOOM : 8 ))

WORK_DIR="$REPO_DIR/data/cache/elevation"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# shellcheck disable=SC2086
terrain_outline "$WORK_DIR" "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" $AREAS
terrain_estimate "$WORK_DIR" elevation

echo "Step 1/2: download elevation tiles"
download_dem "$WORK_DIR"
CELLS="$(terrain_land_cells "$WORK_DIR")"
# The tiles are written straight from the downloaded elevation data; the space needed
# is the output (measured: about 40 MB per square degree at zoom 12), with room to spare.
NEED_GB="$(python3 -c "print(max(2, int($CELLS * 0.06 + 1)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "elevation tiles for $CELLS one-degree cells with land"

echo "Step 2/2: write the elevation tiles (zoom $MINZOOM-$MAXZOOM, $ELEVATION_ENCODING $ELEVATION_FORMAT)"
# One mosaic per outline part (see tools/region-outline.py); the tile writer masks it to the
# outline block by block, so the low zooms can read the elevation data's own overviews.
mapfile -t DEMS < <(awk '{print $2}' "$WORK_DIR/outline/mosaics.txt")
python3 "$REPO_DIR/tools/elevation-tiles.py" --outline "$WORK_DIR/outline/outline.geojson" \
    --minzoom "$MINZOOM" --maxzoom "$MAXZOOM" \
    --encoding "$ELEVATION_ENCODING" --format "$ELEVATION_FORMAT" \
    --source "Copernicus GLO-30" --source-date "2011-2015" \
    --attribution "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved" \
    "${DEMS[@]}" "$REPO_DIR/data/elevation.mbtiles"

report_work_space "$WORK_DIR" "$REPO_DIR/data/elevation.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/elevation.mbtiles (zoom $MINZOOM-$MAXZOOM)"
