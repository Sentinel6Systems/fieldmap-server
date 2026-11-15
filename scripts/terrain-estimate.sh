#!/usr/bin/env bash
# Print the expected output size and time of terrain build steps for a region, before
# anything is downloaded. build-region.sh runs it before the first step.
#
# Usage:
#   ./scripts/terrain-estimate.sh [--areas "<geofabrik paths>"] <min-lon> <min-lat> <max-lon> <max-lat> <step> ...
#
# Steps: contours, hillshade, elevation. ELEVATION_MAXZOOM, HILLSHADE_MAXZOOM and
# HILLSHADE_FORMAT are read as in the build scripts.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

AREAS=""
if [ "${1:-}" = "--areas" ]; then
    AREAS="${2:-}"
    shift 2 || true
fi
if [ $# -lt 5 ]; then
    echo "Usage: $0 [--areas \"<geofabrik paths>\"] <min-lon> <min-lat> <max-lon> <max-lat> <step> ..." >&2
    exit 1
fi

reexec_in_tools ${AREAS:+--areas "$AREAS"} "$@"

WORK_DIR="$REPO_DIR/data/cache/terrain-estimate"
rm -rf "$WORK_DIR"
# shellcheck disable=SC2086
terrain_outline "$WORK_DIR" "$1" "$2" "$3" "$4" $AREAS
shift 4
terrain_estimate "$WORK_DIR" "$@"
rm -rf "$WORK_DIR"
