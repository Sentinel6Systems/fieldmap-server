#!/usr/bin/env python3
"""Contour lines for a region outline, one 1° elevation cell at a time.

    python3 tools/contour-cells.py --outline-dir <work>/outline --dem-dir data/cache/dem \
        --out-dir <cells-dir> --units ft|m [--jobs N]

build-contours.sh runs this after tools/region-outline.py has written the outline and the
elevation cells are downloaded. Each cell in <outline-dir>/cells.txt is contoured on its own
grid, in parallel, and its lines are written to <out-dir> as two FlatGeobuf files that
tippecanoe reads directly:

  <cell>-major.fgb   every 200 ft (or 100 m)
  <cell>-minor.fgb   the other 40 ft (or 20 m) lines
  <cell>.done        written last; a cell that has it is skipped on the next run

so a stopped build carries on where it was, and no file ever holds more than one cell.

A cell is read with one more pixel column to the east and row to the north, taken from its
neighbours, and its lines are cut at the pixel centres of its first column, its bottom row and
that extra column and row. The neighbours' lines are cut along the same pixel centres, so the
lines meet there (GDAL draws half a pixel past the outermost centres from one row of heights;
that part is kept only on a side with no neighbour). Pixels whose centre is outside the outline
are no data, so the lines stop at the outline. Heights are in metres; for feet they
are multiplied by 3.28084 first, so the levels are whole multiples of the interval.
"""
import argparse
import json
import multiprocessing as mp
import os
import re
import sys
import time
import uuid

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

NODATA = -32768.0
UNITS = {"ft": {"minor": 40, "major": 200, "attr": "elev_ft", "scale": 3.28084, "type": "integer"},
         "m":  {"minor": 20, "major": 100, "attr": "elev_m", "scale": 1.0, "type": "float"}}

_cfg = {}


def cell_name(lat, lon):
    return (f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM")


def cell_origin(name):
    m = re.fullmatch(r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d\d\d)_00_DEM", name)
    lat, lon = int(m.group(2)), int(m.group(4))
    return (lat if m.group(1) == "N" else -lat), (lon if m.group(3) == "E" else -lon)


def box(w, s, e, n):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((w, s), (e, s), (e, n), (w, n), (w, s)):
        ring.AddPoint_2D(x, y)
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(ring)
    return g


def wgs84():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def part_outline(part):
    if part not in _cfg["parts"]:                # once per worker process
        with open(os.path.join(_cfg["outline_dir"], f"part-{part}.geojson")) as f:
            _cfg["parts"][part] = ogr.CreateGeometryFromJson(json.dumps(json.load(f)["features"][0]["geometry"]))
    return _cfg["parts"][part]


def inside_mask(piece, gt, xs, ys, srs):
    """True where the pixel centre is inside the geometry."""
    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vds.CreateLayer("outline", srs, ogr.wkbMultiPolygon)
    f = ogr.Feature(layer.GetLayerDefn())
    f.SetGeometry(piece)
    layer.CreateFeature(f)
    r = gdal.GetDriverByName("MEM").Create("", xs, ys, 1, gdal.GDT_Byte)
    r.SetGeoTransform(gt)
    r.SetProjection(srs.ExportToWkt())
    gdal.RasterizeLayer(r, [1], layer, burn_values=[1])
    return r.GetRasterBand(1).ReadAsArray() > 0


def contour_cell(job):
    """Write one cell's lines; returns (name, major lines, minor lines)."""
    part, name = job
    u, dem, out = _cfg["units"], _cfg["dem_dir"], _cfg["out_dir"]
    lat, lon = cell_origin(name)
    src = gdal.Open(os.path.join(dem, name + ".tif"))
    gt, xs, ys = src.GetGeoTransform(), src.RasterXSize, src.RasterYSize
    src = None
    px, py = gt[1], -gt[5]
    # The neighbours that supply the extra column and row. The cell east of 179° is on the
    # other side of the 180° line, in another part of the outline: none there.
    near = {"e": (lat, lon + 1), "n": (lat + 1, lon), "ne": (lat + 1, lon + 1)}
    have = {k: lon + 1 < 180 or k == "n" for k in near}
    files = [os.path.join(dem, name + ".tif")]
    for k, (la, lo) in near.items():
        p = os.path.join(dem, cell_name(la, lo) + ".tif")
        have[k] = have[k] and os.path.exists(p)
        if have[k]:
            files.append(p)
    w, s, e, n = gt[0], gt[3] - ys * py, gt[0] + (xs + 1) * px, gt[3] + py
    # Where the lines are cut: pixel centres on the sides that have a neighbour.
    exists = lambda la, lo: os.path.exists(os.path.join(dem, cell_name(la, lo) + ".tif"))
    clip = (w + px / 2 if lon > -180 and exists(lat, lon - 1) else w,
            s + py / 2 if exists(lat - 1, lon) else s,
            e - px / 2 if have["e"] else e,
            n - py / 2 if have["n"] else n)
    wgt = (w, px, 0.0, n, 0.0, -py)
    vrt_path = f"/vsimem/{uuid.uuid4().hex}.vrt"
    # On the cell's own grid: its pixels and the east neighbour's are copied; a north
    # neighbour above 50°, 60° ... has wider pixels and is interpolated along the row.
    vrt = gdal.BuildVRT(vrt_path, files, resolution="user", xRes=px, yRes=py,
                        outputBounds=(w, s, e, n), resampleAlg="bilinear")
    arr = vrt.GetRasterBand(1).ReadAsArray().astype(np.float32)
    vrt = None
    gdal.Unlink(vrt_path)
    valid = np.ones(arr.shape, dtype=bool)
    if not have["n"]:
        valid[0, :xs] = False
    if not have["e"]:
        valid[1:, xs] = False
    if not have["ne"]:
        valid[0, xs] = False
    srs = wgs84()
    window = box(w, s, e, n)
    piece = part_outline(part).Intersection(window)
    if piece.IsEmpty() or piece.GetArea() == 0:
        valid[:] = False
    elif piece.GetArea() < window.GetArea() * (1 - 1e-9):
        valid &= inside_mask(piece, wgt, xs + 1, ys + 1, srs)
    counts = [0, 0]
    if valid.any():
        arr = np.where(valid, arr * np.float32(u["scale"]), np.float32(NODATA))
        ras = gdal.GetDriverByName("MEM").Create("", xs + 1, ys + 1, 1, gdal.GDT_Float32)
        ras.SetGeoTransform(wgt)
        ras.SetProjection(srs.ExportToWkt())
        band = ras.GetRasterBand(1)
        band.WriteArray(arr)
        band.SetNoDataValue(NODATA)
        mem = gdal.GetDriverByName("Memory").Create("", 0, 0, 0, gdal.GDT_Unknown)
        layer = mem.CreateLayer("contour", srs, ogr.wkbLineString)
        layer.CreateField(ogr.FieldDefn("e", ogr.OFTInteger))
        gdal.ContourGenerateEx(band, layer, options=[f"LEVEL_INTERVAL={u['minor']}", "ELEV_FIELD=0", f"NODATA={NODATA}"])
        for i, (kind, test) in enumerate((("major", "="), ("minor", "<>"))):
            where = f"e % {u['major']} {test} 0"
            layer.SetAttributeFilter(where)
            counts[i] = layer.GetFeatureCount()
            layer.SetAttributeFilter(None)
            if not counts[i]:
                continue
            path = os.path.join(out, f"{name}-{kind}.fgb")
            part = os.path.join(out, f"{name}-{kind}.part.fgb")
            gdal.VectorTranslate(part, mem, format="FlatGeobuf", layerName=kind,
                                 layerCreationOptions=["SPATIAL_INDEX=NO"],
                                 clipSrc=clip, geometryType="LINESTRING", explodeCollections=True,
                                 SQLStatement=f"SELECT CAST(e AS {u['type']}) AS {u['attr']} FROM contour WHERE {where}")
            os.replace(part, path)
    with open(os.path.join(out, name + ".done"), "w") as f:
        f.write(f"{counts[0]} {counts[1]}\n")
    return name, counts[0], counts[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outline-dir", required=True)
    ap.add_argument("--dem-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--units", choices=list(UNITS), default="m")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    _cfg.update(outline_dir=a.outline_dir, dem_dir=a.dem_dir, out_dir=a.out_dir, units=UNITS[a.units], parts={})
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.outline_dir, "cells.txt")) as f:
        rows = [ln.split() for ln in f if ln.strip()]
    cells = [(int(p), name) for p, name in rows if os.path.exists(os.path.join(a.dem_dir, name + ".tif"))]
    if not cells:
        sys.exit("Error: no elevation data for this outline (every cell is open sea).")
    todo = [c for c in cells if not os.path.exists(os.path.join(a.out_dir, c[1] + ".done"))]
    print(f"  {len(cells)} cells with elevation data: {len(cells) - len(todo)} already done, "
          f"{len(todo)} to do, {a.jobs} processes", flush=True)
    t0, done, lines = time.time(), 0, 0
    with mp.Pool(a.jobs) as pool:
        for _name, major, minor in pool.imap_unordered(contour_cell, todo):
            done += 1
            lines += major + minor
            if done % 20 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} cells, {lines:,} lines, {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
