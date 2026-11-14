#!/usr/bin/env python3
"""Reference viewshed with gdal_viewshed, for checking the print tool's viewshed.

    python3 tools/viewshed-compare.py <dem> <lon> <lat> <radius-m> <res-m> <observer-m> <target-m> <k> <out-dir>

Warps the DEM to a square grid in metres centred on the point (azimuthal equidistant,
bilinear, cell size <res-m>, the same layout as the print tool's grid), runs
gdal_viewshed on it from the centre cell with the same heights and curvature
(-cc = 1/k), and writes to <out-dir>:
  grid.f32       heights, float32, row 0 at the north
  gdal.u8        gdal_viewshed result: 255 visible, 0 not visible, 1 beyond the radius
  meta.json      n, res, radius, heights and k
Then run print/test/compare-viewshed.mjs <out-dir> to compare. Run this in the tool
container (it has GDAL):

    docker run --rm -v "$PWD":/work -w /work tileserver-tools python3 tools/viewshed-compare.py ...
"""
import json
import math
import os
import subprocess
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()
dem, lon, lat, radius, res, obs, tgt, k, out = sys.argv[1], *map(float, sys.argv[2:9]), sys.argv[9]
os.makedirs(out, exist_ok=True)
c = math.ceil(radius / res)
n = 2 * c + 1
half = (c + 0.5) * res
grid = os.path.join(out, "grid.tif")
gdal.Warp(grid, dem, dstSRS=f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m",
          outputBounds=(-half, -half, half, half), width=n, height=n, resampleAlg="bilinear",
          warpOptions=["XSCALE=1", "YSCALE=1"], dstNodata=-32768, format="GTiff")
vs = os.path.join(out, "gdal.tif")
subprocess.run(["gdal_viewshed", "-q", "-ox", "0", "-oy", "0", "-oz", str(obs), "-tz", str(tgt), "-md", str(radius),
                "-cc", str(1 / k), "-vv", "255", "-iv", "0", "-ov", "1", grid, vs], check=True)
h = gdal.Open(grid).ReadAsArray().astype(np.float32)
h[h <= -32767] = np.nan
h.tofile(os.path.join(out, "grid.f32"))
gdal.Open(vs).ReadAsArray().astype(np.uint8).tofile(os.path.join(out, "gdal.u8"))
json.dump({"n": n, "c": c, "res": res, "radius": radius, "obsH": obs, "tgtH": tgt, "k": k, "lat": lat, "lon": lon},
          open(os.path.join(out, "meta.json"), "w"))
print(f"wrote {out}: {n} x {n} cells of {res:g} m")
