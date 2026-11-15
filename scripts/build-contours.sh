#!/usr/bin/env bash
# Build contour line vector tiles for one region.
# The script downloads Copernicus GLO-30 elevation data.
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
# Disk estimate (checked after the download, so cells of open sea are not counted):
# the lines of every cell, then tippecanoe's temporary files and the tiles beside them.
# Measured on rugged mountains: 26 kB per km², about 0.2 GB for a whole cell.
NEED_GB="$(python3 -c "print(max(4, int($CELLS * 0.25 + 3)))")"
require_free_space "$REPO_DIR/data" "$NEED_GB" "contours for $CELLS one-degree cells with land"

# The lines of finished cells are kept until the build is complete, so a stopped build
# carries on where it was. They belong to one outline and one unit: others are removed.
SIGNATURE="$( (echo "v1 $UNITS"; cat "$WORK_DIR/outline/outline.geojson") | sha1sum | cut -c1-12)"
CELLS_ROOT="$REPO_DIR/data/cache/contour-cells"
CELLS_DIR="$CELLS_ROOT/$SIGNATURE"
mkdir -p "$CELLS_DIR"
find "$CELLS_ROOT" -mindepth 1 -maxdepth 1 ! -name "$SIGNATURE" -exec rm -rf {} +

echo "Step 2/3: compute contour lines, one elevation cell at a time (interval $MINOR $UNITS)"
python3 "$REPO_DIR/tools/contour-cells.py" --outline-dir "$WORK_DIR/outline" \
    --dem-dir "$REPO_DIR/data/cache/dem" --out-dir "$CELLS_DIR" --units "$UNITS" \
    ${CONTOUR_JOBS:+--jobs "$CONTOUR_JOBS"}
mapfile -t MAJOR_FILES < <(find "$CELLS_DIR" -name '*-major.fgb' | sort)
mapfile -t MINOR_FILES < <(find "$CELLS_DIR" -name '*-minor.fgb' | sort)
if [ ${#MAJOR_FILES[@]} -eq 0 ] && [ ${#MINOR_FILES[@]} -eq 0 ]; then
    echo "Error: no contour lines in this outline (flat, or all sea)." >&2
    exit 1
fi

echo "Step 3/3: build the vector tiles"
# One tippecanoe run over all cells, so the low zooms are thinned the same way everywhere.
# -t keeps its temporary files on the data drive (the container's own /tmp is small).
# -T: tippecanoe reads FlatGeobuf numbers as text unless told the type.
mkdir -p "$WORK_DIR/tmp"
JOIN=()
if [ ${#MAJOR_FILES[@]} -gt 0 ]; then
    tippecanoe -q -f -t "$WORK_DIR/tmp" -o "$WORK_DIR/major.mbtiles" -l contours_major \
        -Z8 -z14 -y "$ELEV_ATTR" -T "$ELEV_ATTR:$ELEV_TYPE" --drop-densest-as-needed \
        -A "Copernicus DEM (c) ESA" "${MAJOR_FILES[@]}"
    JOIN+=("$WORK_DIR/major.mbtiles")
fi
if [ ${#MINOR_FILES[@]} -gt 0 ]; then
    tippecanoe -q -f -t "$WORK_DIR/tmp" -o "$WORK_DIR/minor.mbtiles" -l contours_minor \
        -Z11 -z14 -y "$ELEV_ATTR" -T "$ELEV_ATTR:$ELEV_TYPE" --drop-densest-as-needed \
        -A "Copernicus DEM (c) ESA" "${MINOR_FILES[@]}"
    JOIN+=("$WORK_DIR/minor.mbtiles")
fi
tile-join -q -f -o "$REPO_DIR/data/contours.mbtiles" -n "Contours ($UNITS)" "${JOIN[@]}"

echo "Contour lines of all cells: $(du -sh "$CELLS_DIR" | cut -f1)."
report_work_space "$WORK_DIR" "$REPO_DIR/data/contours.mbtiles"
rm -rf "$WORK_DIR" "$CELLS_ROOT"
echo "Done. Output: data/contours.mbtiles (units: $UNITS)"
