#!/usr/bin/env python3
"""The map builder: a small web page to choose regions and layers and start a build.

    python3 tools/admin-server.py serve       (scripts/admin.sh does this)

Runs on the machine itself, not in a container, because it starts the build scripts, which
start containers. Python's standard library only. It listens on a Unix socket,
data/admin/admin.sock, which the web front serves as /mapadmin/ (and on a port as well when
ADMIN_PORT is set). It serves admin/ (the page) and:

  GET  /api/status     free disk space, what is on the device, the running or last build
  POST /api/estimate   space and time a selection needs      {regions, layers, options}
  POST /api/build      start a build                          {regions, layers, options}
  POST /api/cancel     stop the running build
  GET  /api/log?from=N the build's output from byte N
  POST /api/login      {pin} -> a session cookie (only when ADMIN_PIN is set)

A build writes a region file (data/admin/region.env) and runs
scripts/build-region.sh --staged with it: the new files replace the old ones together, when
every step has finished, and the tile server is then restarted. The build runs as its own
process (this file's "run-job"), so it carries on if the page or this server is closed.

The page is open to whoever can reach it, as the tile server and the print tool are: it is
meant for a network you trust. Whoever can open it can start a build that takes days and
replaces the maps; ADMIN_PIN=<code> makes it ask for that code first, and ADMIN_HOST=127.0.0.1
keeps it to the device itself. Requests only choose from the bundled region list and fixed
options: nothing a browser sends reaches a command line as text.
"""
import glob
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
ADMIN = os.path.join(DATA, "admin")
STATIC = os.path.join(ROOT, "admin")
JOB, STATE, LOG = (os.path.join(ADMIN, n) for n in ("job.json", "state.json", "build.log"))
REGION_FILE = os.path.join(ADMIN, "region.env")
# Written by Stop. The build looks for it as soon as it can be stopped, so that a Stop pressed
# while the build is still starting is not lost.
CANCEL = os.path.join(ADMIN, "cancel")
SOCKET = os.path.join(ADMIN, "admin.sock")
# build-region.sh's "=== step ===" and "=== step finished in N min ===", after any colour codes
# the step before left on the line.
STEP_LINE = re.compile(r"(?:\x1b\[[0-9;]*m)*=== (\w+)( finished)?")
LABEL = "fieldmap-build"            # on every container a build starts (scripts/lib.sh)

# layer on the page -> step of build-region.sh, file in data/
# "detail": the small area of 1 m hillshade, chosen with the terrain option, not ticked.
LAYERS = {"basemap": ("vector", "basemap.mbtiles"), "contours": ("contours", "contours.mbtiles"),
          "hillshade": ("hillshade", "hillshade.mbtiles"), "detail": ("detail", "hillshade-detail.mbtiles"),
          "elevation": ("elevation", "elevation.mbtiles"),
          "extras": ("extras", "extras.mbtiles"), "usdata": ("usdata", "usdata.mbtiles")}
ORDER = ["basemap", "contours", "hillshade", "detail", "elevation", "extras", "usdata"]
DETAIL_KM = (10, 20, 30, 50, 100)   # sides of the 1 m area the page offers
DETAIL_MAX_KM2 = int(os.environ.get("DETAIL_MAX_KM2") or 10000)    # as build-detail.sh
LANGUAGES = ["en", "zh", "hi", "es", "ar", "fr", "bn", "pt", "ru", "id"]
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY")

with open(os.path.join(STATIC, "regions.json")) as _f:
    REGIONS = {r["id"]: r for r in json.load(_f)["regions"]}


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(value, f, indent=1)
    os.replace(path + ".tmp", path)


def is_us(region):
    return region["path"] == "north-america/us" or region["path"].startswith("north-america/us/")


def ancestors(rid):
    while REGIONS[rid].get("parent") in REGIONS:
        rid = REGIONS[rid]["parent"]
        yield rid


# ---------- what a selection needs ----------

class BadRequest(Exception):
    pass


def check_selection(body):
    """Validated (regions, layers, options) of a request; raises BadRequest in plain words."""
    ids = body.get("regions")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in REGIONS for i in ids):
        raise BadRequest("Choose at least one region on the map.")
    ids = sorted(set(ids))
    for i in ids:
        inside = [a for a in ancestors(i) if a in ids]
        if inside:
            raise BadRequest(f"{REGIONS[i]['name']} is part of {REGIONS[inside[0]]['name']}, which is selected too. "
                             "Take one of the two off the list.")
    layers = [k for k in ORDER if k in (body.get("layers") or []) and k != "detail"]
    if not layers:
        raise BadRequest("Tick at least one thing to build.")
    o = body.get("options") or {}
    options = {"world": bool(o.get("world")), "fresh": bool(o.get("fresh")), "units": "m" if o.get("units") == "m" else "ft",
               "terrain": o.get("terrain") if o.get("terrain") in ("10m", "1m") else "30m",
               "language": o.get("language") if o.get("language") in LANGUAGES else "", "detail": None}
    regions = [REGIONS[i] for i in ids]
    usa = [r for r in regions if is_us(r)]
    if "usdata" in layers and not usa:
        raise BadRequest("The US government data layers exist only for regions in the United States.")
    if options["terrain"] == "1m":
        d = o.get("detail") or {}
        try:
            lon, lat, km = float(d["lon"]), float(d["lat"]), int(d["km"])
        except (KeyError, TypeError, ValueError):
            raise BadRequest("Place the very sharp area on the map first (More options).")
        if km not in DETAIL_KM or not -180 < lon < 180 or not -80 < lat < 80:
            raise BadRequest("The very sharp area is not valid. Place it again.")
        if not usa:
            raise BadRequest("Very sharp terrain exists only for the United States. Choose a region there, "
                             "or choose another terrain detail.")
        if not any(r["bbox"][1] <= lat <= r["bbox"][3] and
                   (r["bbox"][0] <= lon <= r["bbox"][2] if r["bbox"][0] <= r["bbox"][2]
                    else lon >= r["bbox"][0] or lon <= r["bbox"][2]) for r in usa):
            raise BadRequest("The very sharp area is outside the regions you chose. Place it again.")
        if "hillshade" not in layers and not layers_status()["hillshade"]["present"]:
            raise BadRequest("The very sharp area goes on top of the hillshade. Tick Hillshade as well.")
        options["detail"] = {"lon": round(lon, 5), "lat": round(lat, 5), "km": km}
        w, s, e, n = detail_bbox(options["detail"])
        area = (e - w) * (n - s) * 111.32 ** 2 * math.cos(math.radians((s + n) / 2))
        if area > DETAIL_MAX_KM2 * 1.01:
            raise BadRequest(f"The very sharp area is {area:,.0f} km²; the most this device allows is {DETAIL_MAX_KM2:,} km².")
        layers = [k for k in ORDER if k in layers or k == "detail"]
    return regions, layers, options


OSM_REFRESH_DAYS = int(os.environ.get("OSM_REFRESH_DAYS") or 30)     # as scripts/lib.sh


def osm_ages():
    """Days since each OpenStreetMap extract on this device was downloaded: {"new-jersey": 3}."""
    folder, now, ages = os.path.join(DATA, "sources", "extracts"), time.time(), {}
    try:
        for name in os.listdir(folder):
            if name.endswith(".osm.pbf"):
                ages[name[:-len(".osm.pbf")]] = int((now - os.path.getmtime(os.path.join(folder, name))) // 86400)
    except OSError:
        pass
    return ages


def detail_bbox(detail):
    """min-lon, min-lat, max-lon, max-lat of the square 1 m area round its centre."""
    half_lat = detail["km"] / 2 / 111.32
    half_lon = half_lat / math.cos(math.radians(detail["lat"]))
    return (detail["lon"] - half_lon, detail["lat"] - half_lat, detail["lon"] + half_lon, detail["lat"] + half_lat)


def world_file(options):
    return os.path.join(DATA, f"world-z0-8{'-' + options['language'] if options['language'] else ''}.mbtiles")


def estimate(regions, layers, options):
    """Rough space (GB) and time (hours) of a build, from the sizes in admin/regions.json and
    the rates the build scripts' own disk checks and estimates use (scripts/, tools/region-outline.py).
    kept: on the drive when the build is over (downloads and the new files); temp: needed only
    while that part runs. The largest temp is added to all that is kept."""
    cpus = os.cpu_count() or 2
    slow = 8 / cpus
    osm = sum(r["osm_mb"] for r in regions) / 1024
    # Extracts on hand that the build uses again (merge-osm.sh): recent enough, and not asked anew.
    ages = osm_ages()
    cached = 0 if options["fresh"] else sum(
        r["osm_mb"] for r in regions if ages.get(os.path.basename(r["path"]), OSM_REFRESH_DAYS) < OSM_REFRESH_DAYS) / 1024
    cells, land = sum(r["cells"] for r in regions), sum(r["land_km2"] for r in regions)
    fine = options["terrain"] in ("10m", "1m")
    us_cells = sum(r["cells"] for r in regions if is_us(r)) if fine else 0
    us_land = sum(r["land_km2"] for r in regions if is_us(r)) if fine else 0
    kb = lambda rate, factor: (land - us_land + us_land * factor) * rate / 1048576     # kB per km2 -> GB
    items = []

    def add(key, label, kept, temp, hours):
        items.append({"key": key, "label": label, "kept_gb": round(kept, 2), "temp_gb": round(max(temp, 0), 2),
                      "hours": round(hours, 2)})

    if "basemap" in layers or "extras" in layers:
        shared = 0 if os.path.exists(os.path.join(DATA, "sources", "water-polygons-split-3857.zip")) else 1.5
        add("osm", "OpenStreetMap download", (osm - cached) + osm + shared, 0, (osm - cached) * 1024 / 10 / 3600)
    if "basemap" in layers:
        if options["world"] and not os.path.exists(world_file(options)):
            add("world", "Whole world, zoomed out (first time only)", 3, 160, 10 * slow)
        add("basemap", "Base map", osm * 1.1 + 0.1, osm * 8 + 3, 0.1 + osm * 0.35 * slow)
    terrain = [k for k in ("contours", "hillshade", "elevation") if k in layers]
    if terrain:
        gb = cells * 0.025 + us_cells * 0.35
        add("dem", "Elevation data download", gb, 0, gb * 1024 / 10 / 3600)
    if "contours" in layers:
        final = kb(5.5, 1.85)
        add("contours", "Contour lines", final, (cells - us_cells) * 0.2 + us_cells * 0.5 - final,
            (land - us_land + us_land * 2.5) / 1000 * 4.5 * slow / 3600)
    if "hillshade" in layers:
        add("hillshade", "Hillshade", kb(0.25, 16), (cells - us_cells) * 0.03 + us_cells * 0.2,
            (land - us_land + us_land * 16) / 1000 * 0.5 * slow / 3600)
    if "detail" in layers:
        # 10 km tiles of up to 500 MB (they are kept), often from two or three overlapping
        # surveys; the hillshade raster and the finished tiles as measured on two test areas
        # (100 km2: 1 and 4 GB of downloads, under 2 minutes).
        km = options["detail"]["km"]
        add("detail", "Very sharp terrain area", (km // 10 + 1) ** 2 * 1.0 + km * km * 0.0002,
            km * km * 0.0015, 0.05 + km * km / 2500 * slow)
    if "elevation" in layers:
        add("elevation", "Elevation for terrain tools", kb(1.4, 4), 0,
            (land - us_land + us_land * 7.2) / 1000 * 1.0 * slow / 3600)
    if "extras" in layers:
        add("extras", "Extra map details", osm * 0.15 + 0.05, osm * 3 + 2, 0.05 + osm * 0.1 * slow)
    if "usdata" in layers:
        states = sum(1 for r in regions if is_us(r)) if not any(r["id"] == "us" for r in regions) else 51
        add("usdata", "US government data layers", 0.05 + states * 0.08, 1 + states * 0.1, 0.1 + states * 0.05)
    kept = sum(i["kept_gb"] for i in items)
    need = kept + max((i["temp_gb"] for i in items), default=0)
    free = shutil.disk_usage(DATA if os.path.isdir(DATA) else ROOT).free / 1024 ** 3
    return {"items": items, "need_gb": round(need, 1), "kept_gb": round(kept, 1), "free_gb": round(free, 1),
            "ok": need <= free, "hours": round(sum(i["hours"] for i in items), 1), "cpus": cpus}


def lon_span(spans):
    """The narrowest stretch of longitude that holds every (west, east) span (west > east: the
    span crosses the 180 degree line); the result has west > east when it crosses too."""
    parts = []
    for w, e in spans:
        parts += [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]
    parts.sort()
    merged = [list(parts[0])]
    for w, e in parts[1:]:
        if w <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([w, e])
    # The widest gap between two stretches, the one round the back of the globe included.
    gaps = [(merged[i + 1][0] - merged[i][1], merged[i + 1][0], merged[i][1]) for i in range(len(merged) - 1)]
    gaps.append((merged[0][0] + 360 - merged[-1][1], merged[0][0], merged[-1][1]))
    _, west, east = max(gaps)
    if (west, east) == (-180.0, 180.0):
        return -180.0, 180.0
    return west, east


def region_file(regions, options):
    """The text of a region file (see regions/ and the README) for a selection."""
    ids = sorted(r["id"] for r in regions)
    west, east = lon_span([(r["bbox"][0], r["bbox"][2]) for r in regions])
    south, north = min(r["bbox"][1] for r in regions), max(r["bbox"][3] for r in regions)
    try:
        ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError):
        ram_gb = 8
    lines = [
        "# Written by the map builder page (tools/admin-server.py) for: " + ", ".join(r["name"] for r in regions),
        f'REGION_NAME="admin-{hashlib.sha1(" ".join(ids).encode()).hexdigest()[:10]}"',
        'GEOFABRIK_AREAS="' + " ".join(r["path"] for r in regions) + '"',
        f'BBOX="{west:g} {south:g} {east:g} {north:g}"',
        f'CONTOUR_UNITS="{options["units"]}"',
        f'WORLD="{"yes" if options["world"] else "no"}"',
        f'LANGUAGE="{options["language"]}"',
        f'DEM_SOURCE="{"copernicus" if options["terrain"] == "30m" else "usgs"}"',
        f'PLANETILER_MEMORY="{int(min(24, max(2, ram_gb * 0.5)))}g"',
    ]
    if options["detail"]:
        lines.append('DETAIL_BBOX="' + " ".join(f"{v:.5f}" for v in detail_bbox(options["detail"])) + '"')
    if any(r["id"] == "us" for r in regions):
        lines.append(f'US_STATES="{US_STATES}"')
    return "\n".join(lines) + "\n"


# ---------- the build ----------

def is_build_process(pid):
    """True when pid is this file's "run-job" process. A pid alone is not enough: after a
    reboot the number may belong to any other program."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
        return any(a.endswith(b"admin-server.py") for a in args) and b"run-job" in args
    except OSError:
        return False


def job_status():
    job = read_json(JOB, None)
    if not job:
        return None
    starting = job.get("pid") is None and time.time() - job.get("started", 0) < 30
    if job["state"] == "running" and not starting and not is_build_process(job.get("pid")):
        job.update(state="failed", message="The build stopped without saying why (was the device switched off?). "
                                           "The maps on this device were not changed.")
        write_json(JOB, job)
    done, current = log_steps(job["started"])
    step_of = {v[0]: k for k, v in LAYERS.items()}
    job["log_size"] = _steps["offset"]
    job["done"] = [step_of[s] for s in done if s in step_of]
    job["current"] = step_of.get(current) if job["state"] == "running" else None
    return job


_steps = {"job": None, "offset": 0, "rest": b"", "done": [], "current": None}


def log_steps(job_started):
    """The steps the log has finished and the one it is in: build-region.sh prints
    "=== step ===" and "=== step finished". Only what was added since the last call is read."""
    s = _steps
    if s["job"] != job_started:
        s.update(job=job_started, offset=0, rest=b"", done=[], current=None)
    try:
        with open(LOG, "rb") as f:
            f.seek(s["offset"])
            data = f.read()
    except OSError:
        return s["done"], s["current"]
    s["offset"] += len(data)
    lines = (s["rest"] + data).split(b"\n")
    s["rest"] = lines.pop()[-4096:]            # an unfinished line; long progress lines are cut
    for line in lines:
        m = STEP_LINE.match(line.decode("utf-8", "replace"))
        if m and m.group(2):
            s["done"].append(m.group(1))
            s["current"] = None
        elif m:
            s["current"] = m.group(1)
    return list(s["done"]), s["current"]


def docker(*args, **kw):
    return subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def start_build(regions, layers, options):
    os.makedirs(ADMIN, exist_ok=True)
    with open(REGION_FILE, "w") as f:
        f.write(region_file(regions, options))
    request = {"regions": [r["id"] for r in regions], "names": [r["name"] for r in regions],
               "layers": layers, "options": options}
    # The job file first (the build reads it), then the build's own process number.
    if os.path.exists(CANCEL):
        os.remove(CANCEL)
    job = {"state": "running", "pid": None, "started": int(time.time()), "request": request}
    write_json(JOB, job)
    with open(LOG, "w") as log:
        # Its own session: the build carries on when this server stops.
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run-job"], cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    job["pid"] = proc.pid
    write_json(JOB, job)


def run_job():
    """The build itself (the "run-job" process): build-region.sh, then the tile server."""
    job = read_json(JOB, {})
    request = job["request"]
    steps = [LAYERS[k][0] for k in request["layers"]]
    child = None
    swapped = []                       # set once build-region.sh has put the new files in place

    def stop(*_):
        # Once the new files are in place there is nothing left to stop: let it finish.
        if swapped:
            return
        # Cancelled: the scripts first (their EXIT trap removes the staged files), then
        # the containers they started, which do not stop by themselves.
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        ids = docker("ps", "-q", "--filter", f"label={LABEL}").stdout.split()
        if ids:
            docker("kill", *ids)
        finish("cancelled", "The build was stopped. The maps on this device were not changed. "
                            "The parts that were finished are kept: start the same build again to carry on.")

    def finish(state, message):
        job.update(state=state, message=message, finished=int(time.time()))
        write_json(JOB, job)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    job["pid"] = os.getpid()
    job["ready"] = True
    write_json(JOB, job)
    if os.path.exists(CANCEL):             # Stop was pressed before this process could hear it
        stop()
    # A build from this page merges the chosen OSM extracts into data/sources/<REGION_NAME>.osm.pbf
    # (merge-osm.sh); the merged files of earlier selections are several GB each and not used again.
    name = re.search(r'^REGION_NAME="([^"]+)"', open(REGION_FILE).read(), re.M).group(1)
    for old in glob.glob(os.path.join(DATA, "sources", "admin-*.osm.pbf")):
        if os.path.basename(old) != name + ".osm.pbf":
            os.remove(old)
            print(f"Removed {os.path.basename(old)}, the merged map data of an earlier selection.", flush=True)
    print(f"Map builder: {', '.join(request['names'])}; steps: {' '.join(steps)}", flush=True)
    env = dict(os.environ)
    env.pop("LANGUAGE", None)          # a locale setting, not the label language of a build
    if request["options"].get("fresh"):
        env["OSM_REFRESH"] = "yes"     # download the OpenStreetMap extracts again (merge-osm.sh)
    # The scripts on the machine itself at low priority too (the containers they start get a
    # low CPU weight, scripts/lib.sh): the page, the tile server and the rest come first.
    os.nice(10)
    child = subprocess.Popen([os.path.join(ROOT, "scripts", "build-region.sh"), "--staged",
                              os.path.relpath(REGION_FILE, ROOT), *steps], cwd=ROOT, env=env, start_new_session=True)
    code = child.wait()
    if code == 0:
        swapped.append(True)
    else:
        finish("failed", "The build stopped with an error. The maps on this device were not changed. "
                         "The details below say what went wrong. The parts that were finished are kept: "
                         "once the problem is solved, start the same build again to carry on from there.")
    state = read_json(STATE, {})
    for k in request["layers"]:
        state[k] = {"regions": request["regions"], "names": request["names"], "built": int(time.time()),
                    "options": request["options"]}
    write_json(STATE, state)
    print("\nRestarting the map server so that it reads the new files.", flush=True)
    if docker("ps", "-aq", "--filter", "name=^tileserver$").stdout.strip():
        r = docker("restart", "tileserver")
    else:
        pi = os.path.join(ROOT, "docker-compose.pi.yml")
        r = docker("compose", *(["-f", pi] if os.path.exists(pi) else []), "up", "-d", cwd=ROOT)
    print(r.stdout, flush=True)
    if r.returncode != 0:
        finish("done", "The new maps are in place, but the map server did not start. "
                       "On the device, run: docker compose up -d")
    finish("done", "The new maps are in place and the map server is using them.")


def cancel_build():
    job = job_status()
    if not job or job["state"] != "running":
        return
    open(CANCEL, "w").close()
    # Only a build that is listening for it: a signal sent earlier would end it without
    # tidying up, and an unknown process is never signalled.
    if job.get("ready") and is_build_process(job.get("pid")):
        os.kill(job["pid"], signal.SIGTERM)


def placeholder(path):
    """True for the empty files data-placeholders.sh writes so that the tile server starts."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = db.execute("SELECT value FROM metadata WHERE name = 'name'").fetchone()
        db.close()
        return bool(row) and row[0] == "empty placeholder"
    except sqlite3.Error:
        return False


def layers_status():
    state = read_json(STATE, {})
    out = {}
    for key, (_step, name) in LAYERS.items():
        path = os.path.join(DATA, name)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        out[key] = {"present": size > 0 and not placeholder(path), "size_gb": round(size / 1024 ** 3, 2),
                    **state.get(key, {})}
    return out


def status():
    base = DATA if os.path.isdir(DATA) else ROOT
    usage = shutil.disk_usage(base)
    return {"free_gb": round(usage.free / 1024 ** 3, 1), "total_gb": round(usage.total / 1024 ** 3, 1),
            "layers": layers_status(), "job": job_status(), "docker": shutil.which("docker") is not None,
            "languages": LANGUAGES, "web_port": web_port(), "detail_km": DETAIL_KM,
            "osm_ages": osm_ages(), "osm_refresh_days": OSM_REFRESH_DAYS}


def web_port():
    """Host port of the map viewer and print tool (.env, see .env.example)."""
    try:
        with open(os.path.join(ROOT, ".env")) as f:
            for line in f:
                if line.startswith("WEB_PORT="):
                    return int(line.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return 80


# ---------- the web server ----------

SESSIONS, TRIES = set(), []
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml"}


def pin():
    """The code the page asks for, when ADMIN_PIN sets one; None (the default) for an open page."""
    return os.environ.get("ADMIN_PIN") or None


class Handler(BaseHTTPRequestHandler):
    server_version = "fieldmap-admin"

    def log_message(self, *_):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def signed_in(self):
        if pin() is None:
            return True
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "fm_admin" in jar and jar["fm_admin"].value in SESSIONS

    def body(self):
        # Only this page's own requests: a JSON body, and no other site as the sender.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            raise BadRequest("This request did not come from the map builder page.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise BadRequest("Expected JSON.")
        n = int(self.headers.get("Content-Length", 0))
        if n > 100_000:
            raise BadRequest("Request too large.")
        value = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(value, dict):
            raise BadRequest("Expected a JSON object.")
        return value

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/status":
            if not self.signed_in():
                return self.send(401, {"error": "pin"})
            return self.send(200, status())
        if url.path == "/api/log":
            if not self.signed_in():
                return self.send(401, {"error": "pin"})
            try:
                start = max(0, int(parse_qs(url.query).get("from", ["0"])[0] or 0))
            except ValueError:
                start = 0
            try:
                with open(LOG, "rb") as f:
                    f.seek(start)
                    text = f.read(200_000)
            except OSError:
                text = b""
            return self.send(200, {"from": start, "next": start + len(text), "text": text.decode(errors="replace")})
        name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        path = os.path.join(STATIC, name)
        ext = os.path.splitext(name)[1]
        if "/" in name or ".." in name or ext not in TYPES or not os.path.isfile(path):
            return self.send(404, {"error": "not found"})
        with open(path, "rb") as f:
            self.send(200, f.read(), TYPES[ext])

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self.body()
            if url.path == "/api/login":
                now = time.time()
                TRIES[:] = [t for t in TRIES if now - t < 60]
                if len(TRIES) >= 8:
                    return self.send(429, {"error": "Too many tries. Wait a minute, then try again."})
                TRIES.append(now)
                if not secrets.compare_digest(str(body.get("pin", "")).strip().encode(), (pin() or "").encode()):
                    return self.send(403, {"error": "That PIN is not right."})
                token = secrets.token_urlsafe(24)
                SESSIONS.add(token)
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", f"fm_admin={token}; Path=/; HttpOnly; SameSite=Strict")])
            if not self.signed_in():
                return self.send(401, {"error": "pin"})
            if url.path == "/api/cancel":
                cancel_build()
                return self.send(200, {"ok": True})
            if url.path in ("/api/estimate", "/api/build"):
                regions, layers, options = check_selection(body)
                est = estimate(regions, layers, options)
                if url.path == "/api/estimate":
                    return self.send(200, est)
                with LOCK:
                    job = job_status()
                    if job and job["state"] == "running":
                        raise BadRequest("A build is already running.")
                    if not shutil.which("docker"):
                        raise BadRequest("Docker is not installed on this device. The build needs it.")
                    if not est["ok"]:
                        raise BadRequest(f"Not enough free space: this needs about {est['need_gb']:.0f} GB and "
                                         f"{est['free_gb']:.0f} GB is free. Choose fewer regions or fewer layers.")
                    start_build(regions, layers, options)
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except BadRequest as e:
            self.send(400, {"error": str(e)})
        except ValueError:
            self.send(400, {"error": "The request could not be read."})


LOCK = threading.Lock()


class SocketServer(ThreadingHTTPServer):
    """The same server on a Unix socket (a file) instead of a network port."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "fieldmap-admin", 0


def serve():
    # The web front (the nginx container, web/nginx.conf) passes /mapadmin/ on to this socket,
    # which it sees through a mounted folder: the page then needs no port of its own, and no
    # firewall rule. ADMIN_PORT adds a port, for a machine that runs no web front.
    try:
        os.makedirs(ADMIN, exist_ok=True)
        if os.path.exists(SOCKET):
            os.remove(SOCKET)
        servers = [SocketServer(SOCKET, Handler)]
    except PermissionError:
        # Docker makes a missing folder it mounts, owned by root (docker-compose.yml mounts this
        # one into the web container).
        sys.exit(f"Error: {ADMIN} belongs to another user (often root, when Docker made it).\n"
                 f"Fix it with:  sudo chown -R $(id -un) {ADMIN}   (scripts/install.sh does this too)")
    os.chmod(SOCKET, 0o666)             # nginx runs as another user
    print("Map builder: http://<this device's address>/mapadmin/", flush=True)
    port = os.environ.get("ADMIN_PORT") or os.environ.get("PORT")
    if port:
        servers.append(ThreadingHTTPServer((os.environ.get("ADMIN_HOST", "0.0.0.0"), int(port)), Handler))
        print(f"        and: http://<this device's address>:{port}/", flush=True)
    print("It asks for the PIN in ADMIN_PIN." if pin() else
          "Open to everyone who can reach this device (ADMIN_PIN=<code> makes it ask for a code).", flush=True)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(SOCKET):
            os.remove(SOCKET)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    elif sys.argv[1:] == ["run-job"]:
        run_job()
    else:
        sys.exit(__doc__)
