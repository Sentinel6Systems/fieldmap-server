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
    phone", on the box's Phones page (phones/, at /phones/).
  * Off by default: until someone switches it on on the Phones page, nothing outside can
    reach the box, the router is not asked for anything, and no phone can pair
    (set_enabled). Switched off again, it is all closed, and the paired phones are kept.
  * From outside, the app connects to the gateway (an nginx that demands such a
    certificate, see gateway_conf()), which asks this service for every request whether the
    certificate is still on the list (`GET /check`). Removing a phone locks it out at once.
  * Reaching the gateway: every few minutes this service asks the home router to forward an
    outside port to it (UPnP or NAT-PMP, tools/portmap.py) and notes the box's IPv6
    addresses. The app learns these addresses whenever it is home, from `GET /info`.

Who may ask what:
  * through the box's web server, which puts the caller's address in X-Real-IP: only
    callers on the local network (see lan()), for /info, /devices, /pairing, /remove, /enrol;
  * with no X-Real-IP at all: the gateway (only /check) and programs on the box itself.

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
SETTINGS = os.path.join(REMOTE, "settings.json")    # the Phones page's switch; scripts/firewall.sh reads it too
PAGE = os.path.join(ROOT, "phones")                 # the Phones page
SHARED = os.path.join(ROOT, "ui")                   # the look and the header every page shares
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml"}

MAX_DEVICES = 200
MAX_NAME = 40
ENROLS_PER_HOUR = 30
DEVICE_DAYS = 1825
SERVER_DAYS = 3650
CA_DAYS = 7300
PAIRING_SECONDS = 600           # how long "Pair a phone" keeps pairing open
IDLE_DAYS = 90                  # switched on, with no phone from outside this long: off by itself
REACH_EVERY = 900               # how often the router is asked again
SEEN_EVERY = 600                # how often "last used from outside" is written down

lock = threading.Lock()
recent_enrols = []
pairing_until = 0.0
last_seen = {}                  # device id -> time it last came in from outside, not written yet


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
    }}

    location {prefix} {{
        if ($request_method !~ ^(GET|HEAD)$) {{ return 405; }}
        if ($uri !~ "{allowed}") {{ return 404; }}
        auth_request /_device;
        proxy_pass {upstream};
        proxy_set_header Host $fieldmap_host;
        proxy_set_header X-Forwarded-Proto $fieldmap_proto;
        proxy_read_timeout 120;
    }}

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
    return name[:MAX_NAME] or "Phone"


def enrol(name, public_key_pem):
    """Puts a device on the list and returns its entry. The same key twice gives the same entry."""
    public_key = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
    try:
        described = openssl("pkey", "-pubin", "-noout", "-text", stdin=public_key).decode(errors="replace")
        der = openssl("pkey", "-pubin", "-outform", "DER", stdin=public_key)
    except ValueError:
        raise ValueError("That is not a public key.")
    if "prime256v1" not in described:
        raise ValueError("The key must be an elliptic-curve key on the P-256 curve.")
    key_id = hashlib.sha256(der).hexdigest()
    with lock:
        devices = load_devices()
        for d in devices:
            if d.get("key") == key_id:
                return d
        if len(devices) >= MAX_DEVICES:
            raise PermissionError("The box has as many phones as it takes. Remove one on its Phones page first.")
        now = time.time()
        recent_enrols[:] = [t for t in recent_enrols if now - t < 3600]
        if len(recent_enrols) >= ENROLS_PER_HOUR:
            raise PermissionError("Too many new phones in the last hour. Try again later.")
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
        device = {"id": device_id, "name": clean_name(name), "added": datetime.date.today().isoformat(),
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
        return True


def allowed(fingerprint):
    """True when the certificate is on the list; notes when that phone came in from outside."""
    fingerprint = (fingerprint or "").strip().lower().replace(":", "")
    if not fingerprint:
        return False
    for d in load_devices():
        if secrets.compare_digest(fingerprint, d.get("sha1", "")):
            last_seen[d["id"]] = int(time.time())
            return True
    return False


def write_seen():
    """Writes down when each phone last came in from outside (kept in memory in between)."""
    if not last_seen:
        return
    with lock:
        devices = load_devices()
        for d in devices:
            if d["id"] in last_seen:
                d["seen"] = last_seen[d["id"]]
        last_seen.clear()
        save_devices(devices)


# ---------- the switch ----------

def load_settings():
    """The Phones page's settings: "enabled" (off by default), "autoOff" (switch off by itself
    after IDLE_DAYS with no phone from outside; on by default), "since" (when it was switched
    on) and "offReason" ("idle" when it switched itself off)."""
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
    """Remote access on or off (the default), as the Phones page's switch says."""
    return load_settings().get("enabled") is True


def auto_off():
    return load_settings().get("autoOff", True) is not False


def idle_since():
    """When remote access was last used: the newest of a phone coming in from outside and
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
        print(f"Remote access switched itself off: no phone came in from outside for {IDLE_DAYS} days.", flush=True)
        return True
    return False


def set_enabled(on, reason=""):
    """Off is complete isolation: no pairing, no phone let in, the router's port closed, and the
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


def release_router():
    state = load_reach()
    try:
        portmap.release(state)
    except Exception as e:          # the router lets the port lapse by itself within two hours
        print("Closing the router's port failed: %s" % e, flush=True)
    write_private(REACH, json.dumps({"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": "switched-off"}).encode())


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
    """Forwarding by hand (the Phones page): {"manual": bool, "address": the box's internet address}."""
    f = load_settings().get("forwarding") or {}
    return {"manual": f.get("manual") is True, "address": str(f.get("address") or "")}


def check_router():
    """Asks the router again (tools/portmap.py) and writes down what it said."""
    if not enabled():
        state = {"checked": int(time.time()), "ipv4": {}, "ipv6": {}, "problem": "switched-off"}
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
    """Addresses set up by hand: REMOTE_ADDRESS, then a relay's (data/remote/address)."""
    out = []
    texts = [setting("REMOTE_ADDRESS")]
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
    if f["manual"] and f["address"]:
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
    "router-closed": "The router does not open ports for the box by itself. Switch on UPnP (or NAT-PMP) in the "
                     "router's settings, or forward the port by hand (Port forwarding, below).",
    "router-refused": "The router answered, but did not open a port for the box. Routers do this when another "
                      "router or a modem stands in front of them, or when their UPnP settings leave this box out. "
                      "Forwarding the port by hand (below) works around it, if nothing stands in front.",
    "double-nat": "There are two routers in a row. The one in front (often the provider's) would have to "
                  "pass the box's port on, or be set to bridge mode.",
    "shared-address": "Your internet provider shares one address among its customers (as Starlink and most "
                      "cellular home internet do), so nothing outside can reach the box over IPv4.",
    "off": "Opening a port on the router is switched off (REMOTE_DIRECT=off).",
    "manual": "",
    "switched-off": "Remote access is switched off: nothing outside can reach the box, and no phone can pair.",
    "switched-off-idle": "Remote access switched itself off: no phone had come in from outside for %d days. Nothing "
                         "outside can reach the box until someone switches it on again." % IDLE_DAYS,
}


def report():
    """For the Phones page and the app: can phones reach the box from outside, and if not, why."""
    state = load_reach()
    devices = load_devices()
    newest_seen = max([d.get("seen", 0) for d in devices] + list(last_seen.values()), default=0)
    manual = manual_address()
    v6 = state.get("ipv6", {}).get("addresses", [])
    if not enabled():
        idle = load_settings().get("offReason") == "idle"
        return {"status": "off", "text": PROBLEMS["switched-off-idle" if idle else "switched-off"], "checked": state.get("checked", 0)}
    if newest_seen and time.time() - newest_seen < 7 * 86400:
        status, text = "confirmed", "A phone reached the box from outside on %s." % time.strftime("%Y-%m-%d %H:%M", time.localtime(newest_seen))
    elif forwarding()["manual"] and not forwarding()["address"]:
        status, text = "blocked", "Forwarding by hand: forward the port on the router, then give the box's internet address."
    elif forwarding()["manual"]:
        status, text = "expected", "Forwarded by hand. Not confirmed yet: no phone has come in from outside lately."
    elif manual or state.get("ipv4", {}).get("ok"):
        status, text = "expected", "The router lets phones in. Not confirmed yet: no phone has come in from outside lately."
    elif v6:
        status, text = "maybe", PROBLEMS.get(state.get("problem"), "") + " The box also has an IPv6 address, which may let phones in that have IPv6 too."
    elif state:
        status, text = "blocked", PROBLEMS.get(state.get("problem"), "Phones cannot reach the box from outside.")
        said = state.get("ipv4", {}).get("error")
        if said and state.get("problem") == "router-refused":
            text += " (The router said: %s.)" % said
    else:
        status, text = "unknown", "The box has not asked its router yet."
    return {"status": status, "text": text.strip(), "checked": state.get("checked", 0)}


# ---------- the service ----------

# The addresses of home and office networks, and of the box itself. Named one by one:
# Python's "is_private" also covers ranges that are not local networks.
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]

FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "CF-Connecting-IP", "True-Client-IP", "Tailscale-Funnel-Request")


def own_prefixes():
    """The IPv6 networks this machine is on: phones at home have global addresses in them."""
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
        """The Phones page, and the shared look and header beside it."""
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        file = os.path.join(PAGE, name)
        if not os.path.isfile(file):
            file = os.path.join(SHARED, name)
        ext = os.path.splitext(name)[1]
        if "/" in name or ".." in name or ext not in TYPES or not os.path.isfile(file):
            return self.send(404, {"error": "Not here."})
        with open(file, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def info(self):
        return {"service": "fieldmap-remote", "version": 2, "ready": ready(), "enabled": enabled(), "pin": bool(setting("REMOTE_PIN")),
                "autoOff": {"on": auto_off(), "days": IDLE_DAYS, "at": auto_off_at()},
                "forwarding": {**forwarding(), "port": gateway_port(), "boxAddress": portmap.lan_ipv4() or "",
                               "changingTo": load_settings().get("changingPort") if load_settings().get("changingPort") != gateway_port() else None},
                "pairing": {"open": pairing_left() > 0, "seconds": pairing_left()},
                "endpoints": endpoints(), "reach": report(), "phones": len(load_devices())}

    def do_GET(self):
        if self.path == "/check":
            # Only the gateway asks this, and it sends no X-Real-IP. Switched off: 401, which the
            # app tells apart from 403 (removed), so that it stays paired.
            if self.headers.get("X-Real-IP") is not None:
                return self.send(403)
            if not enabled():
                return self.send(401)
            return self.send(204 if allowed(self.headers.get("X-Fieldmap-Fingerprint")) else 403)
        if not lan(self.headers):
            return self.send(403, {"error": "Only on the box's own network."})
        if self.path in ("/", "/index.html") or self.path.lstrip("/") in ("fieldmap.css", "nav.js"):
            return self.page(self.path)
        if self.path == "/info":
            self.send(200, self.info())
        elif self.path == "/devices":
            pending = dict(last_seen)
            self.send(200, {"devices": [{"id": d["id"], "name": d["name"], "added": d.get("added", ""),
                                         "seen": pending.get(d["id"], d.get("seen", 0))} for d in load_devices()]})
        else:
            self.send(404, {"error": "Not here."})

    def do_POST(self):
        body = self.body()
        if self.path not in ("/enrol", "/pairing", "/remove", "/enabled", "/auto-off", "/recheck", "/forwarding", "/find-address", "/new-port"):
            return self.send(404, {"error": "Not here."})
        if not lan(self.headers):
            return self.send(403, {"error": "Pairing is only possible on the box's own network."})
        if not ready():
            return self.send(503, {"error": "Remote access is not set up on this box."})
        if not self.code_ok(body):
            return self.send(403, {"error": "That is not the box's code.", "pin": True})
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
                return self.send(400, {"error": "That is not an internet address: a number such as 203.0.113.30, or a name."})
            set_forwarding(bool(body.get("manual")), address)
            return self.send(200, self.info())
        if self.path == "/find-address":
            found = portmap.stun_public_ip()
            if not found:
                return self.send(502, {"error": "No answer from the internet. Read the address on the router's status page instead."})
            return self.send(200, {"address": found})
        if self.path == "/new-port":
            new_port()
            return self.send(202, self.info())
        if self.path == "/pairing":
            if body.get("open") and not enabled():
                return self.send(409, {"error": "Remote access is switched off. Switch it on first."})
            if body.get("open"):
                open_pairing()
            else:
                close_pairing()
            return self.send(200, self.info())
        if self.path == "/remove":
            return self.send(200 if remove(str(body.get("id", ""))) else 404, {"devices": len(load_devices())})
        # /enrol
        if not enabled():
            return self.send(403, {"error": "Remote access is switched off on the box (on its Phones page).", "closed": True})
        if pairing_left() <= 0:
            return self.send(403, {"error": "Pairing is closed. On the box's Phones page press Pair a phone, "
                                            "then try again within ten minutes.", "closed": True})
        try:
            device = enrol(body.get("name"), str(body.get("publicKey", "")))
        except PermissionError as e:
            return self.send(429, {"error": str(e)})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        with open(CA_CRT) as f:
            ca = f.read()
        self.send(200, {"id": device["id"], "name": device["name"], "certificate": device["certificate"],
                        "ca": ca, "gatewayName": gateway_name(), "endpoints": endpoints()})


class SocketServer(ThreadingHTTPServer):
    """The same server on a Unix socket (a file) instead of a network port."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


def keep_up():
    """In the background: ask the router again now and then, and write down who came in from outside."""
    next_router = next_seen = 0.0
    while True:
        now = time.time()
        try:
            if now >= next_router and enabled():
                state = check_router()
                print("Router: %s" % report()["text"], flush=True)
                # Sooner again while it says no: a router restarting, or UPnP just switched on.
                next_router = now + (300 if state.get("problem") not in ("", "off") else REACH_EVERY)
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
    print("Remote access is " + ("on." if enabled() else "off: switch it on on the Phones page."), flush=True)
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
    sub.add_parser("devices", help="list the phones that are paired")
    p = sub.add_parser("remove", help="lock a phone out")
    p.add_argument("id")
    sub.add_parser("status", help="say what is set up")
    sub.add_parser("router", help="ask the router now and say what it answered")
    args = parser.parse_args()

    if args.command == "init":
        init(args.prefix, args.upstream, args.listen, args.ipv6)
    elif args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "devices":
        devices = load_devices()
        if not devices:
            print("No phone is paired yet.")
        for d in devices:
            seen = time.strftime("%Y-%m-%d", time.localtime(d["seen"])) if d.get("seen") else "never"
            print(f"{d['id']}  added {d['added']}  from outside: {seen}  {d['name']}")
    elif args.command == "remove":
        if remove(args.id):
            print(f"Removed {args.id}: it is locked out from now on.")
        else:
            sys.exit(f"No phone with the id {args.id}. See: remote-server.py devices")
    elif args.command == "router":
        state = check_router()
        print(json.dumps(state, indent=1))
        print(report()["text"])
    elif args.command == "status":
        r = report()
        print("Certificates:   " + ("made" if ready() else "not made yet (run scripts/remote.sh setup)"))
        print("From outside:   " + r["text"])
        print("Addresses:      " + (", ".join("%s:%s" % (e["host"], e["port"]) for e in endpoints()) or "none yet"))
        print(f"Phones paired:  {len(load_devices())}")
        print("Code:           " + ("asked for (REMOTE_PIN)" if setting("REMOTE_PIN") else "none"))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(f"Error: {e}")
