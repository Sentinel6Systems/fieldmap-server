#!/usr/bin/env python3
"""fieldmap-server: remote access for the Fieldmap Android app.

A phone that has been on the box's own network can use the box's maps from anywhere
afterwards; nobody else can. This is the part that decides who is let in.

How it works:
  * The box is its own certificate authority (made by `init`, kept in data/remote/).
  * On the box's network the app asks to be let in (`POST /enrol` with a public key whose
    private half never leaves the phone) and gets a certificate for that key. Only requests
    from the local network are answered: see lan_only().
  * From outside, the app connects to the remote gateway (an nginx that demands such a
    certificate, see gateway_conf()), which asks this service for every request whether the
    certificate is still on the list (`GET /check`). Taking a phone off the list locks it out
    at once.
  * How the gateway is reached from the internet is not decided here: scripts/remote.sh
    sets up a relay (Tailscale Funnel) or takes an address you forwarded yourself, and
    writes it to data/remote/address, which `GET /info` passes on to the app.

Python's standard library and the `openssl` command only.

  remote-server.py init [--prefix /maps/] [--upstream http://127.0.0.1:8080/] [--listen 8443]
  remote-server.py serve [--port 8092] [--host 127.0.0.1]     also always on data/remote/run/remote.sock
  remote-server.py devices | remove <id> | status

Settings in the environment (scripts/remote.sh reads them from .env):
  REMOTE_PIN       a code the app must send to be let in. Without it, being on the local
                   network is enough.
  REMOTE_ADDRESS   host:port the gateway is reached at from outside, instead of the one
                   scripts/remote.sh wrote.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = os.environ.get("REMOTE_DIR") or os.path.join(ROOT, "data", "remote")
GATEWAY = os.path.join(REMOTE, "gateway")          # what the gateway container may read
RUN = os.path.join(REMOTE, "run")
SOCKET = os.path.join(RUN, "remote.sock")
CA_KEY = os.path.join(REMOTE, "ca.key")             # never leaves this folder
CA_CRT = os.path.join(REMOTE, "ca.crt")
DEVICES = os.path.join(REMOTE, "devices.json")
ADDRESS = os.path.join(REMOTE, "address")

MAX_DEVICES = 200
MAX_NAME = 40
ENROLS_PER_HOUR = 30
DEVICE_DAYS = 1825
SERVER_DAYS = 3650
CA_DAYS = 7300

lock = threading.Lock()
recent_enrols = []


def openssl(*args, stdin=None):
    """Runs openssl and returns what it printed; raises ValueError with its complaint."""
    done = subprocess.run(["openssl", *args], input=stdin, capture_output=True)
    if done.returncode != 0:
        raise ValueError(done.stderr.decode(errors="replace").strip() or "openssl failed")
    return done.stdout


def write_private(path, data):
    """Writes a file only its owner can read."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def fingerprints(cert_pem):
    """SHA-1 and SHA-256 of a certificate, as nginx writes them: lower-case hex, no colons."""
    der = openssl("x509", "-outform", "DER", stdin=cert_pem)
    return hashlib.sha1(der).hexdigest(), hashlib.sha256(der).hexdigest()


# ---------- the box's own certificates ----------

def ready():
    return all(os.path.isfile(p) for p in (CA_KEY, CA_CRT, os.path.join(GATEWAY, "server.crt")))


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


def init(prefix, upstream, listen):
    """Makes the certificate authority, the gateway's certificate and the gateway's
    configuration, each only if it is not there yet (the configuration always)."""
    for folder in (REMOTE, GATEWAY, os.path.join(GATEWAY, "nginx"), RUN):
        os.makedirs(folder, exist_ok=True)
    os.chmod(REMOTE, 0o700)
    os.chmod(GATEWAY, 0o755)
    if not os.path.isfile(CA_KEY) or not os.path.isfile(CA_CRT):
        write_private(CA_KEY, openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout"))
        crt = openssl("req", "-x509", "-new", "-key", CA_KEY, "-sha256", "-days", str(CA_DAYS),
                      "-subj", "/CN=Fieldmap box " + secrets.token_hex(4),
                      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                      "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        with open(CA_CRT, "wb") as f:
            f.write(crt)
        print("Made this box's certificate authority.")
    server_key = os.path.join(GATEWAY, "server.key")
    server_crt = os.path.join(GATEWAY, "server.crt")
    if not os.path.isfile(server_key) or not os.path.isfile(server_crt):
        write_private(server_key, openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout"))
        request = openssl("req", "-new", "-key", server_key, "-subj", "/CN=fieldmap-box")
        crt = sign(request, SERVER_DAYS, "subjectAltName=DNS:fieldmap-box\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n")
        with open(server_crt, "wb") as f:
            f.write(crt)
        print("Made the gateway's certificate.")
    with open(CA_CRT, "rb") as src, open(os.path.join(GATEWAY, "ca.crt"), "wb") as dst:
        dst.write(src.read())
    if not os.path.isfile(DEVICES):
        save_devices([])
    with open(os.path.join(GATEWAY, "nginx", "default.conf"), "w") as f:
        f.write(gateway_conf(prefix, upstream, listen))
    print("Wrote the gateway's configuration.")


def gateway_conf(prefix, upstream, listen):
    """nginx for the remote gateway: TLS with a certificate demanded from the visitor, every
    request checked against the device list, and only the tile server behind it."""
    if not prefix.startswith("/") or not prefix.endswith("/") or not re.fullmatch(r"[A-Za-z0-9/_.-]+", prefix):
        raise ValueError("the prefix must look like / or /maps/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+/", upstream):
        raise ValueError("the upstream must look like http://127.0.0.1:8080/")
    if not re.fullmatch(r"([0-9.]+:)?[0-9]{2,5}", listen):
        raise ValueError("listen must be a port, or address:port")
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

server {{
    listen {listen} ssl;
    http2 on;
    server_name _;

    ssl_certificate         /etc/fieldmap-remote/server.crt;
    ssl_certificate_key     /etc/fieldmap-remote/server.key;
    ssl_protocols           TLSv1.2 TLSv1.3;
    # No visitor gets past the handshake without a certificate this box issued...
    ssl_client_certificate  /etc/fieldmap-remote/ca.crt;
    ssl_verify_client       on;

    # ...and none gets an answer unless its certificate is still on the device list.
    location = /_device {{
        internal;
        proxy_pass http://unix:/run/fieldmap-remote/remote.sock:/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;
    }}

    location {prefix} {{
        auth_request /_device;
        proxy_pass {upstream};
        proxy_set_header Host $fieldmap_host;
        proxy_set_header X-Forwarded-Proto $fieldmap_proto;
        proxy_read_timeout 300;
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
    tmp = DEVICES + ".tmp"
    write_private(tmp, json.dumps({"devices": devices}, indent=1).encode())
    os.replace(tmp, DEVICES)


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
            raise PermissionError("The device list is full. Remove a device on the box first.")
        now = time.time()
        recent_enrols[:] = [t for t in recent_enrols if now - t < 3600]
        if len(recent_enrols) >= ENROLS_PER_HOUR:
            raise PermissionError("Too many new devices in the last hour. Try again later.")
        recent_enrols.append(now)
        device_id = secrets.token_hex(4)
        # The certificate is for the phone's key; the signing request only carries the name.
        throwaway = openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout")
        with tempfile.TemporaryDirectory(dir=REMOTE) as tmp:
            key = os.path.join(tmp, "key")
            write_private(key, throwaway)
            request = openssl("req", "-new", "-key", key, "-subj", "/CN=fieldmap-device-" + device_id)
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
        return True


def allowed(fingerprint):
    fingerprint = (fingerprint or "").strip().lower().replace(":", "")
    return bool(fingerprint) and any(secrets.compare_digest(fingerprint, d.get("sha1", "")) for d in load_devices())


def remote_address():
    """Where the gateway is reached from outside: {host, port}, or None when not set up."""
    text = os.environ.get("REMOTE_ADDRESS") or ""
    if not text:
        try:
            with open(ADDRESS) as f:
                text = f.read()
        except OSError:
            return None
    m = re.fullmatch(r"\s*([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\]):([0-9]{1,5})\s*", text)
    return {"host": m.group(1), "port": int(m.group(2))} if m else None


# ---------- the service ----------

# The addresses of home and office networks, and of the box itself. Named one by one:
# Python's "is_private" also covers ranges that are not local networks.
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]

FORWARDED = ("X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "CF-Connecting-IP", "True-Client-IP", "Tailscale-Funnel-Request")


def lan_only(headers):
    """True when a request came from the box's own network, through the box's own web server.

    The web server puts the caller's address in X-Real-IP; it must be a private or local one.
    A request that carries a forwarding header came through some proxy or tunnel on the way,
    and is refused: a box whose web port is published to the internet through one would
    otherwise hand out certificates to the world.
    """
    if any(headers.get(h) for h in FORWARDED):
        return False
    try:
        address = ipaddress.ip_address((headers.get("X-Real-IP") or "").strip())
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in network for network in LOCAL_NETWORKS)


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

    def do_GET(self):
        if self.path == "/check":
            self.send(204 if allowed(self.headers.get("X-Fieldmap-Fingerprint")) else 403)
        elif self.path == "/info":
            self.send(200, {"service": "fieldmap-remote", "version": 1, "ready": ready(),
                            "pin": bool(os.environ.get("REMOTE_PIN")), "remote": remote_address()})
        else:
            self.send(404, {"error": "Not here."})

    def do_POST(self):
        if self.path != "/enrol":
            return self.send(404, {"error": "Not here."})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 16384:
            return self.send(400, {"error": "Nothing to read."})
        raw = self.rfile.read(length)
        if not lan_only(self.headers):
            return self.send(403, {"error": "Remote access can only be set up on the box's own network."})
        if not ready():
            return self.send(503, {"error": "Remote access is not set up on this box."})
        try:
            body = json.loads(raw)
            pin = os.environ.get("REMOTE_PIN")
            if pin and not secrets.compare_digest(str(body.get("pin", "")).strip().encode(), pin.encode()):
                return self.send(403, {"error": "That is not the box's code.", "pin": True})
            device = enrol(body.get("name"), str(body.get("publicKey", "")))
        except PermissionError as e:
            return self.send(429, {"error": str(e)})
        except (ValueError, AttributeError) as e:
            return self.send(400, {"error": str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else "That request cannot be read."})
        with open(CA_CRT) as f:
            ca = f.read()
        self.send(200, {"id": device["id"], "name": device["name"], "certificate": device["certificate"],
                        "ca": ca, "remote": remote_address()})


class SocketServer(ThreadingHTTPServer):
    """The same server on a Unix socket (a file) instead of a network port."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", 0


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
    print("It asks for the code in REMOTE_PIN." if os.environ.get("REMOTE_PIN")
          else "Any device on the local network can be let in (REMOTE_PIN=<code> makes it ask for a code).", flush=True)
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
    parser = argparse.ArgumentParser(description="Remote access for the Fieldmap Android app.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="make the certificates and the gateway's configuration")
    p.add_argument("--prefix", default="/", help="where the tile server is on the box's web address: / or /maps/")
    p.add_argument("--upstream", default="http://tileserver:8080/", help="the tile server as the gateway reaches it")
    p.add_argument("--listen", default="8443", help="port (or address:port) the gateway listens on")
    p = sub.add_parser("serve", help="answer the app and the gateway")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="also listen on this port (the socket is always there)")
    sub.add_parser("devices", help="list the devices that are let in")
    p = sub.add_parser("remove", help="lock a device out")
    p.add_argument("id")
    sub.add_parser("status", help="say what is set up")
    args = parser.parse_args()

    if args.command == "init":
        init(args.prefix, args.upstream, args.listen)
    elif args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "devices":
        devices = load_devices()
        if not devices:
            print("No device has been let in yet.")
        for d in devices:
            print(f"{d['id']}  {d['added']}  {d['name']}")
    elif args.command == "remove":
        if remove(args.id):
            print(f"Removed {args.id}: it is locked out from now on.")
        else:
            sys.exit(f"No device with the id {args.id}. See: remote-server.py devices")
    elif args.command == "status":
        address = remote_address()
        print("Certificates:   " + ("made" if ready() else "not made yet (run scripts/remote.sh setup)"))
        print("Reached at:     " + (f"{address['host']}:{address['port']}" if address else "not set (no relay or address yet)"))
        print(f"Devices let in: {len(load_devices())}")
        print("Code to enrol:  " + ("asked for (REMOTE_PIN)" if os.environ.get("REMOTE_PIN") else "none: being on the local network is enough"))


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        sys.exit(f"Error: {e}")
