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
centre is outside the outline are no data, so the lines stop at the outline.

With --usgs-dir (DEM_SOURCE=usgs, see scripts/lib.sh) a cell that has a USGS 3DEP 1/3 arc-second
tile there is contoured from it, in 3 x 3 blocks (<cell>a ... <cell>i), with the Copernicus
cell underneath where the tile has no data. Its lines do not join those of a neighbouring
Copernicus cell: the two are different surfaces (bare ground, and the top of trees and roofs).

Heights are in metres; for feet they are multiplied by 3.28084 first, so the levels are whole
multiples of the interval.
"""
import argparse
import math
import multiprocessing as mp
import os
import sys
import time
import uuid

import numpy as np
from osgeo import gdal, ogr, osr

from tilewriter import box, cell_name, cell_origin, layer_mask, read_outline, usgs_name

gdal.UseExceptions()
ogr.UseExceptions()

NODATA = -32768.0
UNITS = {"ft": {"minor": 40, "major": 200, "attr": "elev_ft", "scale": 3.28084, "type": "integer"},
         "m":  {"minor": 20, "major": 100, "attr": "elev_m", "scale": 1.0, "type": "float"}}

_cfg = {}


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
    # Only the valid pixels are scaled: a USGS tile can hold a no-data value of -3.4e38 that it
    # does not declare, and scaling that to feet overflows float32 (harmless, but it warns).
    heights = np.full(arr.shape, NODATA, dtype=np.float32)
    heights[valid] = arr[valid] * u["scale"]
    band.WriteArray(heights)
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
                             # The cut can leave a line in several pieces.
                             clipSrc=clip, geometryType="MULTILINESTRING",
                             SQLStatement=f"SELECT CAST(e AS {u['type']}) AS {u['attr']} FROM contour WHERE {where}")
        # The cut can remove every line (a few short ones in the outer half pixel). GDAL then
        # leaves an empty file, which it cannot open again.
        if os.path.getsize(part):
            written = ogr.Open(part)
            counts[i] = written.GetLayer(0).GetFeatureCount()
            written = None
        if counts[i]:
            os.replace(part, path)
        else:
            os.remove(part)
    return counts


def cop_file(lat, lon):
    return os.path.join(_cfg["dem_dir"], cell_name(lat, lon) + ".tif")


def usgs_file(lat, lon):
    """The cell's USGS tile when the build uses them (--usgs-dir) and it exists, else None."""
    p = _cfg["usgs_dir"] and os.path.join(_cfg["usgs_dir"], usgs_name(lat, lon) + ".tif")
    return p if p and os.path.exists(p) else None


def sources(lat, lon):
    """The files a cell is read from, the upper one last: the USGS tile lies over the
    Copernicus cell, which shows through where the tile has no data."""
    return [p for p in (cop_file(lat, lon), usgs_file(lat, lon)) if p and os.path.exists(p)]


def read_window(files, bounds, px, py):
    """Heights of the files on one grid, as float64, and where they have data."""
    vrt_path = f"/vsimem/{uuid.uuid4().hex}.vrt"
    # USGS tiles are in NAD 83, within about a metre of WGS 84: taken as the same.
    vrt = gdal.BuildVRT(vrt_path, files, resolution="user", xRes=px, yRes=py, outputBounds=bounds,
                        resampleAlg="bilinear", VRTNodata=NODATA, allowProjectionDifference=True)
    arr = vrt.GetRasterBand(1).ReadAsArray().astype(np.float64)
    vrt = None
    gdal.Unlink(vrt_path)
    return arr, arr > NODATA + 1


def contour_cell(job):
    """Write one cell's lines, or one ninth of a USGS cell's; returns (name, major lines, minor lines)."""
    part, name, block = job
    lat, lon = cell_origin(name)
    if block is None:
        counts, base = copernicus_cell(part, lat, lon), short_name(name)
    else:
        counts, base = usgs_block(part, lat, lon, block), short_name(name) + BLOCKS[block]
    with open(os.path.join(_cfg["out_dir"], base + ".done"), "w") as f:
        f.write(f"{counts[0]} {counts[1]}\n")
    return name, counts[0], counts[1]


def copernicus_cell(part, lat, lon):
    base = os.path.join(_cfg["out_dir"], short_name(cell_name(lat, lon)))
    src = gdal.Open(cop_file(lat, lon))
    gt, xs, ys = src.GetGeoTransform(), src.RasterXSize, src.RasterYSize
    src = None
    px, py = gt[1], -gt[5]
    # The neighbours that supply the extra column and row. The cell east of 179° is on the
    # other side of the 180° line, in another part of the outline: none there.
    near = {"e": (lat, lon + 1), "n": (lat + 1, lon), "ne": (lat + 1, lon + 1)}
    have = {k: (lon + 1 < 180 or k == "n") and bool(sources(*c)) for k, c in near.items()}
    # Above 50°, 60° ... the cells have fewer, wider pixels. A north neighbour on another grid
    # cannot supply the extra row: the cell stops at its own top row and the row between the
    # two cells is contoured on its own (see seam_row). (A USGS neighbour is on another grid
    # too, and another surface: its heights are interpolated onto the extra row and column.)
    north = gdal.Open(cop_file(*near["n"])) if have["n"] and not usgs_file(*near["n"]) else None
    seam = north is not None and north.RasterXSize != xs
    files = [cop_file(lat, lon)]
    for k in near:
        if have[k] and not (seam and k != "e"):
            files += sources(*near[k])
    w, s, e, n = gt[0], gt[3] - ys * py, gt[0] + (xs + 1) * px, gt[3] + py
    wgt = (w, px, 0.0, n, 0.0, -py)
    # On the cell's own grid, so its pixels are copied, not interpolated.
    arr, valid = read_window(files, (w, s, e, n), px, py)
    valid &= outline_mask(part, wgt, xs + 1, ys + 1)
    if not have["n"] or seam:
        valid[0, :] = False
    if not have["e"]:
        valid[1:, xs] = False
    if not have["ne"]:
        valid[0, xs] = False
    # Where the lines are cut: pixel centres on the sides that have a neighbour.
    clip = (w + px / 2 if lon > -180 and sources(lat, lon - 1) else w,
            s + py / 2 if sources(lat - 1, lon) else s,
            e - px / 2 if have["e"] else e,
            n - py * 1.5 if seam else n - py / 2 if have["n"] else n)
    counts = write_lines(arr, valid, wgt, clip, base)
    if seam:
        ne = cop_file(*near["ne"])
        more = seam_row(part, arr[1], valid[1], north, ne if have["ne"] and os.path.exists(ne) else None,
                        w + px / 2, n - py * 1.5, py, base + "n")
        counts = [counts[0] + more[0], counts[1] + more[1]]
    return counts


BLOCKS = "abcdefghi"                 # a USGS cell is contoured in 3 x 3 blocks, from the south-west
MARGIN = 5                           # pixels read round a block; a USGS tile has 6 past its cell
COPERNICUS_ROW = 1 / 3600            # Copernicus rows are one arc second apart at every latitude


def usgs_block(part, lat, lon, block):
    """One ninth of a cell that has a USGS 1/3 arc-second tile (10800 x 10800 pixels, with
    pixel edges, not centres, on the whole degrees; the whole cell at once would take
    several GB of memory). Neighbouring blocks and cells read the same pixels round a shared
    edge, so cutting the lines at the edge makes them meet exactly: every pixel is taken from
    the tile of the cell it lies in, not from the 6 pixels by which the tiles overlap."""
    base = os.path.join(_cfg["out_dir"], short_name(cell_name(lat, lon)) + BLOCKS[block])
    own = usgs_file(lat, lon)
    src = gdal.Open(own)
    gt = src.GetGeoTransform()
    src = None
    px, py = gt[1], -gt[5]
    i, j = block % 3, block // 3
    x0, x1, y0, y1 = lon + i / 3, lon + (i + 1) / 3, lat + j / 3, lat + (j + 1) / 3
    # A Copernicus cell's lines reach from one row north of its south edge to one row north of
    # its north edge (see copernicus_cell): next to one, this cell's lines are cut there too.
    copernicus = lambda la, lo: os.path.exists(cop_file(la, lo)) and not usgs_file(la, lo)
    clip = (x0, y0 + COPERNICUS_ROW if j == 0 and copernicus(lat - 1, lon) else y0,
            x1, y1 + COPERNICUS_ROW if j == 2 and copernicus(lat + 1, lon) else y1)
    # The block and MARGIN pixels round it, on the tile's own grid.
    col, row = round((x0 - gt[0]) / px) - MARGIN, round((gt[3] - y1) / py) - MARGIN
    xs, ys = round((x1 - x0) / px) + 2 * MARGIN, round((y1 - y0) / py) + 2 * MARGIN
    w, n = gt[0] + col * px, gt[3] - row * py
    bounds = (w, n - ys * py, w + xs * px, n)
    wgt = (w, px, 0.0, n, 0.0, -py)
    files, crops = [], []
    around = [(lat + dj, lon + di) for dj in (-1, 0, 1) for di in (-1, 0, 1)
              if box(lon + di, lat + dj, lon + di + 1, lat + dj + 1).Intersects(box(*bounds))]
    for la, lo in around:
        if os.path.exists(cop_file(la, lo)):
            files.append(cop_file(la, lo))
    files.append(own)
    for la, lo in around:
        if (la, lo) != (lat, lon) and usgs_file(la, lo) and -180 <= lo < 180:
            crops.append(f"/vsimem/{uuid.uuid4().hex}.vrt")
            gdal.Translate(crops[-1], usgs_file(la, lo), format="VRT", projWin=[lo, la + 1, lo + 1, la])
            files.append(crops[-1])
    arr, valid = read_window(files, bounds, px, py)
    for c in crops:
        gdal.Unlink(c)
    valid &= outline_mask(part, wgt, xs, ys)
    return write_lines(arr, valid, wgt, clip, base)


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
    ap.add_argument("--usgs-dir")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--units", choices=list(UNITS), default="m")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    _cfg.update(outline_dir=a.outline_dir, dem_dir=a.dem_dir, usgs_dir=a.usgs_dir, out_dir=a.out_dir,
                units=UNITS[a.units], parts={})
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.outline_dir, "cells.txt")) as f:
        rows = [ln.split() for ln in f if ln.strip()]
    jobs, cells, usgs = [], 0, 0
    for p, name in rows:
        lat, lon = cell_origin(name)
        if usgs_file(lat, lon):
            jobs += [(int(p), name, b) for b in range(len(BLOCKS))]
            usgs += 1
        elif os.path.exists(cop_file(lat, lon)):
            jobs.append((int(p), name, None))
        else:
            continue
        cells += 1
    if not jobs:
        sys.exit("Error: no elevation data for this outline (every cell is open sea).")
    done_file = lambda j: os.path.join(a.out_dir, short_name(j[1]) + ("" if j[2] is None else BLOCKS[j[2]]) + ".done")
    todo = [j for j in jobs if not os.path.exists(done_file(j))]
    print(f"  {cells} cells with elevation data{f' ({usgs} from USGS tiles, in 9 blocks each)' if usgs else ''}: "
          f"{len(jobs) - len(todo)} of {len(jobs)} pieces already done, {len(todo)} to do, {a.jobs} processes", flush=True)
    t0, done, lines = time.time(), 0, 0
    with mp.Pool(a.jobs) as pool:
        for _name, major, minor in pool.imap_unordered(contour_cell, todo):
            done += 1
            lines += major + minor
            if done % 20 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} pieces, {lines:,} lines, {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
