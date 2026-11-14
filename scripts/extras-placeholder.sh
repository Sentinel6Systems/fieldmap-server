#!/usr/bin/env bash
# Make sure data/extras.mbtiles exists. config.json lists it and the print styles read
# it, and TileServer GL stops at start-up when a listed file is missing. Without a real
# build (build-extras.sh) this writes an empty one: the extra layers are then simply
# not drawn. An existing file is never touched.
#
# Usage: ./scripts/extras-placeholder.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO_DIR/data/extras.mbtiles"
[ -s "$OUT" ] && exit 0
mkdir -p "$REPO_DIR/data"
python3 - "$OUT" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE metadata (name TEXT, value TEXT);
CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
""")
layers = [{"id": i, "fields": {}, "minzoom": 9, "maxzoom": 14} for i in ("power", "pipeline", "barrier", "tower", "landmark")]
db.executemany("INSERT INTO metadata VALUES (?, ?)", [
    ("name", "fieldmap extras (empty placeholder)"), ("format", "pbf"), ("minzoom", "9"), ("maxzoom", "14"),
    ("bounds", "-180,-85.05113,180,85.05113"), ("json", json.dumps({"vector_layers": layers}))])
db.commit()
PY
echo "Wrote an empty data/extras.mbtiles (run build-extras.sh for the real layers)."
