#!/usr/bin/env python3
"""Write hillshade raster tiles from gray hillshade rasters (gdaldem hillshade output).

    python3 tools/hillshade-tiles.py [--outline outline.geojson] [--minzoom 6] [--maxzoom 12] \
        [--format webp|webp-lossless|png] [--quality 80] [--jobs N] <hillshade> [...] <out.mbtiles>

Each <hillshade> is a one-band byte raster where 0 is no data (build-hillshade.sh passes
one per part of the region outline, masked to it). Tiles are 256 px, Web Mercator: bilinear
at maxzoom, the average of the source pixels below it. Pixels without data are transparent;
tiles without any data are not written.

Formats: webp (lossy, quality 80, method 4: the settings of tools/hillshade-webp.py, which
look the same as PNG on paper at a fraction of the size), webp-lossless, or png (gray).
"""
import argparse
from datetime import date

import numpy as np
from osgeo import gdal

import tilewriter

ap = argparse.ArgumentParser()
ap.add_argument("sources", nargs="+")
ap.add_argument("out")
ap.add_argument("--outline")
ap.add_argument("--minzoom", type=int, default=6)
ap.add_argument("--maxzoom", type=int, default=12)
ap.add_argument("--format", choices=["webp", "webp-lossless", "png"], default="webp")
ap.add_argument("--quality", type=int, default=80)
ap.add_argument("--jobs", type=int, default=tilewriter.default_jobs())
ap.add_argument("--attribution", default="")
a = ap.parse_args()


def warp(src, z, bounds, w, h):
    ds = gdal.Warp("", src, format="MEM", dstSRS="EPSG:3857", outputBounds=bounds, width=w, height=h,
                   resampleAlg="bilinear" if z == a.maxzoom else "average",
                   srcNodata=0, dstNodata=0, outputType=gdal.GDT_Byte, multithread=False)
    arr = ds.GetRasterBand(1).ReadAsArray()
    return arr, arr > 0


def encode(g, valid):
    full = bool(valid.all())
    if a.format == "png":
        bands = [g] if full else [g, np.where(valid, 255, 0).astype(np.uint8)]
        return tilewriter.image_bytes(bands, "PNG", ["ZLEVEL=6"])
    bands = [g, g, g] if full else [g, g, g, np.where(valid, 255, 0).astype(np.uint8)]
    opts = ["LOSSLESS=YES"] if a.format == "webp-lossless" else [f"QUALITY={a.quality}", "METHOD=4"]
    return tilewriter.image_bytes(bands, "WEBP", opts)


def main():
    tilewriter.write(a.out, a.sources, a.outline, a.minzoom, a.maxzoom, warp, encode, {
        "name": "Hillshade", "type": "overlay", "version": "1",
        "format": "png" if a.format == "png" else "webp",
        "description": f"Hillshade (azimuth 315°, altitude 45°), {a.format}",
        "attribution": a.attribution, "fieldmap:built": date.today().isoformat(),
    }, a.jobs)


if __name__ == "__main__":
    main()
