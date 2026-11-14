#!/usr/bin/env python3
"""Write elevation raster tiles (Terrarium or Mapbox terrain-RGB) from a DEM.

    python3 tools/elevation-tiles.py --bbox <min-lon> <min-lat> <max-lon> <max-lat> \
        [--minzoom 8] [--maxzoom 12] [--encoding terrarium|mapbox] [--format png|webp] \
        [--jobs N] <dem> <out.mbtiles>

<dem> is anything GDAL reads in metres above sea level (build-elevation.sh passes a VRT
of the Copernicus GLO-30 tiles). Every tile that touches the box is written, 256 px,
Web Mercator, for each zoom from minzoom to maxzoom. Each block of tiles is warped
straight from the DEM: at maxzoom each pixel is the DEM's bilinear value at the pixel
centre (no smoothing, so summits and ridge lines keep their height), below it the
average of the source pixels (GDAL reads the source's own overviews there); no zoom is
built from another one.

Heights are lossless to within the encoding's step:
  terrarium: h = R * 256 + G + B / 256 - 32768      (step 1/256 m; rounded to --step)
  mapbox:    h = -10000 + (R * 65536 + G * 256 + B) * 0.1   (step 0.1 m)
Pixels the DEM does not cover are written as R = G = B = 0 (-32768 m or -10000 m): read
anything below -9000 m as "no data". Needs the GDAL Python bindings and numpy (the tool
container has both).
"""
import argparse
import math
import multiprocessing as mp
import os
import sqlite3
import time
import uuid
from datetime import date

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

ORIGIN = 20037508.342789244          # half the Web Mercator world width, metres
TILE = 256
NODATA = -32768.0
BLOCK = 8                            # tiles per side warped in one go

ap = argparse.ArgumentParser()
ap.add_argument("dem")
ap.add_argument("out")
ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
ap.add_argument("--minzoom", type=int, default=8)
ap.add_argument("--maxzoom", type=int, default=12)
ap.add_argument("--encoding", choices=["terrarium", "mapbox"], default="terrarium")
ap.add_argument("--format", choices=["png", "webp"], default="png")
ap.add_argument("--step", type=float, default=0.1, help="terrarium height step in metres (default 0.1; 0 keeps 1/256)")
ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
ap.add_argument("--attribution", default="")
ap.add_argument("--source", default="")
ap.add_argument("--source-date", default="")
a = ap.parse_args()


def tile_range(z):
    """Tile columns and rows (XYZ, row 0 at the north) that touch the box at zoom z."""
    n = 2 ** z
    w, s, e, nn = a.bbox
    lat = lambda v: max(-85.0511, min(85.0511, v))
    x = lambda lon: min(n - 1, max(0, int(math.floor((lon + 180) / 360 * n))))
    y = lambda la: min(n - 1, max(0, int(math.floor((1 - math.asinh(math.tan(math.radians(lat(la)))) / math.pi) / 2 * n))))
    return x(w), x(e - 1e-9), y(nn), y(s + 1e-9)


def encode(h):
    """Float heights (metres, NODATA where missing) -> uint8 array (3, H, W)."""
    missing = ~np.isfinite(h) | (h <= NODATA + 1)
    if a.encoding == "terrarium":
        q = np.round((np.where(missing, 0, h) + 32768.0) * 256.0)
        if a.step > 0:
            q = np.round(q / (a.step * 256.0)) * (a.step * 256.0)
        q = np.clip(q, 1, 2 ** 24 - 1).astype(np.uint32)
    else:
        q = np.clip(np.round((np.where(missing, 0, h) + 10000.0) * 10.0), 1, 2 ** 24 - 1).astype(np.uint32)
    q[missing] = 0
    return np.stack([(q >> 16) & 255, (q >> 8) & 255, q & 255]).astype(np.uint8)


def to_image(rgb):
    mem = gdal.GetDriverByName("MEM").Create("", TILE, TILE, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[i])
    path = f"/vsimem/{uuid.uuid4().hex}.{a.format}"
    if a.format == "png":
        gdal.GetDriverByName("PNG").CreateCopy(path, mem, options=["ZLEVEL=9"])
    else:
        gdal.GetDriverByName("WEBP").CreateCopy(path, mem, options=["LOSSLESS=YES", "QUALITY=100"])
    f = gdal.VSIFOpenL(path, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    size = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, size, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(path)
    return data


def block(job):
    """Warp one block of tiles and encode them. Returns [(z, x, y, bytes), ...]."""
    z, x0, y0, x1, y1 = job
    size = 2 * ORIGIN / 2 ** z
    bounds = (-ORIGIN + x0 * size, ORIGIN - (y1 + 1) * size, -ORIGIN + (x1 + 1) * size, ORIGIN - y0 * size)
    w, h = (x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE
    ds = gdal.Warp("", a.dem, format="MEM", dstSRS="EPSG:3857", outputBounds=bounds, width=w, height=h,
                   resampleAlg="bilinear" if z == a.maxzoom else "average", dstNodata=NODATA, multithread=False,
                   # XSCALE/YSCALE=1: sample the DEM at the pixel centre instead of widening the
                   # bilinear kernel when the tile pixels are larger than the DEM's.
                   warpOptions=["XSCALE=1", "YSCALE=1"] if z == a.maxzoom else [])
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    out = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            t = arr[(ty - y0) * TILE:(ty - y0 + 1) * TILE, (tx - x0) * TILE:(tx - x0 + 1) * TILE]
            if np.all(t <= NODATA + 1):
                continue
            out.append((z, tx, 2 ** z - 1 - ty, to_image(encode(t))))   # MBTiles rows count from the south
    return out


def main():
    tmp = a.out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
    CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
    """)
    jobs = []
    for z in range(a.minzoom, a.maxzoom + 1):
        xa, xb, ya, yb = tile_range(z)
        for by in range(ya, yb + 1, BLOCK):
            for bx in range(xa, xb + 1, BLOCK):
                jobs.append((z, bx, by, min(bx + BLOCK - 1, xb), min(by + BLOCK - 1, yb)))
    print(f"  {len(jobs)} blocks of up to {BLOCK} x {BLOCK} tiles, zoom {a.minzoom}-{a.maxzoom}, {a.jobs} processes", flush=True)
    t0, done, tiles = time.time(), 0, 0
    with mp.Pool(a.jobs) as pool:
        for rows in pool.imap_unordered(block, jobs):
            db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", rows)
            done += 1
            tiles += len(rows)
            if done % 50 == 0 or done == len(jobs):
                db.commit()
                print(f"  {done}/{len(jobs)} blocks, {tiles:,} tiles, {time.time() - t0:.0f} s", flush=True)
    w, s, e, n = a.bbox
    meta = {
        "name": "Elevation", "type": "baselayer", "version": "1", "format": a.format, "encoding": a.encoding,
        "minzoom": str(a.minzoom), "maxzoom": str(a.maxzoom), "bounds": f"{w},{s},{e},{n}",
        "center": f"{(w + e) / 2},{(s + n) / 2},{a.minzoom}",
        # No tileSize row: TileServer GL would read it as a string. config.json sets 256.
        "description": f"Elevation in metres, {a.encoding} encoding; R=G=B=0 is no data",
        "attribution": a.attribution,
        "fieldmap:source": a.source, "fieldmap:source_date": a.source_date, "fieldmap:built": date.today().isoformat(),
    }
    db.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    db.commit()
    db.close()
    os.replace(tmp, a.out)
    print(f"  wrote {tiles:,} tiles, {os.path.getsize(a.out) / 1048576:.1f} MB", flush=True)


if __name__ == "__main__":
    main()
