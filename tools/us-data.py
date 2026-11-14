#!/usr/bin/env python3
"""Build data/usdata.mbtiles from US federal data (public domain), for the print styles.
Runs inside the tools container (GDAL, Tippecanoe); scripts/build-usdata.sh starts it.

    python3 tools/us-data.py [step ...] --bbox W S E N --states NJ PA ...

Steps, each written to data/cache/usdata/<step>.mbtiles and then joined:
  obstacles  FAA Digital Obstacle File (daily, whole US): towers, stacks, tanks, wind
             turbines and other obstacles with height above ground          layer obstacle
  names      USGS GNIS Domestic Names, per state: terrain and water names,
             springs                                                        layer gnis
  marks      NOAA NGS survey marks, per state: bench marks and horizontal
             control with elevation                                         layer survey_mark
  protected  USGS PAD-US 4.1 (fee lands) with public access: open, restricted,
             closed                                                         layer protected
  flood      FEMA National Flood Hazard Layer: 100-year zone and floodway.
             Large; ask for it by name                                      layer flood
Default: obstacles names marks protected. A step whose download fails is reported and
skipped; earlier results of that step are kept and joined.
"""
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "data", "cache", "usdata")
OUT = os.path.join(ROOT, "data", "usdata.mbtiles")
UA = {"User-Agent": "fieldmap-server us-data build"}

# Short names for the print collar: FAA Digital Obstacle File, USGS Geographic Names
# Information System, NOAA National Geodetic Survey, USGS Protected Areas Database,
# FEMA National Flood Hazard Layer.
CREDITS = {
    "obstacles": "FAA DOF",
    "names": "USGS GNIS",
    "marks": "NOAA NGS",
    "protected": "USGS PAD-US",
    "flood": "FEMA NFHL",
}
DEFAULT_STEPS = ["obstacles", "names", "marks", "protected"]

DOF_URL = "https://aeronav.faa.gov/Obst_Data/DAILY_DOF_CSV.ZIP"
GNIS_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/DomesticNames/DomesticNames_{st}_Text.zip"
NGS_URL = "https://geodesy.noaa.gov/pub/DS_ARCHIVE/ShapeFiles/{st}.ZIP"
PADUS_URL = "https://edits.nationalmap.gov/arcgis/rest/services/PAD-US/PAD_US_4_1/MapServer/0/query"
NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"

# GNIS feature classes kept, by kind. Populated places, streams and lakes come from OSM.
GNIS_TERRAIN = {"Summit", "Gap", "Ridge", "Valley", "Flat", "Basin", "Bench", "Cliff", "Range", "Slope",
                "Plain", "Pillar", "Arch", "Crater", "Lava", "Isthmus", "Cape", "Woods", "Plateau"}
GNIS_WATER = {"Spring", "Bay", "Rapids", "Falls", "Bend", "Gut", "Swamp", "Channel", "Bar", "Cove", "Glacier", "Arroyo"}


def log(*a):
    print(*a, flush=True)


def fetch(url, dest):
    """Download url to dest (kept for reuse when it is less than a day old)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0 and os.path.getmtime(dest) > time.time() - 86400:
        log(f"  cached: {os.path.basename(dest)}")
        return dest
    log(f"  download: {url}")
    tmp = dest + ".part"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)
    return dest


class Writer:
    """Line-delimited GeoJSON for Tippecanoe."""
    def __init__(self, path):
        self.f, self.n = open(path, "w"), 0

    def point(self, lon, lat, props):
        self.f.write(json.dumps({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                                 "properties": {k: v for k, v in props.items() if v not in (None, "")}}) + "\n")
        self.n += 1

    def close(self):
        self.f.close()
        return self.n


def inside(bbox, lon, lat):
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def num(s):
    try:
        return float(str(s).strip())
    except ValueError:
        return None


# ------------------------------------------------------------------ steps

def obstacles(a):
    z = fetch(DOF_URL, os.path.join(WORK, "DOF.zip"))
    out = Writer(os.path.join(WORK, "obstacles.geojsonl"))
    with zipfile.ZipFile(z) as zf:
        name = next(n for n in zf.namelist() if n.upper().endswith(".CSV"))
        for r in csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="latin-1")):
            lon, lat = num(r["LONDEC"]), num(r["LATDEC"])
            if lon is None or lat is None or not inside(a.bbox, lon, lat):
                continue
            if r.get("ACTION", "").strip() == "D":   # dismantled
                continue
            agl = num(r["AGL"])
            out.point(lon, lat, {"type": r["TYPE"].strip().lower().replace(" ", "_"), "agl_ft": int(agl) if agl else None,
                                 "amsl_ft": int(num(r["AMSL"]) or 0) or None, "lit": 1 if r["LIGHTING"].strip() not in ("N", "U", "") else None,
                                 "count": int(num(r["QUANTITY"]) or 1)})
    return out.close()


def names(a):
    out = Writer(os.path.join(WORK, "names.geojsonl"))
    for st in a.states:
        z = fetch(GNIS_URL.format(st=st), os.path.join(WORK, f"gnis_{st}.zip"))
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            for r in csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig"), delimiter="|"):
                cls = r["feature_class"]
                kind = "water" if cls in GNIS_WATER else "terrain" if cls in GNIS_TERRAIN else None
                lon, lat = num(r["prim_long_dec"]), num(r["prim_lat_dec"])
                if not kind or not lon or not lat or not inside(a.bbox, lon, lat):
                    continue
                out.point(lon, lat, {"class": cls.lower(), "kind": kind, "name": r["feature_name"]})
    return out.close()


def marks(a):
    out = Writer(os.path.join(WORK, "marks.geojsonl"))
    for st in a.states:
        z = fetch(NGS_URL.format(st=st), os.path.join(WORK, f"ngs_{st}.zip"))
        d = os.path.join(WORK, f"ngs_{st}")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(d)
        shp = next(os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith(".shp"))
        js = json.loads(subprocess.check_output(["ogr2ogr", "-f", "GeoJSON", "/vsistdout/", shp,
                                                 "-select", "PID,NAME,DEC_LAT,DEC_LON,ORTHO_HT,VERT_ORDER,POS_ORDER"]))
        for f in js["features"]:
            p = f["properties"]
            lon, lat = num(p["DEC_LON"]), num(p["DEC_LAT"])
            if lon is None or lat is None or not inside(a.bbox, lon, lat):
                continue
            h = num(p.get("ORTHO_HT"))
            out.point(lon, lat, {"pid": p["PID"], "kind": "benchmark" if p.get("VERT_ORDER") and h is not None else "horizontal",
                                 "elev_ft": round(h * 3.28084) if h is not None else None})
    return out.close()


def rest_query(url, fields, where, a, dest, **extra):
    """Page through an ArcGIS REST layer inside the bbox with GDAL, into line-delimited GeoJSON."""
    q = {"where": where, "outFields": fields, "returnGeometry": "true", "f": "json", "outSR": "4326",
         "geometry": ",".join(map(str, a.bbox)), "geometryType": "esriGeometryEnvelope", "inSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "orderByFields": "OBJECTID", "resultRecordCount": "1000", **extra}
    src = "ESRIJSON:" + url + "?" + urllib.parse.urlencode(q)
    if os.path.exists(dest):
        os.remove(dest)
    subprocess.check_call(["ogr2ogr", "-f", "GeoJSONSeq", dest, src, "-oo", "FEATURE_SERVER_PAGING=YES",
                           "-makevalid", "-skipfailures", "-nlt", "PROMOTE_TO_MULTI", "-lco", "RS=NO"])
    with open(dest) as f:
        return sum(1 for _ in f)


def protected(a):
    return rest_query(PADUS_URL, "Pub_Access,Unit_Nm,Mang_Type,Des_Tp,GIS_Acres", "1=1", a, os.path.join(WORK, "protected.geojsonl"))


def flood(a):
    # The zones are drawn in great detail; simplified to about 1 m on the server, in pages of 200,
    # or the service gives up.
    return rest_query(NFHL_URL, "FLD_ZONE,ZONE_SUBTY,SFHA_TF", "SFHA_TF='T' OR ZONE_SUBTY LIKE '%FLOODWAY%'", a,
                      os.path.join(WORK, "flood.geojsonl"), resultRecordCount="200", maxAllowableOffset="0.00001",
                      geometryPrecision="6")


# Tippecanoe settings per step: layer name and zoom range.
TILES = {
    "obstacles": ("obstacle", 9, ["-r1"]),
    "names": ("gnis", 10, ["-r1"]),
    "marks": ("survey_mark", 12, ["-r1"]),
    "protected": ("protected", 6, ["--coalesce-densest-as-needed", "--detect-shared-borders", "-S", "4"]),
    "flood": ("flood", 11, ["--coalesce-densest-as-needed", "--detect-shared-borders", "-S", "4"]),
}


def tiles(step):
    layer, minzoom, extra = TILES[step]
    src = os.path.join(WORK, f"{step}.geojsonl")
    dst = os.path.join(WORK, f"{step}.mbtiles")
    subprocess.check_call(["tippecanoe", "-q", "-f", "-o", dst, "-l", layer, "-Z", str(minzoom), "-z", "14",
                           "-P", "--no-tile-size-limit", "-A", CREDITS[step], *extra, src])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    ap.add_argument("--states", nargs="+", required=True)
    ap.add_argument("steps", nargs="*")
    a = ap.parse_args()
    a.states = [s.upper() for s in a.states]
    steps = a.steps or DEFAULT_STEPS
    bad = [s for s in steps if s not in CREDITS]
    if bad:
        sys.exit(f"unknown step(s): {', '.join(bad)}; use {', '.join(CREDITS)}")
    os.makedirs(WORK, exist_ok=True)
    failed = []
    for step in steps:
        log(f"== {step}: {CREDITS[step]}")
        try:
            n = globals()[step](a)
            log(f"  {n} features")
            if n:
                tiles(step)
            elif os.path.exists(os.path.join(WORK, f"{step}.mbtiles")):
                os.remove(os.path.join(WORK, f"{step}.mbtiles"))
        except Exception as e:   # one source being down should not stop the others
            log(f"  FAILED: {e}")
            failed.append(step)
    parts = [s for s in CREDITS if os.path.exists(os.path.join(WORK, f"{s}.mbtiles"))]
    if not parts:
        sys.exit("nothing to join")
    log(f"== join: {', '.join(parts)}")
    tmp = OUT + ".tmp.mbtiles"
    subprocess.check_call(["tile-join", "-q", "-f", "-pk", "-o", tmp, "-n", "fieldmap US data",
                           "-A", "US public domain: " + ", ".join(CREDITS[s] for s in parts),
                           *[os.path.join(WORK, f"{s}.mbtiles") for s in parts]])
    os.replace(tmp, OUT)
    log(f"Done: data/usdata.mbtiles, {os.path.getsize(OUT) / 1e6:.1f} MB")
    if failed:
        log(f"These steps failed and were skipped: {', '.join(failed)}. Run them again later.")
        sys.exit(2)


if __name__ == "__main__":
    main()
