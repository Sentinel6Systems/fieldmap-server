#!/usr/bin/env python3
"""fieldmap-server: remote access for the Fieldmap Android app.

A phone that was paired on the box's own network can use the box's maps from anywhere
afterwards; nobody else can. This service decides who is let in, and keeps the box
reachable from outside through its own router: no account, no outside service.

How it works:
  * The box is its own certificate authority (made by `init`, kept in data/remote/).
  * Pairing: on the box's network the app asks to be let in (`POST /enrol` with a public key
    whose private half never leaves the phone) and gets a certificate for that key. Only
    requests from the local network are answered (see lan()), and only while pairing is
    open: for ten minutes after someone switches remote access on, or presses "Pair a
    device", on the box's Devices page (phones/, at /phones/).
  * Off by default: until someone switches it on on the Remote access page (Admin, at
    /phones/remote.html: the same file as the Devices page), nothing outside can
    reach the box, the router is not asked for anything, and no phone can pair
    (set_enabled). Switched off again, it is all closed, and the paired phones are kept.
  * The app itself is kept on the box (scripts/app.sh, tools/app-file.py: data/app/), and devices
    on the local network download it from the Devices page (`GET /fieldmap.apk`): no app store.
  * From outside, the app connects to the gateway (an nginx that demands such a
    certificate, see gateway_conf()), which asks this service for every request whether the
    certificate is still on the list (`GET /check`). Removing a phone locks it out at once.
    Behind that check the gateway serves the maps and the app's routing data
    (routing_locations()), and passes the few addresses of live sharing on to its own
    service (tools/share-server.py, see share_locations()).
  * Reaching the gateway: every few minutes this service asks the home router to forward an
    outside port to it (UPnP or NAT-PMP, tools/portmap.py) and notes the box's IPv6
    addresses. The app learns these addresses whenever it is home, from `GET /info`.
  * The other way in, for a box behind an internet connection where no port can be opened
    (Starlink, a router nobody can change): NetBird (netbird.io). The owner types a setup key
    of a NetBird network on the Remote access page; the box joins that network (a container,
    see scripts/remote.sh netbird-apply and tools/netbird-run.sh) and gives the key to paired
    devices only, which then reach the same gateway, with the same certificate, over the
    NetBird network. See "NetBird" below.
  * One way in at a time (the owner's rule): the port on the router ("direct", the default) or
    NetBird. Choosing one closes the other: its router port or its NetBird client, and its
    rules in the box's firewall (set_way()).

Who may ask what:
  * through the box's web server, which puts the caller's address in X-Real-IP: only
    callers on the local network (see lan()), for /info, /devices, /pairing, /remove, /enrol;
  * with no X-Real-IP at all: the gateway (/check, and /gateway/netbird for a paired device)
    and programs on the box itself.

Python's standard library and the `openssl` and `ip` commands only.

  remote-server.py init [--prefix /maps/] [--upstream http://127.0.0.1:8080/] [--listen 8443] [--ipv6]
  remote-server.py serve [--port 8092] [--host 127.0.0.1]     also always on data/remote/run/remote.sock
  remote-server.py devices | remove <id> | pair | status

Settings in the environment (scripts/remote.sh reads them from .env):
  REMOTE_PIN         a code that pairing, opening pairing and removing a phone then ask for
  REMOTE_ADDRESS     host:port of the gateway from outside that you set up yourself (a port you
                     forwarded, a name of your own); offered to the phones before the others
  REMOTE_PORT        the gateway's port on this machine (scripts/remote.sh setup picks one at random)
  REMOTE_DIRECT      off: do not ask the router for anything
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portmap  # noqa: E402
import netbird_isolation as isolation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = os.environ.get("REMOTE_DIR") or os.path.join(ROOT, "data", "remote")
GATEWAY = os.path.join(REMOTE, "gateway")          # what the gateway container may read
RUN = os.path.join(REMOTE, "run")
SOCKET = os.path.join(RUN, "remote.sock")
CA_KEY = os.path.join(REMOTE, "ca.key")             # never leaves this folder
CA_CRT = os.path.join(REMOTE, "ca.crt")
DEVICES = os.path.join(REMOTE, "devices.json")
ADDRESS = os.path.join(REMOTE, "address")           # a relay's address (scripts/remote.sh, Tailscale)
REACH = os.path.join(REMOTE, "reach.json")          # what the router said last
NAME = os.path.join(GATEWAY, "name")                # the gateway's name in its certificate
SETTINGS = os.path.join(REMOTE, "settings.json")    # the Remote access page's switch; scripts/firewall.sh reads it too
NETBIRD = os.path.join(REMOTE, "netbird.json")      # NetBird as set up on the Remote access page (no key in it)
NETBIRD_DIR = os.path.join(REMOTE, "netbird")       # what the NetBird container reads and writes (tools/netbird-run.sh)
NETBIRD_KEY = os.path.join(NETBIRD_DIR, "setup-key")        # the secret: paired devices get it, nobody else
NETBIRD_CLIENT = os.path.join(NETBIRD_DIR, "client.env")    # on or off and the server, for scripts/remote.sh netbird-apply
NETBIRD_APPLY = os.path.join(NETBIRD_DIR, "apply-request")  # fieldmap-netbird.path runs scripts/remote.sh netbird-apply
NETBIRD_STATUS = os.path.join(NETBIRD_DIR, "status.json")   # `netbird status --json`, every few seconds
NETBIRD_STATUS_FOR = os.path.join(NETBIRD_DIR, "status.for")    # the apply-request that the running client answers
NETBIRD_PROBLEM = os.path.join(NETBIRD_DIR, "problem")      # why NetBird could not connect, if it could not
NETBIRD_CHECK = os.path.join(NETBIRD_DIR, "key-check")      # "unchecked": the box joined before, NetBird did not look at the new key
PAGE = os.path.join(ROOT, "phones")                 # the Devices page, and Admin's Remote access page
SHARED = os.path.join(ROOT, "ui")                   # the look and the header every page shares
APP_DIR = os.environ.get("APP_DIR") or os.path.join(ROOT, "data", "app")     # the app (scripts/app.sh)
APK = os.path.join(APP_DIR, "fieldmap.apk")
APP_INFO = os.path.join(APP_DIR, "app.json")
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml"}

MAX_DEVICES = 200
MAX_NAME = 40
ENROLS_PER_HOUR = 30
DEVICE_DAYS = 1825
SERVER_DAYS = 3650
CA_DAYS = 7300
PAIRING_SECONDS = 600           # how long "Pair a device" keeps pairing open
IDLE_DAYS = 90                  # switched on, with no phone from outside this long: off by itself
REACH_EVERY = 900               # how often the router is asked again
SEEN_EVERY = 600                # how often "last used from outside" is written down
NETBIRD_CLOUD = "https://api.netbird.io:443"    # NetBird's own management server (the client's default)
NETBIRD_FRESH = 45              # a status older than this many seconds: the NetBird client is not running
NETBIRD_START = 300             # how long a first start may take (it downloads NetBird)

lock = threading.Lock()
recent_enrols = []
pairing_until = 0.0
last_seen = {}                  # device id -> time it last came in from outside, not written yet
last_way = {}                   # device id -> {"direct" | "netbird": time}, not written yet
last_nb_address = {}            # device id -> the NetBird address it last came from, not written yet
isolation_lock = threading.Lock()


def stamp(seconds):
    """A moment for a text a person reads: as the server's clock writes it ("atText", for the app and
    the command line), and the moment itself ("at"), so that the page can show it in the browser's
    own time zone in place of atText."""
    return {"at": int(seconds), "atText": time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))}


def openssl(*args, stdin=None):
    """Runs openssl and returns what it printed; raises ValueError with its complaint."""
    done = subprocess.run(["openssl", *args], input=stdin, capture_output=True)
    if done.returncode != 0:
        raise ValueError(done.stderr.decode(errors="replace").strip() or "openssl failed")
    return done.stdout


def write_private(path, data):
    """Writes a file only its owner can read, all at once."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def fingerprints(cert_pem):
    """SHA-1 and SHA-256 of a certificate, as nginx writes them: lower-case hex, no colons."""
    der = openssl("x509", "-outform", "DER", stdin=cert_pem)
    return hashlib.sha1(der).hexdigest(), hashlib.sha256(der).hexdigest()


def random_name():
    """A name that says nothing: certificates are seen by whoever connects, and logged."""
    return "b" + secrets.token_hex(10)


def setting(name, default=""):
    return os.environ.get(name) or default


def gateway_port():
    try:
        return int(setting("REMOTE_PORT", "8443"))
    except ValueError:
        return 8443


# ---------- the box's own certificates ----------

def ready():
    return all(os.path.isfile(p) for p in (CA_KEY, CA_CRT, os.path.join(GATEWAY, "server.crt"), NAME))


def gateway_name():
    try:
        with open(NAME) as f:
            return f.read().strip()
    except OSError:
        return ""


def sign(request_pem, days, extensions, public_key=None):
    """A certificate from the box's authority for a signing request. With public_key, the
    certificate is for that key instead of the request's own."""
    with tempfile.TemporaryDirectory(dir=REMOTE) as tmp:
        ext = os.path.join(tmp, "ext")
        with open(ext, "w") as f:
            f.write(extensions)
        args = ["x509", "-req", "-sha256", "-days", str(days), "-CA", CA_CRT, "-CAkey", CA_KEY,
                "-set_serial", "0x" + secrets.token_hex(16), "-extfile", ext]
        if public_key is not None:
            pub = os.path.join(tmp, "pub")
            with open(pub, "wb") as f:
                f.write(public_key)
            args += ["-force_pubkey", pub]
        return openssl(*args, stdin=request_pem)


def init(prefix, upstream, listen, ipv6):
    """Makes the certificate authority, the gateway's certificate and the gateway's
    configuration, each only if it is not there yet (the configuration always)."""
    for folder in (REMOTE, GATEWAY, os.path.join(GATEWAY, "nginx"), RUN):
        os.makedirs(folder, exist_ok=True)
    os.chmod(REMOTE, 0o700)
    os.chmod(GATEWAY, 0o755)
    if not os.path.isfile(CA_KEY) or not os.path.isfile(CA_CRT):
        write_private(CA_KEY, openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout"))
        crt = openssl("req", "-x509", "-new", "-key", CA_KEY, "-sha256", "-days", str(CA_DAYS),
                      "-subj", "/CN=" + random_name(),
                      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                      "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        with open(CA_CRT, "wb") as f:
            f.write(crt)
        print("Made this box's certificate authority.")
    server_key = os.path.join(GATEWAY, "server.key")
    server_crt = os.path.join(GATEWAY, "server.crt")
    if not all(os.path.isfile(p) for p in (server_key, server_crt, NAME)):
        name = random_name()
        write_private(server_key, openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout"))
        request = openssl("req", "-new", "-key", server_key, "-subj", "/CN=" + name)
        crt = sign(request, SERVER_DAYS, f"subjectAltName=DNS:{name}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n")
        with open(server_crt, "wb") as f:
            f.write(crt)
        with open(NAME, "w") as f:
            f.write(name + "\n")
        print("Made the gateway's certificate.")
    with open(CA_CRT, "rb") as src, open(os.path.join(GATEWAY, "ca.crt"), "wb") as dst:
        dst.write(src.read())
    if not os.path.isfile(DEVICES):
        save_devices([])
    with open(os.path.join(GATEWAY, "nginx", "default.conf"), "w") as f:
        f.write(gateway_conf(prefix, upstream, listen, ipv6))
    print("Wrote the gateway's configuration.")


# What the app asks the tile server for, and nothing else: the list of styles, a style, its
# sprites, fonts and data tiles. Not the pictures the server draws (they are slow to make
# and the app does not use them), not its own pages, not the print tool or the map builder.
APP_PATHS = (r"(styles\.json|styles/[A-Za-z0-9_.-]+/style\.json|styles/[A-Za-z0-9_.-]+/sprite(@[23]x)?\.(json|png)"
             r"|fonts/[^/]+/[0-9]+-[0-9]+\.pbf|data/[A-Za-z0-9_.-]+\.json"
             r"|data/[A-Za-z0-9_.-]+/[0-9]+/[0-9]+/[0-9]+\.(pbf|png|jpg|jpeg|webp))")


# Live sharing between the apps (tools/share-server.py), at /mapshare/ as on the box's own web
# server: these addresses and methods and nothing else, each with the most it may be sent.
SHARE_PATHS = (("= /mapshare/info", "GET", "1k"), ("= /mapshare/state", "GET", "1k"),
               ("= /mapshare/position", "POST", "2k"), ("= /mapshare/leave", "POST", "2k"), ("= /mapshare/mesh", "GET", "1k"),
               ("= /mapshare/messages", "GET|POST", "2k"), ("= /mapshare/messages/delivered", "POST", "2k"),
               ('~ "^/mapshare/markers/[A-Za-z0-9-]{8,64}$"', "PUT|DELETE", "8k"))


def share_locations():
    """The gateway's part for live sharing. The sharing service is told which certificate asks
    (after the device check), and never an address: only the box's own web server gives it one."""
    out = ""
    for match, methods, most in SHARE_PATHS:
        out += f"""    location {match} {{
        if ($request_method !~ ^({methods})$) {{ return 405; }}
        auth_request /_device;
        limit_req zone=fieldmap_req burst=400 nodelay;
        limit_req zone=fieldmap_share burst=40 nodelay;
        limit_req_status 429;
        client_max_body_size {most};
        rewrite ^/mapshare(/.*)$ $1 break;
        proxy_pass http://unix:/run/fieldmap-share/share.sock;
        proxy_set_header X-Real-IP "";
        proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;
        proxy_read_timeout 15;
        error_page 502 504 = @no_share;
    }}
"""
    return out + """    location /mapshare/ { return 404; }
    location @no_share { return 404; }
"""


# The app's routing data (scripts/build-routing.sh), at /routing/ as on the box's own web server:
# the index and BRouter's files, read from data/routing (mounted into the gateway's container),
# and nothing else of that folder.
# The index changes with every build under the same name: asked anew each time.
ROUTING_PATHS = (("= /routing/index.json", '        add_header Cache-Control "no-cache";\n'),
                 (r'~ "^/routing/[EW][0-9]{1,3}_[NS][0-9]{1,2}\.rd5$"', ""))


def routing_locations():
    """The gateway's part for the routing data: files only, for paired devices only."""
    out = ""
    for match, extra in ROUTING_PATHS:
        out += f"""    location {match} {{
        if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
        auth_request /_device;
        root /usr/share/nginx;
{extra}    }}
"""
    return out + """    location /routing/ { return 404; }
"""


def gateway_conf(prefix, upstream, listen, ipv6=False):
    """nginx for the remote gateway: TLS with a certificate demanded from the visitor, every
    request checked against the device list, and only what the app needs behind it."""
    if not prefix.startswith("/") or not prefix.endswith("/") or not re.fullmatch(r"[A-Za-z0-9/_.-]+", prefix):
        raise ValueError("the prefix must look like / or /maps/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+/", upstream):
        raise ValueError("the upstream must look like http://127.0.0.1:8080/")
    if not re.fullmatch(r"([0-9.]+:)?[0-9]{2,5}", listen):
        raise ValueError("listen must be a port, or address:port")
    port = listen.split(":")[-1]
    six = f"    listen [::]:{port} ssl;\n" if ipv6 and ":" not in listen else ""
    allowed = "^" + re.escape(prefix).replace("\\/", "/") + APP_PATHS + "$"
    nothing_else = "" if prefix == "/" else "    location / { return 404; }\n"
    return f"""# fieldmap-server: the remote gateway for the Fieldmap Android app.
# Written by tools/remote-server.py init (scripts/remote.sh setup); do not edit, run that again.

# The app asks for the maps under the address it uses at home and says so in these two
# headers, so that the tile server writes the same addresses into its answers as it does on
# the local network. Anything that does not look like a host name is ignored.
map $http_x_fieldmap_host $fieldmap_host {{
    default $host;
    "~^[A-Za-z0-9._:\\[\\]-]+$" $http_x_fieldmap_host;
}}
map $http_x_fieldmap_proto $fieldmap_proto {{
    default http;
    https   https;
}}

# Per visitor: at most 30 connections, and requests at 30 a second with room for the
# hundreds of tiles a phone asks for at once when the map moves.
limit_conn_zone $binary_remote_addr zone=fieldmap_conn:2m;
limit_req_zone  $binary_remote_addr zone=fieldmap_req:4m rate=30r/s;
# Per paired phone, for live sharing: a position and a look at the others every few seconds.
limit_req_zone  $ssl_client_fingerprint zone=fieldmap_share:1m rate=5r/s;

server {{
    listen {listen} ssl;
{six}    http2 on;
    server_name _;
    server_tokens off;

    ssl_certificate         /etc/fieldmap-remote/server.crt;
    ssl_certificate_key     /etc/fieldmap-remote/server.key;
    ssl_protocols           TLSv1.2 TLSv1.3;
    # No visitor gets past the handshake without a certificate this box issued...
    ssl_client_certificate  /etc/fieldmap-remote/ca.crt;
    ssl_verify_client       on;

    limit_conn fieldmap_conn 30;
    limit_req  zone=fieldmap_req burst=400 nodelay;

    # ...and none gets an answer unless its certificate is still on the device list.
    location = /_device {{
        internal;
        proxy_pass http://unix:/run/fieldmap-remote/remote.sock:/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;
        # The visitor's address, only to tell the two ways in apart (the router's port, NetBird).
        proxy_set_header X-Fieldmap-Peer $remote_addr;
    }}

    # NetBird, the other way in: a paired device asks here what it needs to join the NetBird
    # network of this box (the setup key among it). The service itself checks the certificate
    # against the device list, and takes this address from the gateway only.
    location = /mapremote/netbird {{
        if ($request_method != GET) {{ return 405; }}
        proxy_pass http://unix:/run/fieldmap-remote/remote.sock:/gateway/netbird;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Real-IP "";
        proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;
    }}
    location /mapremote/ {{ return 404; }}

    location {prefix} {{
        if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
        if ($uri !~ "{allowed}") {{ return 404; }}
        auth_request /_device;
        proxy_pass {upstream};
        proxy_set_header Host $fieldmap_host;
        proxy_set_header X-Forwarded-Proto $fieldmap_proto;
        proxy_read_timeout 120;
    }}

{share_locations()}
{routing_locations()}
{nothing_else}}}
"""


# ---------- the device list ----------

def load_devices():
    try:
        with open(DEVICES) as f:
            return json.load(f).get("devices", [])
    except (OSError, ValueError):
        return []


def save_devices(devices):
    write_private(DEVICES, json.dumps({"devices": devices}, indent=1).encode())


def clean_name(name):
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    return name[:MAX_NAME] or "Device"


def enrol(name, public_key_pem):
    """Puts a device on the list and returns its entry. The same key twice gives the same entry."""
    public_key = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
    try:
        described = openssl("pkey", "-pubin", "-noout", "-text", stdin=public_key).decode(errors="replace")
        der = openssl("pkey", "-pubin", "-outform", "DER", stdin=public_key)
    except ValueError:
        raise ValueError("The device did not send a correct public key.")
    if "prime256v1" not in described:
        raise ValueError("The key must be an elliptic-curve key on the P-256 curve.")
    key_id = hashlib.sha256(der).hexdigest()
    with lock:
        devices = load_devices()
        for d in devices:
            if d.get("key") == key_id:
                return d
        if len(devices) >= MAX_DEVICES:
            raise PermissionError("The server cannot pair more devices. Remove a device on the Devices page first.")
        now = time.time()
        recent_enrols[:] = [t for t in recent_enrols if now - t < 3600]
        if len(recent_enrols) >= ENROLS_PER_HOUR:
            raise PermissionError("Too many pairing requests in the last hour. Try again later.")
        recent_enrols.append(now)
        device_id = secrets.token_hex(4)
        # The certificate is for the phone's key; the signing request is only a carrier.
        throwaway = openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout")
        with tempfile.TemporaryDirectory(dir=REMOTE) as tmp:
            key = os.path.join(tmp, "key")
            write_private(key, throwaway)
            request = openssl("req", "-new", "-key", key, "-subj", "/CN=" + random_name())
        cert = sign(request, DEVICE_DAYS, "extendedKeyUsage=clientAuth\nbasicConstraints=CA:FALSE\n", public_key=public_key)
        sha1, sha256 = fingerprints(cert)
        device = {"id": device_id, "name": clean_name(name), "added": datetime.date.today().isoformat(), "addedAt": int(time.time()),
                  "key": key_id, "sha1": sha1, "sha256": sha256, "certificate": cert.decode()}
        devices.append(device)
        save_devices(devices)
        return device


def remove(device_id):
    with lock:
        devices = load_devices()
        kept = [d for d in devices if d.get("id") != device_id]
        if len(kept) == len(devices):
            return False
        save_devices(kept)
        last_seen.pop(device_id, None)
        last_way.pop(device_id, None)
        last_nb_address.pop(device_id, None)
        return True


def listed(fingerprint):
    """The paired device this certificate belongs to, or None."""
    fingerprint = (fingerprint or "").strip().lower().replace(":", "")
    if not fingerprint:
        return None
    for d in load_devices():
        if secrets.compare_digest(fingerprint, d.get("sha1", "")):
            return d
    return None


def allowed(fingerprint, peer=None):
    """True when the certificate is on the list; notes when that device came in from outside,
    and by which way: `peer` is the address the gateway saw it come from."""
    d = listed(fingerprint)
    if d is None:
        return False
    now = int(time.time())
    last_seen[d["id"]] = now
    via_netbird = from_netbird(peer)
    last_way.setdefault(d["id"], {})["netbird" if via_netbird else "direct"] = now
    if via_netbird:
        # The certificate proves the device, the address points to its NetBird peer: network
        # isolation moves that peer into this server's device group (netbird_isolation.migrate).
        last_nb_address[d["id"]] = isolation.valid_address(peer)
    return True


def write_seen():
    """Writes down when each device last came in from outside (kept in memory in between)."""
    if not last_seen:
        return
    with lock:
        devices = load_devices()
        for d in devices:
            if d["id"] in last_seen:
                d["seen"] = last_seen[d["id"]]
                d["way"] = {**d.get("way", {}), **last_way.get(d["id"], {})}
                if last_nb_address.get(d["id"]):
                    d["netbirdAddress"] = last_nb_address[d["id"]]
        last_seen.clear()
        last_way.clear()
        last_nb_address.clear()
        save_devices(devices)


def newest(way):
    """When a device last came in from outside by this way ("direct", "netbird"); 0 if never.
    A device that came in before the box knew two ways came in by the router's port."""
    times = [w.get(way, 0) for w in last_way.values()]
    for d in load_devices():
        times.append(d.get("way", {}).get(way, 0))
        if way == "direct" and "way" not in d:
            times.append(d.get("seen", 0))
    return max(times, default=0)


# ---------- the app, for devices to install ----------

def app():
    """The app the box offers (scripts/app.sh): {"version", "size", "sha256", "source", "added"}, or None."""
    try:
        with open(APP_INFO) as f:
            info = json.load(f)
        if not isinstance(info, dict) or os.path.getsize(APK) != info.get("size"):
            return None
        version = re.sub(r"[^A-Za-z0-9._+-]", "", str(info.get("version", "")))[:32] or "?"
        return {"version": version, "size": info["size"], "sha256": str(info.get("sha256", ""))[:64],
                "source": "by hand" if info.get("source") == "by hand" else "release", "added": str(info.get("added", ""))[:10]}
    except (OSError, ValueError, TypeError):
        return None


# ---------- the switch ----------

def load_settings():
    """The Remote access page's settings: "enabled" (off by default), "autoOff" (switch off by itself
    after IDLE_DAYS with no phone from outside; on by default), "since" (when it was switched
    on), "offReason" ("idle" when it switched itself off), "way" (the one way in from outside:
    "direct", the router's port, by default, or "netbird") and "netbird" (NetBird is wanted
    now; scripts/firewall.sh reads "way" and "netbird" to open the gateway's port to the
    world, or on NetBird's interface only)."""
    try:
        with open(SETTINGS) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = {**load_settings(), **changes}
    write_private(SETTINGS, json.dumps(data).encode())
    os.chmod(SETTINGS, 0o644)       # the firewall reads it as root; nothing in it is secret
    return data


def enabled():
    """Remote access on or off (the default), as the Remote access page's switch says."""
    return load_settings().get("enabled") is True


def auto_off():
    return load_settings().get("autoOff", True) is not False


WAYS = ("direct", "netbird")


def way():
    """The one way in from outside the owner chose: "direct" (the router's port; the default)
    or "netbird"."""
    return "netbird" if load_settings().get("way") == "netbird" else "direct"


def direct():
    """The router's port is the way in. Not so: the router is not asked, a port it opened is
    closed, and the box's firewall lets nobody from outside reach the gateway's port but
    through NetBird."""
    return way() == "direct"


def idle_since():
    """When remote access was last used: the newest of a device coming in from outside and
    the moment it was switched on."""
    seen = [d.get("seen", 0) for d in load_devices()] + list(last_seen.values())
    return max(seen + [load_settings().get("since", 0)])


def auto_off_at():
    """When it will switch itself off if nothing comes in from outside until then; 0 if it will not."""
    if not (enabled() and auto_off()):
        return 0
    return int(idle_since() + IDLE_DAYS * 86400)


def switch_off_if_idle(now=None):
    at = auto_off_at()
    if at and (now or time.time()) >= at:
        set_enabled(False, reason="idle")
        print(f"Remote access switched itself off: no device came in from outside for {IDLE_DAYS} days.", flush=True)
        return True
    return False


def set_enabled(on, reason=""):
    """Off is complete isolation: no pairing, no device let in, the router's port closed, and the
    box's own firewall shuts the gateway's port (scripts/firewall.sh reads this file, and
    fieldmap-firewall.path applies it as soon as it changes). The paired phones are kept for
    when it is switched on again. Switching on also opens pairing: one press, then the phone."""
    if on:
        save_settings(enabled=True, since=int(time.time()), offReason="")
    else:
        save_settings(enabled=False, offReason=reason)
    if on:
        open_pairing()
        threading.Thread(target=check_router, daemon=True).start()
    else:
        close_pairing()
        threading.Thread(target=release_router, daemon=True).start()
    netbird_follow()                # NetBird runs only while remote access is on


def set_way(new):
    """Chooses the one way in (the Remote access page). The other way is closed at once: to
    NetBird, the router's port is given back and the firewall closes it to the world; to the
    router's port, NetBird's client is stopped and the firewall closes its interface. NetBird
    needs a setup key first (the box keeps it when the way changes back)."""
    if new not in WAYS:
        raise ValueError("Unknown remote connectivity method.")
    if new == "netbird" and not netbird_key():
        raise ValueError("Enter a setup key first.")
    was = way()
    save_settings(way=new)
    if enabled() and was != new:
        if new == "netbird":
            threading.Thread(target=release_router, args=("direct-off",), daemon=True).start()
        else:
            threading.Thread(target=check_router, daemon=True).start()
    netbird_follow()


def release_router(problem="switched-off"):
    state = load_reach()
    try:
        portmap.release(state)
    except Exception as e:          # the router lets the port lapse by itself within two hours
        print("Closing the router's port failed: %s" % e, flush=True)
    write_private(REACH, json.dumps({"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": problem}).encode())


# ---------- pairing ----------

def pairing_left():
    return max(0, int(pairing_until - time.time()))


def open_pairing(seconds=PAIRING_SECONDS):
    global pairing_until
    if enabled():
        pairing_until = time.time() + seconds


def close_pairing():
    global pairing_until
    pairing_until = 0.0


# ---------- reaching the gateway from outside ----------

def load_reach():
    try:
        with open(REACH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def forwarding():
    """Forwarding by hand (the Remote access page): {"manual": bool, "address": the box's internet address}."""
    f = load_settings().get("forwarding") or {}
    return {"manual": f.get("manual") is True, "address": str(f.get("address") or "")}


def check_router():
    """Asks the router again (tools/portmap.py) and writes down what it said."""
    if not enabled():
        state = {"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": "switched-off"}
    elif not direct():
        state = {"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": "direct-off"}
    elif forwarding()["manual"]:
        # Forwarded by hand: the router is not asked; the box's IPv6 addresses still count.
        state = {"checked": int(time.time()), "ipv4": {}, "ipv6": {"addresses": portmap.global_ipv6()[:2]}, "problem": "manual"}
    elif setting("REMOTE_DIRECT").lower() == "off":
        state = {"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": "off"}
    else:
        state = portmap.reach(gateway_port(), load_reach())
    write_private(REACH, json.dumps(state, indent=1).encode())
    return state


def manual_address():
    """Addresses set up by hand: REMOTE_ADDRESS, then a relay's (data/remote/address). With the
    router's port switched off as a way in, only the relay's."""
    out = []
    texts = [setting("REMOTE_ADDRESS") if direct() else ""]
    try:
        with open(ADDRESS) as f:
            texts.append(f.read())
    except OSError:
        pass
    for text in texts:
        m = re.fullmatch(r"\s*([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\]):([0-9]{1,5})\s*", text or "")
        if m:
            out.append({"host": m.group(1), "port": int(m.group(2))})
    f = forwarding()
    if direct() and f["manual"] and f["address"]:
        out.append({"host": f["address"], "port": gateway_port()})
    return out


ADDRESS_RE = re.compile(r"^([A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*|[0-9.]{7,15}|\[?[0-9A-Fa-f:]{2,39}\]?)$")


def clean_address(text):
    """The box's internet address as typed: a name or an IPv4 or IPv6 address, or "" if it is not one."""
    text = str(text or "").strip()
    text = re.sub(r"^[a-z]+://", "", text).split("/")[0]
    if not ADDRESS_RE.match(text):
        return ""
    if ":" in text.strip("[]"):
        return "[" + text.strip("[]") + "]"
    return text


def set_forwarding(manual, address=None):
    """Switches between the router opening the port by itself and forwarding by hand. Going to
    by hand, the port the router opened by itself is closed again."""
    current = forwarding()
    if address is None:
        address = current["address"]
    save_settings(forwarding={"manual": bool(manual), "address": clean_address(address)})
    if manual and not current["manual"] and enabled():
        state = load_reach()
        try:
            portmap.release(state)
        except Exception as e:
            print("Closing the router's port failed: %s" % e, flush=True)
    if enabled():
        threading.Thread(target=check_router, daemon=True).start()


def free_port():
    """A port between 20000 and 60999 that nothing on this machine uses."""
    import random as _random
    for _ in range(200):
        port = _random.randint(20000, 60999)
        s = socket.socket()
        try:
            s.bind(("", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise ValueError("No free port found.")


ENV = os.path.join(ROOT, ".env")
APPLY = os.path.join(REMOTE, "apply-request")     # fieldmap-remote-apply.path runs remote.sh setup


def new_port():
    """Gives the gateway a new random port: written to .env, then applied by remote.sh setup,
    which fieldmap-remote-apply.path runs (as root) when APPLY changes. This service is
    restarted by it."""
    port = free_port()
    try:
        with open(ENV) as f:
            lines = [l for l in f.read().splitlines() if not l.startswith("REMOTE_PORT=")]
    except OSError:
        lines = []
    tmp = ENV + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines + ["REMOTE_PORT=%d" % port]) + "\n")
    os.replace(tmp, ENV)
    save_settings(changingPort=port)
    write_private(APPLY, str(int(time.time())).encode())
    os.chmod(APPLY, 0o644)
    return port


def endpoints():
    if not enabled():
        return []
    seen, out = set(), []
    for e in manual_address() + portmap.endpoints(load_reach(), gateway_port()):
        key = (e["host"], e["port"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


PROBLEMS = {
    "router-closed": "The router did not open a port. Enable UPnP or NAT-PMP on the router, or use Manual Port "
                     "Forwarding below.",
    "router-refused": "The router refused the port mapping. A second router or modem may be in front of it, or its "
                      "UPnP settings exclude this server. Use Manual Port Forwarding below.",
    "double-nat": "Two routers are in line (double NAT). Forward the port on the provider's router too, or set it "
                  "to bridge mode.",
    "shared-address": "The internet provider shares one public IP address among many customers (CGNAT, e.g. "
                      "Starlink or cellular). Port forwarding cannot work over IPv4: use NetBird VPN.",
    "off": "Port mapping is disabled (REMOTE_DIRECT=off).",
    "manual": "",
    "direct-off": "Method in use: NetBird VPN. Port forwarding is disabled.",
    "switched-off": "Remote connectivity is disabled. Devices can connect on the local network only.",
    "switched-off-idle": "Remote connectivity was disabled automatically after %d days without a remote connection. "
                         "Enable it to allow remote devices again." % IDLE_DAYS,
}


def report():
    """For the Remote access page and the app: can devices reach the box from outside through the
    router's port, and if not, why. (NetBird, the other way in: netbird_report().)"""
    state = load_reach()
    newest_seen = newest("direct")
    manual = manual_address()
    v6 = state.get("ipv6", {}).get("addresses", [])
    if not enabled():
        idle = load_settings().get("offReason") == "idle"
        return {"status": "off", "text": PROBLEMS["switched-off-idle" if idle else "switched-off"], "checked": state.get("checked", 0)}
    if not direct():
        return {"status": "off", "text": PROBLEMS["direct-off"], "checked": 0}
    if state.get("problem") in ("direct-off", "switched-off"):
        # The way changed back a moment ago: the router is being asked now.
        return {"status": "unknown", "text": "Retrying port mapping…", "checked": 0}
    if newest_seen and time.time() - newest_seen < 7 * 86400:
        at = stamp(newest_seen)
        return {"status": "confirmed", "text": "Last remote connection via port forwarding: %s." % at["atText"], **at,
                "checked": state.get("checked", 0)}
    elif forwarding()["manual"] and not forwarding()["address"]:
        status, text = "blocked", "Forward the port on the router, then enter the public IP address below."
    elif forwarding()["manual"]:
        status, text = "expected", "Manual port forwarding is set. Not yet confirmed by a remote connection."
    elif manual or state.get("ipv4", {}).get("ok"):
        status, text = "expected", "The router opened the port. Not yet confirmed by a remote connection."
    elif v6:
        status, text = "maybe", PROBLEMS.get(state.get("problem"), "") + " The server has a public IPv6 address, so devices with IPv6 may still connect."
    elif state:
        status, text = "blocked", PROBLEMS.get(state.get("problem"), "Remote devices cannot connect to the server.")
        said = state.get("ipv4", {}).get("error")
        if said and state.get("problem") == "router-refused":
            text += " Router response: %s." % said
    else:
        status, text = "unknown", "No port mapping attempt yet."
    return {"status": status, "text": text.strip(), "checked": state.get("checked", 0)}


# ---------- NetBird, the other way in ----------
#
# For a box whose router cannot open a port. The owner makes a NetBird network (an account on
# NetBird's cloud, or an own server) and types one reusable setup key here. The box joins with
# it, and hands it to paired devices, which join too and then reach the gateway at the box's
# NetBird address. NetBird is only the road: the gateway asks for the same certificate.
#
# Who does what:
#   * this service keeps the settings (NETBIRD), the key (NETBIRD_KEY) and says what is wanted
#     (NETBIRD_CLIENT), then touches NETBIRD_APPLY;
#   * fieldmap-netbird.path sees that and runs `scripts/remote.sh netbird-apply` as root, which
#     starts or removes the container "fieldmap-netbird" (NetBird's own image);
#   * in the container, tools/netbird-run.sh joins and writes NETBIRD_STATUS every few seconds,
#     and NETBIRD_PROBLEM when it cannot join;
#   * scripts/firewall.sh lets in, on NetBird's interface, the gateway's port and nothing else.
# The key is given out in two places only, both to a paired device: the answer to pairing, and
# /gateway/netbird behind the gateway's certificate check. It is never printed or logged.

def load_netbird():
    """{"on": the owner connected it, "management": the server, "salt", "address": the box's
    address in the NetBird network, "addressFor": the server that address is from}."""
    try:
        with open(NETBIRD) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_netbird(**changes):
    data = {**load_netbird(), **changes}
    write_private(NETBIRD, json.dumps(data).encode())
    return data


def netbird_key():
    try:
        with open(NETBIRD_KEY) as f:
            return f.read().strip()
    except OSError:
        return ""


def clean_management(text):
    """The management server as typed, as scheme://host:port; "" if it is not an address.
    Without a scheme it is https; without a port, the scheme's own."""
    text = str(text or "").strip().rstrip("/")
    if not text:
        return NETBIRD_CLOUD
    if "://" not in text:
        text = "https://" + text
    m = re.fullmatch(r"(https?)://([A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:]{2,39}\])(:([0-9]{1,5}))?", text)
    if not m:
        return ""
    port = int(m.group(5) or (443 if m.group(1) == "https" else 80))
    if not 0 < port < 65536:
        return ""
    return "%s://%s:%d" % (m.group(1), m.group(2).lower(), port)


def clean_key(text):
    """A setup key as typed: NetBird's are 36 letters, digits and dashes. "" if it is not one."""
    text = re.sub(r"\s+", "", str(text or ""))
    return text if re.fullmatch(r"[A-Za-z0-9-]{16,128}", text) else ""


def netbird_on():
    """NetBird is the way in the owner chose (and a key is there)."""
    return way() == "netbird" and bool(netbird_key())


def netbird_wanted():
    """NetBird runs only while remote access is switched on."""
    return enabled() and netbird_on()


def install_id():
    """This installation's id in NetBird, made once: the server's peer is named fieldmap-server-<id>,
    and network isolation names its groups and policy after it. An id that network isolation
    recorded already is kept."""
    nb = load_netbird()
    found = (nb.get("isolation") or {}).get("id") or nb.get("installId")
    if not (isinstance(found, str) and re.fullmatch(r"[0-9a-f]{6,20}", found)):
        found = isolation.new_id()
    if nb.get("installId") != found:
        save_netbird(installId=found)
    return found


def netbird_follow():
    """Tells the rest of the box what is wanted now: the firewall (through the settings file),
    and the container (through scripts/remote.sh netbird-apply)."""
    nb = load_netbird()
    wanted = netbird_wanted()
    if load_settings().get("netbird", False) is not wanted:
        save_settings(netbird=wanted)
    if not nb.get("management") and not os.path.isfile(NETBIRD_CLIENT):
        return                      # never set up on this box: nothing to start or stop
    os.makedirs(NETBIRD_DIR, exist_ok=True)
    os.chmod(NETBIRD_DIR, 0o700)
    write_private(NETBIRD_CLIENT, ("NB_MANAGEMENT_URL=%s\nFIELDMAP_NETBIRD=%s\nFIELDMAP_NETBIRD_NAME=%s\n" % (
        nb.get("management") or NETBIRD_CLOUD, "on" if wanted else "off", isolation.names(install_id())["server"])).encode())
    write_private(NETBIRD_APPLY, secrets.token_hex(8).encode())    # a new name for this request
    os.chmod(NETBIRD_APPLY, 0o644)


def set_netbird(management=None, key=None):
    """Keeps a new key or server for NetBird (the Remote access page's Connect) and makes
    NetBird the way in. The key and the box's place in the NetBird network are kept also when
    the way changes back to the router's port, for the next time."""
    nb = load_netbird()
    changes = {}
    if management and management != nb.get("management"):
        changes.update(management=management, address="", addressFor="")
    if "salt" not in nb:
        changes["salt"] = secrets.token_hex(16)
    os.makedirs(NETBIRD_DIR, exist_ok=True)
    os.chmod(NETBIRD_DIR, 0o700)
    if key:
        write_private(NETBIRD_KEY, (key + "\n").encode())
    for stale in (NETBIRD_PROBLEM, NETBIRD_CHECK):      # what was wrong may be right now
        try:
            os.unlink(stale)
        except OSError:
            pass
    save_netbird(**changes)
    set_way("netbird")


def netbird_state():
    """What the NetBird client in the container says, or None when it is not running:
    {"connected", "address", "network", "error"}."""
    try:
        if time.time() - os.path.getmtime(NETBIRD_STATUS) > NETBIRD_FRESH:
            return None
        # A client that was started for an earlier request (another key, another server) is on
        # its way out: what it says does not count.
        with open(NETBIRD_STATUS_FOR) as f, open(NETBIRD_APPLY) as g:
            if f.read().strip() != g.read().strip():
                return None
        with open(NETBIRD_STATUS) as f:
            status = json.load(f)
        management = status.get("management") or {}
        ip = str(status.get("netbirdIp") or "")
        network = ipaddress.ip_network(ip, strict=False) if "/" in ip else None
        address = str(ipaddress.ip_address(ip.split("/")[0])) if ip else ""
    except (OSError, ValueError, AttributeError):
        return None
    return {"connected": management.get("connected") is True and bool(address), "address": address,
            "network": network, "error": str(management.get("error") or "")}


_network = {"at": 0.0, "network": None}


def from_netbird(peer):
    """True when this address (as the gateway saw it) is in the box's NetBird network."""
    if not peer or not load_settings().get("netbird"):
        return False
    now = time.time()
    if now - _network["at"] > 30:       # asked for every tile: read the file now and then only
        state = netbird_state()
        _network.update(at=now, network=state["network"] if state else None)
    try:
        address = ipaddress.ip_address(peer.strip())
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return _network["network"] is not None and address in _network["network"]
    except ValueError:
        return False


def netbird_address():
    """The box's address in the NetBird network: what the client says now, or what it said last
    for this server (it stays the same). Written down when it is new."""
    nb = load_netbird()
    state = netbird_state()
    if state and state["connected"] and (state["address"] != nb.get("address") or nb.get("addressFor") != nb.get("management")):
        nb = save_netbird(address=state["address"], addressFor=nb.get("management"))
    return nb.get("address", "") if nb.get("addressFor") == nb.get("management") else ""


def netbird_block():
    """What a paired device needs to come in through NetBird, or None: only when remote access
    and NetBird are on and the box has its address there. Holds the setup key: a secret."""
    if not netbird_wanted():
        return None
    address, key, nb = netbird_address(), netbird_key(), load_netbird()
    if not address or not key:
        return None
    block = {"management": nb.get("management") or NETBIRD_CLOUD, "setupKey": key, "host": address, "port": gateway_port()}
    # Changes when anything in the block does; the salt keeps it from saying anything about the key.
    block["rev"] = hashlib.sha256((nb.get("salt", "") + json.dumps(block, sort_keys=True)).encode()).hexdigest()[:12]
    return block


NETBIRD_PROBLEMS = {
    "key": "Setup key rejected: it is invalid, expired, or used up. Create a new setup key in the NetBird dashboard, "
           "enter it below and click Connect.",
    "server": "Cannot reach the NetBird management URL %s. Check the server's internet connection and the URL.",
    "download": "Cannot download NetBird. Check the server's internet connection, then click Connect again.",
    "start": "NetBird failed to start. Run ./scripts/remote.sh setup on the server and try again.",
}


def netbird_report():
    """For the Remote access page: is NetBird set up, does it work, and if not, why.
    status: none (not set up), off, starting, connected, blocked."""
    nb = load_netbird()
    management = nb.get("management") or NETBIRD_CLOUD
    out = {"keySet": bool(netbird_key()), "wanted": way() == "netbird", "management": management, "address": "", "seen": 0}
    if not out["keySet"]:
        return {**out, "wanted": False, "status": "none", "text": "NetBird VPN is not configured."}
    if not out["wanted"]:
        return {**out, "status": "off", "text": "Method in use: Port Forwarding. NetBird VPN is disabled; the setup key is kept."}
    if not enabled():
        return {**out, "status": "off", "text": "Remote connectivity is disabled. NetBird VPN starts when you enable it."}
    state = netbird_state()
    out["address"] = netbird_address()
    out["seen"] = newest("netbird")
    try:
        with open(NETBIRD_PROBLEM) as f:
            lines = f.read().splitlines()
        problem, said = lines[:1], " ".join(lines[1:]).strip()
    except OSError:
        problem, said = [], ""
    if state and state["connected"]:
        text = "Connected to NetBird. Server address in the NetBird network: %s." % state["address"]
        try:
            with open(NETBIRD_CHECK) as f:
                out["keyChecked"] = f.read().strip() != "unchecked"
        except OSError:
            out["keyChecked"] = True
        if not out["keyChecked"] and not isolated():
            text += (" The new setup key was not verified because the server was already a NetBird peer."
                     " Make sure it shows as valid in the NetBird dashboard; newly paired devices need a valid key.")
        if out["seen"] and time.time() - out["seen"] < 7 * 86400:
            at = stamp(out["seen"])
            out.update(at)
            text += " Last remote connection via NetBird: %s." % at["atText"]
        else:
            text += " No recent remote connection."
        return {**out, "status": "connected", "text": text}
    if problem and problem[0] in NETBIRD_PROBLEMS:
        text = NETBIRD_PROBLEMS[problem[0]]
        text = text % management if "%s" in text else text
        if problem[0] == "start" and said:
            # Docker's own words, for the person who helps: short, and only from the box itself.
            text += " Details: %s" % re.sub(r"[\x00-\x1f]", " ", said)[:200]
        return {**out, "status": "blocked", "text": text}
    try:
        asked = os.path.getmtime(NETBIRD_APPLY)
    except OSError:
        asked = 0
    if state is None and time.time() - asked < NETBIRD_START:
        return {**out, "status": "starting", "text": "Starting NetBird. The first start downloads NetBird and can take a few minutes."}
    if state is None:
        return {**out, "status": "blocked", "text": NETBIRD_PROBLEMS["start"]}
    return {**out, "status": "blocked", "text": NETBIRD_PROBLEMS["server"] % management}


# ---------- network isolation in NetBird ----------
#
# Each server gets its own groups, access policy and device setup key in the NetBird account, so
# that servers sharing one account cannot reach each other's devices (tools/netbird_isolation.py).
# The owner pastes a NetBird access token on the Remote Access page; it is used for that one run
# and never written down. What the run did is kept in netbird.json under "isolation".

def load_isolation():
    data = load_netbird().get("isolation")
    return data if isinstance(data, dict) else {}


def isolated():
    """This server's device key is managed by network isolation (not typed by hand)."""
    return bool(load_isolation().get("deviceKey"))


def device_addresses():
    """{device id: the NetBird address it last came in from, or ""} for every paired device."""
    return {d["id"]: last_nb_address.get(d["id"]) or d.get("netbirdAddress", "") for d in load_devices()}


def keep_device_key(key):
    """The device key from network isolation: it replaces the key paired devices get (the block's
    rev changes, so they pick it up). The server's own NetBird client needs no key any more: it is a
    member already."""
    os.makedirs(NETBIRD_DIR, exist_ok=True)
    os.chmod(NETBIRD_DIR, 0o700)
    write_private(NETBIRD_KEY, (key + "\n").encode())


def run_isolation(action, token):
    """"setup" (also repairs), "check" (any role that can read), or "replace-key". Returns a note
    for the owner, or raises ValueError with what failed. The token is not kept."""
    if way() != "netbird" or not enabled():
        raise ValueError("Network isolation works with the NetBird VPN method while remote connectivity is enabled.")
    address = netbird_address()
    if not address:
        raise ValueError("The server is not connected to NetBird yet. Connect NetBird with a setup key first.")
    if not isolation_lock.acquire(blocking=False):
        raise ValueError("Another network isolation run is in progress. Try again in a minute.")
    try:
        nb = load_netbird()
        record = dict(load_isolation())
        management = nb.get("management") or NETBIRD_CLOUD
        typed_before = bool(netbird_key()) and not record.get("deviceKey")
        try:
            if action == "check":
                isolation.check(record, management, token, gateway_port(), device_addresses())
            else:
                record.setdefault("id", install_id())
                isolation.setup(record, management, token, address, gateway_port(), device_addresses(), keep_device_key,
                                new_key=action == "replace-key" or not netbird_key())
        except isolation.IsolationError as e:
            raise ValueError(str(e))
        finally:
            save_netbird(isolation=record)
        if typed_before:
            return ("Devices now receive this server's own setup key. Revoke the setup key you entered by hand "
                    "earlier in the NetBird dashboard, so that it cannot add more peers.")
        return ""
    finally:
        isolation_lock.release()


def isolation_report():
    return isolation.status(load_isolation(), gateway_port(), netbird_address() if netbird_wanted() else "")


# ---------- the service ----------

# The addresses of home and office networks, and of the box itself. Named one by one:
# Python's "is_private" also covers ranges that are not local networks.
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]

FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "CF-Connecting-IP", "True-Client-IP", "Tailscale-Funnel-Request")


def own_prefixes():
    """The IPv6 networks this machine is on: devices at home have global addresses in them."""
    out = []
    for address in portmap.global_ipv6():
        try:
            out.append(ipaddress.ip_network(address + "/64", strict=False))
        except ValueError:
            pass
    return out


def lan(headers):
    """True when a request came from the box's own network, through the box's own web server,
    or from a program on the box itself (no X-Real-IP at all).

    The web server puts the caller's address in X-Real-IP; it must be a local one. A request
    that carries a forwarding header came through some proxy or tunnel on the way, and is
    refused: a box whose web port is published to the internet through one would otherwise
    let the world pair.
    """
    if any(headers.get(h) for h in FORWARDED):
        return False
    real = headers.get("X-Real-IP")
    if real is None:
        return True
    try:
        address = ipaddress.ip_address(real.strip())
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in network for network in LOCAL_NETWORKS + own_prefixes())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fieldmap-remote"

    def log_message(self, fmt, *args):
        if self.path != "/check":
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def address_string(self):
        return self.headers.get("X-Real-IP") or "-"

    def send(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if data:                        # an answer without one is complete, and the gateway may be gone
            self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 16384:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def code_ok(self, body):
        pin = setting("REMOTE_PIN")
        return not pin or secrets.compare_digest(str(body.get("pin", "")).strip().encode(), pin.encode())

    def page(self, path):
        """The Devices page, and the shared look and header beside it."""
        # The Devices page and Admin's Remote access page are one file (it tells them apart).
        name = "index.html" if path in ("/", "", "/remote.html") else path.lstrip("/")
        file = os.path.join(PAGE, name)
        if not os.path.isfile(file):
            file = os.path.join(SHARED, name)
        ext = os.path.splitext(name)[1]
        if "/" in name or ".." in name or ext not in TYPES or not os.path.isfile(file):
            return self.send(404, {"error": "Not found."})
        with open(file, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def download(self):
        """The app, for a device on the local network to install. Sent in pieces: it is tens of MB."""
        info = app()
        if not info:
            return self.send(404, {"error": "No app is installed on the server."})
        with open(APK, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(info["size"]))
            self.send_header("Content-Disposition", 'attachment; filename="fieldmap-%s.apk"' % info["version"])
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile, 1 << 16)

    def info(self):
        # "netbird": on and rev are for the app (is there something to ask the gateway for, and
        # did it change); the rest is for the Remote access page. Nothing secret: not the key.
        block = netbird_block()
        return {"service": "fieldmap-remote", "version": 3, "ready": ready(), "enabled": enabled(), "pin": bool(setting("REMOTE_PIN")),
                "autoOff": {"on": auto_off(), "days": IDLE_DAYS, "at": auto_off_at()},
                "way": way(), "direct": direct(),
                "netbird": {"on": block is not None, "rev": block["rev"] if block else "", **netbird_report(),
                            "isolation": isolation_report()},
                "forwarding": {**forwarding(), "port": gateway_port(), "boxAddress": portmap.lan_ipv4() or "",
                               "changingTo": load_settings().get("changingPort") if load_settings().get("changingPort") != gateway_port() else None},
                "pairing": {"open": pairing_left() > 0, "seconds": pairing_left()},
                "endpoints": endpoints(), "reach": report(), "phones": len(load_devices()), "app": app()}

    def do_GET(self):
        if self.path == "/check":
            # Only the gateway asks this, and it sends no X-Real-IP. Switched off: 401, which the
            # app tells apart from 403 (removed), so that it stays paired.
            if self.headers.get("X-Real-IP") is not None:
                return self.send(403)
            if not enabled():
                return self.send(401)
            return self.send(204 if allowed(self.headers.get("X-Fieldmap-Fingerprint"), self.headers.get("X-Fieldmap-Peer")) else 403)
        if self.path == "/gateway/netbird":
            # Only the gateway passes this on (/mapremote/netbird there), for a visitor whose
            # certificate it checked; like /check it comes with no X-Real-IP. The certificate
            # must be a paired device's: this answer holds the setup key.
            if self.headers.get("X-Real-IP") is not None:
                return self.send(404, {"error": "Not found."})
            if not enabled():
                return self.send(401)
            if listed(self.headers.get("X-Fieldmap-Fingerprint")) is None:
                return self.send(403)
            return self.send(200, {"netbird": netbird_block()})
        if not lan(self.headers):
            return self.send(403, {"error": "This page is available on the local network only."})
        if self.path in ("/", "/index.html", "/remote.html") or self.path.lstrip("/") in ("fieldmap.css", "nav.js"):
            return self.page(self.path)
        if self.path == "/fieldmap.apk":
            return self.download()
        if self.path == "/info":
            self.send(200, self.info())
        elif self.path == "/devices":
            pending = dict(last_seen)
            self.send(200, {"devices": [{"id": d["id"], "name": d["name"], "added": d.get("added", ""), "addedAt": d.get("addedAt", 0),
                                         "seen": pending.get(d["id"], d.get("seen", 0))} for d in load_devices()]})
        else:
            self.send(404, {"error": "Not found."})

    def do_POST(self):
        body = self.body()
        if self.path not in ("/enrol", "/pairing", "/remove", "/enabled", "/auto-off", "/recheck", "/forwarding", "/find-address", "/new-port",
                             "/way", "/netbird", "/netbird/isolation"):
            return self.send(404, {"error": "Not found."})
        if not lan(self.headers):
            return self.send(403, {"error": "Devices can be paired on the local network only."})
        if not ready():
            return self.send(503, {"error": "Remote access is not installed on this server."})
        if not self.code_ok(body):
            return self.send(403, {"error": "Incorrect access code.", "pin": True})
        if self.path == "/enabled":
            set_enabled(bool(body.get("on")))
            return self.send(200, self.info())
        if self.path == "/auto-off":
            save_settings(autoOff=bool(body.get("on")))
            return self.send(200, self.info())
        if self.path == "/recheck":
            check_router()
            return self.send(200, self.info())
        if self.path == "/forwarding":
            address = body.get("address")
            if address is not None and str(address).strip() and not clean_address(address):
                return self.send(400, {"error": "Invalid public IP address. Enter an IP address such as 203.0.113.30, or a host name."})
            set_forwarding(bool(body.get("manual")), address)
            return self.send(200, self.info())
        if self.path == "/find-address":
            found = portmap.stun_public_ip()
            if not found:
                return self.send(502, {"error": "Detection failed. Check the router's status page for its public IP address."})
            return self.send(200, {"address": found})
        if self.path == "/new-port":
            new_port()
            return self.send(202, self.info())
        if self.path == "/way":
            # {"way": "direct"} or {"way": "netbird"}: one way in at a time.
            try:
                set_way(str(body.get("way", "")))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            return self.send(200, self.info())
        if self.path == "/netbird/isolation":
            # {"action": "setup" | "check" | "replace-key", "token": "nbp_..."}: the token is used for
            # this one request and never written anywhere (not to a file, not to a log).
            action = str(body.get("action", ""))
            token = isolation.clean_token(body.get("token"))
            if action not in ("setup", "check", "replace-key"):
                return self.send(400, {"error": "Unknown action."})
            if not token:
                return self.send(400, {"error": "Invalid access token. Copy it from the NetBird dashboard (it starts with nbp_)."})
            try:
                note = run_isolation(action, token)
            except ValueError as e:
                return self.send(409, {"error": str(e)})
            return self.send(200, {**self.info(), "note": note})
        if self.path == "/netbird":
            # {"on": true, "management": "...", "setupKey": "..."} keeps the key and the server and
            # makes NetBird the way in (a key that is kept needs no typing again); {"on": false}
            # goes back to the router's port.
            on = bool(body.get("on"))
            management = clean_management(body.get("management")) if "management" in body else (load_netbird().get("management") or NETBIRD_CLOUD)
            key = clean_key(body.get("setupKey")) if str(body.get("setupKey") or "").strip() else ""
            if on and not management:
                return self.send(400, {"error": "Invalid management URL. For NetBird's cloud, use %s." % NETBIRD_CLOUD})
            if str(body.get("setupKey") or "").strip() and not key:
                return self.send(400, {"error": "Invalid setup key. Copy it from the NetBird dashboard."})
            if key and isolated():
                # A key typed by hand would put new devices outside this server's segment.
                return self.send(409, {"error": "Network isolation manages this server's setup key. Use Replace Device Key "
                                                "instead. To stop network isolation, run ./scripts/remote.sh netbird-forget."})
            if on and isolated() and management and management != (load_netbird().get("management") or NETBIRD_CLOUD):
                return self.send(409, {"error": "Network isolation is set up for the current management server. To change "
                                                "the server, run ./scripts/remote.sh netbird-forget first."})
            if on and not key and not netbird_key():
                return self.send(400, {"error": "Enter a setup key first."})
            if on:
                set_netbird(management or None, key)
            else:
                set_way("direct")
            return self.send(200, self.info())
        if self.path == "/pairing":
            if body.get("open") and not enabled():
                return self.send(409, {"error": "Remote connectivity is disabled. Enable it first."})
            if body.get("open"):
                open_pairing()
            else:
                close_pairing()
            return self.send(200, self.info())
        if self.path == "/remove":
            return self.send(200 if remove(str(body.get("id", ""))) else 404, {"devices": len(load_devices())})
        # /enrol
        if not enabled():
            return self.send(403, {"error": "Remote connectivity is disabled on the server. Enable it under Admin > Remote Access.", "closed": True})
        if pairing_left() <= 0:
            return self.send(403, {"error": "Pairing mode is not active. Click Start Pairing on the server's Devices page, then try "
                                            "again within 10 minutes.", "closed": True})
        try:
            device = enrol(body.get("name"), str(body.get("publicKey", "")))
        except PermissionError as e:
            return self.send(429, {"error": str(e)})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        with open(CA_CRT) as f:
            ca = f.read()
        answer = {"id": device["id"], "name": device["name"], "certificate": device["certificate"],
                  "ca": ca, "gatewayName": gateway_name(), "endpoints": endpoints()}
        block = netbird_block()
        if block:                       # the other way in; the key in it is for this device only
            answer["netbird"] = block
        self.send(200, answer)


class SocketServer(ThreadingHTTPServer):
    """The same server on a Unix socket (a file) instead of a network port."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


def keep_up():
    """In the background: ask the router again now and then, and write down who came in from outside."""
    next_router = next_seen = 0.0
    nb_was = "off"
    while True:
        now = time.time()
        try:
            if now >= next_router and enabled():
                state = check_router()
                print("Router: %s" % report()["text"], flush=True)
                # Sooner again while it says no: a router restarting, or UPnP just switched on.
                next_router = now + (300 if state.get("problem") not in ("", "off") else REACH_EVERY)
            was, state = nb_was, netbird_report()["status"] if netbird_wanted() else "off"
            if state != was:
                nb_was = state
                print("NetBird: %s" % state, flush=True)
            if now >= next_seen:
                write_seen()
                switch_off_if_idle(now)
                next_seen = now + SEEN_EVERY
        except Exception as e:      # the service keeps running whatever the router does
            print("Router check failed: %s" % e, flush=True)
            next_router = now + 300
        time.sleep(30)


def serve(host, port):
    os.makedirs(RUN, exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    # Bound by its name from inside its folder: a socket's full path may be only about a
    # hundred characters long, and a clone can sit deeper than that.
    os.chdir(RUN)
    by_socket = SocketServer(os.path.basename(SOCKET), Handler)
    os.chmod(SOCKET, 0o666)             # the web server and the gateway run as other users
    print(f"Remote access service on {SOCKET}", flush=True)
    if port:
        by_port = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=by_port.serve_forever, daemon=True).start()
        print(f"...and on http://{host}:{port}/", flush=True)
    # systemd stops the service with SIGTERM: leave through `finally`, which writes down who came in.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("Remote access is " + ("on." if enabled() else "off: switch it on on the Remote access page (Admin)."), flush=True)
    if load_settings().get("netbird", False) is not netbird_wanted():     # the firewall follows this file
        save_settings(netbird=netbird_wanted())
    threading.Thread(target=keep_up, daemon=True).start()
    try:
        by_socket.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        write_seen()
        try:
            os.unlink(SOCKET)
        except OSError:
            pass


def running():
    """What the running service answers to /info (it knows who came in a moment ago), or None."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            here = os.getcwd()
            os.chdir(RUN)               # by its name from inside its folder, as it was bound
            try:
                s.connect(os.path.basename(SOCKET))
            finally:
                os.chdir(here)
            s.sendall(b"GET /info HTTP/1.0\r\n\r\n")
            data = b""
            while True:
                part = s.recv(65536)
                if not part:
                    break
                data += part
        return json.loads(data.split(b"\r\n\r\n", 1)[1])
    except (OSError, ValueError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Remote access for the Fieldmap Android app.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="make the certificates and the gateway's configuration")
    p.add_argument("--prefix", default="/", help="where the tile server is on the box's web address: / or /maps/")
    p.add_argument("--upstream", default="http://tileserver:8080/", help="the tile server as the gateway reaches it")
    p.add_argument("--listen", default="8443", help="port (or address:port) the gateway listens on")
    p.add_argument("--ipv6", action="store_true", help="also listen on IPv6 (a gateway on the host's own network)")
    p = sub.add_parser("serve", help="answer the app and the gateway, and keep the router's port open")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="also listen on this port (the socket is always there)")
    sub.add_parser("devices", help="list the devices that are paired")
    p = sub.add_parser("remove", help="lock a device out")
    p.add_argument("id")
    sub.add_parser("status", help="say what is set up")
    sub.add_parser("router", help="ask the router now and say what it answered")
    sub.add_parser("netbird-forget", help="forget NetBird: the setup key and what was set up (scripts/remote.sh netbird-forget)")
    args = parser.parse_args()

    if args.command == "init":
        init(args.prefix, args.upstream, args.listen, args.ipv6)
    elif args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "devices":
        devices = load_devices()
        if not devices:
            print("No paired devices.")
        for d in devices:
            seen = time.strftime("%Y-%m-%d", time.localtime(d["seen"])) if d.get("seen") else "never"
            print(f"{d['id']}  paired {d['added']}  last remote connection: {seen}  {d['name']}")
    elif args.command == "remove":
        if remove(args.id):
            print(f"Removed {args.id}: it is locked out from now on.")
        else:
            sys.exit(f"No device with the id {args.id}. See: remote-server.py devices")
    elif args.command == "netbird-forget":
        managed = load_isolation().get("id", "")
        for path in (NETBIRD, NETBIRD_KEY, NETBIRD_CLIENT):
            try:
                os.unlink(path)
            except OSError:
                pass
        # Without NetBird, the router's port is the way in again.
        save_settings(netbird=False, way="direct")
        if managed:
            n = isolation.names(managed)
            print("NetBird is forgotten on this server. Network isolation was set up: delete the groups %s and %s, the "
                  "policy %s and the setup key %s in the NetBird dashboard." % (n["server"], n["devices"], n["policy"], n["key"]))
    elif args.command == "router":
        state = check_router()
        print(json.dumps(state, indent=1))
        print(report()["text"])
    elif args.command == "status":
        live = running() or {}
        r = live.get("reach") or report()
        n = live.get("netbird") or netbird_report()
        print("Certificates:   " + ("made" if ready() else "not made yet (run scripts/remote.sh setup)"))
        print("Remote Connectivity:        " + ("Enabled" if enabled() else "Disabled"))
        print("Remote Connectivity Method: " + ("NetBird VPN" if (live.get("way") or way()) == "netbird" else "Port Forwarding"))
        print("Port Forwarding:            " + r["text"])
        print("NetBird VPN:                " + n["text"])
        print("Network Isolation:          " + (n.get("isolation") or isolation_report())["text"])
        print("Public Endpoints:           " + (", ".join("%s:%s" % (e["host"], e["port"]) for e in endpoints()) or "none"))
        print(f"Paired Devices:             {len(load_devices())}")
        print("Code:           " + ("asked for (REMOTE_PIN)" if setting("REMOTE_PIN") else "none"))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(f"Error: {e}")
