#!/usr/bin/env python3
"""Hillshade raster of one part of a region outline, computed in chunks.

    python3 tools/hillshade-raster.py [--jobs N] <dem.vrt> <outline-part.geojson> <out.tif> \
        <xmin> <ymin> <xmax> <ymax> <res>

build-hillshade.sh runs this once per line of <work>/outline/mosaics.txt (see
tools/region-outline.py): <dem.vrt> is the part's elevation mosaic in metres, the last five
arguments its extent and pixel size in Web Mercator. The output is a one-band byte GeoTIFF on
that grid (azimuth 315°, altitude 45°), 0 where there is no data or outside the outline.

The grid is cut into chunks of CHUNK x CHUNK pixels. Each chunk that touches the outline is
reprojected in memory with one more pixel on every side (the hillshade of a pixel needs its
eight neighbours), shaded, and its inner part written to the output, several chunks in
parallel: the same pixels as shading the whole grid in one piece. So the reprojected elevation data is never written to disk, and chunks of sea or
outside the outline are not computed or stored (the GeoTIFF is sparse there).
"""
import argparse
import math
import multiprocessing as mp
import time

import numpy as np
from osgeo import gdal, osr

import tilewriter

gdal.UseExceptions()

CHUNK = 4096                         # a multiple of the GeoTIFF block size
NODATA = -32768.0

_cfg = {}


def lonlat(x, y):
    o = tilewriter.ORIGIN
    return x / o * 180, math.degrees(math.atan(math.sinh(y / o * math.pi)))


def shade(job):
    """Hillshade of one chunk: (xoff, yoff, byte array) or None when it has no data."""
    xoff, yoff, w, h = job
    a = _cfg["args"]
    # One more pixel on each side, except at the edge of the grid (there GDAL extends the
    # heights itself, as it does when it shades the whole grid in one piece).
    left, top = min(1, xoff), min(1, yoff)
    right, bottom = min(1, _cfg["xs"] - xoff - w), min(1, _cfg["ys"] - yoff - h)
    x0, y1 = a.xmin + (xoff - left) * a.res, a.ymax - (yoff - top) * a.res
    bounds = (x0, y1 - (h + top + bottom) * a.res, x0 + (w + left + right) * a.res, y1)
    # errorThreshold=0: exact positions, so neighbouring chunks agree on their shared pixels.
    dem = gdal.Warp("", a.dem, format="MEM", dstSRS="EPSG:3857", outputBounds=bounds, xRes=a.res, yRes=a.res,
                    resampleAlg="bilinear", cutlineDSName=a.outline, dstNodata=NODATA,
                    outputType=gdal.GDT_Float32, errorThreshold=0, multithread=False)
    if not (dem.GetRasterBand(1).ReadAsArray() > NODATA + 1).any():
        return None
    hs = gdal.DEMProcessing("", dem, "hillshade", format="MEM", zFactor=1, azimuth=315, altitude=45,
                            computeEdges=True)
    return xoff, yoff, hs.GetRasterBand(1).ReadAsArray()[top:top + h, left:left + w].astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dem")
    ap.add_argument("outline")
    ap.add_argument("out")
    for name in ("xmin", "ymin", "xmax", "ymax", "res"):
        ap.add_argument(name, type=float)
    ap.add_argument("--jobs", type=int, default=tilewriter.default_jobs())
    a = ap.parse_args()
    _cfg["args"] = a
    xs, ys = int((a.xmax - a.xmin) / a.res + 0.5), int((a.ymax - a.ymin) / a.res + 0.5)
    _cfg.update(xs=xs, ys=ys)
    out = gdal.GetDriverByName("GTiff").Create(a.out, xs, ys, 1, gdal.GDT_Byte, options=[
        "COMPRESS=DEFLATE", "BIGTIFF=YES", "TILED=YES", "SPARSE_OK=TRUE"])
    out.SetGeoTransform((a.xmin, a.res, 0, a.ymax, 0, -a.res))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(3857)
    out.SetProjection(srs.ExportToWkt())
    band = out.GetRasterBand(1)
    band.SetNoDataValue(0)
    outline = tilewriter.read_outline(a.outline)
    jobs = []
    for yoff in range(0, ys, CHUNK):
        for xoff in range(0, xs, CHUNK):
            w, h = min(CHUNK, xs - xoff), min(CHUNK, ys - yoff)
            lw, ln = lonlat(a.xmin + (xoff - 1) * a.res, a.ymax - (yoff - 1) * a.res)
            le, ls = lonlat(a.xmin + (xoff + w + 1) * a.res, a.ymax - (yoff + h + 1) * a.res)
            if outline.Intersects(tilewriter.box(lw, ls, le, ln)):
                jobs.append((xoff, yoff, w, h))
    print(f"  {xs:,} x {ys:,} pixels, {len(jobs)} chunks of up to {CHUNK} x {CHUNK} touch the outline, "
          f"{a.jobs} processes", flush=True)
    t0, done, written = time.time(), 0, 0
    with mp.Pool(a.jobs) as pool:
        for res in pool.imap_unordered(shade, jobs):
            done += 1
            if res is not None:
                band.WriteArray(res[2], res[0], res[1])
                written += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} chunks, {written} with data, {time.time() - t0:.0f} s", flush=True)
    out.FlushCache()


if __name__ == "__main__":
    main()
