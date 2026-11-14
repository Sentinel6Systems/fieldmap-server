#!/usr/bin/env python3
"""Check data/elevation.mbtiles against the DEM it was built from.

    python3 tools/check-elevation.py [--samples 2000] <dem> <elevation.mbtiles>

Decodes tiles at the file's maxzoom and compares, at random pixel centres inside the
file's bounds, the stored height with the DEM's own value there (bilinear, as the
build samples it): the two must agree to within the encoding step. It also reports the
highest point of the DEM (a summit) and the flattest non-sea spot (a lake or reservoir
surface), read back from the tiles. Run it in the tool container (GDAL and numpy):

    docker run --rm -v "$PWD":/work -w /work tileserver-tools \
        python3 tools/check-elevation.py data/cache/elevation/dem.vrt data/elevation.mbtiles
"""
import argparse
import math
import random
import sqlite3
import uuid

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
ap = argparse.ArgumentParser()
ap.add_argument("dem")
ap.add_argument("mbtiles")
ap.add_argument("--samples", type=int, default=2000)
a = ap.parse_args()

db = sqlite3.connect(a.mbtiles)
meta = dict(db.execute("SELECT name, value FROM metadata"))
z, enc = int(meta["maxzoom"]), meta.get("encoding", "terrarium")
w, s, e, n = map(float, meta["bounds"].split(","))
cache = {}


def tile(x, y):
    if (x, y) not in cache:
        row = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?", (z, x, 2 ** z - 1 - y)).fetchone()
        if not row:
            cache[(x, y)] = None
        else:
            path = f"/vsimem/{uuid.uuid4().hex}"
            gdal.FileFromMemBuffer(path, row[0])
            rgb = gdal.Open(path).ReadAsArray().astype(np.float64)
            gdal.Unlink(path)
            h = (rgb[0] * 256 + rgb[1] + rgb[2] / 256 - 32768) if enc == "terrarium" else (-10000 + (rgb[0] * 65536 + rgb[1] * 256 + rgb[2]) * 0.1)
            h[(rgb[0] == 0) & (rgb[1] == 0) & (rgb[2] == 0)] = np.nan
            cache[(x, y)] = h
    return cache[(x, y)]


def world(lon, lat):
    """Pixel coordinates in the 256 px world at zoom z."""
    size = 256 * 2 ** z
    return (lon + 180) / 360 * size, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * size


def from_world(px, py):
    size = 256 * 2 ** z
    return px / size * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / size))))


def tile_bilinear(lon, lat):
    px, py = world(lon, lat)
    px, py = px - 0.5, py - 0.5
    x0, y0 = math.floor(px), math.floor(py)
    fx, fy = px - x0, py - y0
    v = []
    for yy, xx in ((y0, x0), (y0, x0 + 1), (y0 + 1, x0), (y0 + 1, x0 + 1)):
        t = tile(xx // 256, yy // 256)
        v.append(np.nan if t is None else t[yy % 256, xx % 256])
    return (v[0] * (1 - fx) + v[1] * fx) * (1 - fy) + (v[2] * (1 - fx) + v[3] * fx) * fy


dem = gdal.Open(a.dem)
gt = dem.GetGeoTransform()
band = dem.GetRasterBand(1)
c0, r0 = int((w - gt[0]) / gt[1]), int((n - gt[3]) / gt[5])
c0, r0 = max(0, c0), max(0, r0)
c1 = min(dem.RasterXSize, int(math.ceil((e - gt[0]) / gt[1])))
r1 = min(dem.RasterYSize, int(math.ceil((s - gt[3]) / gt[5])))
arr = band.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)


def dem_bilinear(lon, lat):
    cx, cy = (lon - gt[0]) / gt[1] - 0.5 - c0, (lat - gt[3]) / gt[5] - 0.5 - r0
    x0, y0 = math.floor(cx), math.floor(cy)
    fx, fy = cx - x0, cy - y0
    if x0 < 0 or y0 < 0 or x0 + 1 >= arr.shape[1] or y0 + 1 >= arr.shape[0]:
        return np.nan
    q = arr[y0:y0 + 2, x0:x0 + 2]
    return (q[0, 0] * (1 - fx) + q[0, 1] * fx) * (1 - fy) + (q[1, 0] * (1 - fx) + q[1, 1] * fx) * fy


step = 1 / 256 if enc == "terrarium" else 0.1
random.seed(1)
diffs = []
x0, y0 = world(w, n)
x1, y1 = world(e, s)
while len(diffs) < a.samples:
    px, py = random.randint(int(x0) + 2, int(x1) - 3), random.randint(int(y0) + 2, int(y1) - 3)
    t = tile(px // 256, py // 256)
    if t is None or np.isnan(t[py % 256, px % 256]):
        continue
    lon, lat = from_world(px + 0.5, py + 0.5)
    ref = dem_bilinear(lon, lat)
    if not np.isnan(ref):
        diffs.append(t[py % 256, px % 256] - ref)
d = np.abs(np.array(diffs))
print(f"encoding {enc} (step {step:g} m), zoom {z}, {len(d)} pixel centres:")
print(f"  difference tile - DEM: mean {d.mean():.3f} m, 99% below {np.percentile(d, 99):.3f} m, max {d.max():.3f} m")

r, c = np.unravel_index(np.nanargmax(arr), arr.shape)
lon, lat = gt[0] + (c0 + c + 0.5) * gt[1], gt[3] + (r0 + r + 0.5) * gt[5]
print(f"  highest DEM point: DEM {arr[r, c]:.2f} m, tiles {tile_bilinear(lon, lat):.2f} m (30 m resampling smooths sharp tops)")

k = 7
best = None
for rr in range(k, arr.shape[0] - k, 3):
    for cc in range(k, arr.shape[1] - k, 3):
        win = arr[rr - k:rr + k + 1, cc - k:cc + k + 1]
        if win.min() > 1 and win.max() - win.min() < 0.01:
            best = (rr, cc)
            break
    if best:
        break
if best:
    rr, cc = best
    lon, lat = gt[0] + (c0 + cc + 0.5) * gt[1], gt[3] + (r0 + rr + 0.5) * gt[5]
    print(f"  flat water surface: DEM {arr[rr, cc]:.2f} m, tiles {tile_bilinear(lon, lat):.2f} m")
else:
    print("  no flat inland water surface found in this area")
