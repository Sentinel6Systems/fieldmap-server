#!/usr/bin/env bash
# Build all map data for one region from a region file.
# A region file lists Geofabrik extracts, a terrain bounding box, and options.
# See regions/mid-atlantic.env for an example region file.
#
# Usage:
#   ./scripts/build-region.sh [--world] [--language <code>] [--staged] <region-file> [step ...]
#
# Options (or set WORLD=yes / LANGUAGE=<code> in the region file; a flag wins):
#   --world            zoom 0-8 for the whole world, the region only from zoom 9.
#                      The world layer is built once by build-world.sh (about
#                      96 GB to download the first time) and reused.
#   --language <code>  keep map labels in one language only, to save space:
#                      en zh hi es ar fr bn pt ru id (the ten most spoken).
#                      Without it every language is kept.
#   --staged           write the new files to data/incoming and move them into data/
#                      together when every step has finished. Until then, and when a
#                      step fails, the files in data/ stay as they were, and the steps
#                      that finished are kept: the same build run again carries on
#                      from there. (Without it each step replaces its file as soon as
#                      it is done.)
#
# Steps (default: all five, in this order):
#   vector     download and merge the OSM extracts, then run Planetiler
#              -> data/basemap.mbtiles
#   contours   -> data/contours.mbtiles
#   hillshade  -> data/hillshade.mbtiles
#   elevation  heights for the print tool's terrain analysis, up to zoom
#              ELEVATION_MAXZOOM (default 12) -> data/elevation.mbtiles
#              (contours, hillshade and elevation use the 10 m USGS data when the
#              region file sets DEM_SOURCE="usgs")
#   extras     power lines, pipelines, fences and gates, towers,
#              springs and other landmarks for the print styles
#              -> data/extras.mbtiles (build-extras.sh; reuses the merged
#              OSM file from the vector step)
#   detail     only when the region file sets DETAIL_BBOX: very sharp hillshade of
#              that small area from the USGS 1 m lidar data, after the hillshade
#              step -> data/hillshade-detail.mbtiles (build-detail.sh)
# Only when named:
#   usdata     US federal data for the print styles (build-usdata.sh; the region
#              file must be inside this folder) -> data/usdata.mbtiles
#
# Rebuild only the contours:
#   ./scripts/build-region.sh <region-file> contours

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

usage() {
    echo "Usage: $0 [--world] [--language <code>] [--staged] <region-file> [vector] [contours] [hillshade] [detail] [elevation] [extras] [usdata]" >&2
    exit 1
}
OPT_WORLD="" OPT_LANGUAGE="" STAGED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --world) OPT_WORLD=yes; shift ;;
        --language) [ $# -ge 2 ] || usage; OPT_LANGUAGE="$2"; shift 2 ;;
        --staged) STAGED=yes; shift ;;
        -*) usage ;;
        *) break ;;
    esac
done
[ $# -ge 1 ] || usage

REGION_FILE="$1"
shift
if [ ! -f "$REGION_FILE" ]; then
    echo "Error: region file not found: $REGION_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$REGION_FILE"
WORLD="${OPT_WORLD:-${WORLD:-no}}"
LANGUAGE="${OPT_LANGUAGE:-${LANGUAGE:-}}"
check_language

: "${REGION_NAME:?REGION_NAME is not set in $REGION_FILE}"
: "${GEOFABRIK_AREAS:?GEOFABRIK_AREAS is not set in $REGION_FILE}"
: "${BBOX:?BBOX is not set in $REGION_FILE}"
CONTOUR_UNITS="${CONTOUR_UNITS:-ft}"
PLANETILER_MEMORY="${PLANETILER_MEMORY:-8g}"
# Elevation data for the terrain steps: copernicus or usgs (see terrain_source in lib.sh).
export DEM_SOURCE="${DEM_SOURCE:-copernicus}"
ELEVATION_MAXZOOM="${ELEVATION_MAXZOOM:-$(terrain_default_zoom elevation)}"
# Hillshade tile format and highest zoom (build-hillshade.sh reads them from the environment).
export HILLSHADE_FORMAT="${HILLSHADE_FORMAT:-webp}" HILLSHADE_MAXZOOM="${HILLSHADE_MAXZOOM:-$(terrain_default_zoom hillshade)}"

# shellcheck disable=SC2206
BBOX_ARGS=($BBOX)
if [ ${#BBOX_ARGS[@]} -ne 4 ]; then
    echo "Error: BBOX must have 4 numbers: min-lon min-lat max-lon max-lat" >&2
    exit 1
fi
# A box whose min-lon is larger than its max-lon crosses the 180° line. The terrain
# builds cover both sides; the base map and extras (Planetiler) take the wider side only.
BBOX_CSV="$(python3 -c "
w, s, e, n = map(float, '${BBOX_ARGS[*]}'.split())
if w > e:
    w, e = (w, 180.0) if 180 - w > e + 180 else (-180.0, e)
print(f'{w:g},{s:g},{e:g},{n:g}')")"
# The terrain builds cover the areas' Geofabrik outlines inside BBOX (tools/region-outline.py).
# shellcheck disable=SC2086
AREAS_LIST="$(printf '%s ' $GEOFABRIK_AREAS)"

STEPS=("$@")
if [ ${#STEPS[@]} -eq 0 ]; then
    STEPS=(vector contours hillshade ${DETAIL_BBOX:+detail} elevation extras)
fi
for step in "${STEPS[@]}"; do
    case "$step" in
        vector|contours|hillshade|elevation|extras|usdata) ;;
        detail) : "${DETAIL_BBOX:?the detail step needs DETAIL_BBOX in $REGION_FILE}" ;;
        *) echo "Error: unknown step '$step'" >&2; exit 1 ;;
    esac
done

run_vector() {
    # shellcheck disable=SC2086
    "$REPO_DIR/scripts/merge-osm.sh" "$REGION_NAME" $GEOFABRIK_AREAS

    # Fetch Planetiler's shared inputs here, with retries and fallback
    # mirrors, so Planetiler itself makes no network calls.
    fetch_shared_sources "$REPO_DIR/data/sources"

    echo "Planetiler: build data/basemap.mbtiles (Java heap $PLANETILER_MEMORY${LANGUAGE:+, language $LANGUAGE}$([ "$WORLD" = yes ] && echo ", world layer for zoom 0-8"))"
    # --bounds limits the output to the region box. Without it Planetiler
    # takes the bounds from the merged OSM data, which can be far larger
    # (extracts include complete ways and relations that reach outside).
    build_basemap "$REGION_NAME.osm.pbf" "$PLANETILER_MEMORY" --bounds="$BBOX_CSV"
}

run_contours() {
    "$REPO_DIR/scripts/build-contours.sh" --areas "$AREAS_LIST" "${BBOX_ARGS[@]}" "$CONTOUR_UNITS"
}

run_hillshade() {
    "$REPO_DIR/scripts/build-hillshade.sh" --areas "$AREAS_LIST" "${BBOX_ARGS[@]}"
}

run_elevation() {
    "$REPO_DIR/scripts/build-elevation.sh" --areas "$AREAS_LIST" "${BBOX_ARGS[@]}" "$ELEVATION_MAXZOOM"
}

run_extras() {
    if [ ! -s "$REPO_DIR/data/sources/$REGION_NAME.osm.pbf" ]; then
        # shellcheck disable=SC2086
        "$REPO_DIR/scripts/merge-osm.sh" "$REGION_NAME" $GEOFABRIK_AREAS
    fi
    PLANETILER_MEMORY="$PLANETILER_MEMORY" "$REPO_DIR/scripts/build-extras.sh" --bounds="$BBOX_CSV" "$REGION_NAME.osm.pbf"
}

run_detail() {
    # shellcheck disable=SC2086
    "$REPO_DIR/scripts/build-detail.sh" $DETAIL_BBOX
}

run_usdata() {
    "$REPO_DIR/scripts/build-usdata.sh" "$REGION_FILE"
}

if [ -n "$STAGED" ]; then
    # The build scripts read FIELDMAP_STAGE (see OUT_DIR in lib.sh).
    export FIELDMAP_STAGE="incoming"
    # shellcheck disable=SC2034
    OUT_DIR="$REPO_DIR/data/$FIELDMAP_STAGE" OUT_SUB="$FIELDMAP_STAGE/"
    # A staged build that stops keeps the steps it finished in data/incoming. The same build
    # (same region file and settings) run again carries on from there; any other starts
    # afresh. The key is what the files depend on.
    KEY="$( { cat "$REGION_FILE"; echo "$DEM_SOURCE $HILLSHADE_FORMAT $HILLSHADE_MAXZOOM $ELEVATION_MAXZOOM $CONTOUR_UNITS $WORLD $LANGUAGE"; } | sha1sum | cut -c1-16)"
    if [ "$(cat "$OUT_DIR/.key" 2>/dev/null)" != "$KEY" ]; then
        rm -rf "$OUT_DIR"
    fi
    mkdir -p "$OUT_DIR"
    echo "$KEY" > "$OUT_DIR/.key"
    trap 'if [ -d "$OUT_DIR" ]; then echo; echo "The build did not finish: the map files in data/ were not changed."; echo "The steps that finished are kept: run the same build again to carry on from where it stopped."; fi' EXIT
fi

echo "Region: $REGION_NAME${LANGUAGE:+ (labels: $LANGUAGE)}$([ "$WORLD" = yes ] && echo ' (world layer for zoom 0-8)')"
echo "Steps:  ${STEPS[*]}"
mkdir -p "$REPO_DIR/data"
echo "Free disk space: $(free_gb "$REPO_DIR/data") GB on the drive that holds $REPO_DIR/data"
echo "Each step checks its own space needs before it starts."
TERRAIN_STEPS=()
for step in "${STEPS[@]}"; do
    case "$step" in contours|hillshade|elevation) TERRAIN_STEPS+=("$step") ;; esac
done
if [ ${#TERRAIN_STEPS[@]} -gt 0 ]; then
    echo
    ELEVATION_MAXZOOM="$ELEVATION_MAXZOOM" "$REPO_DIR/scripts/terrain-estimate.sh" \
        --areas "$AREAS_LIST" "${BBOX_ARGS[@]}" "${TERRAIN_STEPS[@]}"
fi
for step in "${STEPS[@]}"; do
    start=$(date +%s)
    echo
    echo "=== $step ==="
    if [ -n "$STAGED" ] && grep -qx "$step" "$OUT_DIR/.done" 2>/dev/null; then
        echo "=== $step finished in an earlier run of this build: kept ==="
        continue
    fi
    "run_$step"
    end=$(date +%s)
    [ -z "$STAGED" ] || echo "$step" >> "$OUT_DIR/.done"
    echo "=== $step finished in $(( (end - start) / 60 )) min ==="
done

if [ -n "$STAGED" ]; then
    echo
    echo "Every step finished: putting the new files in place."
    for f in "$OUT_DIR"/*.mbtiles; do
        mv -f "$f" "$REPO_DIR/data/"
    done
    rm -f "$OUT_DIR/.key" "$OUT_DIR/.done"
    rmdir "$OUT_DIR"
fi

# The print styles read data/extras.mbtiles and data/usdata.mbtiles, the print tool
# data/elevation.mbtiles; without the extras or elevation step or build-usdata.sh they
# are empty files.
"$REPO_DIR/scripts/data-placeholders.sh"

echo
echo "Output files:"
ls -lh "$REPO_DIR"/data/*.mbtiles 2>/dev/null || echo "(none)"
