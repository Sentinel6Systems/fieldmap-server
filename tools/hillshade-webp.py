#!/usr/bin/env python3
"""Shrink data/hillshade.mbtiles: drop zoom levels above --maxzoom (default 12) and
re-encode the PNG tiles as WebP. The print styles stretch z12 hillshade over z13 and
up without visible loss on paper; a US-wide file goes from about 37 GB to a few GB.

    python3 tools/hillshade-webp.py [--maxzoom 12] [--quality 80] [in.mbtiles] [out.mbtiles]

Defaults: data/hillshade.mbtiles -> data/hillshade-webp.mbtiles. Check the result, then
move it over the original and restart the tile server. Needs Pillow with WebP support.
Nothing in this repository asks for hillshade tiles by a .png name: the styles reach
them through the tile server's TileJSON, which takes the format from the file.
"""
import argparse
import gzip
import io
import os
import shutil
import sqlite3
import sys

from PIL import Image, features

ap = argparse.ArgumentParser()
ap.add_argument("src", nargs="?", default="data/hillshade.mbtiles")
ap.add_argument("dst", nargs="?", default="data/hillshade-webp.mbtiles")
ap.add_argument("--maxzoom", type=int, default=12)
ap.add_argument("--quality", type=int, default=80)
a = ap.parse_args()
if not features.check("webp"):
    sys.exit("Pillow here has no WebP support (pip install --upgrade pillow)")

tmp = a.dst + ".tmp"
shutil.copyfile(a.src, tmp)
db = sqlite3.connect(tmp)
names = {r[0]: r[1] for r in db.execute("SELECT name, type FROM sqlite_master")}
if names.get("tiles") != "table":
    sys.exit("expected a plain 'tiles' table (as gdal_translate writes); this file uses another layout")

dropped = db.execute("DELETE FROM tiles WHERE zoom_level > ?", (a.maxzoom,)).rowcount
db.commit()
total = db.execute("SELECT count(*) FROM tiles").fetchone()[0]
done = 0
rows = db.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles").fetchall() if total < 200000 else None


def batches():
    if rows is not None:
        yield rows
        return
    last = (-1, -1, -1)
    while True:
        chunk = db.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles WHERE (zoom_level, tile_column, tile_row) > (?, ?, ?) "
                           "ORDER BY zoom_level, tile_column, tile_row LIMIT 20000", last).fetchall()
        if not chunk:
            return
        last = chunk[-1][:3]
        yield chunk


for chunk in batches():
    out = []
    for z, x, y, data in chunk:
        if data[:2] == b"\x1f\x8b":   # some writers store tiles gzipped
            data = gzip.decompress(data)
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGBA" if im.mode in ("LA", "RGBA", "P") else "RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=a.quality, method=4)
        out.append((buf.getvalue(), z, x, y))
    db.executemany("UPDATE tiles SET tile_data = ? WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?", out)
    db.commit()
    done += len(chunk)
    print(f"  {done:,} / {total:,} tiles", flush=True)

db.execute("DELETE FROM metadata WHERE name IN ('format', 'maxzoom')")
db.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", [("format", "webp"), ("maxzoom", str(a.maxzoom))])
db.commit()
db.execute("VACUUM")
db.close()
os.replace(tmp, a.dst)
print(f"{a.dst}: {total:,} tiles (dropped {dropped:,} above z{a.maxzoom}), "
      f"{os.path.getsize(a.src) / 1e9:.2f} GB -> {os.path.getsize(a.dst) / 1e9:.2f} GB")
