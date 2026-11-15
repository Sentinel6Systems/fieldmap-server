#!/usr/bin/env python3
"""Write admin/regions.json, the regions the map builder page (admin/) offers, and
admin/land.json and admin/places.json, the coastlines and towns it draws behind them.

    docker run --rm -v "$PWD":/work -w /work tileserver-tools python3 tools/make-admin-regions.py [land]

(the tool image of the terrain build scripts: it has GDAL). Run it again now and then to pick
up new Geofabrik extracts and current download sizes; the page works without a network once
the file is there.

The regions are Geofabrik's extracts (https://download.geofabrik.de/index-v1.json: outlines
derived from OpenStreetMap, ODbL). For each one the file holds what the page needs to draw it
and to estimate a build before anything is downloaded:

  id, name           as in Geofabrik's index
  path               what a region file's GEOFABRIK_AREAS takes, e.g. europe/monaco
  parent             id of the region it is part of: the one whose path is one step shorter
                     (Geofabrik lists the US states under North America; here they are parts
                     of the United States)
  osm_mb             size of the OpenStreetMap extract today
  cells, land_km2    1 degree elevation cells with land that touch the outline, and the outline's
                     area inside them (from Copernicus GLO-30's list of tiles: no tile, no land)
  bbox               min-lon, min-lat, max-lon, max-lat; min-lon > max-lon when the region is
                     narrower across the 180 degree line (the form a region file's BBOX takes)
  geom               the outline, simplified: polygons of rings of [lon, lat, lon, lat, ...]

land.json is Natural Earth's 1:50m land (public domain), simplified, in the same form as geom.
places.json is Natural Earth's 1:10m populated places: [name, lon, lat, rank], the larger the
town the lower the rank. With the argument "land" only those two files are written.
"""
import concurrent.futures
import datetime
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from osgeo import ogr

from tilewriter import box

ogr.UseExceptions()
INDEX = "https://download.geofabrik.de/index-v1.json"
TILES = "https://copernicus-dem-30m.s3.amazonaws.com/tileList.txt"
LAND = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_land.geojson"
PLACES = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places_simple.geojson"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "regions.json")
UA = {"User-Agent": "fieldmap-server region list"}
# Extracts that only group other extracts of the same level (the five US census regions,
# Germany + Austria + Switzerland ...): on the page they would lie on top of their members.
GROUPS = {"us-midwest", "us-northeast", "us-pacific", "us-south", "us-west", "dach", "alps", "britain-and-ireland"}


def fetch(url):
    for attempt in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
        except OSError:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def size_mb(url):
    """Size of a download from its headers alone. Geofabrik redirects ...-latest to the dated
    file; urllib would follow that with a GET and start the download."""
    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(3):
        try:
            r = opener.open(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=60)
            return int(r.headers.get("Content-Length", 0)) / 1048576
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 307, 308) or not e.headers.get("Location"):
                return 0
            url = urllib.parse.urljoin(url, e.headers["Location"])
        except OSError:
            time.sleep(5)
    return 0


def land_cells():
    """South-west corners (lat, lon) of the 1 degree cells Copernicus GLO-30 has a tile for."""
    cells = set()
    for m in re.finditer(r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d\d\d)_00_DEM", fetch(TILES).read().decode()):
        cells.add((int(m.group(2)) * (1 if m.group(1) == "N" else -1), int(m.group(4)) * (1 if m.group(3) == "E" else -1)))
    return cells


def lon_span(lons):
    """The narrowest stretch of longitude that holds all of them: (west, east), west > east
    when it crosses the 180 degree line."""
    lons = sorted(set(round(v, 4) for v in lons))
    if lons[-1] - lons[0] <= 180:
        return lons[0], lons[-1]
    gap, at = max((b - a, i) for i, (a, b) in enumerate(zip(lons, lons[1:])))
    if gap <= 360 - (lons[-1] - lons[0]):
        return lons[0], lons[-1]
    return lons[at + 1], lons[at]


def clean_name(name):
    """Geofabrik's name, without the markup some have; the US states are listed as "us/new-york"."""
    name = re.sub(r"\s*<br\s*/?>\s*", " ", name).strip()
    if "/" in name:
        words = name.rsplit("/", 1)[1].replace("-", " ").split()
        name = " ".join(w if w in ("of", "and") else w.capitalize() for w in words)
    return name


def rings(g):
    """Polygons of an OGR (multi)polygon as lists of rings of flat, rounded coordinates."""
    polys = [g] if g.GetGeometryName() == "POLYGON" else [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
    out = []
    for p in polys:
        rs = []
        for i in range(p.GetGeometryCount()):
            pts = p.GetGeometryRef(i).GetPoints()
            flat = [round(v, 2) for pt in pts for v in pt[:2]]
            if len(flat) >= 8:
                rs.append(flat)
        if rs:
            out.append(rs)
    return out


def write_land():
    polys = []
    for f in json.load(fetch(LAND))["features"]:
        g = ogr.CreateGeometryFromJson(json.dumps(f["geometry"])).SimplifyPreserveTopology(0.03)
        polys += rings(g)
    path = os.path.join(os.path.dirname(OUT), "land.json")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"source": "Natural Earth 1:50m land (public domain)", "geom": polys}, f, separators=(",", ":"))
    print(f"wrote {path}: {len(polys)} polygons, {os.path.getsize(path) / 1024:.0f} kB", flush=True)
    places = []
    for f in json.load(fetch(PLACES))["features"]:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"][:2]
        places.append([p["name"], round(lon, 3), round(lat, 3), p.get("scalerank", 10)])
    places.sort(key=lambda v: (v[3], v[0]))
    path = os.path.join(os.path.dirname(OUT), "places.json")
    with open(path, "w") as f:
        json.dump({"source": "Natural Earth 1:10m populated places (public domain)", "places": places}, f,
                  separators=(",", ":"), ensure_ascii=False)
    print(f"wrote {path}: {len(places)} places, {os.path.getsize(path) / 1024:.0f} kB", flush=True)


def main():
    write_land()
    if sys.argv[1:] == ["land"]:
        return
    land = land_cells()
    print(f"{len(land)} elevation cells with land", flush=True)
    features = json.load(fetch(INDEX))["features"]
    regions = []
    for f in features:
        p = f["properties"]
        pbf = p.get("urls", {}).get("pbf")
        if not pbf or not f.get("geometry") or p["id"] in GROUPS:
            continue
        g = ogr.CreateGeometryFromJson(json.dumps(f["geometry"]))
        if not g.IsValid():
            g = g.MakeValid()
        w, e, s, n_ = g.GetEnvelope()
        cells, km2 = 0, 0.0
        for lat in range(math.floor(s), math.ceil(n_)):
            for lon in range(math.floor(w), math.ceil(e)):
                if (lat, lon) not in land:
                    continue
                piece = g.Intersection(box(lon, lat, lon + 1, lat + 1))
                if piece.IsEmpty() or piece.GetArea() == 0:
                    continue
                cells += 1
                km2 += piece.GetArea() * 111.32 ** 2 * math.cos(math.radians(lat + 0.5))
        lons = [pt[0] for poly in rings(g) for r in poly for pt in zip(r[0::2], r[1::2])]
        west, east = lon_span(lons)
        simple = g.SimplifyPreserveTopology(max(0.02, math.hypot(e - w, n_ - s) * 0.003))
        regions.append({
            "id": p["id"], "name": clean_name(p["name"]), "parent": p.get("parent"),
            "path": pbf.split("download.geofabrik.de/", 1)[1].removesuffix("-latest.osm.pbf"),
            "osm_mb": pbf, "cells": cells, "land_km2": round(km2),
            "bbox": [round(west, 3), round(s, 3), round(east, 3), round(n_, 3)],
            "geom": rings(simple)})
    print(f"{len(regions)} regions; asking for their download sizes", flush=True)
    # A few at a time: each answer takes a second or more.
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        for r, mb in zip(regions, pool.map(size_mb, [r["osm_mb"] for r in regions])):
            r["osm_mb"] = round(mb, 1)
    by_path = {r["path"]: r["id"] for r in regions}
    for r in regions:
        r["parent"] = by_path.get(r["path"].rpartition("/")[0], r["parent"])
    regions.sort(key=lambda r: r["path"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump({"generated": datetime.date.today().isoformat(),
                   "source": "Geofabrik extract index (outlines from OpenStreetMap, ODbL); Copernicus GLO-30 tile list",
                   "regions": regions}, f, separators=(",", ":"))
    print(f"wrote {OUT}: {len(regions)} regions, {os.path.getsize(OUT) / 1024:.0f} kB")
    missing = [r["id"] for r in regions if not r["osm_mb"]]
    if missing:
        print(f"Note: no download size for {missing}")


if __name__ == "__main__":
    main()
