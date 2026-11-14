#!/usr/bin/env bash
# Build all map data for one region from a region file.
# A region file lists Geofabrik extracts, a terrain bounding box, and options.
# See regions/mid-atlantic.env for an example.
#
# Usage:
#   ./scripts/build-region.sh [--world] [--language <code>] <region-file> [step ...]
#
# Options (or set WORLD=yes / LANGUAGE=<code> in the region file; a flag wins):
#   --world            zoom 0-8 for the whole world, the region only from zoom 9.
#                      The world layer is built once by build-world.sh (about
#                      96 GB to download the first time) and reused.
#   --language <code>  keep map labels in one language only, to save space:
#                      en zh hi es ar fr bn pt ru id (the ten most spoken).
#                      Without it every language is kept.
#
# Steps (default: all three, in this order):
#   vector     download and merge the OSM extracts, then run Planetiler
#              -> data/basemap.mbtiles
#   contours   -> data/contours.mbtiles
#   hillshade  -> data/hillshade.mbtiles
#
# Example: rebuild only the contours
#   ./scripts/build-region.sh regions/mid-atlantic.env contours

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"
trap 'disk_full_hint "$REPO_DIR/data"' ERR

usage() {
    echo "Usage: $0 [--world] [--language <code>] <region-file> [vector] [contours] [hillshade]" >&2
    exit 1
}
OPT_WORLD="" OPT_LANGUAGE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --world) OPT_WORLD=yes; shift ;;
        --language) [ $# -ge 2 ] || usage; OPT_LANGUAGE="$2"; shift 2 ;;
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

# shellcheck disable=SC2206
BBOX_ARGS=($BBOX)
if [ ${#BBOX_ARGS[@]} -ne 4 ]; then
    echo "Error: BBOX must have 4 numbers: min-lon min-lat max-lon max-lat" >&2
    exit 1
fi
BBOX_CSV="$(IFS=,; echo "${BBOX_ARGS[*]}")"

STEPS=("$@")
if [ ${#STEPS[@]} -eq 0 ]; then
    STEPS=(vector contours hillshade)
fi
for step in "${STEPS[@]}"; do
    case "$step" in
        vector|contours|hillshade) ;;
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
    "$REPO_DIR/scripts/build-contours.sh" "${BBOX_ARGS[@]}" "$CONTOUR_UNITS"
}

run_hillshade() {
    "$REPO_DIR/scripts/build-hillshade.sh" "${BBOX_ARGS[@]}"
}

echo "Region: $REGION_NAME${LANGUAGE:+ (labels: $LANGUAGE)}$([ "$WORLD" = yes ] && echo ' (world layer for zoom 0-8)')"
echo "Steps:  ${STEPS[*]}"
mkdir -p "$REPO_DIR/data"
echo "Free disk space: $(free_gb "$REPO_DIR/data") GB on the drive that holds $REPO_DIR/data"
echo "Each step checks its own space needs before it starts."
for step in "${STEPS[@]}"; do
    start=$(date +%s)
    echo
    echo "=== $step ==="
    "run_$step"
    end=$(date +%s)
    echo "=== $step finished in $(( (end - start) / 60 )) min ==="
done

echo
echo "Output files:"
ls -lh "$REPO_DIR"/data/*.mbtiles 2>/dev/null || echo "(none)"
