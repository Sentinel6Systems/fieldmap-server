#!/usr/bin/env python3
"""Region outline for the terrain builds (contours, hillshade, elevation).

    region-outline.py prepare  --out DIR --dem-dir DIR --bbox W S E N [--areas PATH ...] [--poly-cache DIR]
    region-outline.py mosaic   --out DIR --dem-dir DIR
    region-outline.py estimate --out DIR --dem-dir DIR STEP ... [--units ft|m] [--elevation-maxzoom N]
                               [--hillshade-maxzoom N] [--hillshade-format F]

prepare: the outline is the union of the Geofabrik .poly files of the areas (downloaded once
into --poly-cache), cut to the box. Without areas, or when any area has no outline, it is the
box alone. A box whose min-lon is larger than its max-lon crosses the 180° line. An outline
that spans more than 180° of longitude (Alaska's Aleutians reach past 180°) is split into a
west part (-180..0) and an east part (0..180), and each part is processed on its own, so no
mosaic spans the whole globe. Writes, in --out:
  outline.geojson       the whole outline (EPSG:4326)
  part-N.geojson        the outline of part N
  parts.txt             "N min-lon min-lat max-lon max-lat" for each part
  cells.txt             "N <Copernicus tile name>" for each 1° cell that touches part N
  summary.json          areas and cell counts, for the estimate
mosaic: for each part with at least one downloaded cell, part-N.vrt (the cells on their own
grid) and part-N-masked.vrt (the same grid, -32768 outside the outline); lists them in
mosaics.txt as "N part-N.vrt part-N-masked.vrt part-N.geojson XMIN YMIN XMAX YMAX RES": the
last five are the part's extent and pixel size in Web Mercator, for gdalwarp -te/-tr (left
to itself, gdalwarp sees a part that touches 180° wrap round the globe and picks a far too
coarse grid).
estimate: prints the expected output size and time of each step.

Cells that Copernicus has no file for (open sea, mostly) are listed in <dem-dir>/ocean-cells.txt
by the download (scripts/lib.sh), so later runs neither request them again nor count them as land.
"""
import argparse
import json
import math
import os
import sys
import urllib.request

from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

GEOFABRIK = "https://download.geofabrik.de"
ORIGIN = 20037508.342789244          # half the Web Mercator world width, metres


def cell_name(lat, lon):
    return (f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM")


def box(w, s, e, n):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((w, s), (e, s), (e, n), (w, n), (w, s)):
        ring.AddPoint_2D(x, y)
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(ring)
    return g


def parse_poly(path):
    """Osmosis .poly file -> polygon geometry (outer rings united, '!' rings cut out)."""
    outer, holes = ogr.Geometry(ogr.wkbMultiPolygon), []
    with open(path) as f:
        lines = [ln.strip() for ln in f]
    i = 1                                    # line 0 is the file's name
    while i < len(lines) and lines[i] != "END":
        hole, pts = lines[i].startswith("!"), []
        i += 1
        while lines[i] != "END":
            x, y = map(float, lines[i].split()[:2])
            pts.append((x, y))
            i += 1
        i += 1
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in pts + pts[:1]:
            ring.AddPoint_2D(x, y)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        if not poly.IsValid():
            poly = poly.MakeValid()
        pieces = [poly] if poly.GetGeometryType() == ogr.wkbPolygon else list(poly)
        for p in pieces:
            if ogr.GT_Flatten(p.GetGeometryType()) != ogr.wkbPolygon:
                continue
            if hole:
                holes.append(p.Clone())
            else:
                outer.AddGeometry(p)
    g = outer.UnionCascaded()
    for h in holes:
        g = g.Difference(h)
    return g


def fetch_poly(area, cache):
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, area.strip("/").replace("/", "_") + ".poly")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        url = f"{GEOFABRIK}/{area.strip('/')}.poly"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
        except Exception as e:                # noqa: BLE001 (any failure means: no outline)
            print(f"  no outline for {area} ({e})", file=sys.stderr)
            return None
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
    return parse_poly(path)


def equal_area_km2(g):
    """Area of a lon/lat geometry in km² (WGS 84 cylindrical equal-area)."""
    src, dst = osr.SpatialReference(), osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst.ImportFromEPSG(6933)
    for s in (src, dst):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    c = g.Clone()
    c.Transform(osr.CoordinateTransformation(src, dst))
    return c.GetArea() / 1e6


def write_geojson(path, g):
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": json.loads(g.ExportToJson())}]}, f)


def read_geojson(path):
    with open(path) as f:
        return ogr.CreateGeometryFromJson(json.dumps(json.load(f)["features"][0]["geometry"]))


def ocean_cells(dem_dir):
    p = os.path.join(dem_dir, "ocean-cells.txt")
    if not os.path.exists(p):
        return set()
    with open(p) as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}


def prepare(a):
    w, s, e, n = a.bbox
    if not (-90 <= s < n <= 90 and -180 <= w <= 180 and -180 <= e <= 180 and w != e):
        sys.exit(f"Error: bad box {w} {s} {e} {n} (min-lon min-lat max-lon max-lat)")
    clip = box(w, s, e, n) if w < e else box(w, s, 180, n).Union(box(-180, s, e, n))
    outline, source = None, "the box (no Geofabrik areas given)"
    if a.areas:
        polys = [fetch_poly(x, a.poly_cache) for x in a.areas]
        if all(p is not None for p in polys):
            outline = polys[0]
            for p in polys[1:]:
                outline = outline.Union(p)
            outline = outline.Intersection(clip)
            source = f"Geofabrik outline{'s' if len(polys) > 1 else ''} of {', '.join(a.areas)}, cut to the box"
            if outline.IsEmpty():
                sys.exit("Error: the areas' outline and BBOX do not overlap.")
        else:
            source = "the box (an area's outline could not be loaded)"
    if outline is None:
        outline = clip
    os.makedirs(a.out, exist_ok=True)
    write_geojson(os.path.join(a.out, "outline.geojson"), outline)

    # Split at 180° when the outline spans more than half the globe.
    x0, x1, _, _ = outline.GetEnvelope()
    halves = [box(-180, -90, 0, 90), box(0, -90, 180, 90)] if x1 - x0 > 180 else [None]
    parts = []
    for h in halves:
        g = outline if h is None else outline.Intersection(h)
        if not g.IsEmpty() and g.GetArea() > 0:
            parts.append(g)

    ocean = ocean_cells(a.dem_dir)
    summary = {"source": source, "parts": [], "km2": 0.0, "land_km2": 0.0,
               "cells": 0, "cached": 0, "ocean": 0}
    with open(os.path.join(a.out, "parts.txt"), "w") as pf, open(os.path.join(a.out, "cells.txt"), "w") as cf:
        for i, g in enumerate(parts):
            write_geojson(os.path.join(a.out, f"part-{i}.geojson"), g)
            gw, ge, gs, gn = g.GetEnvelope()
            pf.write(f"{i} {gw} {gs} {ge} {gn}\n")
            km2 = equal_area_km2(g)
            land = 0.0
            for lat in range(math.floor(gs), math.ceil(gn)):
                for lon in range(math.floor(gw), math.ceil(ge)):
                    piece = g.Intersection(box(lon, lat, lon + 1, lat + 1))
                    if piece.IsEmpty() or piece.GetArea() == 0:
                        continue
                    name = cell_name(lat, lon)
                    summary["cells"] += 1
                    if name in ocean:
                        summary["ocean"] += 1
                        continue
                    cf.write(f"{i} {name}\n")
                    land += equal_area_km2(piece)
                    if os.path.exists(os.path.join(a.dem_dir, name + ".tif")):
                        summary["cached"] += 1
            summary["parts"].append({"bbox": [gw, gs, ge, gn], "km2": round(km2)})
            summary["km2"] += km2
            summary["land_km2"] += land
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    todo = summary["cells"] - summary["cached"] - summary["ocean"]
    print(f"Outline: {source}")
    print(f"  {len(parts)} part{'s' if len(parts) != 1 else ''}"
          f"{' (split at the 180° line)' if len(parts) > 1 else ''}, {summary['km2']:,.0f} km²; "
          f"{summary['cells']} one-degree cells touch it: {summary['cached']} downloaded, "
          f"{summary['ocean']} known to have no data, {todo} to fetch")


def mosaic(a):
    out = []
    with open(os.path.join(a.out, "cells.txt")) as f:
        rows = [ln.split() for ln in f if ln.strip()]
    for i in sorted({int(r[0]) for r in rows}):
        files = [os.path.join(a.dem_dir, r[1] + ".tif") for r in rows if int(r[0]) == i]
        files = [p for p in files if os.path.exists(p)]
        if not files:
            continue
        raw = os.path.join(a.out, f"part-{i}.vrt")
        cut = os.path.join(a.out, f"part-{i}.geojson")
        # The cells on their own grid (cutting the mosaic to a box would shift the heights by
        # half a pixel). Above 50° Copernicus cells are narrower in longitude; -resolution
        # highest with bilinear keeps the finest grid and interpolates the coarser cells.
        # A missing cell inside the mosaic is open sea and reads as 0 m.
        vrt = gdal.BuildVRT(raw, files, resolution="highest", resampleAlg="bilinear")
        gt, xs, ys = vrt.GetGeoTransform(), vrt.RasterXSize, vrt.RasterYSize
        vrt = None
        masked = os.path.join(a.out, f"part-{i}-masked.vrt")
        # Same grid (nearest neighbour on identical pixels is a copy), -32768 outside the outline.
        gdal.Warp(masked, raw, format="VRT", cutlineDSName=cut, dstNodata=-32768, resampleAlg="near",
                  outputBounds=(gt[0], gt[3] + ys * gt[5], gt[0] + xs * gt[1], gt[3]), xRes=gt[1], yRes=-gt[5],
                  outputType=gdal.GDT_Float32)
        # Web Mercator x is linear in longitude: keep the cells' longitude spacing.
        w, e, s, n = read_geojson(cut).GetEnvelope()
        mx = lambda lon: max(-180.0, min(180.0, lon)) * ORIGIN / 180
        my = lambda lat: math.log(math.tan(math.pi / 4 + math.radians(max(-85.0511, min(85.0511, lat))) / 2)) * ORIGIN / math.pi
        out.append(f"{i} {raw} {masked} {cut} {mx(w):.3f} {my(s):.3f} {mx(e):.3f} {my(n):.3f} {gt[1] * ORIGIN / 180:.4f}")
    if not out:
        sys.exit("Error: no elevation data for this outline (every cell is open sea).")
    with open(os.path.join(a.out, "mosaics.txt"), "w") as f:
        f.write("\n".join(out) + "\n")


# Measured on test builds (see the README's "Terrain outline"): output kB per km² of land
# (low = flat, high = rugged; at zoom 12 and 40 ft contours) and seconds per 1000 km² on
# 8 CPUs. Elevation and hillshade tiles are written in parallel and scale with the CPUs;
# the contour lines are mostly one process.
RATES = {
    "contours":  {"kb": (1.5, 5.5), "secs": 9.0, "parallel": False},
    "hillshade": {"kb": {"webp": (0.1, 0.25), "webp-lossless": (0.4, 0.9), "png": (0.5, 1.0)},
                  "secs": 1.0, "parallel": False},
    "elevation": {"kb": (0.5, 1.4), "secs": 1.0, "parallel": True},
}
CELL_MB = 25          # average Copernicus cell download


def estimate(a):
    with open(os.path.join(a.out, "summary.json")) as f:
        sm = json.load(f)
    land = sm["land_km2"]
    cpus = os.cpu_count() or 2
    todo = sm["cells"] - sm["cached"] - sm["ocean"]
    print(f"Estimate for {land:,.0f} km² (cells known to be open sea left out), {cpus} CPUs; "
          f"sizes run from flat to rugged terrain:")
    if todo:
        print(f"  download     {todo} cells, at most {todo * CELL_MB / 1024:.1f} GB "
              f"({fmt_time(todo * CELL_MB / 10)} at 10 MB/s)")
    for step in a.steps:
        r = RATES[step]
        kb = r["kb"][a.hillshade_format] if step == "hillshade" else r["kb"]
        zoom = {"elevation": a.elevation_maxzoom, "hillshade": a.hillshade_maxzoom}.get(step, 12)
        scale = 4.0 ** (zoom - 12)
        lo, hi = (land * k * scale / 1024 for k in kb)
        secs = 10 + land / 1000 * r["secs"] * max(scale, 0.25) * (8 / cpus if r["parallel"] else 1)
        print(f"  {step:<12} {fmt_mb(lo)} to {fmt_mb(hi)}, {fmt_time(secs)}")


def fmt_mb(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB" if mb >= 1 else "<1 MB"


def fmt_time(s):
    return f"about {s / 3600:.1f} h" if s >= 5400 else f"about {round(s / 60)} min" if s >= 90 else "under 2 min"


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--bbox", nargs=4, type=float, required=True)
    p.add_argument("--areas", nargs="*", default=[])
    p.add_argument("--poly-cache", default="data/cache/outlines")
    m = sub.add_parser("mosaic")
    q = sub.add_parser("estimate")
    q.add_argument("steps", nargs="+", choices=list(RATES))
    q.add_argument("--elevation-maxzoom", type=int, default=12)
    q.add_argument("--hillshade-maxzoom", type=int, default=12)
    q.add_argument("--hillshade-format", default="webp", choices=["webp", "webp-lossless", "png"])
    for x in (p, m, q):
        x.add_argument("--out", required=True)
        x.add_argument("--dem-dir", required=True)
    a = ap.parse_args()
    {"prepare": prepare, "mosaic": mosaic, "estimate": estimate}[a.cmd](a)


if __name__ == "__main__":
    main()
