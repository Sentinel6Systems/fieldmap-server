#!/usr/bin/env bash
# Build elevation raster tiles for one region: heights in metres as Mapbox terrain-RGB
# (0.1 m steps) in lossless WebP, for the print tool's terrain analysis (viewshed and radio coverage).
# Same Copernicus GLO-30 elevation data as build-hillshade.sh and build-contours.sh.
# The output file is data/elevation.mbtiles.
#
# Usage:
#   ./scripts/build-elevation.sh <min-lon> <min-lat> <max-lon> <max-lat> [maxzoom]
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

if [ $# -lt 4 ] || [ $# -gt 5 ]; then
    echo "Usage: $0 <min-lon> <min-lat> <max-lon> <max-lat> [maxzoom]" >&2
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

reexec_in_tools "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"
MINZOOM=$(( MAXZOOM < 8 ? MAXZOOM : 8 ))

DEM_DIR="$REPO_DIR/data/cache/dem"
WORK_DIR="$REPO_DIR/data/cache/elevation"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

AREA_DEG2="$(bbox_area "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT")"
# The tiles are written straight from the downloaded elevation data; the space needed
# is the downloads plus the output (measured: about 40 MB per square degree at zoom 12).
NEED_GB="$(python3 -c "print(max(2, int($AREA_DEG2 * 0.1 + 1)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "elevation tiles for a $AREA_DEG2 square-degree area"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo "Step 1/2: download elevation tiles"
download_dem "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" "$DEM_DIR"

echo "Step 2/2: write the elevation tiles (zoom $MINZOOM-$MAXZOOM, $ELEVATION_ENCODING $ELEVATION_FORMAT)"
# The mosaic is the downloaded 1-degree cells around the box, on their own grid (cutting
# it with gdalbuildvrt -te would shift the heights by half a pixel). Within those cells a
# missing file is open sea (Copernicus has no tiles there) and reads as 0 m; outside
# them there is no data.
mapfile -t DEM_FILES < <(python3 - "$DEM_DIR" "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" <<'PY'
import math, os, sys
d, w, s, e, n = sys.argv[1], *map(float, sys.argv[2:])
for lat in range(math.floor(s), math.floor(n) + 1):
    for lon in range(math.floor(w), math.floor(e) + 1):
        f = f"{d}/Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM.tif"
        if os.path.exists(f):
            print(f)
PY
)
gdalbuildvrt -q "$WORK_DIR/dem.vrt" "${DEM_FILES[@]}"
python3 "$REPO_DIR/tools/elevation-tiles.py" \
    --bbox "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" \
    --minzoom "$MINZOOM" --maxzoom "$MAXZOOM" \
    --encoding "$ELEVATION_ENCODING" --format "$ELEVATION_FORMAT" \
    --source "Copernicus GLO-30" --source-date "2011-2015" \
    --attribution "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved" \
    "$WORK_DIR/dem.vrt" "$REPO_DIR/data/elevation.mbtiles"

report_work_space "$WORK_DIR" "$REPO_DIR/data/elevation.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/elevation.mbtiles (zoom $MINZOOM-$MAXZOOM)"
