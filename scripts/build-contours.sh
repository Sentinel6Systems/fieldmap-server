#!/usr/bin/env bash
# Build contour line vector tiles for one region.
# The script downloads Copernicus GLO-30 elevation data, or with DEM_SOURCE=usgs the
# 10 m USGS data where it exists (see terrain_source in lib.sh).
# The output file is data/contours.mbtiles.
#
# Usage:
#   ./scripts/build-contours.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]
#
# With --areas (Geofabrik extract paths, as in a region file) the contours cover only
# those areas' outlines inside the box; without it, the whole box. A box whose min-lon is
# larger than its max-lon crosses the 180° line. See tools/region-outline.py.
#
# The lines are computed one 1° elevation cell at a time, CONTOUR_JOBS cells in parallel
# (default: one fewer than the CPUs), and kept in data/cache/contour-cells until the build
# is complete: run the same command again after a stop and it carries on with the
# missing cells. See tools/contour-cells.py.
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

AREAS=""
if [ "${1:-}" = "--areas" ]; then
    AREAS="${2:-}"
    shift 2 || true
fi
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
    echo "Usage: $0 [--areas \"<geofabrik paths>\"] <min-lon> <min-lat> <max-lon> <max-lat> [m|ft]" >&2
    exit 1
fi

reexec_in_tools ${AREAS:+--areas "$AREAS"} "$@"

MIN_LON="$1" MIN_LAT="$2" MAX_LON="$3" MAX_LAT="$4"
UNITS="${5:-m}"

case "$UNITS" in
    m)  MINOR=20; ELEV_ATTR="elev_m"; ELEV_TYPE=float ;;
    ft) MINOR=40; ELEV_ATTR="elev_ft"; ELEV_TYPE=int ;;
    *)  echo "Error: units must be 'm' or 'ft'." >&2; exit 1 ;;
esac

WORK_DIR="$REPO_DIR/data/cache/contours"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# shellcheck disable=SC2086
terrain_outline "$WORK_DIR" "$MIN_LON" "$MIN_LAT" "$MAX_LON" "$MAX_LAT" $AREAS
terrain_estimate "$WORK_DIR" contours

echo "Step 1/3: download elevation tiles"
download_dem "$WORK_DIR"
CELLS="$(terrain_land_cells "$WORK_DIR")"

# The lines of finished cells, and the tiles made from them, are kept until the build is
# complete, so a stopped build carries on where it was. They belong to one outline, one
# unit and one elevation source: others are removed.
SIGNATURE="$( (echo "v3 $UNITS $DEM_SOURCE"; cat "$WORK_DIR/outline/outline.geojson") | sha1sum | cut -c1-12)"
CELLS_ROOT="$REPO_DIR/data/cache/contour-cells"
CELLS_DIR="$CELLS_ROOT/$SIGNATURE"
mkdir -p "$CELLS_DIR"
find "$CELLS_ROOT" -mindepth 1 -maxdepth 1 ! -name "$SIGNATURE" -exec rm -rf {} +
rm -f "$CELLS_DIR"/*.part.*

# Disk estimate (checked after the download, so cells of open sea are not counted): the
# lines of every cell, then tippecanoe's temporary files and the tiles beside them.
# Measured on rugged mountains: 17 kB per km² at the peak, about 0.15 GB for a whole
# cell; from the USGS data 48 kB per km², about 0.4 GB. What an earlier, stopped run
# already wrote is taken off.
HAVE_MB="$(du -sm "$CELLS_DIR" | cut -f1)"
PER_CELL=0.2
[ "$DEM_SOURCE" = usgs ] && PER_CELL=0.5
NEED_GB="$(python3 -c "print(max(4, int($CELLS * $PER_CELL + 3 - $HAVE_MB / 1024)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "contours for $CELLS one-degree cells with land"

echo "Step 2/3: compute contour lines, one elevation cell at a time (interval $MINOR $UNITS)"
python3 "$REPO_DIR/tools/contour-cells.py" --outline-dir "$WORK_DIR/outline" \
    --dem-dir "$REPO_DIR/data/cache/dem" "${USGS_ARGS[@]}" --out-dir "$CELLS_DIR" --units "$UNITS" \
    ${CONTOUR_JOBS:+--jobs "$CONTOUR_JOBS"}

echo "Step 3/3: build the vector tiles"
# tippecanoe maps its temporary files into memory. Keep them on the data drive (the
# container's own /tmp is on Docker's drive and not counted by the disk check) unless
# that drive cannot do it (some network and FUSE mounts).
TIPPE_TMP="$WORK_DIR/tmp"
mkdir -p "$TIPPE_TMP"
if ! python3 -c "
import mmap, sys
with open(sys.argv[1], 'w+b') as f:
    f.truncate(4096)
    mmap.mmap(f.fileno(), 4096).close()" "$TIPPE_TMP/probe" 2>/dev/null; then
    echo "Note: $REPO_DIR/data cannot map files into memory; tippecanoe's temporary files go to /tmp."
    TIPPE_TMP=/tmp
fi
rm -f "$TIPPE_TMP/probe"

# tippecanoe thins each line to within one tile unit of its place (1/4096 of a tile at
# each zoom). The 10 m USGS data gets half that tolerance, so that its finer shapes stay.
SIMPLIFY=() ATTRIBUTION="Copernicus DEM (c) ESA"
if [ "$DEM_SOURCE" = usgs ]; then
    SIMPLIFY=(--simplification=0.5)
    ATTRIBUTION="USGS 3D Elevation Program; $ATTRIBUTION"
fi

# One tippecanoe run per layer over all cells, so the low zooms are thinned the same way
# everywhere. A finished layer is kept, and its cells' lines are deleted at once.
# Arguments: KIND LAYER MINZOOM
tile_layer() {
    local kind="$1" layer="$2" minzoom="$3" files
    [ -f "$CELLS_DIR/$kind.mbtiles" ] && return 0
    # Short names, relative to the folder: a large region has thousands of files.
    mapfile -t files < <(cd "$CELLS_DIR" && find . -maxdepth 1 -name "*-$kind.fgb" | sort)
    [ ${#files[@]} -gt 0 ] || return 0
    # -T: tippecanoe reads FlatGeobuf numbers as text unless told the type.
    (cd "$CELLS_DIR" && tippecanoe -q -f -t "$TIPPE_TMP" -o "$kind.part.mbtiles" -l "$layer" \
        -Z"$minzoom" -z14 -y "$ELEV_ATTR" -T "$ELEV_ATTR:$ELEV_TYPE" --drop-densest-as-needed \
        "${SIMPLIFY[@]}" -A "$ATTRIBUTION" "${files[@]}")
    mv "$CELLS_DIR/$kind.part.mbtiles" "$CELLS_DIR/$kind.mbtiles"
    (cd "$CELLS_DIR" && rm -f "${files[@]}")
}
tile_layer major contours_major 8
tile_layer minor contours_minor 11
mapfile -t JOIN < <(find "$CELLS_DIR" -maxdepth 1 \( -name major.mbtiles -o -name minor.mbtiles \) | sort)
if [ ${#JOIN[@]} -eq 0 ]; then
    echo "Error: no contour lines in this outline (flat, or all sea)." >&2
    exit 1
fi
# -pk: no size limit here. Each layer was already kept under tippecanoe's 500 kB a tile;
# without it tile-join leaves out every tile where the two together are larger.
tile-join -q -pk -f -o "$REPO_DIR/data/contours.mbtiles" -n "Contours ($UNITS)" "${JOIN[@]}"

report_work_space "$CELLS_DIR" "$REPO_DIR/data/contours.mbtiles"
rm -rf "$WORK_DIR" "$CELLS_ROOT"
echo "Done. Output: data/contours.mbtiles (units: $UNITS)"
