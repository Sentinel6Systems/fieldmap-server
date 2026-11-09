#!/usr/bin/env bash
# Build all map data for one region from a region file.
# A region file lists Geofabrik extracts, a terrain bounding box, and options.
# See regions/mid-atlantic.env for an example.
#
# Usage:
#   ./scripts/build-region.sh <region-file> [step ...]
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

if [ $# -lt 1 ]; then
    echo "Usage: $0 <region-file> [vector] [contours] [hillshade]" >&2
    exit 1
fi

REGION_FILE="$1"
shift
if [ ! -f "$REGION_FILE" ]; then
    echo "Error: region file not found: $REGION_FILE" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$REGION_FILE"

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

# Shared inputs that Planetiler needs besides the OSM file.
# Format: "<file name in data/sources> <url> [<fallback url> ...]"
# URLs are tried in order. Each one gets several attempts before the
# script moves on to the next. Add mirrors here when a source is unreliable.
SHARED_SOURCES=(
    "water-polygons-split-3857.zip https://osmdata.openstreetmap.de/download/water-polygons-split-3857.zip https://www.osmdata.org/download/water-polygons-split-3857.zip"
    "natural_earth_vector.sqlite.zip https://naciscdn.org/naturalearth/packages/natural_earth_vector.sqlite.zip"
    "lake_centerline.shp.zip https://dev.maptiler.download/geodata/omt/lake_centerline.shp.zip"
)

# Download one shared input, trying each URL in turn.
# Arguments: DEST_FILE URL [URL ...]
# A file that already exists and looks like a zip archive is kept.
fetch_source() {
    local dest="$1"
    shift
    local name
    name="$(basename "$dest")"

    if [ -s "$dest" ] && [ "$(head -c 2 "$dest")" = "PK" ]; then
        echo "cached: $name"
        return 0
    fi

    local url
    for url in "$@"; do
        echo "download: $url"
        # Start each URL from scratch: a mirror can hold a different
        # version of the file, so a partial file must not be resumed
        # from another source.
        rm -f "$dest.part"
        if curl -fSL --retry 3 --retry-delay 10 --retry-connrefused \
            --connect-timeout 30 -o "$dest.part" "$url" \
            && [ "$(head -c 2 "$dest.part")" = "PK" ]; then
            mv "$dest.part" "$dest"
            return 0
        fi
        echo "  failed: $url"
    done
    rm -f "$dest.part"
    echo "Error: could not download $name from any source." >&2
    return 1
}

run_vector() {
    # shellcheck disable=SC2086
    "$REPO_DIR/scripts/merge-osm.sh" "$REGION_NAME" $GEOFABRIK_AREAS

    # Fetch Planetiler's shared inputs here, with retries and fallback
    # mirrors, so Planetiler itself makes no network calls.
    local sources="$REPO_DIR/data/sources"
    mkdir -p "$sources"
    local entry
    for entry in "${SHARED_SOURCES[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        local file="$1"
        shift
        fetch_source "$sources/$file" "$@"
    done

    echo "Planetiler: build data/basemap.mbtiles (Java heap $PLANETILER_MEMORY)"
    # --bounds limits the output to the region box. Without it Planetiler
    # takes the bounds from the merged OSM data, which can be far larger
    # (extracts include complete ways and relations that reach outside).
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -e JAVA_TOOL_OPTIONS="-Xmx$PLANETILER_MEMORY" \
        -v "$REPO_DIR/data":/data \
        ghcr.io/onthegomap/planetiler:latest \
        --force \
        --osm-path="/data/sources/$REGION_NAME.osm.pbf" \
        --bounds="$BBOX_CSV" \
        --tmpdir=/data/tmp \
        --output=/data/basemap.mbtiles
}

run_contours() {
    "$REPO_DIR/scripts/build-contours.sh" "${BBOX_ARGS[@]}" "$CONTOUR_UNITS"
}

run_hillshade() {
    "$REPO_DIR/scripts/build-hillshade.sh" "${BBOX_ARGS[@]}"
}

echo "Region: $REGION_NAME"
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
