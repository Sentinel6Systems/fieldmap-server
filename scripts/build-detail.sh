#!/usr/bin/env bash
# Build a small area of very sharp hillshade from the USGS 3DEP 1 m elevation data
# (lidar, bare ground). United States only, and only where USGS has published a 1 m
# survey. The output file is data/hillshade-detail.mbtiles.
#
# Usage:
#   ./scripts/build-detail.sh <min-lon> <min-lat> <max-lon> <max-lat>
#
# The area is limited to DETAIL_MAX_KM2 (default 10000 km², 100 x 100 km): at 1 m, every
# 100 km² is a download of 0.3 to 1.5 GB (more where several surveys overlap) and about
# 3,000 tiles, and 100 x 100 km takes some hours. A region file sets
# the area as DETAIL_BBOX and build-region.sh then runs this script as its "detail" step.
#
# The tiles are a layer of their own, drawn over the hillshade: zooms above the
# hillshade's own (which must exist already, build-hillshade.sh), up to DETAIL_MAXZOOM
# (default 17, 1.2 m pixels at the equator), inside the area only. One file holds one
# area: a new one replaces the old. Contours and the elevation tiles are not changed.
#
# The USGS tiles (10 km squares) are kept in data/cache/dem-usgs1m.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -ne 4 ]; then
    echo "Usage: $0 <min-lon> <min-lat> <max-lon> <max-lat>" >&2
    exit 1
fi
export DETAIL_MAX_KM2="${DETAIL_MAX_KM2:-10000}" DETAIL_MAXZOOM="${DETAIL_MAXZOOM:-17}"
export HILLSHADE_FORMAT="${HILLSHADE_FORMAT:-webp}"

reexec_in_tools "$@"

WORK_DIR="$REPO_DIR/data/cache/detail"
DEM_DIR="$REPO_DIR/data/cache/dem-usgs1m"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR" "$DEM_DIR"

# The zooms start above the hillshade's own: the one this build has staged, else the one
# in use.
BASE="$OUT_DIR/hillshade.mbtiles"
[ -s "$BASE" ] || BASE="$REPO_DIR/data/hillshade.mbtiles"
if [ ! -s "$BASE" ]; then
    echo "Error: there are no hillshade tiles yet. Build the hillshade first: the detail area goes on top of it." >&2
    exit 1
fi
BASE_ZOOM="$(python3 -c "
import sqlite3, sys
print(dict(sqlite3.connect(sys.argv[1]).execute('SELECT name, value FROM metadata')).get('maxzoom', ''))" "$BASE")"
if [ -z "$BASE_ZOOM" ] || [ "$BASE_ZOOM" -ge "$DETAIL_MAXZOOM" ]; then
    echo "Error: the hillshade goes to zoom ${BASE_ZOOM:-?}; DETAIL_MAXZOOM ($DETAIL_MAXZOOM) must be higher." >&2
    exit 1
fi

echo "Step 1/4: find the USGS 1 m elevation tiles of the area"
python3 "$REPO_DIR/tools/detail-area.py" find --bbox "$@" --out "$WORK_DIR" \
    --max-km2 "$DETAIL_MAX_KM2" --zoom "$DETAIL_MAXZOOM"
TILES="$(grep -c . "$WORK_DIR/tiles.txt")"
# Up to about 400 MB a tile to download, then the hillshade raster and the tiles.
require_free_space "$REPO_DIR/data" "$(( TILES * 6 / 10 + 2 ))" "the 1 m detail area ($TILES elevation tiles)"

echo "Step 2/4: download them"
fetched=0
while read -r url <&3; do
    name="${url##*/}"
    echo "$DEM_DIR/$name" >> "$WORK_DIR/rasters.txt"
    [ -f "$DEM_DIR/$name" ] && continue
    if fetch_file "$url" "$DEM_DIR/$name"; then
        echo "download: $name"
        fetched=$((fetched + 1))
    else
        echo "Error: $name is listed by USGS but could not be downloaded." >&2
        exit 1
    fi
done 3< "$WORK_DIR/tiles.txt"
echo "Elevation tiles: $fetched downloaded, $((TILES - fetched)) already here."

echo "Step 3/4: compute the hillshade"
read -r xmin ymin xmax ymax res < "$WORK_DIR/grid.txt"
python3 "$REPO_DIR/tools/hillshade-raster.py" "@$WORK_DIR/rasters.txt" "$WORK_DIR/box.geojson" \
    "$WORK_DIR/hillshade.tif" "$xmin" "$ymin" "$xmax" "$ymax" "$res"
gdaladdo -q -r average "$WORK_DIR/hillshade.tif" 2 4 8

echo "Step 4/4: write the tiles (zoom $((BASE_ZOOM + 1))-$DETAIL_MAXZOOM, $HILLSHADE_FORMAT)"
python3 "$REPO_DIR/tools/hillshade-tiles.py" --outline "$WORK_DIR/box.geojson" \
    --minzoom "$((BASE_ZOOM + 1))" --maxzoom "$DETAIL_MAXZOOM" --format "$HILLSHADE_FORMAT" \
    --attribution "USGS 3D Elevation Program" \
    "$WORK_DIR/hillshade.tif" "$OUT_DIR/hillshade-detail.mbtiles"

report_work_space "$WORK_DIR" "$OUT_DIR/hillshade-detail.mbtiles"
rm -rf "$WORK_DIR"
echo "Done. Output: data/hillshade-detail.mbtiles (zoom $((BASE_ZOOM + 1))-$DETAIL_MAXZOOM)"
