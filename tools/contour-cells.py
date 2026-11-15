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

(<cell> is short, N48W123, so that tippecanoe can be given every file of a large region on
one command line). A stopped build carries on where it was, and no file ever holds more than
one cell.

A cell is read with one more pixel column to the east and row to the north, taken from its
neighbours, and its lines are cut at the pixel centres of its first column, its bottom row and
that extra column and row. The neighbours' lines are cut along the same pixel centres, so the
lines meet there (GDAL draws half a pixel past the outermost centres from one row of heights;
that part is kept only on a side with no neighbour). Where the north neighbour has wider pixels
(at 50°, 60°, 70° ...) the row between the two cells is contoured on its own, on a grid that
holds both cells' pixel centres, into <cell>n-major.fgb and <cell>n-minor.fgb. Pixels whose
centre is outside the outline are no data, so the lines stop at the outline. Heights are in metres; for feet they
are multiplied by 3.28084 first, so the levels are whole multiples of the interval.
"""
import argparse
import math
import multiprocessing as mp
import os
import re
import sys
import time
import uuid

import numpy as np
from osgeo import gdal, ogr, osr

from tilewriter import box, cell_name, layer_mask, read_outline

gdal.UseExceptions()
ogr.UseExceptions()

NODATA = -32768.0
UNITS = {"ft": {"minor": 40, "major": 200, "attr": "elev_ft", "scale": 3.28084, "type": "integer"},
         "m":  {"minor": 20, "major": 100, "attr": "elev_m", "scale": 1.0, "type": "float"}}

_cfg = {}


def cell_origin(name):
    m = re.fullmatch(r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d\d\d)_00_DEM", name)
    lat, lon = int(m.group(2)), int(m.group(4))
    return (lat if m.group(1) == "N" else -lat), (lon if m.group(3) == "E" else -lon)


def short_name(name):
    lat, lon = cell_origin(name)
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def wgs84():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def part_outline(part):
    if part not in _cfg["parts"]:                # once per worker process
        _cfg["parts"][part] = read_outline(os.path.join(_cfg["outline_dir"], f"part-{part}.geojson"))
    return _cfg["parts"][part]


def outline_mask(part, gt, xs, ys):
    """True where the pixel centre is inside the part's outline, on a grid in EPSG:4326."""
    window = box(gt[0], gt[3] + ys * gt[5], gt[0] + xs * gt[1], gt[3])
    piece = part_outline(part).Intersection(window)
    if piece.IsEmpty() or piece.GetArea() == 0:
        return np.zeros((ys, xs), dtype=bool)
    if piece.GetArea() >= window.GetArea() * (1 - 1e-9):
        return np.ones((ys, xs), dtype=bool)
    srs = wgs84()
    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vds.CreateLayer("outline", srs, ogr.wkbMultiPolygon)
    f = ogr.Feature(layer.GetLayerDefn())
    f.SetGeometry(piece)
    layer.CreateFeature(f)
    return layer_mask(layer, srs.ExportToWkt(), gt, xs, ys)


def write_lines(arr, valid, gt, clip, base):
    """Contour a grid of heights in metres (lines only between valid pixels), cut the lines to
    the clip box and write <base>-major.fgb and <base>-minor.fgb; returns the two line counts."""
    u, counts = _cfg["units"], [0, 0]
    if not valid.any():
        return counts
    srs = wgs84()
    ras = gdal.GetDriverByName("MEM").Create("", arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ras.SetGeoTransform(gt)
    ras.SetProjection(srs.ExportToWkt())
    band = ras.GetRasterBand(1)
    band.WriteArray(np.where(valid, arr.astype(np.float32) * np.float32(u["scale"]), np.float32(NODATA)))
    band.SetNoDataValue(NODATA)
    mem = gdal.GetDriverByName("Memory").Create("", 0, 0, 0, gdal.GDT_Unknown)
    layer = mem.CreateLayer("contour", srs, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("e", ogr.OFTInteger))
    gdal.ContourGenerateEx(band, layer, options=[f"LEVEL_INTERVAL={u['minor']}", "ELEV_FIELD=0", f"NODATA={NODATA}"])
    for i, (kind, test) in enumerate((("major", "="), ("minor", "<>"))):
        where = f"e % {u['major']} {test} 0"
        layer.SetAttributeFilter(where)
        found = layer.GetFeatureCount()
        layer.SetAttributeFilter(None)
        if not found:
            continue
        path, part = f"{base}-{kind}.fgb", f"{base}-{kind}.part.fgb"
        gdal.VectorTranslate(part, mem, format="FlatGeobuf", layerName=kind,
                             layerCreationOptions=["SPATIAL_INDEX=NO"],
                             clipSrc=clip, geometryType="LINESTRING", explodeCollections=True,
                             SQLStatement=f"SELECT CAST(e AS {u['type']}) AS {u['attr']} FROM contour WHERE {where}")
        # The cut can remove every line (a few short ones in the outer half pixel).
        written = ogr.Open(part)
        counts[i] = written.GetLayer(0).GetFeatureCount()
        written = None
        if counts[i]:
            os.replace(part, path)
        else:
            os.remove(part)
    return counts


def contour_cell(job):
    """Write one cell's lines; returns (name, major lines, minor lines)."""
    part, name = job
    dem, out = _cfg["dem_dir"], _cfg["out_dir"]
    lat, lon = cell_origin(name)
    short = short_name(name)
    tif = lambda la, lo: os.path.join(dem, cell_name(la, lo) + ".tif")
    src = gdal.Open(tif(lat, lon))
    gt, xs, ys = src.GetGeoTransform(), src.RasterXSize, src.RasterYSize
    src = None
    px, py = gt[1], -gt[5]
    # The neighbours that supply the extra column and row. The cell east of 179° is on the
    # other side of the 180° line, in another part of the outline: none there.
    near = {"e": (lat, lon + 1), "n": (lat + 1, lon), "ne": (lat + 1, lon + 1)}
    have = {k: (lon + 1 < 180 or k == "n") and os.path.exists(tif(*c)) for k, c in near.items()}
    # Above 50°, 60° ... the cells have fewer, wider pixels. A north neighbour on another grid
    # cannot supply the extra row: the cell stops at its own top row and the row between the
    # two cells is contoured on its own (see seam_row).
    north = gdal.Open(tif(*near["n"])) if have["n"] else None
    seam = north is not None and north.RasterXSize != xs
    files = [tif(lat, lon)] + [tif(*near[k]) for k in near if have[k] and not (seam and k != "e")]
    w, s, e, n = gt[0], gt[3] - ys * py, gt[0] + (xs + 1) * px, gt[3] + py
    wgt = (w, px, 0.0, n, 0.0, -py)
    vrt_path = f"/vsimem/{uuid.uuid4().hex}.vrt"
    # On the cell's own grid, so the pixels are copied, not interpolated.
    vrt = gdal.BuildVRT(vrt_path, files, resolution="user", xRes=px, yRes=py,
                        outputBounds=(w, s, e, n), resampleAlg="bilinear")
    arr = vrt.GetRasterBand(1).ReadAsArray().astype(np.float64)
    vrt = None
    gdal.Unlink(vrt_path)
    valid = outline_mask(part, wgt, xs + 1, ys + 1)
    if not have["n"] or seam:
        valid[0, :] = False
    if not have["e"]:
        valid[1:, xs] = False
    if not have["ne"]:
        valid[0, xs] = False
    # Where the lines are cut: pixel centres on the sides that have a neighbour.
    clip = (w + px / 2 if lon > -180 and os.path.exists(tif(lat, lon - 1)) else w,
            s + py / 2 if os.path.exists(tif(lat - 1, lon)) else s,
            e - px / 2 if have["e"] else e,
            n - py * 1.5 if seam else n - py / 2 if have["n"] else n)
    counts = write_lines(arr, valid, wgt, clip, os.path.join(out, short))
    if seam:
        more = seam_row(part, arr[1], valid[1], north, tif(*near["ne"]) if have["ne"] else None,
                        w + px / 2, n - py * 1.5, py, os.path.join(out, short + "n"))
        counts = [counts[0] + more[0], counts[1] + more[1]]
    with open(os.path.join(out, short + ".done"), "w") as f:
        f.write(f"{counts[0]} {counts[1]}\n")
    return name, counts[0], counts[1]


def seam_row(part, row, row_valid, north, north_east, lon, lat, py, base):
    """The lines between a cell's top row and the bottom row of a north neighbour that has
    another pixel width. Both rows are put on a grid fine enough to hold the pixel centres of
    both (0.5 arc seconds between a 1.5 and a 2 arc second cell), by linear interpolation
    along the row: that is how a contour line crosses a row of pixel centres, so the lines
    here start and end exactly where the lines of the two cells end.
    row, row_valid: the cell's top row with the extra east column; lon, lat: the centre of
    its first pixel; north: the neighbour's open file; north_east: path of the cell east of it."""
    xs, nx, ny = len(row) - 1, north.RasterXSize, north.RasterYSize
    top = np.empty(nx + 1)
    top[:nx] = north.GetRasterBand(1).ReadAsArray(0, ny - 1, nx, 1)[0]
    top_valid = np.ones(nx + 1)
    if north_east:
        ne = gdal.Open(north_east)
        top[nx] = ne.GetRasterBand(1).ReadAsArray(0, ny - 1, 1, 1)[0, 0]
    else:
        top[nx], top_valid[nx] = 0, 0
    fine = math.lcm(xs, nx)
    t = np.arange(fine + 1) / fine
    arr = np.vstack([np.interp(t, np.arange(nx + 1) / nx, top), np.interp(t, np.arange(xs + 1) / xs, row)])
    gt = (lon - 0.5 / fine, 1.0 / fine, 0.0, lat + py * 1.5, 0.0, -py)
    valid = outline_mask(part, gt, fine + 1, 2)
    # Between two pixel centres only where both have data.
    valid[0] &= np.interp(t, np.arange(nx + 1) / nx, top_valid) > 1 - 1e-9
    valid[1] &= np.interp(t, np.arange(xs + 1) / xs, row_valid.astype(float)) > 1 - 1e-9
    return write_lines(arr, valid, gt, (lon, lat, lon + 1.0, lat + py), base)


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
    todo = [c for c in cells if not os.path.exists(os.path.join(a.out_dir, short_name(c[1]) + ".done"))]
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
