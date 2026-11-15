#!/usr/bin/env bash
# Build hillshade raster tiles for one region.
# The script downloads Copernicus GLO-30 elevation data.
# The output file is data/hillshade.mbtiles.
#
# Usage:
#   ./scripts/build-hillshade.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat>
#
# With --areas (Geofabrik extract paths, as in a region file) the tiles cover only those
# areas' outlines inside the box; without it, the whole box. A box whose min-lon is larger
# than its max-lon crosses the 180° line. See tools/region-outline.py.
#
# Output: gray hillshade tiles, zoom 6 to HILLSHADE_MAXZOOM (default 12), transparent
# outside the outline. HILLSHADE_FORMAT chooses the encoding:
#   webp           lossy WebP, quality 80 (the default; about a tenth of PNG, and the
#                  same as PNG on paper)
#   webp-lossless  lossless WebP
#   png            gray PNG

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

AREAS=""
if [ "${1:-}" = "--areas" ]; then
    AREAS="${2:-}"
    shift 2 || true
fi
if [ $# -ne 4 ]; then
    echo "Usage: $0 [--areas \"<geofabrik paths>\"] <min-lon> <min-lat> <max-lon> <max-lat>" >&2
    exit 1
fi

export HILLSHADE_FORMAT="${HILLSHADE_FORMAT:-webp}" HILLSHADE_MAXZOOM="${HILLSHADE_MAXZOOM:-12}"
case "$HILLSHADE_FORMAT" in
    webp|webp-lossless|png) ;;
    *) echo "Error: HILLSHADE_FORMAT must be webp, webp-lossless or png." >&2; exit 1 ;;
esac
case "$HILLSHADE_MAXZOOM" in
    ''|*[!0-9]*) echo "Error: HILLSHADE_MAXZOOM must be a whole number (for example 12)." >&2; exit 1 ;;
esac

reexec_in_tools ${AREAS:+--areas "$AREAS"} "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"
MINZOOM=$(( HILLSHADE_MAXZOOM < 6 ? HILLSHADE_MAXZOOM : 6 ))

WORK_DIR="$REPO_DIR/data/cache/hillshade"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# shellcheck disable=SC2086
terrain_outline "$WORK_DIR" "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" $AREAS
terrain_estimate "$WORK_DIR" hillshade

echo "Step 1/3: download elevation tiles"
download_dem "$WORK_DIR"
CELLS="$(terrain_land_cells "$WORK_DIR")"
# The hillshade raster of each outline part, its overviews and the tiles (land cells
# only, after the download). Measured: under 0.015 GB per one-degree cell of mountains.
NEED_GB="$(python3 -c "print(max(2, int($CELLS * 0.03 + 2)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "hillshade for $CELLS one-degree cells with land"

terrain_mosaic "$WORK_DIR"
echo "Step 2/3: compute the hillshade, masked to the outline"
HILLSHADES=()
while read -r part raw cut xmin ymin xmax ymax res <&3; do
    hs="$WORK_DIR/hillshade-$part.tif"
    # The extent and pixel size come from region-outline.py (GDAL's own choice goes wrong
    # for a part that touches the 180° line). The elevation data is reprojected and shaded
    # in memory, chunk by chunk: no reprojected copy on disk.
    python3 "$REPO_DIR/tools/hillshade-raster.py" "$raw" "$cut" "$hs" "$xmin" "$ymin" "$xmax" "$ymax" "$res"
    # Overviews make the lower zoom levels fast to read.
    gdaladdo -q -r average "$hs" 2 4 8 16 32 64
    HILLSHADES+=("$hs")
done 3< "$WORK_DIR/outline/mosaics.txt"

echo "Step 3/3: write the raster tiles (zoom $MINZOOM-$HILLSHADE_MAXZOOM, $HILLSHADE_FORMAT)"
python3 "$REPO_DIR/tools/hillshade-tiles.py" --outline "$WORK_DIR/outline/outline.geojson" \
    --minzoom "$MINZOOM" --maxzoom "$HILLSHADE_MAXZOOM" --format "$HILLSHADE_FORMAT" \
    --attribution "Copernicus DEM (c) ESA" \
    "${HILLSHADES[@]}" "$REPO_DIR/data/hillshade.mbtiles"

report_work_space "$WORK_DIR" "$REPO_DIR/data/hillshade.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/hillshade.mbtiles"
