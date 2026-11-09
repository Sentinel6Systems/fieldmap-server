#!/usr/bin/env bash
# Build the base map vector tiles for one region.
# The script uses Planetiler in Docker.
# Planetiler downloads the OpenStreetMap extract for the region from Geofabrik.
# The output file is data/basemap.mbtiles.
#
# Usage:
#   ./scripts/build-vector.sh <area>
#
# <area> is a Geofabrik area name. Examples: monaco, switzerland, germany.
# Find area names at https://download.geofabrik.de
#
# Large areas need more memory. Set PLANETILER_MEMORY to change the Java
# heap size. Example: PLANETILER_MEMORY=8g ./scripts/build-vector.sh germany

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <geofabrik-area>" >&2
    exit 1
fi

AREA="$1"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MEMORY="${PLANETILER_MEMORY:-2g}"

# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"
mkdir -p "$REPO_DIR/data"
trap 'disk_full_hint "$REPO_DIR/data"' ERR
# The extract size is unknown until Planetiler downloads it. Ask for a
# baseline that covers a mid-sized country or a US state.
require_free_space "$REPO_DIR/data" 15 "the base map build"

# Planetiler writes the mbtiles file and keeps downloaded sources in data/sources.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e JAVA_TOOL_OPTIONS="-Xmx$MEMORY" \
    -v "$REPO_DIR/data":/data \
    ghcr.io/onthegomap/planetiler:latest \
    --download \
    --force \
    --area="$AREA" \
    --output=/data/basemap.mbtiles

echo "Done. Output: $REPO_DIR/data/basemap.mbtiles"
