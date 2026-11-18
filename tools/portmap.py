"""fieldmap-server: asks the home router to let the remote gateway be reached from outside.

No account and no outside service: the box talks only to its own router, the way games
and video calls do, with UPnP (IGD v1 and v2) or NAT-PMP, whichever the router answers.
It also finds the box's own IPv6 addresses, which need no translation, and asks the router
for a hole in its IPv6 firewall where the router offers that (UPnP IGD v2).

reach(state) does it all and returns a plain report; tools/remote-server.py calls it every
few minutes to keep the mapping alive and notice a new address. Python's standard library
and the `ip` command only.
"""
import ipaddress
import json
import random
import re
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SSDP = ("239.255.255.250", 1900)
NATPMP_PORT = 5351
LEASE = 7200                # seconds a mapping is asked for; renewed well before it ends
DESCRIPTION = "fieldmap remote"
PORTS = (20000, 60999)      # outside ports to pick from: away from the well-known ones

CGNAT = ipaddress.ip_network("100.64.0.0/10")
# A router's outside address in one of these means another router stands in front of it.
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")]
SEARCH = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
V6_FIREWALL = "urn:schemas-upnp-org:service:WANIPv6FirewallControl:1"


class Refused(Exception):
    """The router answered, but no."""


# ---------- this machine ----------

def _ip_json(*args):
    try:
        out = subprocess.run(["ip", "-j", *args], capture_output=True, text=True, timeout=5).stdout
        return json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def default_gateway():
    """The router's IPv4 address, or None."""
    for route in _ip_json("-4", "route", "show", "default"):
        if route.get("gateway"):
            return route["gateway"]
    try:                                        # without iproute2's JSON: the kernel's own table
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, IndexError, ValueError):
        pass
    return None


def local_ipv4(toward):
    """This machine's address on the network that leads to `toward`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((toward, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def global_ipv6():
    """This machine's lasting IPv6 addresses that the internet can route to: not the
    temporary privacy ones, not deprecated ones, not unique-local (fc00::/7) ones."""
    out = []
    for link in _ip_json("-6", "addr", "show", "scope", "global", "-temporary", "-deprecated", "-tentative"):
        if link.get("ifname", "").startswith(("docker", "br-", "veth", "tailscale", "wg", "zt")):
            continue
        for a in link.get("addr_info", []):
            try:
                address = ipaddress.ip_address(a.get("local", ""))
            except ValueError:
                continue
            if address.version == 6 and address.is_global and not a.get("dadfailed"):
                out.append(str(address))
    return out


def kind(address):
    """'public', 'shared' (carrier-grade NAT), 'private' or 'none'."""
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return "none"
    if ip in CGNAT:
        return "shared"
    if any(ip in n for n in PRIVATE) or ip.is_unspecified:
        return "private"
    return "public"


# ---------- NAT-PMP (RFC 6886) ----------

def _natpmp(gateway, request, size, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        wait, deadline = 0.25, time.monotonic() + timeout
        while time.monotonic() < deadline:
            s.sendto(request, (gateway, NATPMP_PORT))
            s.settimeout(min(wait, max(0.05, deadline - time.monotonic())))
            try:
                data, sender = s.recvfrom(64)
            except socket.timeout:
                wait *= 2
                continue
            if sender[0] == gateway and len(data) >= size and data[0] == 0 and data[1] == request[1] + 128:
                result = struct.unpack("!H", data[2:4])[0]
                if result:
                    raise Refused("NAT-PMP result %d" % result)
                return data
        return None
    finally:
        s.close()


def natpmp_external(gateway):
    data = _natpmp(gateway, b"\x00\x00", 12)
    return socket.inet_ntoa(data[8:12]) if data else None


def natpmp_map(gateway, internal_port, external_port, lifetime=LEASE):
    """Maps TCP; returns the outside port the router gave (it may differ), or None."""
    data = _natpmp(gateway, struct.pack("!BBHHHI", 0, 2, 0, internal_port, external_port, lifetime), 16)
    return struct.unpack("!H", data[10:12])[0] if data else None


# ---------- UPnP IGD ----------

def ssdp_search(local_ip=None, timeout=2.0):
    """The description addresses of the internet gateways that answer on this network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    found = []
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if local_ip:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        for st in SEARCH:
            s.sendto(("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                      "MX: 1\r\nST: %s\r\n\r\n" % st).encode(), SSDP)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                location = m.group(1).decode(errors="replace")
                if location.startswith("http://") and location not in found:
                    found.append(location)
    except OSError:
        pass
    finally:
        s.close()
    return found


class Gateway:
    """One UPnP internet gateway: where to send which request."""

    def __init__(self, location, timeout=4.0):
        self.timeout = timeout
        with urllib.request.urlopen(location, timeout=timeout) as r:
            root = ET.fromstring(r.read())
        ns = {"d": "urn:schemas-upnp-org:device-1-0"}
        base = (root.findtext("d:URLBase", default="", namespaces=ns) or location).strip()
        self.services = {}
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            kind_ = (svc.findtext("d:serviceType", default="", namespaces=ns) or "").strip()
            control = (svc.findtext("d:controlURL", default="", namespaces=ns) or "").strip()
            if kind_ and control:
                self.services.setdefault(kind_, urllib.parse.urljoin(base, control))
        self.wan = next((t for t in WAN_SERVICES if t in self.services), None)
        if not self.wan:
            raise Refused("not an internet gateway")
        self.host = urllib.parse.urlparse(self.services[self.wan]).hostname

    def call(self, service, action, **args):
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s'
                '</u:%s></s:Body></s:Envelope>') % (
            action, service, "".join("<%s>%s</%s>" % (k, _xml(v), k) for k, v in args.items()), action)
        req = urllib.request.Request(self.services[service], data=body.encode(), method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": '"%s#%s"' % (service, action)})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read()
        except urllib.error.HTTPError as e:
            text = e.read()
            code = re.search(rb"<(?:\w+:)?errorCode>\s*(\d+)", text)
            desc = re.search(rb"<(?:\w+:)?errorDescription>([^<]*)", text)
            raise Refused("%s %s" % (code.group(1).decode() if code else e.code,
                                     desc.group(1).decode(errors="replace") if desc else "")) from None
        out = {}
        for el in ET.fromstring(text).iter():
            tag = el.tag.split("}")[-1]
            if tag.startswith("New") or tag in ("UniqueID", "FirewallEnabled", "InboundPinholeAllowed", "OutboundPinholeTimeout"):
                out[tag] = (el.text or "").strip()
        return out

    def external_ip(self):
        return self.call(self.wan, "GetExternalIPAddress").get("NewExternalIPAddress")

    def map(self, external_port, internal_port, client, lease=LEASE):
        def add(seconds):
            self.call(self.wan, "AddPortMapping", NewRemoteHost="", NewExternalPort=external_port, NewProtocol="TCP",
                      NewInternalPort=internal_port, NewInternalClient=client, NewEnabled=1,
                      NewPortMappingDescription=DESCRIPTION, NewLeaseDuration=seconds)
        try:
            add(lease)
        except Refused as e:
            if str(e).startswith("725"):        # OnlyPermanentLeasesSupported
                add(0)
            else:
                raise
        return external_port

    def pinhole(self, address, port, previous=None, lease=3600):
        """Opens (or keeps open) the router's IPv6 firewall for `address`:`port`. Returns the
        pinhole's id, or None when the router has no such firewall control."""
        if V6_FIREWALL not in self.services:
            return None
        if previous:
            try:
                self.call(V6_FIREWALL, "UpdatePinhole", UniqueID=previous, NewLeaseTime=lease)
                return previous
            except Refused:
                pass
        status = self.call(V6_FIREWALL, "GetFirewallStatus")
        if status.get("FirewallEnabled") == "0":
            return "open"                       # no firewall: nothing to open
        if status.get("InboundPinholeAllowed") == "0":
            raise Refused("the router does not allow IPv6 pinholes")
        return self.call(V6_FIREWALL, "AddPinhole", RemoteHost="", RemotePort=0, InternalClient=address,
                         InternalPort=port, Protocol=6, LeaseTime=lease).get("UniqueID")


def _xml(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- all of it ----------

def reach(internal_port, state, discover=ssdp_search, gateway_ip=None):
    """Asks the router to forward an outside port to this machine's `internal_port`, and
    opens IPv6 where it can. `state` is what the last run returned (or {}): the same outside
    port and pinholes are kept where possible. Returns the new state, with "ipv4", "ipv6"
    and "problem" for the report."""
    out = {"checked": int(time.time()), "internal_port": internal_port, "ipv4": {}, "ipv6": {}, "problem": ""}
    gw = gateway_ip or default_gateway()
    local = local_ipv4(gw) if gw else None
    previous = state.get("ipv4", {})
    want = previous.get("external_port") or random.randint(*PORTS)

    # IPv4: UPnP first (it also says which address the router has outside), then NAT-PMP.
    igd = None
    for location in discover(local):
        try:
            igd = Gateway(location)
            break
        except (OSError, ValueError, ET.ParseError, Refused):
            continue
    v4 = out["ipv4"]
    if igd and local:
        v4["method"] = "upnp"
        try:
            v4["external_ip"] = igd.external_ip()
        except (Refused, OSError, ET.ParseError) as e:
            v4["error"] = "GetExternalIPAddress: " + str(e)[:100]
        try:
            for attempt in range(5):
                try:
                    v4["external_port"] = igd.map(want, internal_port, local)
                    v4.pop("error", None)
                    break
                except Refused as e:
                    if str(e).startswith("718") and attempt < 3:    # the port is taken: another one
                        want = random.randint(*PORTS)
                        continue
                    if str(e).startswith("724") and want != internal_port:   # SamePortValuesRequired
                        want = internal_port
                        continue
                    raise
        except (Refused, OSError, ET.ParseError) as e:
            v4["error"] = "AddPortMapping: " + str(e)[:100]
    if gw and not v4.get("external_port"):
        # No UPnP, or UPnP said no: the same router may still answer NAT-PMP, and it at least
        # says which address it has outside, which tells why.
        try:
            ext = natpmp_external(gw)
            if ext:
                v4.setdefault("method", "natpmp")
                v4.setdefault("external_ip", ext)
                port = natpmp_map(gw, internal_port, want)
                if port:
                    v4["method"], v4["external_port"] = "natpmp", port
                    v4.pop("error", None)
        except (Refused, OSError) as e:
            v4.setdefault("method", "natpmp")
            v4.setdefault("error", "NAT-PMP: " + str(e)[:100])

    address_kind = kind(v4.get("external_ip"))
    if not v4.get("method"):
        out["problem"] = "router-closed"
    elif address_kind == "shared":
        out["problem"] = "shared-address"
    elif address_kind == "private":
        out["problem"] = "double-nat"
    elif not v4.get("external_port"):
        out["problem"] = "router-refused"
    v4["ok"] = not out["problem"]

    # IPv6: no translation, the box's own address. Open the router's firewall where it lets us.
    v6 = out["ipv6"]
    v6["addresses"] = global_ipv6()[:2]
    holes = dict(state.get("ipv6", {}).get("pinholes", {}))
    v6["pinholes"] = {}
    for address in v6["addresses"]:
        if not igd:
            break
        try:
            hole = igd.pinhole(address, internal_port, holes.get(address))
            if hole:
                v6["pinholes"][address] = hole
        except (Refused, OSError, ET.ParseError) as e:
            v6["error"] = str(e)[:120]
    return out


def release(state, discover=ssdp_search, gateway_ip=None):
    """Asks the router to close what reach() opened. Returns what is left open, if anything
    (the router may also let a mapping lapse by itself within LEASE seconds)."""
    gw = gateway_ip or default_gateway()
    local = local_ipv4(gw) if gw else None
    v4, v6 = state.get("ipv4", {}), state.get("ipv6", {})
    left = []
    igd = None
    for location in discover(local):
        try:
            igd = Gateway(location)
            break
        except (OSError, ValueError, ET.ParseError, Refused):
            continue
    if v4.get("external_port"):
        try:
            if v4.get("method") == "upnp" and igd:
                igd.call(igd.wan, "DeletePortMapping", NewRemoteHost="", NewExternalPort=v4["external_port"], NewProtocol="TCP")
            elif v4.get("method") == "natpmp" and gw:
                # Lifetime 0 removes the mapping (RFC 6886 3.4); the outside port must be 0 too.
                _natpmp(gw, struct.pack("!BBHHHI", 0, 2, 0, v4.get("internal_port", 0) or state.get("internal_port", 0), 0, 0), 16)
            else:
                left.append("ipv4")
        except (Refused, OSError, ET.ParseError):
            left.append("ipv4")
    for address, hole in v6.get("pinholes", {}).items():
        if hole == "open":
            continue
        try:
            if not igd:
                raise Refused("no router")
            igd.call(V6_FIREWALL, "DeletePinhole", UniqueID=hole)
        except (Refused, OSError, ET.ParseError):
            left.append(address)
    return left


# ---------- for forwarding by hand ----------

def lan_ipv4():
    """This machine's address on the home network: what a port is forwarded to."""
    gw = default_gateway()
    return local_ipv4(gw) if gw else None


# Public STUN servers, asked only when someone presses "Find it" on the Phones page.
STUN = (("stun.cloudflare.com", 3478), ("stun.l.google.com", 19302))


def stun_public_ip(servers=STUN, timeout=3.0):
    """The address this network has on the internet, as a STUN server sees it (RFC 5389), or None."""
    for host, port in servers:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(timeout)
            tid = random.getrandbits(96).to_bytes(12, "big")
            s.sendto(struct.pack("!HHI", 0x0001, 0, 0x2112A442) + tid, (host, port))
            data, _ = s.recvfrom(2048)
            if len(data) < 20 or data[:2] != b"\x01\x01" or data[8:20] != tid:
                continue
            pos, end = 20, 20 + struct.unpack("!H", data[2:4])[0]
            while pos + 4 <= min(end, len(data)):
                kind_, length = struct.unpack("!HH", data[pos:pos + 4])
                value = data[pos + 4:pos + 4 + length]
                if kind_ in (0x0020, 0x0001) and len(value) >= 8 and value[1] == 1:     # IPv4
                    raw = struct.unpack("!I", value[4:8])[0]
                    if kind_ == 0x0020:
                        raw ^= 0x2112A442
                    return socket.inet_ntoa(struct.pack("!I", raw))
                pos += 4 + length + (-length % 4)
        except (OSError, ValueError):
            continue
        finally:
            s.close()
    return None


def endpoints(state, internal_port):
    """Where a phone can try to reach the gateway from outside, most likely first."""
    out = []
    v4 = state.get("ipv4", {})
    if v4.get("ok"):
        out.append({"host": v4["external_ip"], "port": v4["external_port"]})
    for address in state.get("ipv6", {}).get("addresses", []):
        out.append({"host": "[%s]" % address, "port": internal_port})
    return out
