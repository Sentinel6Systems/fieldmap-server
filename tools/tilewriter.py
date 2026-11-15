"""Shared raster tile writing for elevation-tiles.py and hillshade-tiles.py.

Each source (one per part of a region outline; two when the outline crosses the 180° line)
is warped block by block (BLOCK x BLOCK tiles of 256 px, Web Mercator) for each zoom, in
parallel, and the tiles are written into one MBTiles file. Blocks that miss the outline are
not warped; with mask=True the outline is also rasterised into each block (pixel centres
inside it are valid), so the sources need not be masked and can keep their overviews.
Tiles with no valid pixel are not written.
"""
import json
import math
import multiprocessing as mp
import os
import re
import sqlite3
import time
import uuid

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

ORIGIN = 20037508.342789244          # half the Web Mercator world width, metres
TILE = 256
BLOCK = 8                            # tiles per side warped in one go
MAX_LAT = 85.0511

_cfg = {}
_mask = {}


def source_bbox(path):
    """Lon/lat bounds of a raster in EPSG:4326 or EPSG:3857, from its geotransform (gdalinfo's
    WGS 84 extent turns a Web Mercator edge at -180° into +180°)."""
    ds = gdal.Open(path)
    gt, w, h = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
    x0, x1 = gt[0], gt[0] + w * gt[1]
    y1, y0 = gt[3], gt[3] + h * gt[5]
    srs = ds.GetSpatialRef()
    if srs is not None and srs.IsProjected():
        lon = lambda x: x / ORIGIN * 180
        lat = lambda y: math.degrees(math.atan(math.sinh(y / ORIGIN * math.pi)))
        x0, x1, y0, y1 = lon(x0), lon(x1), lat(y0), lat(y1)
    return max(-180.0, x0), max(-90.0, y0), min(180.0, x1), min(90.0, y1)


def tile_range(z, w, s, e, n):
    """Tile columns and rows (XYZ, row 0 at the north) that touch the box at zoom z."""
    k = 2 ** z
    lat = lambda v: max(-MAX_LAT, min(MAX_LAT, v))
    x = lambda lon: min(k - 1, max(0, int(math.floor((lon + 180) / 360 * k))))
    y = lambda la: min(k - 1, max(0, int(math.floor((1 - math.asinh(math.tan(math.radians(lat(la)))) / math.pi) / 2 * k))))
    return x(w), x(e - 1e-9), y(n), y(s + 1e-9)


def _tile_lonlat(z, x, y):
    k = 2 ** z
    return x / k * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / k))))


def cell_name(lat, lon):
    """File name (without .tif) of the Copernicus GLO-30 cell whose south-west corner is lat, lon."""
    return (f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM")


def cell_origin(name):
    """Latitude and longitude of the south-west corner of a Copernicus cell, from its name."""
    m = re.fullmatch(r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d\d\d)_00_DEM", name)
    lat, lon = int(m.group(2)), int(m.group(4))
    return (lat if m.group(1) == "N" else -lat), (lon if m.group(3) == "E" else -lon)


def usgs_name(lat, lon):
    """File name (without .tif) of the USGS 3DEP 1/3 arc-second tile of the cell whose south-west
    corner is lat, lon. USGS names a tile by its north-west corner."""
    return f"USGS_13_{'n' if lat + 1 >= 0 else 's'}{abs(lat + 1):02d}{'w' if lon < 0 else 'e'}{abs(lon):03d}"


def read_list(path):
    """The lines of a list file such as ocean-cells.txt, without comments; empty if it is missing."""
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}


def box(w, s, e, n):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in ((w, s), (e, s), (e, n), (w, n), (w, s)):
        ring.AddPoint_2D(px, py)
    g = ogr.Geometry(ogr.wkbPolygon)
    g.AddGeometry(ring)
    return g


def read_outline(path):
    with open(path) as f:
        return ogr.CreateGeometryFromJson(json.dumps(json.load(f)["features"][0]["geometry"]))


def image_bytes(bands, driver, options):
    """Encode a list of 2D uint8 arrays with a GDAL driver (PNG or WEBP) and return the bytes."""
    mem = gdal.GetDriverByName("MEM").Create("", bands[0].shape[1], bands[0].shape[0], len(bands), gdal.GDT_Byte)
    for i, b in enumerate(bands):
        mem.GetRasterBand(i + 1).WriteArray(b)
    path = f"/vsimem/{uuid.uuid4().hex}.{driver.lower()}"
    gdal.GetDriverByName(driver).CreateCopy(path, mem, options=options)
    f = gdal.VSIFOpenL(path, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    size = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, size, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(path)
    return data


def _outline_mask(bounds, w, h):
    """The outline rasterised onto a block: True where the pixel centre is inside."""
    if "layer" not in _mask:                 # once per worker process
        g = read_outline(_cfg["outline"]).Intersection(box(-180, -MAX_LAT, 180, MAX_LAT))
        src, dst = osr.SpatialReference(), osr.SpatialReference()
        src.ImportFromEPSG(4326)
        dst.ImportFromEPSG(3857)
        for r in (src, dst):
            r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        g.Transform(osr.CoordinateTransformation(src, dst))
        ds = ogr.GetDriverByName("Memory").CreateDataSource("")
        layer = ds.CreateLayer("outline", dst, ogr.wkbMultiPolygon)
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetGeometry(g)
        layer.CreateFeature(f)
        _mask.update(ds=ds, layer=layer, srs=dst.ExportToWkt())
    gt = (bounds[0], (bounds[2] - bounds[0]) / w, 0, bounds[3], 0, -(bounds[3] - bounds[1]) / h)
    return layer_mask(_mask["layer"], _mask["srs"], gt, w, h)


def layer_mask(layer, srs_wkt, gt, w, h):
    """A polygon layer rasterised onto a grid: True where the pixel centre is inside."""
    r = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    r.SetGeoTransform(gt)
    r.SetProjection(srs_wkt)
    gdal.RasterizeLayer(r, [1], layer, burn_values=[1])
    return r.GetRasterBand(1).ReadAsArray() > 0


def _block(job):
    src, z, x0, y0, x1, y1 = job
    size = 2 * ORIGIN / 2 ** z
    bounds = (-ORIGIN + x0 * size, ORIGIN - (y1 + 1) * size, -ORIGIN + (x1 + 1) * size, ORIGIN - y0 * size)
    w, h = (x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE
    arr, valid = _cfg["warp"](src, z, bounds, w, h)
    if _cfg["mask"]:
        valid = valid & _outline_mask(bounds, w, h)
    out = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            sl = (slice((ty - y0) * TILE, (ty - y0 + 1) * TILE), slice((tx - x0) * TILE, (tx - x0 + 1) * TILE))
            v = valid[sl]
            if not v.any():
                continue
            out.append((z, tx, 2 ** z - 1 - ty, _cfg["encode"](arr[sl], v)))   # MBTiles rows count from the south
    return out


def write(out, sources, outline_path, minzoom, maxzoom, warp, encode, meta, jobs, mask=False):
    """sources: raster paths, one per part. warp(src, z, bounds_3857, width, height) -> (array, valid
    mask); encode(tile_array, tile_valid) -> bytes. meta: metadata rows besides format/zoom/bounds.
    mask: rasterise the outline into each block (needs outline_path)."""
    _cfg.update(warp=warp, encode=encode, outline=outline_path, mask=bool(mask and outline_path))
    outline = read_outline(outline_path) if outline_path else None
    job_list, boxes = [], []
    for src in sources:
        w, s, e, n = source_bbox(src)
        boxes.append((w, s, e, n))
        for z in range(minzoom, maxzoom + 1):
            xa, xb, ya, yb = tile_range(z, w, s, e, n)
            for by in range(ya, yb + 1, BLOCK):
                for bx in range(xa, xb + 1, BLOCK):
                    bx1, by1 = min(bx + BLOCK - 1, xb), min(by + BLOCK - 1, yb)
                    if outline is not None:
                        lw, ln = _tile_lonlat(z, bx, by)
                        le, ls = _tile_lonlat(z, bx1 + 1, by1 + 1)
                        if not outline.Intersects(box(lw, ls, le, ln)):
                            continue
                    job_list.append((src, z, bx, by, bx1, by1))
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript("""
    CREATE TABLE metadata (name TEXT, value TEXT);
    CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
    CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
    """)
    print(f"  {len(job_list)} blocks of up to {BLOCK} x {BLOCK} tiles, zoom {minzoom}-{maxzoom}, {jobs} processes", flush=True)
    t0, done, tiles = time.time(), 0, 0
    with mp.Pool(jobs) as pool:
        for rows in pool.imap_unordered(_block, job_list):
            db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", rows)
            done += 1
            tiles += len(rows)
            if done % 50 == 0 or done == len(job_list):
                db.commit()
                print(f"  {done}/{len(job_list)} blocks, {tiles:,} tiles, {time.time() - t0:.0f} s", flush=True)
    if outline is not None:
        w, e, s, n = outline.GetEnvelope()
    else:
        w, s, e, n = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    w, e = max(-180.0, w), min(180.0, e)
    s, n = max(-MAX_LAT, s), min(MAX_LAT, n)
    rows = dict(meta)
    rows.update({"minzoom": str(minzoom), "maxzoom": str(maxzoom),
                 "bounds": f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}",
                 "center": f"{(w + e) / 2:.5f},{(s + n) / 2:.5f},{minzoom}"})
    db.executemany("INSERT INTO metadata VALUES (?, ?)", rows.items())
    db.commit()
    db.close()
    os.replace(tmp, out)
    print(f"  wrote {tiles:,} tiles, {os.path.getsize(out) / 1048576:.1f} MB", flush=True)


def default_jobs():
    return max(1, (os.cpu_count() or 2) - 1)


def as_float(ds):
    return ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
