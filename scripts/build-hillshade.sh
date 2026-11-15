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
CELLS="$(terrain_cells "$WORK_DIR")"
# Measured: 2 GB working space for 64 one-degree cells.
NEED_GB="$(python3 -c "print(max(4, int($CELLS * 0.06 + 2)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "hillshade for $CELLS one-degree cells"

echo "Step 1/4: download elevation tiles"
download_dem "$WORK_DIR"

echo "Step 2/4: reproject the elevation data, masked to the outline"
HILLSHADES=()
while read -r part raw _masked cut xmin ymin xmax ymax res <&3; do
    # The extent and pixel size come from region-outline.py (gdalwarp's own choice goes
    # wrong for a part that touches the 180° line).
    # BIGTIFF=YES lifts the 4 GB TIFF limit, which large regions exceed.
    gdalwarp -q -t_srs EPSG:3857 -te "$xmin" "$ymin" "$xmax" "$ymax" -tr "$res" "$res" \
        -r bilinear -cutline "$cut" -dstnodata -32768 -ot Float32 \
        -co COMPRESS=DEFLATE -co PREDICTOR=3 -co BIGTIFF=YES -co TILED=YES \
        "$raw" "$WORK_DIR/dem_3857-$part.tif"
    HILLSHADES+=("$WORK_DIR/hillshade-$part.tif")
done 3< "$WORK_DIR/outline/mosaics.txt"

echo "Step 3/4: compute the hillshade"
for hs in "${HILLSHADES[@]}"; do
    dem="$WORK_DIR/dem_3857-${hs##*/hillshade-}"
    # Outside the outline the hillshade is 0 (no data); the tiles are transparent there.
    gdaldem hillshade -q -z 1 -az 315 -alt 45 -compute_edges \
        -co COMPRESS=DEFLATE -co BIGTIFF=YES -co TILED=YES \
        "$dem" "$hs"
    rm -f "$dem"
    # Overviews make the lower zoom levels fast to read.
    gdaladdo -q -r average "$hs" 2 4 8 16 32 64
done

echo "Step 4/4: write the raster tiles (zoom $MINZOOM-$HILLSHADE_MAXZOOM, $HILLSHADE_FORMAT)"
python3 "$REPO_DIR/tools/hillshade-tiles.py" --outline "$WORK_DIR/outline/outline.geojson" \
    --minzoom "$MINZOOM" --maxzoom "$HILLSHADE_MAXZOOM" --format "$HILLSHADE_FORMAT" \
    --attribution "Copernicus DEM (c) ESA" \
    "${HILLSHADES[@]}" "$REPO_DIR/data/hillshade.mbtiles"

report_work_space "$WORK_DIR" "$REPO_DIR/data/hillshade.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/hillshade.mbtiles"
