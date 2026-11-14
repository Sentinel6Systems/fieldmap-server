#!/usr/bin/env bash
# Download the whole OpenStreetMap planet and keep only what the map shows at
# zoom 0-8: places, major roads and their route numbers, railways, country and
# state borders, water, glaciers, peaks and airports.
# build-world.sh calls this; you do not need to run it yourself.
#
# The planet (about 96 GB) comes from the fastest of PLANET_MIRRORS (lib.sh)
# whose copy is at most OSM_MAX_AGE_DAYS (default 30) days old. An interrupted
# download resumes on the next run. The planet file is deleted after filtering.
#
# Output: data/sources/planet-z0-8.osm.pbf

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

reexec_in_tools "$@"

SOURCES="$REPO_DIR/data/sources"
PLANET="$SOURCES/planet-latest.osm.pbf"
OUT="$SOURCES/planet-z0-8.osm.pbf"
mkdir -p "$SOURCES"

if [ ! -s "$PLANET" ]; then
    OSM_MIRRORS=("${PLANET_MIRRORS[@]}")
    rank_osm_mirrors planet
    download_osm_extract planet "$PLANET"
fi

echo "Filtering the planet down to zoom 0-8 features (this takes a while)"
osmium tags-filter --overwrite -f pbf -o "$OUT.part" "$PLANET" \
    nwr/place \
    w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link \
    r/route=road \
    w/railway=rail \
    r/admin_level=2,3,4 \
    nwr/natural=water,glacier,peak,volcano \
    nwr/water \
    nwr/waterway=riverbank \
    w/waterway=river \
    nwr/landuse=reservoir \
    nwr/aeroway=aerodrome
mv "$OUT.part" "$OUT"
rm -f "$PLANET"
echo "Done. Output: $OUT ($(du -h "$OUT" | cut -f1)); the planet file was deleted."
