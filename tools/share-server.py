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
  PUT    /markers/<id>     a shared marker (a GeoJSON Feature with a Point), an arrow (a
                           LineString of two points, as the print tool makes them), or a route
                           (a LineString of up to 30 points, fieldmap.kind "route"); kept on disk
  DELETE /markers/<id>
  GET    /mesh             the server's mesh radio channel (a secret, see "The mesh radio channel")
  POST   /messages         a short text message, to one device (its uid) or to everybody ("*")
  GET    /messages?for=<uid>[,<uid>...][&since=<seq>]
                           the messages for those devices (and to everybody) after seq, which
                           of the messages they sent were delivered, and who has those they sent
                           to everybody ("groupDelivered": {id: [uids]})
  POST   /messages/delivered {"ids": [...], "by": "<uid>"}: these messages reached that device
  GET    /admin            for the Devices page: the switch, who is sharing right now
  POST   /admin            {"on": true|false}: the switch; {"newMeshKey": true}: a new channel key

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
import base64
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
MESH = os.path.join(SHARE, "mesh.json")             # the mesh radio channel: its key is a secret
MESSAGES = os.path.join(SHARE, "messages.json")     # messages waiting, and the receipts of delivered ones

VERSION = 1
INTERVAL = 5                    # seconds between a phone's posts and polls
STALE = 120                     # seconds after its last post at which a position is dropped
MAX_PEOPLE = 100                # positions at once
MAX_PER_PHONE = 4               # ...of which from one paired phone (one is normal)
MAX_PER_ADDRESS = 25            # ...and from one address on the home network
MAX_MARKERS = 2000
MAX_MARKER_BYTES = 4 << 20      # all the markers together
MAX_REMOVED = 1000              # how many removals are remembered for phones that were away
MESH_PRESET = "LONG_FAST"       # Meshtastic's default modem preset: the radios of other people relay us
MESH_HOPS = 3                   # Meshtastic's default hop limit
MESH_MOVING = 60                # seconds between positions over the mesh on the move...
MESH_STILL = 600                # ...and while staying in one place: the mesh is slow and shared
MESH_MOVE = 15                  # meters: a device counts as moving only when it went farther than
                                # this in MESH_MOVING seconds (GPS positions wander a little)
MAX_MESSAGE = 140               # characters of a message: with who sent it, one fits a mesh radio packet (233 bytes)
MESSAGE_DAYS = 7                # a message (and a receipt) is kept this long at most
MAX_MESSAGES = 5000
MAX_FOR = 8                     # devices one GET /messages may ask for (a relay asks for those it carries)
POSITION_BYTES = 2048           # body limits
MESSAGE_BYTES = 2048
MARKER_BYTES = 8192
ADMIN_BYTES = 1024
MAX_PROPERTIES = 32             # entries in a marker's properties, and in an object inside them
MAX_TEXT = 300                  # characters of a text in a marker
MAX_ROUTE_POINTS = 30           # points of a route: as many as one mesh radio packet carries
MAX_BUSY = 64                   # requests answered at the same time

# Per caller, as (a second, burst): every request; changing markers (each change is written
# to disk); the switch (the box's code must not be guessable by trying).
RATE_ALL = (10, 60)
RATE_MARKERS = (1, 60)
RATE_MARKERS_BOX = (4, 240)     # all callers together
RATE_SWITCH = (1 / 6, 10)
RATE_MESSAGES = (1, 30)         # messages sent (each is written to disk)

ID = re.compile(r"[A-Za-z0-9-]{8,64}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})")
FINGERPRINT = re.compile(r"[0-9a-f]{40}")
KEY = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
# Marks that turn the direction of the text around them: a name could hide what it says.
TURNING = set("‪‫‬‭‮⁦⁧⁨⁩")

lock = threading.RLock()
on = False                      # the switch; read from SETTINGS when the service starts
mesh = {}                       # the mesh radio channel, read from MESH (made when there is none)
messages = {"seq": 0, "list": [], "delivered": {}}  # read from MESSAGES; see "Messages"
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


# ---------- the mesh radio channel ----------

# Off the grid, the app shares over Meshtastic LoRa radios instead (one radio paired with each
# device). The devices of one server use one private channel: a name and a random AES-256
# key that only this server makes. A device takes them from here while it reaches the server
# (at home, or as a paired device from outside) and writes them into its radio; the radios
# then carry positions and markers between the devices with no server at all. The key is
# given out exactly as positions are: to the local network and to paired devices, and only
# while Location Sharing is enabled. "New Channel Key" on the Devices page makes another one,
# for a radio that was lost: each device picks it up the next time it reaches the server.

def new_mesh():
    """A new channel: a name of 9 characters (Meshtastic takes up to 11) and 32 random bytes."""
    return {"name": "FM-" + secrets.token_hex(3), "psk": base64.b64encode(secrets.token_bytes(32)).decode(),
            "rev": secrets.token_hex(6), "made": int(time.time())}


def load_mesh():
    """Reads the channel; makes one, and keeps it, when there is none or it cannot be read."""
    global mesh
    with lock:
        try:
            with open(MESH) as f:
                data = json.load(f)
            if not (isinstance(data, dict) and isinstance(data.get("name"), str) and len(base64.b64decode(data.get("psk", ""), validate=True)) == 32
                    and isinstance(data.get("rev"), str) and isinstance(data.get("made"), int)):
                raise ValueError("not a channel")
            mesh = data
        except (OSError, ValueError, TypeError):
            new_mesh_key()
        return mesh


def new_mesh_key():
    """Replaces the channel. Radios on the old one no longer hear the devices that took the new one."""
    global mesh
    with lock:
        made = new_mesh()
        write_private(MESH, json.dumps(made).encode())
        mesh = made
        return mesh


def channel():
    """The channel as it is now (read, or made, the first time it is needed)."""
    with lock:
        return mesh or load_mesh()


def mesh_channel():
    """What a device writes into its radio, and how often it sends a position through it."""
    c = channel()
    return {"version": 1, "name": c["name"], "psk": c["psk"], "preset": MESH_PRESET, "hopLimit": MESH_HOPS,
            "moving": MESH_MOVING, "still": MESH_STILL, "move": MESH_MOVE, "rev": c["rev"]}


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
        raise ValueError("A location must be a JSON object.")
    name = body.get("name")
    if not isinstance(name, str) or bad_text(name) or not 1 <= len(name.strip()) <= 24:
        raise ValueError("The name must have 1 to 24 characters.")
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
    """A shared marker as it is kept and given out: a GeoJSON Feature with a Point (a marker), a
    LineString of two points (an arrow) or of up to MAX_ROUTE_POINTS (a route, fieldmap.kind
    "route"), its id, and properties that are plain values; a route's points also in
    fieldmap.waypoints. ValueError in plain words otherwise."""
    if not isinstance(body, dict) or body.get("type") != "Feature":
        raise ValueError("A marker is a GeoJSON Feature.")
    if body.get("id") is not None and body["id"] != marker_id:
        raise ValueError("The marker's id is not the one in the address.")
    geometry = body.get("geometry")
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    raw = geometry.get("coordinates") if kind in ("Point", "LineString") else None

    def position(p):
        if not isinstance(p, list) or len(p) not in (2, 3):
            raise ValueError("A marker is a Point: [lon, lat]; an arrow or a route a LineString of such points.")
        out = [number(p[0], -180, 180, "lon"), number(p[1], -90, 90, "lat")]
        if len(p) == 3:
            out.append(number(p[2], -1000, 100000, "alt"))
        return out
    properties = body.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("A marker needs properties.")
    fieldmap = properties.get("fieldmap")
    route = isinstance(fieldmap, dict) and fieldmap.get("kind") == "route"
    # A marker is a point; an arrow (the print tool's) a line from its start to its head; a route
    # the line through its points.
    if kind == "LineString":
        most = MAX_ROUTE_POINTS if route else 2
        if not isinstance(raw, list) or not 2 <= len(raw) <= most:
            raise ValueError(f"A route is a LineString of 2 to {MAX_ROUTE_POINTS} points." if route
                             else "An arrow is a LineString of exactly two points.")
        coordinates = [position(p) for p in raw]
    else:
        coordinates = position(raw)
    # A route's points (the ones tapped for it) are the one list a marker may carry.
    waypoints = None
    if route and "waypoints" in fieldmap:
        raw_points = fieldmap["waypoints"]
        if kind != "LineString" or not isinstance(raw_points, list) or not 2 <= len(raw_points) <= MAX_ROUTE_POINTS:
            raise ValueError(f"A route's waypoints are 2 to {MAX_ROUTE_POINTS} points.")
        waypoints = [position(p) for p in raw_points]
        properties = {**properties, "fieldmap": {k: v for k, v in fieldmap.items() if k != "waypoints"}}
    properties = clean_properties(properties)
    if waypoints is not None:
        properties["fieldmap"]["waypoints"] = waypoints
    clean_id(properties.get("by"), "marker's \"by\"")
    clean_time(properties.get("updated"), "marker's \"updated\"")
    return {"type": "Feature", "id": marker_id, "geometry": {"type": kind, "coordinates": coordinates}, "properties": properties}


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
                raise Refused(507, "The server cannot accept more sharing devices. Try again later.")
            mine = sum(1 for p in positions.values() if p["caller"] == caller)
            if mine >= (MAX_PER_PHONE if outside else MAX_PER_ADDRESS):
                raise Refused(507, "This device sends too many location updates.")
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
        raise Refused(507, "The server could not save the change. Its disk may be full.")


def put_marker(feature):
    """Keeps a marker (the last one sent wins) and returns the new rev."""
    global rev
    marker_id, size = feature["id"], len(json.dumps(feature))
    with lock:
        if marker_id not in markers and len(markers) >= MAX_MARKERS:
            raise Refused(507, "The server cannot store more shared markers. Remove some markers first.")
        if sum(m["bytes"] for i, m in markers.items() if i != marker_id) + size > MAX_MARKER_BYTES:
            raise Refused(507, "The server cannot store more shared markers. Remove some markers first.")
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


# ---------- messages: on the disk, for a week at most ----------

# Short text messages between the devices: to one device (by its uid) or to everybody ("*").
# The server keeps a message until it is delivered (to one device), or for a week (to
# everybody, or never picked up), so that a device that is away gets it when it is back. A
# device asks for its own messages by its uid; a device that also has a mesh radio may ask for
# the devices it carries messages to over the mesh (a relay), and says when one reached them.
# Like positions, messages are for the people of this server: anybody who may share locations
# here may read them. The text is never written to the log.

def clean_message(body):
    if not isinstance(body, dict):
        raise ValueError("A message must be a JSON object.")
    text, name, to = body.get("text"), body.get("name"), body.get("to")
    if not isinstance(text, str) or bad_text(text) or not 1 <= len(text.strip()) <= MAX_MESSAGE:
        raise ValueError(f"A message has 1 to {MAX_MESSAGE} characters, on one line.")
    if not isinstance(name, str) or bad_text(name) or not 1 <= len(name.strip()) <= 24:
        raise ValueError("The name must have 1 to 24 characters.")
    if to != "*":
        clean_id(to, "recipient")
    return {"id": clean_id(body.get("id"), "message id"), "from": clean_id(body.get("from")), "name": name.strip(), "to": to,
            "text": text.strip(), "time": clean_time(body.get("time"))}


def load_messages():
    """Reads the messages kept on the disk. With no file (or one that cannot be read, which is
    put aside), there are none, and seq starts from the clock: a device that knew this server
    before then never holds a seq that it will come to again."""
    global messages
    with lock:
        try:
            with open(MESSAGES) as f:
                data = json.load(f)
            kept = [{**clean_message(m), "seq": int(m["seq"]), "at": float(m["at"])} for m in data["list"]]
            receipts = {clean_id(i, "id"): {"from": clean_id(r["from"]), "at": float(r["at"])} for i, r in data["delivered"].items()}
            seen = {clean_id(i, "id"): sorted({clean_id(u) for u in us}) for i, us in data.get("seen", {}).items()}
            messages = {"seq": int(data["seq"]), "list": kept, "delivered": receipts, "seen": seen}
        except FileNotFoundError:
            messages = {"seq": int(time.time()), "list": [], "delivered": {}, "seen": {}}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            print("The messages on the disk cannot be read (%s): put aside as messages.json.bad, starting empty." % e, flush=True)
            try:
                os.replace(MESSAGES, MESSAGES + ".bad")
            except OSError:
                pass
            messages = {"seq": int(time.time()), "list": [], "delivered": {}, "seen": {}}
        return messages


def save_messages():
    try:
        write_private(MESSAGES, json.dumps(messages).encode())
    except OSError as e:
        print("The messages could not be written: %s" % e, flush=True)
        raise Refused(507, "The server could not save the message. Its disk may be full.")


def fresh_messages():
    """The messages as they are now: read the first time, and without what is older than a week
    (messages nobody picked up, and receipts)."""
    if not messages["seq"]:
        load_messages()
    limit = time.time() - MESSAGE_DAYS * 86400
    messages["list"] = [m for m in messages["list"] if m["at"] >= limit]
    messages["delivered"] = {i: r for i, r in messages["delivered"].items() if r["at"] >= limit}
    kept = {m["id"] for m in messages["list"]}
    messages["seen"] = {i: us for i, us in messages.setdefault("seen", {}).items() if i in kept}
    return messages


def post_message(message):
    """Keeps a message and returns its seq. The same message twice (it came over the mesh too, or
    was sent again after a lost answer) is kept once."""
    with lock:
        box = fresh_messages()
        for m in box["list"]:
            if m["id"] == message["id"]:
                return m["seq"]
        if message["id"] in box["delivered"]:
            return box["seq"]
        if len(box["list"]) >= MAX_MESSAGES:
            raise Refused(507, "The server holds too many messages. Try again later.")
        box["seq"] += 1
        box["list"].append({**message, "seq": box["seq"], "at": time.time()})
        save_messages()
        return box["seq"]


def messages_for(uids, since=None):
    """The messages for these devices and to everybody after `since` (not their own), and the
    ids of the messages they sent that were delivered."""
    with lock:
        box = fresh_messages()
        wanted = set(uids)
        found = [{k: m[k] for k in ("seq", "id", "from", "name", "to", "text", "time")} for m in box["list"]
                 if (m["to"] in wanted or m["to"] == "*") and m["from"] not in wanted and (since is None or m["seq"] > since)]
        delivered = sorted(i for i, r in box["delivered"].items() if r["from"] in wanted)
        # Who has their messages to everybody: the uids that said so.
        group = {m["id"]: box["seen"].get(m["id"], []) for m in box["list"] if m["to"] == "*" and m["from"] in wanted and box["seen"].get(m["id"])}
        return {"seq": box["seq"], "messages": found, "delivered": delivered, "groupDelivered": group}


def deliver(ids, by):
    """These messages reached device `by`: one to that device is done with, and its sender learns it."""
    with lock:
        box = fresh_messages()
        done = [m for m in box["list"] if m["id"] in ids and m["to"] == by]
        for m in done:
            box["list"].remove(m)
            box["delivered"][m["id"]] = {"from": m["from"], "at": time.time()}
        # A message to everybody stays for the others; the server notes who has it.
        seen = [m for m in box["list"] if m["id"] in ids and m["to"] == "*" and m["from"] != by and by not in box["seen"].get(m["id"], [])]
        for m in seen:
            box["seen"].setdefault(m["id"], []).append(by)
        if done or seen:
            save_messages()


# ---------- the service ----------

MARKER_PATH = re.compile(r"/markers/([A-Za-z0-9-]{8,64})")
ROUTES = {"/info": ("GET",), "/state": ("GET",), "/position": ("POST",), "/leave": ("POST",), "/mesh": ("GET",), "/admin": ("GET", "POST"),
          "/messages": ("GET", "POST"), "/messages/delivered": ("POST",)}


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
            raise Refused(400, "The request has no content.")
        if length > limit:
            raise Refused(413, "The request is too large.")
        if length <= 0:
            raise Refused(400, "The request has no content.")
        data = self.rfile.read(length)
        self.unread = False

        def no_constant(name):          # NaN and Infinity are not JSON
            raise ValueError(name)
        try:
            return json.loads(data, parse_constant=no_constant)
        except (ValueError, RecursionError):
            raise Refused(400, "The request is not JSON.")

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
            self.send(500, {"error": "An error occurred on the server."})

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def route(self):
        """Answers one request: (status, body)."""
        url = urlsplit(self.path)
        who = caller(self.headers)
        if who is None:
            raise Refused(403, "Location sharing is available to local and paired devices only.")
        if not allow("all", who, RATE_ALL):
            raise Refused(429, "Too many requests. Try again in a few seconds.")
        marker = MARKER_PATH.fullmatch(url.path)
        methods = ("PUT", "DELETE") if marker else ROUTES.get(url.path)
        if methods is None or len(self.path) > 200:
            raise Refused(404, "Not found.")
        if self.command not in methods:
            raise Refused(405, "This method is not permitted for this address.")
        if url.query and url.path not in ("/state", "/messages"):
            raise Refused(400, "This address takes nothing after a question mark.")
        if url.path == "/admin":
            return self.admin(who)
        if url.path == "/info":
            # "mesh": the channel's rev, for a device to tell that it changed; never the key.
            return 200, {"version": VERSION, "enabled": on, "interval": INTERVAL, "stale": STALE, **({"mesh": channel()["rev"], "messages": True} if on else {})}
        if not on:
            return 503, {"enabled": False}
        if url.path == "/mesh":
            return 200, mesh_channel()
        if url.path == "/messages" and self.command == "GET":
            query = re.fullmatch(r"for=([A-Za-z0-9,-]{8,600})(&since=([0-9]{1,18}))?", url.query)
            uids = query.group(1).split(",") if query else []
            if not query or not 1 <= len(uids) <= MAX_FOR or not all(ID.fullmatch(u) for u in uids):
                raise Refused(400, f"Ask with for=<uid> (up to {MAX_FOR}, separated by commas) and since=<seq>.")
            return 200, messages_for(uids, int(query.group(3)) if query.group(3) else None)
        if url.path == "/messages":
            if not allow("messages", who, RATE_MESSAGES):
                raise Refused(429, "Too many messages. Try again in 1 minute.")
            return 200, {"seq": post_message(clean_message(self.body(MESSAGE_BYTES)))}
        if url.path == "/messages/delivered":
            body = self.body(MESSAGE_BYTES)
            ids = body.get("ids") if isinstance(body, dict) else None
            if not isinstance(ids, list) or not 1 <= len(ids) <= 50:
                raise Refused(400, "ids must be a list of 1 to 50 message ids.")
            deliver({clean_id(i, "message id") for i in ids}, clean_id(body.get("by")))
            return 204, None
        if url.path == "/state":
            since = re.fullmatch(r"since=([0-9]{1,18})", url.query)
            if url.query and not since:
                raise Refused(400, "since must be a rev the server gave out.")
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
            raise Refused(429, "Too many marker changes. Try again in 1 minute.")
        if self.command == "DELETE":
            return 200, {"rev": delete_marker(marker.group(1))}
        return 200, {"rev": put_marker(clean_marker(marker.group(1), self.body(MARKER_BYTES)))}

    def admin(self, who):
        """The Devices page: the switch, and who is sharing right now. Never from outside."""
        if who[0] == "outside":
            raise Refused(403, "Available on the local network only.")
        if self.command == "POST":
            body = self.body(ADMIN_BYTES)
            renew = isinstance(body, dict) and body.get("newMeshKey") is True
            if not isinstance(body, dict) or not (renew or isinstance(body.get("on"), bool)):
                raise Refused(400, "on must be true or false.")
            if not allow("switch", who, RATE_SWITCH):
                raise Refused(429, "Too many requests. Try again in 1 minute.")
            pin = setting("REMOTE_PIN")
            if pin and not secrets.compare_digest(str(body.get("pin", "")).strip().encode(), pin.encode()):
                return 403, {"error": "Incorrect access code.", "pin": True}
            if renew:
                new_mesh_key()
                print("New mesh radio channel key.", flush=True)
            else:
                set_enabled(body["on"])
                print("Location sharing " + ("enabled." if on else "disabled."), flush=True)
        with lock:
            return 200, {"service": "fieldmap-share", "version": VERSION, "enabled": on, "pin": bool(setting("REMOTE_PIN")),
                         "interval": INTERVAL, "stale": STALE, "people": people(), "markers": len(markers),
                         "mesh": {"made": channel()["made"]}}


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
    load_mesh()
    load_messages()
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
    print("Location sharing is " + ("enabled." if on else "disabled (enable it on the Devices page).") + f" Shared markers: {len(markers)}.", flush=True)
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
        print("Location Sharing:           " + ("Enabled" if load_settings() else "Disabled (Admin > Devices)") + f"; shared markers: {count}")


if __name__ == "__main__":
    main()
