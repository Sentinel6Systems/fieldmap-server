#!/usr/bin/env python3
"""Write elevation raster tiles (Terrarium or Mapbox terrain-RGB) from a DEM.

    python3 tools/elevation-tiles.py [--outline outline.geojson] [--minzoom 8] [--maxzoom 12] \
        [--encoding terrarium|mapbox] [--format png|webp] [--jobs N] <dem> [<dem> ...] <out.mbtiles>

Each <dem> is anything GDAL reads in metres above sea level (build-elevation.sh passes one
VRT of the Copernicus GLO-30 cells per part of the region outline; see tools/region-outline.py).
With --outline, pixels outside the outline are written as no data. Every tile that touches a
DEM (and the outline, if given) and has data is written, 256 px, Web Mercator, for each zoom from
minzoom to maxzoom. Each block of tiles is warped straight from the DEM: at maxzoom each
pixel is the DEM's bilinear value at the pixel centre (no smoothing, so summits and ridge
lines keep their height), below it the average of the source pixels (GDAL reads the
source's own overviews there); no zoom is built from another one.

Heights are lossless to within the encoding's step:
  terrarium: h = R * 256 + G + B / 256 - 32768      (step 1/256 m; rounded to --step)
  mapbox:    h = -10000 + (R * 65536 + G * 256 + B) * 0.1   (step 0.1 m)
Pixels the DEM does not cover are written as R = G = B = 0 (-32768 m or -10000 m): read
anything below -9000 m as "no data". Needs the GDAL Python bindings and numpy (the tool
container has both).
"""
import argparse
from datetime import date

import numpy as np
from osgeo import gdal

import tilewriter

NODATA = -32768.0

ap = argparse.ArgumentParser()
ap.add_argument("dems", nargs="+")
ap.add_argument("out")
ap.add_argument("--outline")
ap.add_argument("--minzoom", type=int, default=8)
ap.add_argument("--maxzoom", type=int, default=12)
ap.add_argument("--encoding", choices=["terrarium", "mapbox"], default="terrarium")
ap.add_argument("--format", choices=["png", "webp"], default="png")
ap.add_argument("--step", type=float, default=0.1, help="terrarium height step in metres (default 0.1; 0 keeps 1/256)")
ap.add_argument("--jobs", type=int, default=tilewriter.default_jobs())
ap.add_argument("--attribution", default="")
ap.add_argument("--source", default="")
ap.add_argument("--source-date", default="")
a = ap.parse_args()


def warp(src, z, bounds, w, h):
    ds = gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857", outputBounds=bounds, width=w, height=h,
                   resampleAlg="bilinear" if z == a.maxzoom else "average", dstNodata=NODATA, multithread=False,
                   # XSCALE/YSCALE=1: sample the DEM at the pixel centre instead of widening the
                   # bilinear kernel when the tile pixels are larger than the DEM's.
                   warpOptions=["XSCALE=1", "YSCALE=1"] if z == a.maxzoom else [])
    arr = tilewriter.as_float(ds)
    return arr, np.isfinite(arr) & (arr > NODATA + 1)


def encode(h, valid):
    """Float heights (metres) -> encoded image bytes; invalid pixels as R = G = B = 0."""
    if a.encoding == "terrarium":
        q = np.round((np.where(valid, h, 0) + 32768.0) * 256.0)
        if a.step > 0:
            q = np.round(q / (a.step * 256.0)) * (a.step * 256.0)
        q = np.clip(q, 1, 2 ** 24 - 1).astype(np.uint32)
    else:
        q = np.clip(np.round((np.where(valid, h, 0) + 10000.0) * 10.0), 1, 2 ** 24 - 1).astype(np.uint32)
    q[~valid] = 0
    bands = [((q >> s) & 255).astype(np.uint8) for s in (16, 8, 0)]
    if a.format == "png":
        return tilewriter.image_bytes(bands, "PNG", ["ZLEVEL=9"])
    # Lossless; QUALITY is the compression effort. 100 takes about 6 times as long for 0.3 % less.
    return tilewriter.image_bytes(bands, "WEBP", ["LOSSLESS=YES", "QUALITY=75"])


def main():
    tilewriter.write(a.out, a.dems, a.outline, a.minzoom, a.maxzoom, warp, encode, {
        "name": "Elevation", "type": "baselayer", "version": "1", "format": a.format, "encoding": a.encoding,
        # No tileSize row: TileServer GL would read it as a string. config.json sets 256.
        "description": f"Elevation in metres, {a.encoding} encoding; R=G=B=0 is no data",
        "attribution": a.attribution,
        "fieldmap:source": a.source, "fieldmap:source_date": a.source_date, "fieldmap:built": date.today().isoformat(),
    }, a.jobs, mask=True)


if __name__ == "__main__":
    main()
