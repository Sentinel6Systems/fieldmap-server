#!/usr/bin/env python3
"""Helper for scripts/build-detail.sh: a small area of 1 m hillshade from USGS lidar.

    detail-area.py find --bbox W S E N --out DIR [--max-km2 10000] [--zoom 17]

Checks the size of the box, asks The National Map (https://tnmaccess.nationalmap.gov) for the
USGS 3DEP 1 m elevation tiles that touch it, and writes in DIR
  tiles.txt    "URL" per line, the oldest survey first: where several cover a place, the build
               lays them on top of each other in this order, so the newest shows
  box.geojson  the box, as the outline of the build
  grid.txt     "XMIN YMIN XMAX YMAX RES": the box on the pixel grid of the tiles at --zoom, in
               Web Mercator, for tools/hillshade-raster.py
The 1 m tiles are 10 km squares in UTM, one survey project at a time; where no project has
flown yet there is no tile, and the area keeps its coarser hillshade.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import tilewriter

API = "https://tnmaccess.nationalmap.gov/api/v1/products"
DATASET = "Digital Elevation Model (DEM) 1 meter"
UA = {"User-Agent": "fieldmap-server detail area"}


def km2(w, s, e, n):
    return (e - w) * (n - s) * 111.32 ** 2 * math.cos(math.radians((s + n) / 2))


def products(bbox):
    items, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"datasets": DATASET, "bbox": ",".join(f"{v:g}" for v in bbox),
                                    "prodFormats": "GeoTIFF", "max": 200, "offset": offset})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(f"{API}?{q}", headers=UA), timeout=120) as r:
                    page = json.load(r)
                break
            except (OSError, ValueError) as err:
                if attempt == 4:
                    sys.exit(f"Error: The National Map did not answer ({err}). Try again later.")
                time.sleep(10 * (attempt + 1))
        items += page.get("items", [])
        offset += 200
        if offset >= page.get("total", 0) or not page.get("items"):
            return items


def find(a):
    w, s, e, n = a.bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        sys.exit("Error: the detail area must be min-lon min-lat max-lon max-lat, and not across the 180° line.")
    area = km2(w, s, e, n)
    if area > a.max_km2:
        sys.exit(f"Error: the detail area is {area:,.0f} km²; the most is {a.max_km2:,.0f} km² "
                 f"(about {math.sqrt(a.max_km2):.0f} x {math.sqrt(a.max_km2):.0f} km). At 1 m, every 100 km² is a download "
                 "of up to 500 MB and thousands of tiles. Set DETAIL_MAX_KM2 to allow more.")
    # Every survey's tile, not only the newest of each 10 km square: a tile holds only what
    # its survey flew, so the newest is often a sliver of a square that an older one covers.
    tiles = {}
    for it in products(a.bbox):
        url = it.get("downloadURL", "")
        if url.lower().endswith(".tif"):
            tiles[url] = it.get("publicationDate") or it.get("dateCreated") or ""
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "tiles.txt"), "w") as f:
        f.writelines(url + "\n" for url, _date in sorted(tiles.items(), key=lambda v: (v[1], v[0])))
    with open(os.path.join(a.out, "box.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}}]}, f)
    # The box, widened to whole tiles at the highest zoom.
    o, k = tilewriter.ORIGIN, 2 ** a.zoom
    x0, x1, y0, y1 = tilewriter.tile_range(a.zoom, w, s, e, n)
    size = 2 * o / k
    with open(os.path.join(a.out, "grid.txt"), "w") as f:
        f.write(f"{-o + x0 * size:.4f} {o - (y1 + 1) * size:.4f} {-o + (x1 + 1) * size:.4f} {o - y0 * size:.4f} "
                f"{size / tilewriter.TILE:.9f}\n")
    print(f"Detail area: {area:,.0f} km², {len(tiles)} USGS 1 m tiles (10 km squares, one per survey) touch it")
    if not tiles:
        sys.exit("Error: USGS has no 1 m elevation data for this area yet.")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("--out", required=True)
    f.add_argument("--max-km2", type=float, default=10000)
    f.add_argument("--zoom", type=int, default=17)
    f.add_argument("--bbox", nargs=4, type=float, required=True)
    find(ap.parse_args())


if __name__ == "__main__":
    main()
