#!/usr/bin/env bash
# Build contour line vector tiles for one region.
# The script downloads Copernicus GLO-30 elevation data.
# The output file is data/contours.mbtiles.
#
# Usage:
#   ./scripts/build-contours.sh <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]
#
# The last argument selects the units:
#   m  = meters. Minor interval 20 m, major interval 100 m. This is the default.
#   ft = feet. Minor interval 40 ft, major interval 200 ft.
#
# Output tile schema:
#   Layer "contours_major": lines, zoom 8-14.
#   Layer "contours_minor": lines, zoom 11-14.
# The elevation attribute name states the units:
#   "elev_m" (number) when you build in meters.
#   "elev_ft" (integer) when you build in feet.
# This matches the terrain_builder project. Its mbtiles files are a
# drop-in replacement for data/contours.mbtiles.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -lt 4 ] || [ $# -gt 5 ]; then
    echo "Usage: $0 <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]" >&2
    exit 1
fi

reexec_in_tools "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"
UNITS="${5:-m}"

case "$UNITS" in
    m)  MINOR=20; MAJOR=100; ELEV_ATTR="elev_m" ;;
    ft) MINOR=40; MAJOR=200; ELEV_ATTR="elev_ft" ;;
    *)  echo "Error: units must be 'm' or 'ft'." >&2; exit 1 ;;
esac

DEM_DIR="$REPO_DIR/data/cache/dem"
WORK_DIR="$REPO_DIR/data/cache/contours"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

# Disk estimate: the elevation tiles, the feet-converted copy, the contour
# lines, and the tile build each take space, scaling with the box area.
AREA_DEG2="$(bbox_area "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT")"
NEED_GB="$(python3 -c "print(max(8, int($AREA_DEG2 * 0.6 + 5)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "contours for a $AREA_DEG2 square-degree area"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo "Step 1/5: download elevation tiles"
download_dem "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" "$DEM_DIR"

echo "Step 2/5: merge and clip the elevation data"
gdalbuildvrt -q -te "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" \
    "$WORK_DIR/dem.vrt" "$DEM_DIR"/*.tif

DEM_INPUT="$WORK_DIR/dem.vrt"
if [ "$UNITS" = "ft" ]; then
    echo "Step 2b: convert elevation values to feet"
    # Float32 halves the file size (gdal_calc defaults to Float64).
    # BIGTIFF=YES lifts the 4 GB TIFF limit, which large regions exceed.
    # TILED=YES is essential: gdal_calc writes in square blocks, and with
    # a strip-based TIFF each partial strip write appends a new compressed
    # copy of the strip, so the file grows to many times its real size.
    gdal_calc.py -A "$WORK_DIR/dem.vrt" --calc="A*3.28084" \
        --outfile="$WORK_DIR/dem_ft.tif" --type=Float32 \
        --co COMPRESS=DEFLATE --co PREDICTOR=3 --co BIGTIFF=YES \
        --co TILED=YES --co BLOCKXSIZE=512 --co BLOCKYSIZE=512 --quiet
    DEM_INPUT="$WORK_DIR/dem_ft.tif"
fi

echo "Step 3/5: compute contour lines (interval $MINOR $UNITS)"
gdal_contour -q -a elev -i "$MINOR" "$DEM_INPUT" "$WORK_DIR/contours.gpkg" -nln contour

echo "Step 4/5: split major and minor contours"
# Feet values are whole interval multiples. Store them as integers.
if [ "$UNITS" = "ft" ]; then
    SELECT="CAST(ROUND(elev) AS INTEGER) AS $ELEV_ATTR, geom"
else
    SELECT="elev AS $ELEV_ATTR, geom"
fi
ogr2ogr -f GeoJSONSeq "$WORK_DIR/major.json" "$WORK_DIR/contours.gpkg" \
    -dialect SQLITE -sql "SELECT $SELECT FROM contour WHERE CAST(ROUND(elev) AS INTEGER) % $MAJOR = 0"
ogr2ogr -f GeoJSONSeq "$WORK_DIR/minor.json" "$WORK_DIR/contours.gpkg" \
    -dialect SQLITE -sql "SELECT $SELECT FROM contour WHERE CAST(ROUND(elev) AS INTEGER) % $MAJOR <> 0"

echo "Step 5/5: build the vector tiles"
tippecanoe -q -f -o "$WORK_DIR/major.mbtiles" -l contours_major \
    -Z8 -z14 -y "$ELEV_ATTR" --drop-densest-as-needed \
    -A "Copernicus DEM (c) ESA" "$WORK_DIR/major.json"
tippecanoe -q -f -o "$WORK_DIR/minor.mbtiles" -l contours_minor \
    -Z11 -z14 -y "$ELEV_ATTR" --drop-densest-as-needed \
    -A "Copernicus DEM (c) ESA" "$WORK_DIR/minor.json"
tile-join -f -o "$REPO_DIR/data/contours.mbtiles" -n "Contours ($UNITS)" \
    "$WORK_DIR/major.mbtiles" "$WORK_DIR/minor.mbtiles"

report_work_space "$WORK_DIR" "$REPO_DIR/data/contours.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/contours.mbtiles (units: $UNITS)"
