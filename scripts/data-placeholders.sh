#!/usr/bin/env bash
# Make sure every data file the tile server reads exists: the base map, contours and
# hillshade (so that a new install starts before the first build; the map is then blank),
# and the optional data/extras.mbtiles (build-extras.sh), data/usdata.mbtiles
# (build-usdata.sh), data/elevation.mbtiles (build-elevation.sh) and
# data/hillshade-detail.mbtiles (build-detail.sh).
# config.json lists them and the print styles read them, and TileServer GL stops at
# start-up when a listed file is missing. A missing one is written empty: its layers are
# then simply not drawn, and the print tool hides its Terrain section.
# Existing files are never touched.
#
# Usage: ./scripts/data-placeholders.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$REPO_DIR/data/wmm"   # data/wmm: the magnetic model from tools/wmm-update.py

# Arguments: NAME BUILDER (pbf LAYER ... | raster FORMAT ENCODING)
placeholder() {
    local name="$1" builder="$2"
    local out="$REPO_DIR/data/$name.mbtiles"
    shift 2
    [ -s "$out" ] && return 0
    python3 - "$out" "$@" <<'PY'
import json, sqlite3, sys
out, kind, args = sys.argv[1], sys.argv[2], sys.argv[3:]
db = sqlite3.connect(out)
db.executescript("""
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
""")
if kind == "pbf":
    vl = [{"id": i, "fields": {}, "minzoom": 9, "maxzoom": 14} for i in args]
    db.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name", "empty placeholder"), ("format", "pbf"), ("minzoom", "9"), ("maxzoom", "14"),
        ("bounds", "-180,-85.05113,180,85.05113"), ("json", json.dumps({"vector_layers": vl}))])
else:
    db.executemany("INSERT INTO metadata VALUES (?, ?)", [
        ("name", "empty placeholder"), ("format", args[0]), ("encoding", args[1]), ("minzoom", "8"),
        ("maxzoom", "12"), ("bounds", "-180,-85.05113,180,85.05113")])
db.commit()
PY
    echo "Wrote an empty data/$name.mbtiles (run $builder for the real layers)."
}

placeholder basemap "build-region.sh, or the map builder page," pbf
placeholder contours build-contours.sh pbf contours_major contours_minor
placeholder hillshade build-hillshade.sh raster webp ""
placeholder extras build-extras.sh pbf power pipeline barrier tower landmark building_large exit runway
placeholder usdata build-usdata.sh pbf obstacle gnis survey_mark protected flood
placeholder elevation build-elevation.sh raster webp mapbox
placeholder hillshade-detail build-detail.sh raster webp ""
