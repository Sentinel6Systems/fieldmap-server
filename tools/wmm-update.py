#!/usr/bin/env python3
"""Keep the print tool's World Magnetic Model (WMM) current.

The print tool has a WMM built in. This script downloads a newer one when it is
published and writes it to data/wmm/wmm.json, which the web server serves to the
map at /map/wmm/wmm.json. No rebuild is needed.

Usage (Python 3 standard library only):
  tools/wmm-update.py status            what is installed, as JSON
  tools/wmm-update.py update            download a newer model if there is one
  tools/wmm-update.py serve [--port N]  small HTTP service for the print tool's
                                        "Update magnetic model" button:
                                        GET /status, POST /update

Sources, tried in order until one gives a model that passes the checks:
  1. NOAA NCEI, the publisher (coefficient file plus official test values)
  2. PyPI, the pygeomag package (a copy of NOAA's coefficient file)
  3. npm, the geomagnetism package (the coefficients as JSON)
Every model is checked before it is used: its layout and size; against NOAA's test
values when the download has them; and against the installed model, which must give
nearly the same declination at the new model's start. The first source that answers
with a valid model settles it: when that model is no newer than the installed one,
nothing changes and the other sources are not asked. With no Internet the installed model stays in use,
even past its end date (the print tool then carries its rate of change forward).
"""
import argparse
import datetime as dt
import http.server
import io
import json
import math
import os
import re
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT_DIR = os.environ.get("WMM_DIR", os.path.join(REPO, "data", "wmm"))
SEED = os.path.join(HERE, "wmm", "WMM2025.COF")   # the model the print tool has built in
UA = "fieldmap-server wmm-update (+https://github.com/Sentinel6Systems/fieldmap-server)"
TIMEOUT = 30

NCEI_PAGE = "https://www.ncei.noaa.gov/products/world-magnetic-model/wmm-coefficients"
PYPI_JSON = "https://pypi.org/pypi/pygeomag/json"
NPM_LATEST = "https://registry.npmjs.org/geomagnetism/latest"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url, limit=20 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than expected")
    return data


# ---------------------------------------------------------------- model files

def parse_cof(text):
    """WMM.COF -> the JSON layout the print tool reads (that of the geomagnetism package)."""
    m = {"main_field_coeff_g": [0], "main_field_coeff_h": [0],
         "secular_var_coeff_g": [0], "secular_var_coeff_h": [0], "n_max": 0, "n_max_sec_var": 0}
    rows = 0
    for line in text.splitlines():
        v = line.split()
        if len(v) == 3 and "epoch" not in m:
            m["epoch"] = float(v[0])
            m["name"] = v[1]
            mo, d, y = (int(x) for x in v[2].split("/"))
            start = dt.datetime(y, mo, d, tzinfo=dt.timezone.utc)
            m["start_date"] = start.isoformat().replace("+00:00", "Z")
            m["end_date"] = start.replace(year=y + 5).isoformat().replace("+00:00", "Z")
        elif len(v) == 6:
            n, k = int(v[0]), int(v[1])
            if k > n:
                raise ValueError("bad coefficient row")
            i = n * (n + 1) // 2 + k
            for key, val in zip(("main_field_coeff_g", "main_field_coeff_h", "secular_var_coeff_g", "secular_var_coeff_h"), v[2:]):
                arr = m[key]
                arr.extend([0] * (i + 1 - len(arr)))
                arr[i] = float(val)
            m["n_max"] = m["n_max_sec_var"] = max(m["n_max"], n)
            rows += 1
    if "epoch" not in m:
        raise ValueError("no header line")
    if rows != m["n_max"] * (m["n_max"] + 3) // 2:
        raise ValueError(f"{rows} coefficient rows for degree {m['n_max']}")
    return m


def check_layout(m):
    """Shape and size checks that any genuine WMM passes."""
    n = m.get("n_max")
    if not isinstance(n, int) or not 6 <= n <= 20:
        raise ValueError(f"degree {n!r}")
    size = n * (n + 1) // 2 + n + 1
    for key in ("main_field_coeff_g", "main_field_coeff_h", "secular_var_coeff_g", "secular_var_coeff_h"):
        if len(m.get(key, [])) < size:
            raise ValueError(f"{key} too short")
    g10 = m["main_field_coeff_g"][1]
    if not -32000 < g10 < -28000:
        raise ValueError(f"g(1,0) = {g10} is not an Earth main field")
    if not re.fullmatch(r"WMM[-\w.]*", m.get("name", "")):
        raise ValueError(f"name {m.get('name')!r}")
    if dates(m)[1] <= dates(m)[0]:
        raise ValueError("end date before start date")


def dates(m):
    p = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return p(m["start_date"]), p(m["end_date"])


def declination(m, lat, lon, h_km, year):
    """Declination in degrees from model m: the WMM spherical harmonic synthesis."""
    a, f, re_ = 6378.137, 1 / 298.257223563, 6371.2
    e2 = f * (2 - f)
    phi, lam = math.radians(lat), math.radians(lon)
    rc = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    p = (rc + h_km) * math.cos(phi)
    z = (rc * (1 - e2) + h_km) * math.sin(phi)
    r = math.hypot(p, z)
    phic = math.asin(z / r)
    nmax, dyr = m["n_max"], year - m["epoch"]
    idx = lambda n, k: n * (n + 1) // 2 + k
    g = lambda n, k: m["main_field_coeff_g"][idx(n, k)] + dyr * m["secular_var_coeff_g"][idx(n, k)]
    h = lambda n, k: m["main_field_coeff_h"][idx(n, k)] + dyr * m["secular_var_coeff_h"][idx(n, k)]
    ct, st = math.sin(phic), math.cos(phic)        # cos and sin of the colatitude
    P = [[0.0] * (nmax + 2) for _ in range(nmax + 2)]
    dP = [[0.0] * (nmax + 2) for _ in range(nmax + 2)]
    P[0][0] = 1.0
    for n in range(1, nmax + 1):                   # Gauss-normalised, then Schmidt below
        for k in range(n + 1):
            if n == k:
                P[n][k] = st * P[n - 1][k - 1]
                dP[n][k] = st * dP[n - 1][k - 1] + ct * P[n - 1][k - 1]
            elif n == 1:
                P[n][k] = ct * P[0][k]
                dP[n][k] = ct * dP[0][k] - st * P[0][k]
            else:
                K = ((n - 1) ** 2 - k ** 2) / ((2 * n - 1) * (2 * n - 3)) if n > 1 else 0
                P[n][k] = ct * P[n - 1][k] - K * P[n - 2][k]
                dP[n][k] = ct * dP[n - 1][k] - st * P[n - 1][k] - K * dP[n - 2][k]
    S = [[0.0] * (nmax + 2) for _ in range(nmax + 2)]
    S[0][0] = 1.0
    for n in range(1, nmax + 1):
        S[n][0] = S[n - 1][0] * (2 * n - 1) / n
        for k in range(1, n + 1):
            S[n][k] = S[n][k - 1] * math.sqrt((n - k + 1) * (2 if k == 1 else 1) / (n + k))
    X = Y = Z = 0.0
    for n in range(1, nmax + 1):
        ar = (re_ / r) ** (n + 2)
        for k in range(n + 1):
            c, s = math.cos(k * lam), math.sin(k * lam)
            pn, dpn = S[n][k] * P[n][k], S[n][k] * dP[n][k]
            X += ar * (g(n, k) * c + h(n, k) * s) * dpn
            if st > 1e-10:
                Y += ar * k * (g(n, k) * s - h(n, k) * c) * pn / st
            Z -= (n + 1) * ar * (g(n, k) * c + h(n, k) * s) * pn
    psi = phic - phi
    Xg = X * math.cos(psi) - Z * math.sin(psi)
    return math.degrees(math.atan2(Y, Xg))


def check_test_values(m, text):
    """NOAA's test values: year, height km, lat, lon first; the header names the declination field."""
    col = None
    for line in text.splitlines():
        h = re.match(r"#\s*Field\s+(\d+):\s*declination\b", line, re.I)
        if h:
            col = int(h.group(1)) - 1
    if col is None:
        raise ValueError("test value file has no declination field")
    n = 0
    for line in text.splitlines():
        v = line.split()
        if len(v) <= col or line.lstrip().startswith("#"):
            continue
        try:
            year, h, lat, lon, want = float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[col])
        except ValueError:
            continue
        got = declination(m, lat, lon, h, year)
        if abs(got - want) > 0.015:
            raise ValueError(f"test value {lat},{lon} {year}: {got:.3f} instead of {want}")
        n += 1
    if n < 6:
        raise ValueError("test value file has too few rows")
    return n


def check_against(m, old):
    """The new model and the installed one carried to the new model's start must agree closely."""
    year = m["epoch"]
    worst = 0.0
    for lat in (-60, -30, 0, 30, 60):
        for lon in range(-180, 180, 45):
            d = abs(declination(m, lat, lon, 0, year) - declination(old, lat, lon, 0, year))
            worst = max(worst, min(d, 360 - d))
    if worst > 3.0:
        raise ValueError(f"differs from the installed model by {worst:.1f}° in declination")
    return worst


# ---------------------------------------------------------------- sources

def from_ncei():
    page = fetch(NCEI_PAGE).decode("utf-8", "replace")
    links = re.findall(r'href="(https://www\.ncei\.noaa\.gov/[^"]*WMM(\d{4})(?:v\d)?COF\.zip)"', page)
    if not links:
        raise ValueError("no coefficient download on the page")
    url = max(links, key=lambda x: x[1])[0]
    z = zipfile.ZipFile(io.BytesIO(fetch(url)))
    cof = [n for n in z.namelist() if re.search(r"WMM\d{4}(v\d)?\.COF$", n, re.I)] or [n for n in z.namelist() if n.upper().endswith("WMM.COF")]
    tv = [n for n in z.namelist() if re.search(r"test_?values\.txt$", n, re.I)]
    if not cof:
        raise ValueError("no .COF file in the download")
    m = parse_cof(z.read(cof[0]).decode("ascii", "replace"))
    tests = z.read(tv[0]).decode("ascii", "replace") if tv else None
    return m, tests, url


def from_pypi():
    info = json.loads(fetch(PYPI_JSON))
    whl = [u["url"] for u in info["urls"] if u["url"].endswith(".whl")]
    if not whl:
        raise ValueError("no wheel")
    z = zipfile.ZipFile(io.BytesIO(fetch(whl[0])))
    cofs = [n for n in z.namelist() if re.search(r"/WMM_(\d{4})(v\d)?\.COF$", n)]
    if not cofs:
        raise ValueError("no WMM_<year>.COF in the package")
    name = max(cofs, key=lambda n: re.search(r"WMM_(\d{4}(v\d)?)", n).group(1))
    return parse_cof(z.read(name).decode("ascii", "replace")), None, whl[0]


def from_npm():
    info = json.loads(fetch(NPM_LATEST))
    url = info["dist"]["tarball"]
    t = tarfile.open(fileobj=io.BytesIO(fetch(url)), mode="r:gz")
    files = [x for x in t.getmembers() if x.isfile() and re.search(r"/data/wmm-(\d{4})(v\d)?\.json$", x.name)]
    if not files:
        raise ValueError("no data/wmm-<year>.json in the package")
    newest = max(files, key=lambda x: re.search(r"wmm-(\d{4}(v\d)?)", x.name).group(1))
    m = json.loads(t.extractfile(newest).read())
    for k in ("epoch", "name", "start_date", "end_date"):
        if k not in m:
            raise ValueError(f"no {k}")
    return m, None, url


SOURCES = [("NOAA NCEI", from_ncei), ("PyPI pygeomag", from_pypi), ("npm geomagnetism", from_npm)]


# ---------------------------------------------------------------- install

def installed():
    path = os.path.join(OUT_DIR, "wmm.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f), True
    with open(SEED) as f:
        return parse_cof(f.read()), False


def read_state():
    try:
        with open(os.path.join(OUT_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = os.path.join(OUT_DIR, f".{name}.tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(OUT_DIR, name))


def status():
    m, downloaded = installed()
    start, end = dates(m)
    st = read_state()
    return {"model": m["name"], "start": m["start_date"], "end": m["end_date"],
            "expired": dt.datetime.now(dt.timezone.utc) > end, "downloaded": downloaded,
            "source": st.get("source"), "updated": st.get("updated"),
            "lastCheck": st.get("lastCheck"), "lastResult": st.get("lastResult")}


def update():
    old, _ = installed()
    old_end = dates(old)[1]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    errors, newest_seen = [], None
    for name, get in SOURCES:
        try:
            log(f"Trying {name} ...")
            m, tests, url = get()
            check_layout(m)
            if tests:
                log(f"  {check_test_values(m, tests)} NOAA test values match")
            if dates(m)[1] <= old_end + dt.timedelta(days=30):   # the same model, perhaps another copy
                # The first source that answers settles it: the mirrors only ever lag behind NOAA.
                newest_seen = (m["name"], name)
                log(f"  {m['name']} is not newer than the installed {old['name']}")
                break
            log(f"  agrees with {old['name']} to {check_against(m, old):.2f}°")
        except Exception as e:                               # network errors included: try the next source
            errors.append(f"{name}: {e}")
            log(f"  failed: {e}")
            continue
        m["source"] = name
        write_json("wmm.json", m)
        result = {"ok": True, "updated": True, "model": m["name"], "source": name,
                  "message": f"Installed {m['name']} from {name}, valid to {m['end_date'][:10]}."}
        write_json("state.json", {"source": name, "url": url, "updated": now, "lastCheck": now, "lastResult": result["message"]})
        return result
    if newest_seen:
        msg = f"No newer model yet: {newest_seen[1]} has {newest_seen[0]}, the one in use. Keeping {old['name']}."
        result = {"ok": True, "updated": False, "model": old["name"], "message": msg}
    else:
        msg = f"Could not get a model from any source, so {old['name']} stays in use. " + "; ".join(errors)
        result = {"ok": False, "updated": False, "model": old["name"], "message": msg}
    st = read_state()
    st.update({"lastCheck": now, "lastResult": result["message"]})
    write_json("state.json", st)
    return result


# ---------------------------------------------------------------- service

class Handler(http.server.BaseHTTPRequestHandler):
    lock = threading.Lock()
    last_run = 0.0
    server_version = "wmm-update"

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def path_end(self):
        return self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]

    def do_GET(self):
        if self.path_end() == "status":
            return self.reply(200, status())
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path_end() != "update":
            return self.reply(404, {"error": "not found"})
        # Only the print tool's own fetch() sends this header; a form on another site cannot.
        if self.headers.get("X-Fieldmap") != "wmm-update":
            return self.reply(403, {"error": "missing X-Fieldmap header"})
        if not Handler.lock.acquire(blocking=False):
            return self.reply(409, {"ok": False, "message": "An update is already running."})
        try:
            if time.time() - Handler.last_run < 30:
                return self.reply(429, {"ok": False, "message": "Updated less than 30 seconds ago; try again shortly."})
            Handler.last_run = time.time()
            self.reply(200, update())
        finally:
            Handler.lock.release()

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["status", "update", "serve"])
    ap.add_argument("--host", default=os.environ.get("WMM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WMM_PORT", "8091")))
    a = ap.parse_args()
    if a.command == "status":
        print(json.dumps(status(), indent=1))
    elif a.command == "update":
        r = update()
        print(r["message"])
        sys.exit(0 if r["ok"] else 1)
    else:
        log(f"Serving GET /status and POST /update on {a.host}:{a.port}; model files in {OUT_DIR}")
        http.server.ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
