#!/usr/bin/env python3
"""Tests for tools/portmap.py against a pretend router. Run: python3 -m unittest tools/test_portmap.py"""
import os
import re
import socket
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portmap  # noqa: E402

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
 <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:2</deviceType>
 <deviceList><device><deviceList><device><serviceList>
  <service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:2</serviceType><controlURL>/ctl/IPConn</controlURL></service>
  <service><serviceType>urn:schemas-upnp-org:service:WANIPv6FirewallControl:1</serviceType><controlURL>/ctl/IP6FCtl</controlURL></service>
 </serviceList></device></deviceList></device></deviceList>
</device></root>"""


class PretendRouter:
    """A UPnP internet gateway: answers description, external address, port mappings and pinholes."""

    def __init__(self, external="203.0.113.30", taken=(), permanent_only=False, pinholes=True):
        self.external, self.taken, self.permanent_only, self.pinholes_allowed = external, set(taken), permanent_only, pinholes
        self.mappings, self.pinholes, self.calls = {}, {}, []
        router = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.reply(200, DESCRIPTION)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                args = dict(re.findall(r"<(New\w+|\w+)>([^<]*)</\1>", body))
                router.calls.append((action, args))
                status, inner = router.answer(action, args)
                self.reply(status, '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                                   '<s:Body>%s</s:Body></s:Envelope>' % inner)

            def reply(self, status, text):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.location = "http://127.0.0.1:%d/rootDesc.xml" % self.server.server_port

    def fault(self, code, text):
        return 500, ("<s:Fault><detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\"><errorCode>%d</errorCode>"
                     "<errorDescription>%s</errorDescription></UPnPError></detail></s:Fault>" % (code, text))

    def answer(self, action, a):
        if action == "GetExternalIPAddress":
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'><NewExternalIPAddress>%s</NewExternalIPAddress></u:R>" % self.external
        if action == "AddPortMapping":
            if self.permanent_only and a["NewLeaseDuration"] != "0":
                return self.fault(725, "OnlyPermanentLeasesSupported")
            port = int(a["NewExternalPort"])
            if port in self.taken:
                return self.fault(718, "ConflictInMappingEntry")
            self.mappings[port] = (a["NewInternalClient"], int(a["NewInternalPort"]), a["NewLeaseDuration"])
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'/>"
        if action == "GetFirewallStatus":
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'><FirewallEnabled>1</FirewallEnabled><InboundPinholeAllowed>%d</InboundPinholeAllowed></u:R>" % self.pinholes_allowed
        if action == "AddPinhole":
            hole = str(len(self.pinholes) + 1)
            self.pinholes[hole] = (a["InternalClient"], a["InternalPort"])
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'><UniqueID>%s</UniqueID></u:R>" % hole
        if action == "DeletePortMapping":
            self.mappings.pop(int(a["NewExternalPort"]), None)
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'/>"
        if action == "DeletePinhole":
            self.pinholes.pop(a["UniqueID"], None)
            return 200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'/>"
        if action == "UpdatePinhole":
            return (200, "<u:R xmlns:u='urn:schemas-upnp-org:service:WANIPConnection:2'/>") if a["UniqueID"] in self.pinholes else self.fault(704, "NoSuchEntry")
        return self.fault(401, "Invalid Action")

    def close(self):
        self.server.shutdown()


class PretendNatPmp:
    """A NAT-PMP router on a local UDP port."""

    def __init__(self, external="198.51.100.20", answer_port=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.external, self.answer_port, self.requests = external, answer_port, []
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                data, peer = self.sock.recvfrom(64)
            except OSError:
                return
            self.requests.append(data)
            if data[1] == 0:
                self.sock.sendto(struct.pack("!BBHI", 0, 128, 0, 1) + socket.inet_aton(self.external), peer)
            elif data[1] == 2:
                _, _, _, internal, external, life = struct.unpack("!BBHHHI", data)
                self.sock.sendto(struct.pack("!BBHIHHI", 0, 130, 0, 1, internal, self.answer_port or external, life), peer)

    def close(self):
        self.sock.close()


class PortmapTest(unittest.TestCase):
    def setUp(self):
        # This machine's own IPv6 addresses are not the test's business.
        self.v6 = mock.patch.object(portmap, "global_ipv6", return_value=["2001:db8:77::10"])
        self.v6.start()

    def tearDown(self):
        self.v6.stop()

    def test_upnp_maps_a_port_and_keeps_it(self):
        router = PretendRouter()
        try:
            state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            v4 = state["ipv4"]
            self.assertEqual(("upnp", "203.0.113.30", True, ""), (v4["method"], v4["external_ip"], v4["ok"], state["problem"]))
            self.assertIn(v4["external_port"], router.mappings)
            self.assertEqual(("127.0.0.1", 8443, "7200"), router.mappings[v4["external_port"]])
            self.assertTrue(portmap.PORTS[0] <= v4["external_port"] <= portmap.PORTS[1])
            # The next run asks for the same port again.
            again = portmap.reach(8443, state, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertEqual(v4["external_port"], again["ipv4"]["external_port"])
            self.assertEqual([{"host": "203.0.113.30", "port": v4["external_port"]}, {"host": "[2001:db8:77::10]", "port": 8443}],
                             portmap.endpoints(again, 8443))
        finally:
            router.close()

    def test_a_taken_port_is_swapped_for_another(self):
        router = PretendRouter()
        try:
            with mock.patch.object(portmap.random, "randint", side_effect=[40000, 40001]):
                router.taken = {40000}
                state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertEqual(40001, state["ipv4"]["external_port"])
        finally:
            router.close()

    def test_a_router_that_only_takes_permanent_mappings(self):
        router = PretendRouter(permanent_only=True)
        try:
            state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertTrue(state["ipv4"]["ok"])
            self.assertEqual("0", list(router.mappings.values())[0][2])
        finally:
            router.close()

    def test_an_address_shared_by_the_provider_is_named(self):
        router = PretendRouter(external="100.72.1.9")
        try:
            state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertEqual("shared-address", state["problem"])
            self.assertFalse(state["ipv4"]["ok"])
            self.assertEqual([{"host": "[2001:db8:77::10]", "port": 8443}], portmap.endpoints(state, 8443), "IPv6 is still offered")
        finally:
            router.close()

    def test_two_routers_in_a_row_are_named(self):
        router = PretendRouter(external="192.168.0.20")
        try:
            self.assertEqual("double-nat", portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")["problem"])
        finally:
            router.close()

    def test_ipv6_pinholes_are_opened_then_renewed(self):
        router = PretendRouter()
        try:
            state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertEqual({"2001:db8:77::10": "1"}, state["ipv6"]["pinholes"])
            self.assertEqual(("2001:db8:77::10", "8443"), router.pinholes["1"])
            portmap.reach(8443, state, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertIn(("UpdatePinhole", {"UniqueID": "1", "NewLeaseTime": "3600"}), router.calls)
            self.assertEqual(1, len(router.pinholes))
        finally:
            router.close()

    def test_switching_off_closes_what_was_opened(self):
        router = PretendRouter()
        try:
            state = portmap.reach(8443, {}, discover=lambda local: [router.location], gateway_ip="127.0.0.1")
            self.assertEqual((1, 1), (len(router.mappings), len(router.pinholes)))
            self.assertEqual([], portmap.release(state, discover=lambda local: [router.location], gateway_ip="127.0.0.1"))
            self.assertEqual((0, 0), (len(router.mappings), len(router.pinholes)))
        finally:
            router.close()

    def test_nat_pmp_when_no_upnp_answers(self):
        router = PretendNatPmp(answer_port=45678)
        try:
            with mock.patch.object(portmap, "NATPMP_PORT", router.port):
                state = portmap.reach(8443, {}, discover=lambda local: [], gateway_ip="127.0.0.1")
            v4 = state["ipv4"]
            self.assertEqual(("natpmp", "198.51.100.20", 45678, True), (v4["method"], v4["external_ip"], v4["external_port"], v4["ok"]))
            with mock.patch.object(portmap, "NATPMP_PORT", router.port):
                self.assertEqual([], portmap.release(state, discover=lambda local: [], gateway_ip="127.0.0.1"))
            self.assertEqual(struct.pack("!BBHHHI", 0, 2, 0, 8443, 0, 0), router.requests[-1], "lifetime 0 removes it")
        finally:
            router.close()

    def test_a_router_that_answers_nothing(self):
        with mock.patch.object(portmap, "NATPMP_PORT", 9), mock.patch.object(portmap, "_natpmp", return_value=None):
            state = portmap.reach(8443, {}, discover=lambda local: [], gateway_ip="127.0.0.1")
        self.assertEqual("router-closed", state["problem"])
        self.assertEqual([{"host": "[2001:db8:77::10]", "port": 8443}], portmap.endpoints(state, 8443))

    def test_address_kinds(self):
        self.assertEqual("public", portmap.kind("203.0.113.30"))
        self.assertEqual("shared", portmap.kind("100.64.0.1"))
        self.assertEqual("private", portmap.kind("10.1.2.3"))
        self.assertEqual("none", portmap.kind(None))


if __name__ == "__main__":
    unittest.main()
