#!/usr/bin/env python3
"""fieldmap-server: live sharing for the Fieldmap Android app.

Phones that reach this box see each other's positions, and the markers their owners chose to
share: one room, everybody sees everybody. A very small stand-in for a TAK server, with its
own short JSON messages instead of CoT. Nobody signs in: at home the app comes through the
box's web server, on the local network; away from home through the remote gateway, which lets
in only phones that were paired here (tools/remote-server.py).

Off until the owner switches it on, on the Devices page. While off, the box takes nothing in
and gives nothing out.

What it answers (the web server and the gateway put /mapshare/ in front):
  GET    /info             {"version", "enabled", "interval", "stale"}, also while off
  POST   /position         a phone's position; kept in memory only, dropped when stale
  POST   /leave            {"uid"}: that position is gone at once
  GET    /state[?since=n]  every position, and the shared markers (all, or those changed since)
  PUT    /markers/<id>     a shared marker (a GeoJSON Feature with a Point); kept on disk
  DELETE /markers/<id>
  GET    /admin            for the Devices page: the switch, who is sharing right now
  POST   /admin            {"on": true|false}: the switch

Who may ask:
  * through the box's web server, which puts the caller's address in X-Real-IP: callers on
    the local network, for everything;
  * through the gateway, which puts the phone's certificate in X-Fieldmap-Fingerprint after
    checking it: everything but /admin;
  * with neither header: programs on the box itself.

This service is on its own, apart from the one that holds the box's certificate authority:
it takes what phones send, also from outside, so it gets to read and write its own folder
(data/share/) and nothing else of the box's (systemd/fieldmap-share.service).

Python's standard library and the `ip` command only.

  share-server.py serve [--port 8093] [--host 127.0.0.1]     also always on data/share/run/share.sock
  share-server.py status

Settings in the environment (scripts/remote.sh reads them from .env):
  REMOTE_PIN         the box's code: the Devices page's switch then asks for it
"""
import argparse
import contextlib
import ipaddress
import json
import math
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portmap  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE = os.path.abspath(os.environ.get("SHARE_DIR") or os.path.join(ROOT, "data", "share"))
RUN = os.path.join(SHARE, "run")
SOCKET = os.path.join(RUN, "share.sock")
SETTINGS = os.path.join(SHARE, "settings.json")     # the Devices page's switch
MARKERS = os.path.join(SHARE, "markers.json")       # the shared markers; positions are never written

VERSION = 1
INTERVAL = 5                    # seconds between a phone's posts and polls
STALE = 120                     # seconds after its last post at which a position is dropped
MAX_PEOPLE = 100                # positions at once
MAX_PER_PHONE = 4               # ...of which from one paired phone (one is normal)
MAX_PER_ADDRESS = 25            # ...and from one address on the home network
MAX_MARKERS = 2000
MAX_MARKER_BYTES = 4 << 20      # all the markers together
MAX_REMOVED = 1000              # how many removals are remembered for phones that were away
POSITION_BYTES = 2048           # body limits
MARKER_BYTES = 8192
ADMIN_BYTES = 1024
MAX_PROPERTIES = 32             # entries in a marker's properties, and in an object inside them
MAX_TEXT = 300                  # characters of a text in a marker
MAX_BUSY = 64                   # requests answered at the same time

# Per caller, as (a second, burst): every request; changing markers (each change is written
# to disk); the switch (the box's code must not be guessable by trying).
RATE_ALL = (10, 60)
RATE_MARKERS = (1, 60)
RATE_MARKERS_BOX = (4, 240)     # all callers together
RATE_SWITCH = (1 / 6, 10)

ID = re.compile(r"[A-Za-z0-9-]{8,64}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})")
FINGERPRINT = re.compile(r"[0-9a-f]{40}")
KEY = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
# Marks that turn the direction of the text around them: a name could hide what it says.
TURNING = set("‪‫‬‭‮⁦⁧⁨⁩")

lock = threading.RLock()
on = False                      # the switch; read from SETTINGS when the service starts
positions = {}                  # uid -> {"position": {...}, "at": clock(), "outside": bool, "caller": key}
markers = {}                    # id -> {"rev": n, "feature": {...}, "bytes": n}
removed = []                    # [id, rev], oldest first
rev = 0                         # counts marker changes; never goes back
floor = 0                       # removals up to this rev are no longer remembered
buckets = {}                    # (what, caller) -> [tokens, clock()]


class Refused(Exception):
    """An answer other than 200: Refused(status, plain words)."""

    def __init__(self, status, text):
        super().__init__(text)
        self.status, self.text = status, text


def clock():
    """Seconds that only go forward: a box without a clock chip sets its time after it starts."""
    return time.monotonic()


def setting(name, default=""):
    return os.environ.get(name) or default


def write_private(path, data):
    """Writes a file only its owner can read, all at once, and waits until it is on the disk:
    boxes get unplugged."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------- the switch ----------

def load_settings():
    """Reads the switch: off unless the file says on."""
    global on
    try:
        with open(SETTINGS) as f:
            data = json.load(f)
        on = isinstance(data, dict) and data.get("enabled") is True
    except (OSError, ValueError):
        on = False
    return on


def set_enabled(value):
    """Off: every position is dropped at once and nothing is taken or given until it is on
    again. The shared markers stay on the disk."""
    global on
    with lock:
        write_private(SETTINGS, json.dumps({"enabled": bool(value)}).encode())
        on = bool(value)
        if not on:
            positions.clear()


# ---------- what phones send, checked ----------

def bad_text(text):
    """True for text with control characters, line breaks or direction marks in it."""
    return any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") or c in TURNING for c in text)


def number(value, low, high, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} must be a number from {low} to {high}.")
    return value


def clean_id(value, what="uid"):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"The {what} must be 8 to 64 letters, digits or hyphens.")
    return value


def clean_time(value, what="time"):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise ValueError(f"The {what} must look like 2026-01-31T12:00:00Z.")
    return value


def clean_position(body):
    """A position with exactly the fields the box knows, or ValueError in plain words."""
    if not isinstance(body, dict):
        raise ValueError("A position is a JSON object.")
    name = body.get("name")
    if not isinstance(name, str) or bad_text(name) or not 1 <= len(name.strip()) <= 24:
        raise ValueError("The name must be 1 to 24 characters.")
    out = {"uid": clean_id(body.get("uid")), "name": name.strip(),
           "lat": number(body.get("lat"), -90, 90, "lat"), "lon": number(body.get("lon"), -180, 180, "lon")}
    for field, low, high in (("alt", -1000, 100000), ("acc", 0, 100000), ("course", 0, 360), ("speed", 0, 10000)):
        if body.get(field) is not None:
            out[field] = number(body[field], low, high, field)
    out["time"] = clean_time(body.get("time"))
    return out


def clean_value(value, inside=False):
    """A value of a marker's properties: text, a number, true, false, nothing, or (once) an
    object of those."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return number(value, -1e15, 1e15, "A number in a marker")
    if isinstance(value, str):
        if len(value) > MAX_TEXT or bad_text(value):
            raise ValueError(f"A text in a marker is at most {MAX_TEXT} characters, on one line.")
        return value
    if isinstance(value, dict) and not inside:
        return clean_properties(value, inside=True)
    raise ValueError("A marker's properties are texts and numbers.")


def clean_properties(properties, inside=False):
    if len(properties) > MAX_PROPERTIES:
        raise ValueError(f"A marker has at most {MAX_PROPERTIES} properties.")
    out = {}
    for key, value in properties.items():
        if not KEY.fullmatch(key) or bad_text(key):
            raise ValueError("A property's name is 1 to 40 characters.")
        out[key] = clean_value(value, inside)
    return out


def clean_marker(marker_id, body):
    """A shared marker as it is kept and given out: a GeoJSON Feature with a Point, its id, and
    properties that are plain values. ValueError in plain words otherwise."""
    if not isinstance(body, dict) or body.get("type") != "Feature":
        raise ValueError("A marker is a GeoJSON Feature.")
    if body.get("id") is not None and body["id"] != marker_id:
        raise ValueError("The marker's id is not the one in the address.")
    geometry = body.get("geometry")
    point = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "Point" else None
    if not isinstance(point, list) or len(point) not in (2, 3):
        raise ValueError("A marker is a Point: [lon, lat].")
    coordinates = [number(point[0], -180, 180, "lon"), number(point[1], -90, 90, "lat")]
    if len(point) == 3:
        coordinates.append(number(point[2], -1000, 100000, "alt"))
    properties = body.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("A marker needs properties.")
    properties = clean_properties(properties)
    clean_id(properties.get("by"), "marker's \"by\"")
    clean_time(properties.get("updated"), "marker's \"updated\"")
    return {"type": "Feature", "id": marker_id, "geometry": {"type": "Point", "coordinates": coordinates}, "properties": properties}


# ---------- positions: in memory only ----------

def drop_stale():
    limit = clock() - STALE
    for uid in [u for u, p in positions.items() if p["at"] < limit]:
        del positions[uid]


def post_position(position, caller, outside):
    with lock:
        if not on:                      # switched off this very moment: nothing is kept
            return
        drop_stale()
        uid = position["uid"]
        if uid not in positions:
            if len(positions) >= MAX_PEOPLE:
                raise Refused(507, "The box has as many people sharing as it takes.")
            mine = sum(1 for p in positions.values() if p["caller"] == caller)
            if mine >= (MAX_PER_PHONE if outside else MAX_PER_ADDRESS):
                raise Refused(507, "Too many positions from this device.")
        positions[uid] = {"position": position, "at": clock(), "outside": outside, "caller": caller}


def leave(uid):
    with lock:
        positions.pop(uid, None)


def people():
    """For the Devices page: who is sharing right now. Names and ages, no places."""
    with lock:
        drop_stale()
        now = clock()
        return sorted(({"name": p["position"]["name"], "age": int(now - p["at"]), "outside": p["outside"]}
                       for p in positions.values()), key=lambda p: p["name"].lower())


# ---------- shared markers: on the disk ----------

def load_markers():
    """Reads the markers kept on the disk. With no file (or one that cannot be read, which is
    put aside), the room starts empty, and rev starts from the clock: a phone that knew this
    box before then never holds a rev that this box will come to again."""
    global rev, floor
    with lock:
        markers.clear()
        removed.clear()
        try:
            with open(MARKERS) as f:
                data = json.load(f)
            rev, floor = int(data["rev"]), int(data["floor"])
            removed.extend([clean_id(i, "id"), int(r)] for i, r in data["removed"])
            for marker_id, entry in data["markers"].items():
                try:                    # checked again as when it came in; one that fails is left out
                    feature = clean_marker(clean_id(marker_id, "id"), entry["feature"])
                    markers[marker_id] = {"rev": int(entry["rev"]), "feature": feature, "bytes": len(json.dumps(feature))}
                except (ValueError, KeyError, TypeError) as e:
                    print("A shared marker on the disk is left out: %s" % e, flush=True)
        except FileNotFoundError:
            rev = floor = int(time.time())
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            print("The shared markers on the disk cannot be read (%s): put aside as markers.json.bad, starting empty." % e, flush=True)
            try:
                os.replace(MARKERS, MARKERS + ".bad")
            except OSError:
                pass
            markers.clear()
            removed.clear()
            rev = floor = int(time.time())


def save_markers():
    write_private(MARKERS, json.dumps({"rev": rev, "floor": floor, "removed": removed,
                                       "markers": {i: {"rev": m["rev"], "feature": m["feature"]} for i, m in markers.items()}}).encode())


@contextlib.contextmanager
def written():
    """Around a change to the markers: it is written to the disk, and if that fails (a full
    disk), taken back, so that no phone is told of a change the box would forget."""
    global rev, floor
    before = (rev, floor, dict(markers), list(removed))
    try:
        yield
        save_markers()
    except OSError as e:
        rev, floor = before[:2]
        markers.clear()
        markers.update(before[2])
        removed[:] = before[3]
        print("The shared markers could not be written: %s" % e, flush=True)
        raise Refused(507, "The box could not write that down.")


def put_marker(feature):
    """Keeps a marker (the last one sent wins) and returns the new rev."""
    global rev
    marker_id, size = feature["id"], len(json.dumps(feature))
    with lock:
        if marker_id not in markers and len(markers) >= MAX_MARKERS:
            raise Refused(507, "The box has as many shared markers as it takes.")
        if sum(m["bytes"] for i, m in markers.items() if i != marker_id) + size > MAX_MARKER_BYTES:
            raise Refused(507, "The box has as many shared markers as it takes.")
        with written():
            rev += 1
            markers[marker_id] = {"rev": rev, "feature": feature, "bytes": size}
            removed[:] = [r for r in removed if r[0] != marker_id]
        return rev


def delete_marker(marker_id):
    """Removes a marker and returns the rev: a new one, or the same when it was not there."""
    global rev, floor
    with lock:
        if marker_id in markers:
            with written():
                rev += 1
                del markers[marker_id]
                removed.append([marker_id, rev])
                while len(removed) > MAX_REMOVED:
                    floor = removed.pop(0)[1]
        return rev


def state(since=None):
    """What a device asks for every few seconds: every position, and the shared markers. With
    `since`, a rev this box gave out and still knows what came after, only what changed."""
    with lock:
        drop_stale()
        now = clock()
        full = since is None or since > rev or since < floor
        return {"rev": rev, "full": full,
                "positions": [{**p["position"], "age": int(now - p["at"])} for p in positions.values()],
                "markers": [m["feature"] for m in markers.values() if full or m["rev"] > since],
                "removed": [] if full else [i for i, r in removed if r > since]}


# ---------- who is asking, and how often ----------

# The addresses of home and office networks, and of the box itself; the forwarding headers a
# proxy or tunnel on the way would add. The same rule as lan() in tools/remote-server.py
# (the tests hold the two together).
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]

FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "CF-Connecting-IP", "True-Client-IP", "Tailscale-Funnel-Request")

own_networks = [0.0, []]        # when asked last, and the answer


def own_prefixes():
    """The IPv6 networks this machine is on: devices at home have global addresses in them.
    Asked again at most once a minute."""
    if clock() - own_networks[0] > 60 or not own_networks[0]:
        found = []
        for address in portmap.global_ipv6():
            try:
                found.append(ipaddress.ip_network(address + "/64", strict=False))
            except ValueError:
                pass
        own_networks[:] = [clock(), found]
    return own_networks[1]


def caller(headers):
    """Who sent a request: ("home", address) through the box's web server from the local
    network, ("outside", certificate) through the gateway, ("box", "") a program on the box
    itself, or None: somebody who gets no answer.

    The web server always puts the caller's address in X-Real-IP, and it must be a local one. A
    request that carries a forwarding header came through some proxy or tunnel on the way and
    is refused: a box whose web port is published to the internet through one would otherwise
    hand everybody's position to the world. The gateway sends no X-Real-IP, and the
    certificate it has checked instead.
    """
    real = headers.get("X-Real-IP")
    if real is None:
        fingerprint = headers.get("X-Fieldmap-Fingerprint")
        if fingerprint is None:
            return ("box", "")
        fingerprint = fingerprint.strip().lower()
        return ("outside", fingerprint) if FINGERPRINT.fullmatch(fingerprint) else None
    if any(headers.get(h) for h in FORWARDED):
        return None
    try:
        address = ipaddress.ip_address(real.strip())
    except ValueError:
        return None
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    if any(address in network for network in LOCAL_NETWORKS) or any(address in network for network in own_prefixes()):
        return ("home", str(address))
    return None


def allow(what, who, rate):
    """False when `who` has asked for `what` more often than `rate` (a second, burst) lets."""
    a_second, burst = rate
    with lock:
        now = clock()
        if len(buckets) > 5000:         # forget whoever has been quiet long enough to be full again
            for key in [k for k, (tokens, then) in buckets.items() if now - then > 600]:
                del buckets[key]
            if len(buckets) > 5000:
                buckets.clear()
        tokens, then = buckets.get((what, who), (burst, now))
        tokens = min(burst, tokens + (now - then) * a_second)
        if tokens < 1:
            buckets[(what, who)] = [tokens, now]
            return False
        buckets[(what, who)] = [tokens - 1, now]
        return True


# ---------- the service ----------

MARKER_PATH = re.compile(r"/markers/([A-Za-z0-9-]{8,64})")
ROUTES = {"/info": ("GET",), "/state": ("GET",), "/position": ("POST",), "/leave": ("POST",), "/admin": ("GET", "POST")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fieldmap-share"
    timeout = 15                    # a request that does not arrive in this time is dropped

    def version_string(self):
        return self.server_version      # and not what runs it

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def log_request(self, code="-", size="-"):
        # Only what went wrong, and never what was sent: names and places stay out of the log.
        if isinstance(code, int) and code >= 400 and code != 503:
            sys.stderr.write("%s %s %s %s\n" % (self.log_date_time_string(), code, self.command, urlsplit(self.path).path[:80]))

    def send(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        # Something sent that was not read: the connection cannot be used for another request.
        if getattr(self, "unread", False):
            self.close_connection = True
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if data:                        # an answer without one is complete, and its reader may be gone
            self.wfile.write(data)

    def body(self, limit):
        """What was sent, as JSON; Refused when it is too large or not JSON at all."""
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise Refused(400, "Nothing was sent.")
        if length > limit:
            raise Refused(413, "That is too large.")
        if length <= 0:
            raise Refused(400, "Nothing was sent.")
        data = self.rfile.read(length)
        self.unread = False

        def no_constant(name):          # NaN and Infinity are not JSON
            raise ValueError(name)
        try:
            return json.loads(data, parse_constant=no_constant)
        except (ValueError, RecursionError):
            raise Refused(400, "That is not JSON.")

    def answer(self):
        self.unread = (self.headers.get("Content-Length") or "0").strip() != "0" or bool(self.headers.get("Transfer-Encoding"))
        try:
            self.send(*self.route())
        except Refused as e:
            self.send(e.status, {"error": e.text})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except (ConnectionError, TimeoutError):     # the caller is gone: nobody to answer
            raise
        except Exception:                           # a fault of the box's: say so, and stay up
            traceback.print_exc()
            self.send(500, {"error": "Something went wrong on the box."})

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def route(self):
        """Answers one request: (status, body)."""
        url = urlsplit(self.path)
        who = caller(self.headers)
        if who is None:
            raise Refused(403, "Only on the box's own network, or from a paired device.")
        if not allow("all", who, RATE_ALL):
            raise Refused(429, "Too fast. Try again in a few seconds.")
        marker = MARKER_PATH.fullmatch(url.path)
        methods = ("PUT", "DELETE") if marker else ROUTES.get(url.path)
        if methods is None or len(self.path) > 200:
            raise Refused(404, "Not here.")
        if self.command not in methods:
            raise Refused(405, "Not that way.")
        if url.query and url.path != "/state":
            raise Refused(400, "This address takes nothing after a question mark.")
        if url.path == "/admin":
            return self.admin(who)
        if url.path == "/info":
            return 200, {"version": VERSION, "enabled": on, "interval": INTERVAL, "stale": STALE}
        if not on:
            return 503, {"enabled": False}
        if url.path == "/state":
            since = re.fullmatch(r"since=([0-9]{1,18})", url.query)
            if url.query and not since:
                raise Refused(400, "since must be a rev the box gave out.")
            return 200, state(int(since.group(1)) if since else None)
        if url.path == "/position":
            post_position(clean_position(self.body(POSITION_BYTES)), who, who[0] == "outside")
            return 204, None
        if url.path == "/leave":
            body = self.body(POSITION_BYTES)
            leave(clean_id(body.get("uid") if isinstance(body, dict) else None))
            return 204, None
        # A marker: each change is written to the disk, so they are counted apart.
        if not allow("markers", who, RATE_MARKERS) or not allow("markers", "everybody", RATE_MARKERS_BOX):
            raise Refused(429, "Too many marker changes. Try again in a minute.")
        if self.command == "DELETE":
            return 200, {"rev": delete_marker(marker.group(1))}
        return 200, {"rev": put_marker(clean_marker(marker.group(1), self.body(MARKER_BYTES)))}

    def admin(self, who):
        """The Devices page: the switch, and who is sharing right now. Never from outside."""
        if who[0] == "outside":
            raise Refused(403, "Only on the box's own network.")
        if self.command == "POST":
            body = self.body(ADMIN_BYTES)
            if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
                raise Refused(400, "on must be true or false.")
            if not allow("switch", who, RATE_SWITCH):
                raise Refused(429, "Too fast. Try again in a minute.")
            pin = setting("REMOTE_PIN")
            if pin and not secrets.compare_digest(str(body.get("pin", "")).strip().encode(), pin.encode()):
                return 403, {"error": "That is not the box's code.", "pin": True}
            set_enabled(body["on"])
            print("Sharing switched " + ("on." if on else "off."), flush=True)
        with lock:
            return 200, {"service": "fieldmap-share", "version": VERSION, "enabled": on, "pin": bool(setting("REMOTE_PIN")),
                         "interval": INTERVAL, "stale": STALE, "people": people(), "markers": len(markers)}


class Server(ThreadingHTTPServer):
    """Answers at most MAX_BUSY requests at the same time; whoever comes on top is hung up on."""
    daemon_threads = True
    busy = threading.BoundedSemaphore(MAX_BUSY)

    def process_request(self, request, client_address):
        if not self.busy.acquire(blocking=False):
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.busy.release()

    def handle_error(self, request, client_address):
        # A caller that hung up before its answer was written: nothing to say.
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class SocketServer(Server):
    """The same server on a Unix socket (a file) instead of a network port."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


def serve(host, port):
    os.makedirs(RUN, exist_ok=True)
    os.chmod(SHARE, 0o700)
    os.chmod(RUN, 0o755)                # the web server and the gateway reach the socket in it
    load_settings()
    load_markers()
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    # Bound by its name from inside its folder: a socket's full path may be only about a
    # hundred characters long, and a clone can sit deeper than that.
    os.chdir(RUN)
    by_socket = SocketServer(os.path.basename(SOCKET), Handler)
    os.chmod(SOCKET, 0o666)             # the web server and the gateway run as other users
    print(f"Sharing service on {SOCKET}", flush=True)
    if port:
        by_port = Server((host, port), Handler)
        threading.Thread(target=by_port.serve_forever, daemon=True).start()
        print(f"...and on http://{host}:{port}/", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Sharing is " + ("on." if on else "off: switch it on on the Devices page.") + f" Shared markers: {len(markers)}.", flush=True)
    try:
        by_socket.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.unlink(SOCKET)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Live sharing for the Fieldmap Android app.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="answer the app and the Devices page")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="also listen on this port (the socket is always there)")
    sub.add_parser("status", help="say whether sharing is on, and how many markers are shared")
    args = parser.parse_args()

    if args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "status":
        try:
            with open(MARKERS) as f:
                count = len(json.load(f)["markers"])
        except (OSError, ValueError, KeyError, TypeError):
            count = 0
        print("Sharing:        " + ("on" if load_settings() else "off (the switch is on the Devices page)") + f"; shared markers: {count}")


if __name__ == "__main__":
    main()
