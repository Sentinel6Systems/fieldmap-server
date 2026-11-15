#!/usr/bin/env bash
# Build data/usdata.mbtiles from US federal data, all public domain, for the print
# styles: FAA obstacles, USGS GNIS names, NOAA NGS survey marks, USGS PAD-US public
# access, and (by name only, it is large) FEMA flood zones. See tools/us-data.py.
#
# Usage:
#   ./scripts/build-usdata.sh <region-file> [obstacles] [names] [marks] [protected] [flood]
#
# The region file gives BBOX, and US_STATES (two-letter codes) or US states in
# GEOFABRIK_AREAS. Default steps: obstacles names marks protected.
# Downloads stay in data/cache/usdata; the FAA file is fetched again after a day.
#
# Size for a six-state region: obstacles, names and marks a few MB each,
# protected land 20-60 MB; flood zones several hundred MB to over 1 GB per large state.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib.sh
source "$REPO_DIR/scripts/lib.sh"

if [ $# -lt 1 ] || [ ! -f "$1" ]; then
    echo "Usage: $0 <region-file> [obstacles] [names] [marks] [protected] [flood]" >&2
    exit 1
fi
REGION_FILE="$(realpath --relative-to="$REPO_DIR" "$1")"
shift
reexec_in_tools "$REGION_FILE" "$@"

# shellcheck source=/dev/null
source "$REGION_FILE"
: "${BBOX:?BBOX is not set in $REGION_FILE}"
if [ -z "${US_STATES:-}" ]; then
    # shellcheck disable=SC2086
    US_STATES="$(python3 - ${GEOFABRIK_AREAS:-} <<'PY'
import sys
codes = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
         "connecticut": "CT", "delaware": "DE", "district-of-columbia": "DC", "florida": "FL", "georgia": "GA",
         "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
         "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
         "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
         "nebraska": "NE", "nevada": "NV", "new-hampshire": "NH", "new-jersey": "NJ", "new-mexico": "NM",
         "new-york": "NY", "north-carolina": "NC", "north-dakota": "ND", "ohio": "OH", "oklahoma": "OK",
         "oregon": "OR", "pennsylvania": "PA", "puerto-rico": "PR", "rhode-island": "RI", "south-carolina": "SC",
         "south-dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
         "washington": "WA", "west-virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
print(" ".join(codes[p.rsplit("/", 1)[1]] for p in sys.argv[1:] if "/us/" in p and p.rsplit("/", 1)[1] in codes))
PY
)"
fi
if [ -z "$US_STATES" ]; then
    echo "Error: no US states found. Set US_STATES=\"NJ PA ...\" in $REGION_FILE." >&2
    exit 1
fi
# A box whose min-lon is larger than its max-lon crosses the 180° line: take its wider
# side, as the base map does.
# shellcheck disable=SC2086
read -r W S E N <<<"$(python3 -c "
import sys
w, s, e, n = map(float, sys.argv[1:])
if w > e:
    w, e = (w, 180.0) if 180 - w > e + 180 else (-180.0, e)
print(f'{w:g} {s:g} {e:g} {n:g}')" $BBOX)"
echo "US data for $REGION_FILE: states $US_STATES, box $W $S $E $N"
# shellcheck disable=SC2086
python3 "$REPO_DIR/tools/us-data.py" "$@" --bbox "$W" "$S" "$E" "$N" --states $US_STATES
echo "Restart the tile server to load it: docker restart tileserver"
