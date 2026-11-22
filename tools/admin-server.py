#!/usr/bin/env python3
"""The map builder: a small web page to choose regions and layers and start a build.

    python3 tools/admin-server.py serve       (scripts/admin.sh does this)

Runs on the machine itself, not in a container, because it starts the build scripts, which
start containers. Python's standard library only. It listens on a Unix socket,
data/admin/admin.sock, which the web front serves as /mapadmin/ (and on a port as well when
ADMIN_PORT is set). It serves admin/ (the page), ui/ (the look every page shares) and:

  GET  /api/status     free disk space, what is on the device, the running or last build
  POST /api/estimate   space and time a selection needs      {regions, layers, options}
  POST /api/build      start a build                          {regions, layers, options, pause_ok}
  POST /api/cancel     stop the running build
  GET  /api/log?from=N the build's output from byte N
  POST /api/login      {pin} -> a session cookie (only when ADMIN_PIN is set)

A build writes a region file (data/admin/region.env) and runs
scripts/build-region.sh --staged with it: the new files replace the old ones together, when
every step has finished, and the tile server is then restarted. The build runs as its own
process (this file's "run-job"), so it carries on if the page or this server is closed.

Pi mode (a machine with less than 8 GB of RAM, or PI_MODE=yes in .env; see scripts/lib.sh):
the build stays within BUILD_MEMORY (2560m), uses the 30 m elevation data only and leaves
BUILD_RESERVE_GB (5) of the drive free; the estimate refuses a selection that cannot be built
within that RAM and counts with a slower pace. On any machine, FIELDMAP_PAUSE_UNITS in .env
names services the build stops to free RAM and starts again when it ends (tools/pause-services.py);
the page warns about it, and /api/build then needs pause_ok.

The page is open to whoever can reach it, as the tile server and the print tool are: it is
meant for a network you trust. Whoever can open it can start a build that takes days and
replaces the maps; ADMIN_PIN=<code> makes it ask for that code first, and ADMIN_HOST=127.0.0.1
keeps it to the device itself. Requests only choose from the bundled region list and fixed
options: nothing a browser sends reaches a command line as text.
"""
import fcntl
import glob
import hashlib
import importlib.util
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


def setting(name, default=""):
    """A setting from the environment, else from .env (as scripts/lib.sh reads it), else default."""
    value = os.environ.get(name, "")
    if not value:
        try:
            with open(os.path.join(ROOT, ".env")) as f:
                lines = [l.rstrip("\n").split("=", 1)[1] for l in f if l.startswith(name + "=")]
            value = lines[-1].strip() if lines else ""
        except OSError:
            value = ""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
    return value or default


# The data folder: data/ here, or FIELDMAP_DATA (scripts/install-nginx.sh keeps it in .env).
DATA = setting("FIELDMAP_DATA", os.path.join(ROOT, "data"))
ADMIN = os.path.join(DATA, "admin")
STATIC = os.path.join(ROOT, "admin")
SHARED = os.path.join(ROOT, "ui")
JOB, STATE, LOG = (os.path.join(ADMIN, n) for n in ("job.json", "state.json", "build.log"))
REGION_FILE = os.path.join(ADMIN, "region.env")
# Written by Stop. The build looks for it as soon as it can be stopped, so that a Stop pressed
# while the build is still starting is not lost.
CANCEL = os.path.join(ADMIN, "cancel")
# Held by every build script (scripts/lib.sh), by a build from this page and by Free Up Storage.
BUILD_LOCK = os.path.join(ADMIN, "build.lock")
SOCKET = os.path.join(ADMIN, "admin.sock")
# build-region.sh's "=== step ===" and "=== step finished in N min ===", after any colour codes
# the step before left on the line.
STEP_LINE = re.compile(r"(?:\x1b\[[0-9;]*m)*=== (\w+)( finished)?")
LABEL = "fieldmap-build"            # on every container a build starts (scripts/lib.sh)

# layer on the page -> step of build-region.sh, file in data/
# "detail": the small area of 1 m hillshade, chosen with the terrain option, not ticked.
# "routing": the Fieldmap app's routing data, a folder of files (scripts/build-routing.sh).
LAYERS = {"basemap": ("vector", "basemap.mbtiles"), "contours": ("contours", "contours.mbtiles"),
          "hillshade": ("hillshade", "hillshade.mbtiles"), "detail": ("detail", "hillshade-detail.mbtiles"),
          "elevation": ("elevation", "elevation.mbtiles"),
          "extras": ("extras", "extras.mbtiles"), "usdata": ("usdata", "usdata.mbtiles"), "routing": ("routing", "routing")}
ORDER = ["basemap", "contours", "hillshade", "detail", "elevation", "extras", "usdata", "routing"]
DETAIL_KM = (10, 20, 30, 50, 100)   # sides of the 1 m area the page offers
DETAIL_MAX_KM2 = int(os.environ.get("DETAIL_MAX_KM2") or 10000)    # as build-detail.sh
LANGUAGES = ["en", "zh", "hi", "es", "ar", "fr", "bn", "pt", "ru", "id"]
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY")

with open(os.path.join(STATIC, "regions.json")) as _f:
    REGIONS = {r["id"]: r for r in json.load(_f)["regions"]}

# The services a build stops (FIELDMAP_PAUSE_UNITS): the list is read with the same code as the
# root helper's, which the installers put at PAUSE_HELPER; PAUSE_STATE is its list of what it stopped.
_spec = importlib.util.spec_from_file_location("pause_services", os.path.join(ROOT, "tools", "pause-services.py"))
pause_services = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pause_services)
PAUSE_HELPER = os.environ.get("FIELDMAP_PAUSE_HELPER", "/usr/local/lib/fieldmap-server/pause-services")
PAUSE_STATE = os.environ.get("FIELDMAP_PAUSE_STATE", pause_services.STATE)


# ---------- this machine: Pi mode, RAM, the drive ----------

def ram_gb():
    """The machine's RAM in GB (GiB, as /proc/meminfo counts it)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError):
        return 8.0


def pi_mode():
    """True on a machine with less than 8 GB of RAM (8 GB machines report a little under 8 GiB,
    so the line is at 7), or with PI_MODE=yes in .env; PI_MODE=no turns it off (scripts/lib.sh)."""
    value = setting("PI_MODE", "auto")
    if value in ("yes", "no"):
        return value == "yes"
    return ram_gb() < 7


def size_gb(text):
    """3g, 3072m -> GB; None for "" or a size that is not one."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([gGmM])\s*", text or "")
    if not m:
        return None
    return float(m[1]) / (1024 if m[2] in "mM" else 1)


def build_memory_gb():
    """The RAM a build may use (BUILD_MEMORY; 2.5 GB in Pi mode, which leaves a 4 GB Pi room for the rest), or None for no cap."""
    return size_gb(setting("BUILD_MEMORY", "2560m" if pi_mode() else ""))


def reserve_gb():
    """The free space a build leaves on the drive (BUILD_RESERVE_GB; 5 in Pi mode)."""
    try:
        return max(0.0, float(setting("BUILD_RESERVE_GB", "5" if pi_mode() else "0")))
    except ValueError:
        return 5.0


def planetiler_heap_gb(cap):
    """The Java heap Planetiler gets under a memory cap: two thirds of it (scripts/lib.sh)."""
    return cap * 2 / 3


def planetiler_max_osm_gb(cap):
    """The largest OpenStreetMap data (GB of .osm.pbf) Planetiler builds within a memory cap.
    With its node locations and work files on disk (memory-mapped), its heap still holds the
    relations and the outlines of areas: about half the size of the input, as its own
    documentation says, plus half a GB."""
    return max(0.0, (planetiler_heap_gb(cap) - 0.5) / 0.5)


def on_sd_card(path):
    """True when path is on an SD card (or eMMC: the kernel's mmcblk devices, major number 179)."""
    try:
        if os.major(os.stat(path).st_dev) == 179:
            return True
        # A file system whose own device number is not the card's (btrfs, a bind mount): the
        # source of the mount that holds the path.
        real, best, source = os.path.realpath(path), "", ""
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                mount = left.split()[4].replace("\\040", " ")
                if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
                    best, source = mount, (right.split() + ["", ""])[1]
        return os.path.basename(source).startswith("mmcblk")
    except (OSError, IndexError):
        return False


def pause_list():
    """What FIELDMAP_PAUSE_UNITS names, what of it is refused, and what a build stopped and has
    not started again yet (the root helper's state file)."""
    services, refused = pause_services.configured(os.path.join(ROOT, ".env"))
    stopped = pause_services.read_state(PAUSE_STATE)["stopped"]
    return {"services": [s["label"] for s in services], "refused": refused,
            "stopped": [s.get("label") or s["name"] for s in stopped], "ready": os.path.isfile(PAUSE_HELPER)}


_restoring = threading.Lock()


def restore_services(say=print):
    """Starts again what a build stopped, when it stopped anything. True when nothing is left
    stopped."""
    if not os.path.exists(PAUSE_STATE):
        return True
    with _restoring:
        r = subprocess.run(["sudo", "-n", PAUSE_HELPER, "restore"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    for line in r.stdout.splitlines():
        say(line)
    return r.returncode == 0 and not os.path.exists(PAUSE_STATE)


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
        raise BadRequest("Select at least one region on the map.")
    ids = sorted(set(ids))
    for i in ids:
        inside = [a for a in ancestors(i) if a in ids]
        if inside:
            raise BadRequest(f"{REGIONS[i]['name']} is part of {REGIONS[inside[0]]['name']}, and both are selected. "
                             "Remove one of them.")
    layers = [k for k in ORDER if k in (body.get("layers") or []) and k != "detail"]
    if not layers:
        raise BadRequest("Select at least one layer.")
    o = body.get("options") or {}
    if pi_mode() and o.get("terrain") in ("10m", "1m"):
        raise BadRequest("This server builds the standard terrain (30 m) only: it runs in Pi mode, with less than 8 GB "
                         "of RAM. Set Terrain Resolution to Standard.")
    options = {"world": bool(o.get("world")), "fresh": bool(o.get("fresh")), "units": "m" if o.get("units") == "m" else "ft",
               "terrain": o.get("terrain") if o.get("terrain") in ("10m", "1m") else "30m",
               "language": o.get("language") if o.get("language") in LANGUAGES else "", "detail": None}
    regions = [REGIONS[i] for i in ids]
    if pi_mode() and options["world"] and "basemap" in layers and not os.path.exists(world_file(options)):
        raise BadRequest("The World Overview cannot be built on this server: it is made from the map data of the whole "
                         "world, which needs a large machine. Clear Include World Overview, or build the overview on "
                         "another machine and copy it into the data folder.")
    usa = [r for r in regions if is_us(r)]
    if "usdata" in layers and not usa:
        raise BadRequest("US Government Data is available only for regions in the United States.")
    if options["terrain"] == "1m":
        d = o.get("detail") or {}
        try:
            lon, lat, km = float(d["lon"]), float(d["lat"]), int(d["km"])
        except (KeyError, TypeError, ValueError):
            raise BadRequest("Select the very high resolution area on the map first (under Advanced Options).")
        if km not in DETAIL_KM or not -180 < lon < 180 or not -80 < lat < 80:
            raise BadRequest("The very high resolution area is not valid. Select it on the map again.")
        if not usa:
            raise BadRequest("Very high resolution terrain is available only in the United States. Select a US region, "
                             "or change the Terrain Resolution.")
        if not any(r["bbox"][1] <= lat <= r["bbox"][3] and
                   (r["bbox"][0] <= lon <= r["bbox"][2] if r["bbox"][0] <= r["bbox"][2]
                    else lon >= r["bbox"][0] or lon <= r["bbox"][2]) for r in usa):
            raise BadRequest("The very high resolution area is outside the selected regions. Select it on the map again.")
        if "hillshade" not in layers and not layers_status()["hillshade"]["present"]:
            raise BadRequest("The very high resolution area requires the Hillshade layer. Select Hillshade as well.")
        options["detail"] = {"lon": round(lon, 5), "lat": round(lat, 5), "km": km}
        w, s, e, n = detail_bbox(options["detail"])
        area = (e - w) * (n - s) * 111.32 ** 2 * math.cos(math.radians((s + n) / 2))
        if area > DETAIL_MAX_KM2 * 1.01:
            raise BadRequest(f"The very high resolution area is {area:,.0f} km². The maximum is {DETAIL_MAX_KM2:,} km².")
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


def dem_cell(name):
    """South-west corner (lat, lon) of the 1 degree cell of a downloaded elevation file, or None."""
    m = re.fullmatch(r"Copernicus_DSM_COG_10_([NS])(\d\d)_00_([EW])(\d\d\d)_00_DEM\.tif", name)
    if m:
        return int(m[2]) * (1 if m[1] == "N" else -1), int(m[4]) * (1 if m[3] == "E" else -1)
    m = re.fullmatch(r"USGS_13_([ns])(\d\d)([ew])(\d\d\d)\.tif", name)     # named by its north-west corner
    if m:
        return int(m[2]) * (1 if m[1] == "n" else -1) - 1, int(m[4]) * (1 if m[3] == "e" else -1)
    return None


def touches(region, lat, lon):
    """True when the 1 degree cell with its south-west corner at (lat, lon) overlaps the region's
    outline (the simplified one of admin/regions.json)."""
    w, s, e, n = lon, lat, lon + 1, lat + 1
    for poly in region["geom"]:
        inside = False
        for ring in poly:
            pts = list(zip(ring[0::2], ring[1::2]))
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                # An edge that enters the cell (Liang-Barsky clipping)...
                t0, t1 = 0.0, 1.0
                for p, q in ((x1 - x2, x1 - w), (x2 - x1, e - x1), (y1 - y2, y1 - s), (y2 - y1, n - y1)):
                    if p == 0:
                        if q < 0:
                            t0 = 2
                    elif p < 0:
                        t0 = max(t0, q / p)
                    else:
                        t1 = min(t1, q / p)
                if t0 <= t1:
                    return True
                # ...or the cell's middle inside the outline (even-odd rule over its rings).
                if (y1 > lat + 0.5) != (y2 > lat + 0.5) and lon + 0.5 < x1 + (lat + 0.5 - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
        if inside:
            return True
    return False


def folder_gb(path):
    total = 0
    for folder, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(folder, f))
            except OSError:
                pass
    return total / 1024 ** 3


# What builds keep in data/ besides the maps: the downloads (OpenStreetMap extracts and shared
# sources, elevation cells, outlines), the working files of the terrain steps, Planetiler's
# temporary files and the finished steps of a build that did not finish. Free Up Storage on the
# page deletes them; the maps do not change, and the next build downloads its data again.
CLEANUP = ("sources", "cache", "tmp", "incoming")
# The planet filtered for the world layer (build-world.sh): deleted too, but making it again
# means downloading the whole planet. The page says so.
PLANET = os.path.join("sources", "planet-z0-8.osm.pbf")
# Free Up Storage runs on its own thread (it can take minutes on an SD card): its state.
CLEANING = {"running": False, "failed": []}


def build_lock():
    """The build lock, taken: an open file to close when done. None while a build (from this page
    or the command line) or Free Up Storage holds it."""
    os.makedirs(ADMIN, exist_ok=True)
    try:
        f = os.fdopen(os.open(BUILD_LOCK, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666), "a")
    except PermissionError:
        f = open(BUILD_LOCK)                 # made by another user (sudo): reading is enough to lock
    try:
        os.fchmod(f.fileno(), 0o666)         # the scripts may run as another user than this server
    except OSError:
        pass
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def build_locked():
    f = build_lock()
    if f is None:
        return True
    f.close()
    return False


def staged():
    """True when data/incoming holds finished steps of a build that did not finish."""
    try:
        with os.scandir(os.path.join(DATA, "incoming")) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def cleanup_status():
    if CLEANING["running"]:
        return {"running": True}
    parts = {k: folder_gb(os.path.join(DATA, k)) for k in CLEANUP}
    planet = os.path.join(DATA, PLANET)
    return {"running": False, "gb": round(sum(parts.values()), 3), "parts": {k: round(v, 3) for k, v in parts.items()},
            "planet_gb": round(os.path.getsize(planet) / 1024 ** 3, 3) if os.path.isfile(planet) else 0,
            "failed": CLEANING["failed"]}


def clean_up():
    """Deletes what is in the CLEANUP folders (the folders stay: a folder can be a link to another
    drive). Returns the names it could not delete."""
    failed = []
    for k in CLEANUP:
        folder = os.path.join(DATA, k)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError:
                failed.append(f"{k}/{name}")
    return failed


def start_cleanup():
    """Free Up Storage, on its own thread with the build lock held. Call with LOCK held."""
    job = job_status()
    if (job and job["state"] == "running") or CLEANING["running"]:
        raise BadRequest("A build is in progress. Delete the downloaded data when it is complete.")
    lock = build_lock()
    if lock is None:
        raise BadRequest("A build started on the server's command line is in progress. "
                         "Delete the downloaded data when it is complete.")
    CLEANING.update(running=True, failed=[])

    def run():
        failed = []
        try:
            failed = clean_up()
        finally:
            lock.close()
            CLEANING.update(running=False, failed=failed)

    threading.Thread(target=run, daemon=True).start()


def on_hand(regions, layers, options):
    """Space (GB) already taken on this device that a build gets back or uses again, by the part
    of the build from which on it counts ("" from the start):
    - the finished steps of a staged build that did not finish (data/incoming): the same build
      carries on with them, any other deletes them first (build-region.sh);
    - the merged map data of a selection (run_job deletes those of other selections);
    - the elevation files of the chosen regions downloaded by earlier builds (download_dem in
      scripts/lib.sh keeps them and does not fetch them again);
    - the working files a terrain step left when it stopped: the step deletes them, or carries
      on with them, before it needs room of its own."""
    out = {"": folder_gb(os.path.join(DATA, "incoming")) +
           sum(os.path.getsize(p) for p in glob.glob(os.path.join(DATA, "sources", "admin-*.osm.pbf"))) / 1024 ** 3}
    if any(k in layers for k in ("contours", "hillshade", "elevation")):
        dem = 0
        for sub in ("dem", "dem-usgs") if options["terrain"] != "30m" else ("dem",):
            folder = os.path.join(DATA, "cache", sub)
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                cell = dem_cell(name)
                if cell and any(touches(r, *cell) for r in regions):
                    dem += os.path.getsize(os.path.join(folder, name))
        out["dem"] = dem / 1024 ** 3
    for k in ("contours", "hillshade", "elevation"):
        if k in layers:
            out[k] = folder_gb(os.path.join(DATA, "cache", k))
    if "contours" in layers:
        out["contours"] += folder_gb(os.path.join(DATA, "cache", "contour-cells"))
    return out


def estimate(regions, layers, options):
    """Rough space (GB) and time (hours) of a build, from the sizes in admin/regions.json and
    the rates the build scripts' own disk checks and estimates use (scripts/, tools/region-outline.py).
    kept: on the drive when the build is over (downloads and the new files); temp: needed only
    while that part runs. The parts run one after another, in the order of the list: the most
    a build needs is the largest of (all kept so far + the temp of the part running), less what
    on_hand() says it gets back by then."""
    cpus = os.cpu_count() or 2
    pi = pi_mode()
    # A Pi's cores take about three times as long as a PC's for the same work, and its build
    # keeps its working data on disk (Pi mode). The rates below are an 8-CPU PC's with an NVMe
    # drive, measured on a 2.8 GB, 900,000 km² build (the terrain ones as tools/region-outline.py).
    slow = 8 / cpus * (3 if pi else 1)
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
        add("osm", "OpenStreetMap Download", (osm - cached) + osm + shared, 0, (osm - cached) * 1024 / 10 / 3600)
    if "basemap" in layers:
        if options["world"] and not os.path.exists(world_file(options)):
            add("world", "World Overview (First Build Only)", 3, 160, 10 * slow)
        add("basemap", "Base Map", osm * 1.1 + 0.1, osm * 8 + 3, 0.1 + osm * 0.06 * slow)
    terrain = [k for k in ("contours", "hillshade", "elevation") if k in layers]
    if terrain:
        gb = cells * 0.025 + us_cells * 0.35
        add("dem", "Elevation Download", gb, 0, gb * 1024 / 10 / 3600)
    if "contours" in layers:
        final = kb(5.5, 1.85)
        add("contours", "Contour Lines", final, (cells - us_cells) * 0.2 + us_cells * 0.5 - final,
            (land - us_land + us_land * 2.5) / 1000 * 1.5 * slow / 3600)
    if "hillshade" in layers:
        add("hillshade", "Hillshade", kb(0.25, 16), (cells - us_cells) * 0.03 + us_cells * 0.2,
            (land - us_land + us_land * 16) / 1000 * 0.2 * slow / 3600)
    if "detail" in layers:
        # 10 km tiles of up to 500 MB (they are kept), often from two or three overlapping
        # surveys; the hillshade raster and the finished tiles as measured on two test areas
        # (100 km2: 1 and 4 GB of downloads, under 2 minutes).
        km = options["detail"]["km"]
        add("detail", "Very High Resolution Area", (km // 10 + 1) ** 2 * 1.0 + km * km * 0.0002,
            km * km * 0.0015, 0.05 + km * km / 2500 * slow)
    if "elevation" in layers:
        add("elevation", "Elevation Data", kb(1.4, 4), 0,
            (land - us_land + us_land * 7.2) / 1000 * 0.55 * slow / 3600)
    if "extras" in layers:
        add("extras", "Printed Map Details", osm * 0.15 + 0.05, osm * 3 + 2, 0.05 + osm * 0.01 * slow)
    if "usdata" in layers:
        states = sum(1 for r in regions if is_us(r)) if not any(r["id"] == "us" for r in regions) else 51
        add("usdata", "US Government Data", 0.05 + states * 0.08, 1 + states * 0.1, 0.1 + states * 0.01)
    if "routing" in layers:
        # A rough estimate: BRouter's files come to about a quarter to a half of the OpenStreetMap
        # extract (the half here), more for a small region, as its 5 degree squares come whole.
        gb = osm * 0.5 + 0.05
        add("routing", "Routing Data", gb, 0, gb * 1024 / 10 / 3600)
    have = on_hand(regions, layers, options)
    have["dem"] = min(have.get("dem", 0), next((i["kept_gb"] for i in items if i["key"] == "dem"), 0))
    need, kept, back = 0, 0, have[""]
    for i in items:
        kept += i["kept_gb"]
        back += have.get(i["key"], 0)
        need = max(need, kept + i["temp_gb"] - back)
    # Afterwards: the new files, less what is already here and the files of the layers they replace.
    replaced = sum(s["size_gb"] for k, s in layers_status().items() if k in layers and s["present"])
    free = shutil.disk_usage(DATA if os.path.isdir(DATA) else ROOT).free / 1024 ** 3
    reserve = reserve_gb()
    memory = memory_problem(regions, layers)
    out = {"items": items, "need_gb": round(max(need, 0.1), 1), "kept_gb": round(kept, 1),
           "more_after_gb": round(kept - back - replaced, 1), "on_hand_gb": round(back, 1),
           "free_gb": round(free, 1), "reserve_gb": round(reserve, 1), "space_ok": need + reserve <= free,
           "memory_gb": build_memory_gb(), "memory_problem": memory, "pi": pi,
           "hours": round(sum(i["hours"] for i in items), 1), "cpus": cpus}
    out["ok"] = out["space_ok"] and not memory
    # The sharper terrain takes roughly ten times the space of the standard one: what that would need.
    if us_cells and any(k in layers for k in ("contours", "hillshade", "elevation")):
        out["standard_need_gb"] = estimate(regions, [k for k in layers if k != "detail"],
                                           {**options, "terrain": "30m", "detail": None})["need_gb"]
    return out


def memory_problem(regions, layers):
    """Why a selection cannot be built within the build's memory cap, in plain words; "" when it can
    (or when there is no cap). Only the steps that run Planetiler (the base map and the printed map
    details) need more RAM for a larger region: the terrain steps work one elevation cell at a
    time, and the routing data is downloaded."""
    cap = build_memory_gb()
    if not cap:
        return ""
    ram = ram_gb()
    if ram < cap + 0.5:
        return (f"This server has {ram:.1f} GB of RAM. A build may use up to {cap:g} GB (BUILD_MEMORY in .env), "
                f"and the server needs about 0.5 GB for itself. Set BUILD_MEMORY lower in .env.")
    planetiler = [k for k in ("basemap", "extras") if k in layers]
    osm = sum(r["osm_mb"] for r in regions) / 1024
    most = planetiler_max_osm_gb(cap)
    if planetiler and osm > most:
        what = "base map" if "basemap" in planetiler else "printed map details"
        return (f"The selected regions are too large for this server to build the {what}: their map data is about "
                f"{osm:.1f} GB, and within {cap:g} GB of RAM it can build up to about {most:.1f} GB. Select fewer or "
                f"smaller regions, or build the {what} on a larger machine.")
    return ""


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
    ram, cap = ram_gb(), build_memory_gb()
    lines = [
        "# Written by the map builder page (tools/admin-server.py) for: " + ", ".join(r["name"] for r in regions),
        f'REGION_NAME="admin-{hashlib.sha1(" ".join(ids).encode()).hexdigest()[:10]}"',
        'GEOFABRIK_AREAS="' + " ".join(r["path"] for r in regions) + '"',
        f'BBOX="{west:g} {south:g} {east:g} {north:g}"',
        f'CONTOUR_UNITS="{options["units"]}"',
        f'WORLD="{"yes" if options["world"] else "no"}"',
        f'LANGUAGE="{options["language"]}"',
        f'DEM_SOURCE="{"copernicus" if options["terrain"] == "30m" else "usgs"}"',
        # Under a memory cap (Pi mode), Planetiler's heap is two thirds of it (scripts/lib.sh).
        (f'PLANETILER_MEMORY="{int(planetiler_heap_gb(cap) * 1024)}m"' if cap else
         f'PLANETILER_MEMORY="{int(min(24, max(2, ram * 0.5)))}g"'),
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
        job.update(state="failed", message="The build stopped unexpectedly. Was the server shut down? "
                                           "The current maps did not change.")
        write_json(JOB, job)
        # It did not start again what it stopped: do that now.
        threading.Thread(target=restore_services, kwargs={"say": lambda _line: None}, daemon=True).start()
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


def check_build(est, body):
    """Raises BadRequest, in plain words, when a build of this estimate must not start."""
    if est["memory_problem"]:
        raise BadRequest(est["memory_problem"])
    if not est["space_ok"]:
        keep = f", and {est['reserve_gb']:g} GB must stay free for the system" if est["reserve_gb"] else ""
        raise BadRequest(f"Not enough free space. The build needs about {est['need_gb']:.0f} GB{keep}, but "
                         f"only {est['free_gb']:.0f} GB is free. Select fewer regions or layers.")
    pause = pause_list()
    if pause["services"] and not pause["ready"]:
        raise BadRequest("The other services (FIELDMAP_PAUSE_UNITS) cannot be stopped: this server is not "
                         "set up for it. On the server, run the installer again.")
    # The page asks first, and says which: starting the build is the answer.
    if pause["services"] and body.get("pause_ok") is not True:
        raise BadRequest("Confirm that these services are stopped during the build: " + ", ".join(pause["services"]) + ".")


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
        finish("cancelled", "Build canceled. The current maps did not change. "
                            "The server keeps the completed parts; start the same build again to resume.")

    def finish(state, message):
        # The services the build stopped, again, however it ended.
        if os.path.exists(PAUSE_STATE):
            print("\nStarting the stopped services again.", flush=True)
            if not restore_services():
                left = ", ".join(pause_list()["stopped"])
                message += (f" Some services did not start again: {left}. Start them on the server, or restart it.")
        job.update(state=state, message=message, finished=int(time.time()))
        write_json(JOB, job)
        sys.exit(0)

    print(f"Map Builder: {', '.join(request['names'])}; steps: {' '.join(steps)}", flush=True)
    # The build lock first, before anything in data/ changes: build-region.sh shares it (the
    # environment and the open file below), and a build from the command line cannot start meanwhile.
    lock = build_lock()
    if lock is None:
        finish("failed", "The build did not start: another build, started on the server's command line, "
                         "is in progress. The current maps did not change. Start this build when it is complete.")
    if pi_mode():
        print(f"Pi mode: at most {build_memory_gb():g} GB of RAM for the build, 30 m elevation data.", flush=True)
    pause = pause_list()
    if pause["services"]:
        print(f"Stopping other services to free RAM for the build: {', '.join(pause['services'])}. "
              "They start again when the build ends.", flush=True)
        # Before this process listens for Cancel Build: a cancel now waits until they are stopped,
        # and then starts them again (finish), so the two never run at once.
        r = subprocess.run(["sudo", "-n", PAUSE_HELPER, "stop"])
        if r.returncode != 0:
            finish("failed", "The build did not start: the other services could not be stopped (FIELDMAP_PAUSE_UNITS). "
                             "The Build Log below shows why. The current maps did not change.")
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
    env = dict(os.environ, FIELDMAP_BUILD_LOCKED="1")
    env.pop("LANGUAGE", None)          # a locale setting, not the label language of a build
    if request["options"].get("fresh"):
        env["OSM_REFRESH"] = "yes"     # download the OpenStreetMap extracts again (merge-osm.sh)
    # The scripts on the machine itself at low priority too (the containers they start get a
    # low CPU weight, scripts/lib.sh): the page, the tile server and the rest come first.
    os.nice(10)
    child = subprocess.Popen([os.path.join(ROOT, "scripts", "build-region.sh"), "--staged",
                              os.path.relpath(REGION_FILE, ROOT), *steps], cwd=ROOT, env=env, start_new_session=True,
                             pass_fds=(lock.fileno(),))
    code = child.wait()
    if code == 0:
        swapped.append(True)
    else:
        finish("failed", "The build failed. The current maps did not change. "
                         "The Build Log below shows the error. The server keeps the completed parts. "
                         "Fix the problem, then start the same build again to resume from where it stopped.")
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
        finish("done", "The new maps are installed, but the map server did not restart. "
                       "On the server, run: docker compose up -d")
    finish("done", "The new maps are installed and available now.")


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
        if os.path.isdir(path):     # the routing data: there once a build has written its index
            size = folder_gb(path) * 1024 ** 3
            present = os.path.isfile(os.path.join(path, "index.json"))
        else:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            present = size > 0 and not placeholder(path)
        out[key] = {"present": present, "size_gb": round(size / 1024 ** 3, 2), **state.get(key, {})}
    return out


def status():
    base = DATA if os.path.isdir(DATA) else ROOT
    usage = shutil.disk_usage(base)
    return {"free_gb": round(usage.free / 1024 ** 3, 1), "total_gb": round(usage.total / 1024 ** 3, 1),
            "layers": layers_status(), "job": job_status(), "docker": shutil.which("docker") is not None,
            "languages": LANGUAGES, "web_port": web_port(), "detail_km": DETAIL_KM,
            "osm_ages": osm_ages(), "osm_refresh_days": OSM_REFRESH_DAYS,
            "pi": pi_mode(), "ram_gb": round(ram_gb(), 1), "memory_gb": build_memory_gb(), "reserve_gb": reserve_gb(),
            "sd_card": on_sd_card(base), "pause": pause_list(), "staged": staged(),
            "world_files": sorted(os.path.basename(p) for p in glob.glob(os.path.join(DATA, "world-z0-8*.mbtiles")))}


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
            raise BadRequest("This request did not come from the Map Builder page.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise BadRequest("Expected JSON.")
        n = int(self.headers.get("Content-Length", 0))
        if n > 100_000:
            raise BadRequest("The request is too large.")
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
        if url.path == "/api/cleanup":
            if not self.signed_in():
                return self.send(401, {"error": "pin"})
            return self.send(200, cleanup_status())
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
        if not os.path.isfile(path):
            path = os.path.join(SHARED, name)       # the look and the header every page shares
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
                    return self.send(429, {"error": "Too many attempts. Wait one minute, then try again."})
                TRIES.append(now)
                if not secrets.compare_digest(str(body.get("pin", "")).strip().encode(), (pin() or "").encode()):
                    return self.send(403, {"error": "Incorrect Access Code."})
                token = secrets.token_urlsafe(24)
                SESSIONS.add(token)
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", f"fm_admin={token}; Path=/; HttpOnly; SameSite=Strict")])
            if not self.signed_in():
                return self.send(401, {"error": "pin"})
            if url.path == "/api/cancel":
                cancel_build()
                return self.send(200, {"ok": True})
            if url.path == "/api/cleanup":
                with LOCK:
                    start_cleanup()
                return self.send(200, {"ok": True})
            if url.path in ("/api/estimate", "/api/build"):
                regions, layers, options = check_selection(body)
                est = estimate(regions, layers, options)
                if url.path == "/api/estimate":
                    return self.send(200, est)
                with LOCK:
                    job = job_status()
                    if job and job["state"] == "running":
                        raise BadRequest("A build is already in progress.")
                    if CLEANING["running"]:
                        raise BadRequest("Free Up Storage is deleting files. Start the build when it has finished.")
                    if build_locked():
                        raise BadRequest("A build started on the server's command line is in progress. "
                                         "Start this build when it is complete.")
                    if not shutil.which("docker"):
                        raise BadRequest("Docker is not installed on the server. The build requires Docker.")
                    check_build(est, body)
                    start_build(regions, layers, options)
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except BadRequest as e:
            self.send(400, {"error": str(e)})
        except ValueError:
            self.send(400, {"error": "The server could not read the request."})


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
    # Services a build stopped and did not start again (it was killed): start them now.
    job = job_status()
    if not job or job["state"] != "running":
        restore_services()
    print("Map Builder: http://<server address>/mapadmin/", flush=True)
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
