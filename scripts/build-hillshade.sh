#!/usr/bin/env bash
# Build hillshade raster tiles for one region.
# The script downloads Copernicus GLO-30 elevation data.
# The output file is data/hillshade.mbtiles.
#
# Usage:
#   ./scripts/build-hillshade.sh <min-lon> <min-lat> <max-lon> <max-lat>
#
# Output: gray PNG raster tiles, zoom levels about 6 to 12.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -ne 4 ]; then
    echo "Usage: $0 <min-lon> <min-lat> <max-lon> <max-lat>" >&2
    exit 1
fi

reexec_in_tools "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"

DEM_DIR="$REPO_DIR/data/cache/dem"
WORK_DIR="$REPO_DIR/data/cache/hillshade"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

AREA_DEG2="$(bbox_area "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT")"
# Measured: 2 GB working space for a 64 square-degree region.
NEED_GB="$(python3 -c "print(max(4, int($AREA_DEG2 * 0.06 + 2)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "hillshade for a $AREA_DEG2 square-degree area"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo "Step 1/4: download elevation tiles"
download_dem "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" "$DEM_DIR"

echo "Step 2/4: merge, clip, and reproject the elevation data"
gdalbuildvrt -q -te "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" \
    "$WORK_DIR/dem.vrt" "$DEM_DIR"/*.tif
# BIGTIFF=YES lifts the 4 GB TIFF limit, which large regions exceed.
gdalwarp -q -t_srs EPSG:3857 -r bilinear \
    -co COMPRESS=DEFLATE -co PREDICTOR=3 -co BIGTIFF=YES -co TILED=YES \
    "$WORK_DIR/dem.vrt" "$WORK_DIR/dem_3857.tif"

echo "Step 3/4: compute the hillshade"
gdaldem hillshade -q -z 1 -az 315 -alt 45 -compute_edges \
    -co COMPRESS=DEFLATE -co BIGTIFF=YES -co TILED=YES \
    "$WORK_DIR/dem_3857.tif" "$WORK_DIR/hillshade.tif"

echo "Step 4/4: write the raster tiles"
gdal_translate -q -of MBTILES -co TILE_FORMAT=PNG \
    "$WORK_DIR/hillshade.tif" "$REPO_DIR/data/hillshade.mbtiles"
# Add the lower zoom levels.
gdaladdo -q -r average "$REPO_DIR/data/hillshade.mbtiles" 2 4 8 16 32 64

report_work_space "$WORK_DIR" "$REPO_DIR/data/hillshade.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/hillshade.mbtiles"
