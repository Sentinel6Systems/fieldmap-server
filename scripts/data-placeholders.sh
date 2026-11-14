#!/usr/bin/env bash
# Make sure the optional data files exist: data/extras.mbtiles (build-extras.sh) and
# data/usdata.mbtiles (build-usdata.sh). config.json lists them and the print styles
# read them, and TileServer GL stops at start-up when a listed file is missing.
# A missing one is written empty: its layers are then simply not drawn.
# Existing files are never touched.
#
# Usage: ./scripts/data-placeholders.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$REPO_DIR/data"

placeholder() {
    local name="$1" builder="$2"
    local out="$REPO_DIR/data/$name.mbtiles"
    shift 2
    [ -s "$out" ] && return 0
    python3 - "$out" "$@" <<'PY'
import json, sqlite3, sys
out, layers = sys.argv[1], sys.argv[2:]
db = sqlite3.connect(out)
db.executescript("""
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
""")
vl = [{"id": i, "fields": {}, "minzoom": 9, "maxzoom": 14} for i in layers]
db.executemany("INSERT INTO metadata VALUES (?, ?)", [
    ("name", "empty placeholder"), ("format", "pbf"), ("minzoom", "9"), ("maxzoom", "14"),
    ("bounds", "-180,-85.05113,180,85.05113"), ("json", json.dumps({"vector_layers": vl}))])
db.commit()
PY
    echo "Wrote an empty data/$name.mbtiles (run $builder for the real layers)."
}

placeholder extras build-extras.sh power pipeline barrier tower landmark
placeholder usdata build-usdata.sh obstacle gnis survey_mark protected flood
