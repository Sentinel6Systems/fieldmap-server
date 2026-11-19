#!/usr/bin/env python3
"""Tests for tools/remote-server.py. Run: python3 -m unittest tools/test_remote_server.py"""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(folder):
    """The service as a module, keeping its files in `folder`."""
    os.environ["REMOTE_DIR"] = folder
    spec = importlib.util.spec_from_file_location("remote_server", os.path.join(HERE, "remote-server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def phone_key():
    """A key pair as a phone makes it: the private key, and the public key in PEM."""
    private = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"], capture_output=True, check=True).stdout
    public = subprocess.run(["openssl", "pkey", "-pubout"], input=private, capture_output=True, check=True).stdout
    return private, public.decode()


class FakeRequest:
    """Just enough of BaseHTTPRequestHandler to call do_GET / do_POST directly. Files the
    handler writes straight out (the page) are caught too."""

    def __init__(self, rs, method, path, headers=None, body=None):
        self.rs, self.path = rs, path
        data = json.dumps(body).encode() if body is not None else b""
        self.headers = dict(headers or {})
        if data:
            self.headers["Content-Length"] = str(len(data))
        handler = rs.Handler.__new__(rs.Handler)
        handler.path, handler.headers, handler.rfile = path, self.headers, io.BytesIO(data)
        self.out = {}
        self.sent_headers = {}
        handler.send = lambda status, payload=None: self.out.update(status=status, body=payload)
        handler.send_response = lambda status: self.out.update(status=status)
        handler.send_header = lambda name, value: self.sent_headers.update({name: value})
        handler.end_headers = lambda: None
        handler.wfile = self.written = io.BytesIO()
        getattr(handler, "do_" + method)()

    @property
    def status(self):
        return self.out["status"]

    @property
    def body(self):
        return self.out["body"]


LAN = {"X-Real-IP": "192.168.1.20"}


class RemoteServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rs = load(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.rs.init("/maps/", "http://127.0.0.1:8080/", "8443", True)
        self.no_v6 = mock.patch.object(self.rs.portmap, "global_ipv6", return_value=[])
        self.no_v6.start()
        # The router is never really asked (tests that look at it patch these again).
        self.no_router = [mock.patch.object(self.rs.portmap, "release", return_value=[]),
                          mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 1, "ipv4": {}, "ipv6": {}, "problem": "router-closed"})]
        for p in self.no_router:
            p.start()
        # Most tests are about a box whose owner switched remote access on (it is off by default).
        self.switch_on()

    def switch_on(self):
        self.rs.write_private(self.rs.SETTINGS, json.dumps({"enabled": True}).encode())

    def tearDown(self):
        self.rs.time.sleep(0.05)            # the router is told in the background
        for p in self.no_router:
            p.stop()
        self.no_v6.stop()
        self.tmp.cleanup()
        for name in ("REMOTE_ADDRESS", "REMOTE_PIN"):
            os.environ.pop(name, None)

    def get(self, path, headers=LAN):
        return FakeRequest(self.rs, "GET", path, headers)

    def post(self, path, body, headers=LAN):
        return FakeRequest(self.rs, "POST", path, headers, body)

    # ---------- certificates ----------

    def test_init_makes_everything_once_with_names_that_say_nothing(self):
        self.assertTrue(self.rs.ready())
        before = open(self.rs.CA_CRT).read()
        with contextlib.redirect_stdout(io.StringIO()):
            self.rs.init("/", "http://tileserver:8080/", "8443", False)
        self.assertEqual(before, open(self.rs.CA_CRT).read(), "a second init keeps the authority")
        self.assertEqual(0o600, os.stat(self.rs.CA_KEY).st_mode & 0o777)
        self.assertFalse(os.path.exists(os.path.join(self.rs.GATEWAY, "ca.key")), "the gateway never gets the authority's key")
        name = self.rs.gateway_name()
        self.assertRegex(name, r"^b[0-9a-f]{20}$")
        for path in (self.rs.CA_CRT, os.path.join(self.rs.GATEWAY, "server.crt")):
            text = subprocess.run(["openssl", "x509", "-in", path, "-noout", "-subject", "-ext", "subjectAltName"],
                                  capture_output=True, text=True).stdout
            self.assertNotRegex(text.lower(), "fieldmap|box", "nothing in a certificate names the project")
        self.assertIn("DNS:" + name, subprocess.run(["openssl", "x509", "-in", os.path.join(self.rs.GATEWAY, "server.crt"),
                                                     "-noout", "-ext", "subjectAltName"], capture_output=True, text=True).stdout)

    def test_a_phone_gets_a_certificate_for_its_own_key(self):
        _, public = phone_key()
        device = self.rs.enrol("Test phone", public)
        self.assertTrue(self.rs.allowed(device["sha1"]))
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(device["certificate"])
            f.flush()
            subprocess.run(["openssl", "verify", "-CAfile", self.rs.CA_CRT, "-purpose", "sslclient", f.name], capture_output=True, check=True)
            in_cert = subprocess.run(["openssl", "x509", "-in", f.name, "-noout", "-pubkey"], capture_output=True, check=True).stdout.decode()
            as_server = subprocess.run(["openssl", "verify", "-CAfile", self.rs.CA_CRT, "-purpose", "sslserver", f.name], capture_output=True)
            subject = subprocess.run(["openssl", "x509", "-in", f.name, "-noout", "-subject"], capture_output=True, text=True).stdout
        self.assertEqual(public.strip(), in_cert.strip())
        self.assertNotEqual(0, as_server.returncode, "a phone's certificate cannot pose as the box")
        self.assertNotIn("fieldmap", subject.lower())

    def test_the_same_key_twice_is_one_phone_and_removal_locks_out(self):
        _, public = phone_key()
        first = self.rs.enrol("Phone", public)
        self.assertEqual(first["id"], self.rs.enrol("Phone again", public)["id"])
        self.assertEqual(1, len(self.rs.load_devices()))
        self.assertTrue(self.rs.remove(first["id"]))
        self.assertFalse(self.rs.allowed(first["sha1"]))
        self.assertFalse(self.rs.remove(first["id"]))

    def test_only_p256_public_keys_are_taken(self):
        with self.assertRaises(ValueError):
            self.rs.enrol("Phone", "not a key")
        rsa = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
        rsa_public = subprocess.run(["openssl", "pkey", "-pubout"], input=rsa, capture_output=True, check=True).stdout.decode()
        with self.assertRaises(ValueError):
            self.rs.enrol("Phone", rsa_public)

    # ---------- pairing ----------

    def test_pairing_works_only_while_open(self):
        _, public = phone_key()
        closed = self.post("/enrol", {"name": "Phone", "publicKey": public})
        self.assertEqual(403, closed.status)
        self.assertTrue(closed.body["closed"])
        self.assertEqual(200, self.post("/pairing", {"open": True}).status)
        self.assertTrue(0 < self.rs.pairing_left() <= self.rs.PAIRING_SECONDS)
        paired = self.post("/enrol", {"name": "Phone", "publicKey": public})
        self.assertEqual(200, paired.status)
        self.assertEqual(self.rs.gateway_name(), paired.body["gatewayName"])
        self.assertIn("BEGIN CERTIFICATE", paired.body["ca"])
        self.post("/pairing", {"open": False})
        _, other = phone_key()
        self.assertEqual(403, self.post("/enrol", {"name": "Other", "publicKey": other}).status)

    def test_only_the_local_network_may_pair_or_manage(self):
        self.rs.open_pairing()
        _, public = phone_key()
        outsiders = [{"X-Real-IP": "203.0.113.30"}, {"X-Real-IP": "100.64.0.9"}, {"X-Real-IP": "2001:db8::1"},
                     {"X-Real-IP": "nonsense"}, {"X-Real-IP": "192.168.1.20", "X-Forwarded-For": "203.0.113.30"},
                     {"X-Real-IP": "127.0.0.1", "Tailscale-Funnel-Request": "?1"}]
        for headers in outsiders:
            self.assertEqual(403, self.post("/enrol", {"name": "x", "publicKey": public}, headers).status, headers)
            self.assertEqual(403, self.post("/pairing", {"open": True}, headers).status, headers)
            self.assertEqual(403, self.post("/remove", {"id": "x"}, headers).status, headers)
            self.assertEqual(403, self.get("/devices", headers).status, headers)
            self.assertEqual(403, self.get("/info", headers).status, headers)
        for headers in [{"X-Real-IP": "10.0.0.7"}, {"X-Real-IP": "172.20.1.1"}, {"X-Real-IP": "fd00::5"},
                        {"X-Real-IP": "::ffff:192.168.1.20"}, {}]:
            self.assertEqual(200, self.get("/info", headers).status, headers)

    def test_phones_at_home_with_a_global_ipv6_address_count_as_local(self):
        with mock.patch.object(self.rs.portmap, "global_ipv6", return_value=["2001:db8:77::10"]):
            self.assertEqual(200, self.get("/info", {"X-Real-IP": "2001:db8:77::abcd"}).status)
            self.assertEqual(403, self.get("/info", {"X-Real-IP": "2001:db8:78::abcd"}).status)

    def test_the_box_code_guards_pairing_and_removal(self):
        os.environ["REMOTE_PIN"] = "4711"
        self.assertEqual(403, self.post("/pairing", {"open": True}).status)
        self.assertEqual(200, self.post("/pairing", {"open": True, "pin": "4711"}).status)
        _, public = phone_key()
        self.assertTrue(self.post("/enrol", {"name": "x", "publicKey": public}).body.get("pin"))
        self.assertEqual(200, self.post("/enrol", {"name": "x", "publicKey": public, "pin": " 4711 "}).status)

    def test_the_check_answers_only_the_gateway(self):
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        self.assertEqual(204, self.get("/check", {"X-Fieldmap-Fingerprint": device["sha1"]}).status)
        self.assertEqual(403, self.get("/check", {"X-Fieldmap-Fingerprint": device["sha1"], "X-Real-IP": "192.168.1.20"}).status,
                         "not through the web server")
        self.assertEqual(403, self.get("/check", {"X-Fieldmap-Fingerprint": "0" * 40}).status)
        self.assertEqual(403, self.get("/check", {}).status)

    def test_coming_in_from_outside_is_written_down(self):
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        self.get("/check", {"X-Fieldmap-Fingerprint": device["sha1"]})
        self.assertTrue(self.get("/devices").body["devices"][0]["seen"] > 0, "shown at once")
        self.assertEqual("confirmed", self.rs.report()["status"], "and counted at once")
        self.rs.write_seen()
        self.assertTrue(self.rs.load_devices()[0]["seen"] > 0, "and written down")
        self.assertEqual("confirmed", self.rs.report()["status"])

    # ---------- the switch ----------

    def test_switched_off_is_complete_isolation_and_keeps_the_phones(self):
        _, public = phone_key()
        self.rs.open_pairing()
        device = self.rs.enrol("Phone", public)
        self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "", "ipv4": {"ok": True, "external_ip": "203.0.113.30",
                                                                                              "external_port": 41000}, "ipv6": {}}).encode())
        with mock.patch.object(self.rs.portmap, "release", return_value=[]) as release:
            off = self.post("/enabled", {"on": False})
            self.rs.time.sleep(0.2)                 # the router is told in the background
        self.assertEqual(200, off.status)
        self.assertFalse(off.body["enabled"])
        release.assert_called_once()
        self.assertEqual(401, self.get("/check", {"X-Fieldmap-Fingerprint": device["sha1"]}).status, "401: switched off, not removed")
        self.assertEqual([], self.get("/info").body["endpoints"])
        self.assertEqual("off", self.get("/info").body["reach"]["status"])
        self.assertEqual(0, self.rs.pairing_left(), "pairing closes")
        self.assertEqual(409, self.post("/pairing", {"open": True}).status)
        _, other = phone_key()
        self.assertEqual(403, self.post("/enrol", {"name": "x", "publicKey": other}).status)
        self.assertEqual(1, len(self.rs.load_devices()), "the phones are kept")
        self.assertFalse(json.load(open(self.rs.SETTINGS))["enabled"])
        self.assertEqual(0o644, os.stat(self.rs.SETTINGS).st_mode & 0o777, "the firewall reads it")
        with mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 2, "ipv4": {"ok": True, "external_ip": "203.0.113.30",
                                                                                                "external_port": 41000}, "ipv6": {}, "problem": ""}):
            self.assertTrue(self.post("/enabled", {"on": True}).body["enabled"])
            self.rs.time.sleep(0.2)
        self.assertEqual(204, self.get("/check", {"X-Fieldmap-Fingerprint": device["sha1"]}).status)

    def test_the_phones_page_and_its_header_are_served_on_the_local_network_only(self):
        page = self.get("/")
        self.assertEqual(200, page.status, page.out)
        for path in ("/fieldmap.css", "/nav.js", "/index.html", "/remote.html"):
            self.assertEqual(200, self.get(path).status, path)
        self.assertEqual(self.get("/").written.getvalue(), self.get("/remote.html").written.getvalue(),
                         "Devices and Admin's Remote access page are one file")
        self.assertEqual(403, self.get("/remote.html", {"X-Real-IP": "203.0.113.30"}).status)
        for path in ("/../tools/remote-server.py", "/devices.json", "/ca.key", "/x.py"):
            self.assertEqual(404, self.get(path).status, path)
        self.assertEqual(403, self.get("/", {"X-Real-IP": "203.0.113.30"}).status)

    # ---------- addresses ----------

    def test_addresses_from_the_router_and_set_by_hand(self):
        self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "", "ipv4": {"ok": True, "external_ip": "203.0.113.30", "external_port": 41000},
                                                          "ipv6": {"addresses": ["2001:db8:77::10"]}}).encode())
        os.environ["REMOTE_ADDRESS"] = "tak.example.com:8443"
        self.assertEqual([{"host": "tak.example.com", "port": 8443}, {"host": "203.0.113.30", "port": 41000},
                          {"host": "[2001:db8:77::10]", "port": 8443}], self.get("/info").body["endpoints"])
        self.assertEqual("expected", self.rs.report()["status"])

    def test_the_report_names_why_nothing_gets_in(self):
        self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "shared-address", "ipv4": {"ok": False}, "ipv6": {"addresses": []}}).encode())
        r = self.rs.report()
        self.assertEqual("blocked", r["status"])
        self.assertIn("shares one address", r["text"])
        self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "router-closed", "ipv4": {}, "ipv6": {"addresses": ["2001:db8:77::10"]}}).encode())
        self.assertEqual("maybe", self.rs.report()["status"])

    # ---------- the gateway ----------

    def test_the_gateway_serves_only_what_the_app_needs(self):
        conf = self.rs.gateway_conf("/maps/", "http://127.0.0.1:8080/", "8443", True)
        self.assertIn("ssl_verify_client       on;", conf)
        self.assertIn("auth_request /_device;", conf)
        self.assertIn("listen [::]:8443 ssl;", conf)
        self.assertIn("server_tokens off;", conf)
        allowed = re.search(r'if \(\$uri !~ "(.*)"\)', conf).group(1)
        for path in ["/maps/styles.json", "/maps/styles/topo-light/style.json", "/maps/styles/topo-light/sprite@2x.png",
                     "/maps/fonts/Open Sans Regular,Open Sans Bold/0-255.pbf", "/maps/data/basemap.json",
                     "/maps/data/basemap/14/8529/5974.pbf", "/maps/data/hillshade/12/2132/1493.webp"]:
            self.assertRegex(path, allowed)
        for path in ["/maps/styles/topo-light/14/8529/5974.png", "/maps/styles/topo-light/wmts.xml", "/maps/",
                     "/maps/index.html", "/mapprinter/", "/mapadmin/", "/maps/data/basemap/", "/maps/../etc/passwd",
                     "/maps/data/basemap/14/1/1.pbf/x", "/maps/files/nav.js"]:
            self.assertNotRegex(path, allowed)
        self.assertIn("location / { return 404; }", conf)
        root = self.rs.gateway_conf("/", "http://tileserver:8080/", "8443", False)
        self.assertNotIn("listen [::]", root)
        self.assertEqual(1, root.count("location / {"), "one catch-all only, or nginx refuses to start")
        for bad in (("maps/", "http://127.0.0.1:8080/", "8443"), ("/maps/", "127.0.0.1:8080", "8443"), ("/maps/", "http://127.0.0.1:8080/", "x")):
            with self.assertRaises(ValueError):
                self.rs.gateway_conf(*bad)

    def test_the_gateway_lets_live_sharing_through_and_nothing_else_of_it(self):
        for prefix in ("/maps/", "/"):
            conf = self.rs.gateway_conf(prefix, "http://127.0.0.1:8080/", "8443", True)
            blocks = re.findall(r"    location ([^\n]*mapshare[^\n]*) \{\n(.*?)\n    \}\n", conf, re.S)
            self.assertEqual(["= /mapshare/info", "= /mapshare/state", "= /mapshare/position", "= /mapshare/leave",
                              '~ "^/mapshare/markers/[A-Za-z0-9-]{8,64}$"'], [match for match, _ in blocks])
            methods = {}
            for match, block in blocks:
                methods[match] = re.search(r"if \(\$request_method !~ \^\((.*)\)\$\) \{ return 405; \}", block).group(1)
                self.assertIn("auth_request /_device;", block, "only paired phones")
                self.assertIn('proxy_set_header X-Real-IP "";', block, "a phone outside cannot pass for one at home")
                self.assertIn("proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;", block)
                self.assertIn("limit_req zone=fieldmap_share", block)
                self.assertRegex(block, r"client_max_body_size [128]k;")
            self.assertEqual(["GET", "GET", "POST", "POST", "PUT|DELETE"], list(methods.values()))
            self.assertIn("location /mapshare/ { return 404; }", conf, "the Devices page's switch is not among them")
            self.assertNotIn("admin", conf)
            self.assertEqual(1, conf.count("location / {"), "one catch-all only, or nginx refuses to start")

    def test_devices_on_the_local_network_can_download_the_app(self):
        self.assertIsNone(self.get("/info").body["app"])
        self.assertEqual(404, self.get("/fieldmap.apk").status, "no app on the box yet")
        folder = tempfile.mkdtemp(dir=self.tmp.name)
        apk = b"PK" + b"x" * 300000
        with open(os.path.join(folder, "fieldmap.apk"), "wb") as f:
            f.write(apk)
        with open(os.path.join(folder, "app.json"), "w") as f:
            json.dump({"version": 'v1.2.0"; x', "size": len(apk), "sha256": "ab" * 32, "source": "github", "added": "2026-01-31"}, f)
        with mock.patch.multiple(self.rs, APK=os.path.join(folder, "fieldmap.apk"), APP_INFO=os.path.join(folder, "app.json")):
            info = self.get("/info").body["app"]
            self.assertEqual({"version": "v1.2.0x", "size": len(apk), "sha256": "ab" * 32, "source": "release", "added": "2026-01-31"}, info)
            r = self.get("/fieldmap.apk")
            self.assertEqual(200, r.status)
            self.assertEqual(apk, r.written.getvalue())
            self.assertEqual("application/vnd.android.package-archive", r.sent_headers["Content-Type"])
            self.assertEqual('attachment; filename="fieldmap-v1.2.0x.apk"', r.sent_headers["Content-Disposition"])
            self.assertEqual(str(len(apk)), r.sent_headers["Content-Length"])
            self.assertEqual(403, self.get("/fieldmap.apk", {"X-Real-IP": "203.0.113.30"}).status, "only on the box's own network")
            self.assertEqual(403, self.get("/fieldmap.apk", {"X-Real-IP": "192.168.1.20", "X-Forwarded-For": "203.0.113.30"}).status)
            with open(os.path.join(folder, "fieldmap.apk"), "ab") as f:
                f.write(b"half a new file")
            self.assertIsNone(self.get("/info").body["app"], "a file that does not match its description is not offered")
            self.assertEqual(404, self.get("/fieldmap.apk").status)
        conf = self.rs.gateway_conf("/maps/", "http://127.0.0.1:8080/", "8443", True)
        self.assertNotIn("apk", conf, "never through the gateway")

    def test_off_by_default_and_nothing_opens_by_itself(self):
        os.remove(self.rs.SETTINGS)
        self.assertFalse(self.rs.enabled())
        self.assertEqual("off", self.rs.report()["status"])
        self.assertEqual([], self.rs.endpoints())
        with mock.patch.object(self.rs.portmap, "reach") as reach:
            self.assertEqual("switched-off", self.rs.check_router()["problem"])
            reach.assert_not_called()
        with mock.patch.object(self.rs, "SocketServer"), mock.patch.object(self.rs.threading, "Thread"), mock.patch.object(self.rs.signal, "signal"), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(self.rs.os, "chmod"), mock.patch.object(self.rs.os, "chdir"):
            self.rs.serve("127.0.0.1", 0)
        self.assertEqual(0, self.rs.pairing_left(), "starting the box opens nothing")
        self.rs.open_pairing()
        self.assertEqual(0, self.rs.pairing_left(), "pairing cannot open while it is off")
        _, public = phone_key()
        self.assertEqual(403, self.post("/enrol", {"name": "x", "publicKey": public}).status)

    def test_it_switches_itself_off_after_90_days_without_a_phone_from_outside(self):
        with mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 1, "ipv4": {}, "ipv6": {}, "problem": ""}), \
                mock.patch.object(self.rs.portmap, "release", return_value=[]):
            self.rs.set_enabled(True)
            self.rs.time.sleep(0.2)
            day = 86400
            switched_on = self.rs.load_settings()["since"]
            self.assertEqual(switched_on + 90 * day, self.rs.auto_off_at())
            self.assertFalse(self.rs.switch_off_if_idle(switched_on + 89 * day))
            # A phone coming in from outside moves the date on.
            _, public = phone_key()
            device = self.rs.enrol("Phone", public)
            self.rs.last_seen[device["id"]] = switched_on + 60 * day
            self.assertFalse(self.rs.switch_off_if_idle(switched_on + 91 * day))
            self.assertTrue(self.rs.switch_off_if_idle(switched_on + 151 * day))
            self.rs.time.sleep(0.2)
        self.assertFalse(self.rs.enabled())
        self.assertIn("switched off automatically", self.rs.report()["text"])
        self.assertEqual(1, len(self.rs.load_devices()), "the phones are kept")

    def test_the_automatic_switch_off_can_be_unticked(self):
        self.assertTrue(self.rs.auto_off(), "ticked by default")
        info = self.post("/auto-off", {"on": False}).body
        self.assertEqual({"on": False, "days": 90, "at": 0}, info["autoOff"])
        self.assertFalse(self.rs.switch_off_if_idle(self.rs.time.time() + 1000 * 86400))
        self.assertTrue(self.rs.enabled())
        self.assertTrue(self.post("/auto-off", {"on": True}).body["autoOff"]["on"])

    def test_forwarding_by_hand(self):
        os.environ["REMOTE_PORT"] = "41234"
        try:
            self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "", "ipv4": {"ok": True, "method": "upnp",
                "external_ip": "203.0.113.30", "external_port": 50000}, "ipv6": {}}).encode())
            with mock.patch.object(self.rs.portmap, "release", return_value=[]) as release, \
                    mock.patch.object(self.rs.portmap, "lan_ipv4", return_value="192.168.1.10"):
                info = self.post("/forwarding", {"manual": True}).body
                self.rs.time.sleep(0.2)
                release.assert_called_once()      # the port the router opened by itself is closed
                self.assertEqual({"manual": True, "address": "", "port": 41234, "boxAddress": "192.168.1.10", "changingTo": None}, info["forwarding"])
                self.assertEqual("blocked", info["reach"]["status"], "no internet address given yet")
                self.assertEqual(400, self.post("/forwarding", {"manual": True, "address": "not an address!"}).status)
                info = self.post("/forwarding", {"manual": True, "address": " http://tak.example.com/ "}).body
                self.rs.time.sleep(0.2)
            self.assertEqual("tak.example.com", info["forwarding"]["address"])
            self.assertIn({"host": "tak.example.com", "port": 41234}, info["endpoints"])
            self.assertEqual("expected", info["reach"]["status"])
            with mock.patch.object(self.rs.portmap, "reach") as reach, mock.patch.object(self.rs.portmap, "global_ipv6", return_value=["2001:db8:77::10"]):
                state = self.rs.check_router()
                reach.assert_not_called()         # the router is not asked while forwarding by hand
            self.assertEqual("manual", state["problem"])
            self.assertEqual([{"host": "tak.example.com", "port": 41234}, {"host": "[2001:db8:77::10]", "port": 41234}], self.rs.endpoints())
            self.assertEqual("[2001:db8::30]", self.rs.clean_address("2001:db8::30"))
            self.assertEqual("203.0.113.30", self.rs.clean_address("203.0.113.30"))
        finally:
            os.environ.pop("REMOTE_PORT", None)

    def test_a_new_random_port_is_written_to_env_and_applied(self):
        env = os.path.join(self.tmp.name, ".env")
        with open(env, "w") as f:
            f.write("TILESERVER_MEM_LIMIT=1g\nREMOTE_PORT=41234\n")
        with mock.patch.object(self.rs, "ENV", env):
            info = self.post("/new-port", {})
        self.assertEqual(202, info.status)
        lines = open(env).read().splitlines()
        self.assertEqual("TILESERVER_MEM_LIMIT=1g", lines[0])
        self.assertEqual(1, sum(l.startswith("REMOTE_PORT=") for l in lines))
        port = int([l for l in lines if l.startswith("REMOTE_PORT=")][0].split("=")[1])
        self.assertTrue(20000 <= port <= 60999 and port != 41234)
        self.assertTrue(os.path.isfile(self.rs.APPLY), "remote.sh setup is asked to apply it")
        self.assertEqual(port, info.body["forwarding"]["changingTo"])

    def test_switching_on_also_opens_pairing(self):
        os.remove(self.rs.SETTINGS)
        with mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 1, "ipv4": {}, "ipv6": {}, "problem": "router-closed"}):
            info = self.post("/enabled", {"on": True}).body
            self.rs.time.sleep(0.2)
        self.assertTrue(info["enabled"])
        self.assertTrue(info["pairing"]["open"])
        self.assertTrue(0 < self.rs.pairing_left() <= self.rs.PAIRING_SECONDS)


    # ---------- NetBird, the other way in ----------

    KEY = "test-setup-key-for-fieldmap-0001"

    def netbird_says(self, connected=True, ip="100.92.14.7/16", error="", age=0):
        """What tools/netbird-run.sh writes for the service: `netbird status --json`."""
        os.makedirs(self.rs.NETBIRD_DIR, exist_ok=True)
        with open(self.rs.NETBIRD_STATUS, "w") as f:
            json.dump({"management": {"url": "https://api.netbird.io:443", "connected": connected, "error": error},
                       "netbirdIp": ip, "fqdn": "fieldmap-box.netbird.cloud", "peers": {"total": 1, "connected": 1}}, f)
        os.utime(self.rs.NETBIRD_STATUS, (time.time() - age, time.time() - age))
        if os.path.isfile(self.rs.NETBIRD_APPLY):       # the client says which request it was started for
            with open(self.rs.NETBIRD_APPLY) as src, open(self.rs.NETBIRD_STATUS_FOR, "w") as dst:
                dst.write(src.read())
        self.rs._network.update(at=0.0, network=None)

    def connect_netbird(self, **more):
        return self.post("/netbird", {"on": True, "setupKey": self.KEY, **more})

    def test_netbird_is_not_there_until_the_owner_sets_it_up(self):
        _, public = phone_key()
        self.rs.open_pairing()
        answer = self.post("/enrol", {"name": "Phone", "publicKey": public})
        self.assertNotIn("netbird", answer.body)
        info = self.get("/info").body
        self.assertEqual((False, "", "none", False), (info["netbird"]["on"], info["netbird"]["rev"], info["netbird"]["status"], info["netbird"]["keySet"]))
        self.assertTrue(info["direct"])
        device = self.rs.load_devices()[0]
        self.assertEqual({"netbird": None}, self.get("/gateway/netbird", {"X-Fieldmap-Fingerprint": device["sha1"]}).body)
        with mock.patch.object(self.rs.portmap, "release", return_value=[]), mock.patch.object(self.rs.portmap, "reach", return_value={}):
            self.post("/enabled", {"on": False})
            self.post("/enabled", {"on": True})
            self.rs.time.sleep(0.2)
        self.assertFalse(os.path.exists(self.rs.NETBIRD_DIR), "nothing of NetBird is made on a box that does not use it")
        self.assertNotIn("netbird", json.load(open(self.rs.SETTINGS)))

    def test_connecting_keeps_the_key_to_itself_and_asks_for_the_container(self):
        self.assertEqual(400, self.post("/netbird", {"on": True}).status, "no key yet")
        self.assertEqual(400, self.post("/netbird", {"on": True, "setupKey": "not a key!"}).status)
        self.assertEqual(400, self.connect_netbird(management="ftp://x").status)
        self.assertEqual(400, self.connect_netbird(management="https://nb.example.com/path?x=1").status)
        self.assertEqual(403, self.post("/netbird", {"on": True, "setupKey": self.KEY}, {"X-Real-IP": "203.0.113.30"}).status)
        self.assertFalse(os.path.exists(self.rs.NETBIRD_KEY))
        done = self.connect_netbird()
        self.assertEqual(200, done.status, done.out)
        self.assertEqual(self.KEY, open(self.rs.NETBIRD_KEY).read().strip())
        self.assertEqual(0o600, os.stat(self.rs.NETBIRD_KEY).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(self.rs.NETBIRD_DIR).st_mode & 0o777)
        self.assertEqual("NB_MANAGEMENT_URL=https://api.netbird.io:443\nFIELDMAP_NETBIRD=on\n", open(self.rs.NETBIRD_CLIENT).read())
        self.assertTrue(os.path.isfile(self.rs.NETBIRD_APPLY), "scripts/remote.sh netbird-apply is asked to start it")
        self.assertTrue(json.load(open(self.rs.SETTINGS))["netbird"], "the firewall opens the gateway on NetBird's interface")
        nb = done.body["netbird"]
        self.assertEqual((True, True, "starting", False), (nb["keySet"], nb["wanted"], nb["status"], nb["on"]))
        for answer in (done.body, self.get("/info").body, self.get("/devices").body, json.load(open(self.rs.NETBIRD)),
                       json.load(open(self.rs.SETTINGS)), open(self.rs.NETBIRD_CLIENT).read()):
            self.assertNotIn(self.KEY, json.dumps(answer), "the key is in its own file and nowhere else")

    def test_an_own_server_and_what_counts_as_an_address(self):
        clean = self.rs.clean_management
        self.assertEqual("https://api.netbird.io:443", clean(""))
        self.assertEqual("https://nb.example.com:443", clean(" nb.example.com/ "))
        self.assertEqual("https://nb.example.com:33073", clean("https://NB.example.com:33073"))
        self.assertEqual("http://192.0.2.10:80", clean("http://192.0.2.10"))
        for bad in ("ftp://nb.example.com", "https://nb.example.com/x", "https://user@nb.example.com", "https://nb.example.com:99999",
                    "https://nb example.com", "https://nb.example.com;rm -rf /", "$(reboot)"):
            self.assertEqual("", clean(bad), bad)
        self.connect_netbird(management="nb.example.com:8443")
        self.assertIn("NB_MANAGEMENT_URL=https://nb.example.com:8443\n", open(self.rs.NETBIRD_CLIENT).read())
        self.assertEqual("https://nb.example.com:8443", self.get("/info").body["netbird"]["management"])

    def test_the_block_goes_to_paired_devices_only(self):
        os.environ["REMOTE_PORT"] = "41234"
        try:
            _, public = phone_key()
            self.rs.open_pairing()
            device = self.rs.enrol("Phone", public)
            self.connect_netbird()
            mine = {"X-Fieldmap-Fingerprint": device["sha1"]}
            self.assertEqual({"netbird": None}, self.get("/gateway/netbird", mine).body, "the box did not join yet: no half block")
            self.netbird_says(connected=False, ip="")
            self.assertFalse(self.get("/info").body["netbird"]["on"])
            self.netbird_says()
            info = self.get("/info").body
            self.assertTrue(info["netbird"]["on"])
            self.assertRegex(info["netbird"]["rev"], r"^[0-9a-f]{12}$")
            self.assertEqual("connected", info["netbird"]["status"])
            self.assertEqual("100.92.14.7", info["netbird"]["address"])
            self.assertNotIn(self.KEY, json.dumps(info), "not in an open answer")
            block = {"management": "https://api.netbird.io:443", "setupKey": self.KEY, "host": "100.92.14.7", "port": 41234, "rev": info["netbird"]["rev"]}
            # A paired device, through the gateway...
            self.assertEqual({"netbird": block}, self.get("/gateway/netbird", mine).body)
            # ...and nobody else: not the box's web server (it adds X-Real-IP), not a stranger's certificate, not no certificate.
            self.assertEqual(404, self.get("/gateway/netbird", {**mine, "X-Real-IP": "192.168.1.20"}).status)
            self.assertEqual(403, self.get("/gateway/netbird", {"X-Fieldmap-Fingerprint": "0" * 40}).status)
            self.assertEqual(403, self.get("/gateway/netbird", {}).status)
            self.assertEqual(404, self.post("/gateway/netbird", {}, mine).status, "only GET")
            # Pairing gives it too.
            _, other = phone_key()
            answer = self.post("/enrol", {"name": "Tablet", "publicKey": other}).body
            self.assertEqual(block, answer["netbird"])
            # Asking for it is not "came in from outside".
            self.assertEqual(0, self.rs.newest("direct") + self.rs.newest("netbird"))
            # A new key: a new rev. The box's address is kept while the client restarts.
            self.connect_netbird(setupKey="B" * 36)
            self.netbird_says(age=600)
            changed = self.get("/gateway/netbird", mine).body["netbird"]
            self.assertEqual(("B" * 36, "100.92.14.7"), (changed["setupKey"], changed["host"]))
            self.assertNotEqual(block["rev"], changed["rev"])
            # Another server: the address from the old one is not given out.
            self.connect_netbird(management="https://nb.example.com")
            self.assertEqual({"netbird": None}, self.get("/gateway/netbird", mine).body)
            # A removed device gets nothing; with remote access off, 401 like every address of the gateway.
            self.rs.remove(device["id"])
            self.assertEqual(403, self.get("/gateway/netbird", mine).status)
            self.rs.write_private(self.rs.SETTINGS, json.dumps({"enabled": False}).encode())
            self.assertEqual(401, self.get("/gateway/netbird", mine).status)
        finally:
            os.environ.pop("REMOTE_PORT", None)

    def test_disconnecting_netbird_keeps_the_key_for_the_next_time(self):
        self.connect_netbird()
        self.netbird_says()
        self.assertIsNotNone(self.rs.netbird_block())
        off = self.post("/netbird", {"on": False})
        self.assertEqual(200, off.status)
        self.assertIsNone(self.rs.netbird_block())
        self.assertEqual((False, "off", True, False), (off.body["netbird"]["on"], off.body["netbird"]["status"], off.body["netbird"]["keySet"], off.body["netbird"]["wanted"]))
        self.assertIn("FIELDMAP_NETBIRD=off", open(self.rs.NETBIRD_CLIENT).read())
        self.assertFalse(json.load(open(self.rs.SETTINGS))["netbird"], "the firewall closes NetBird's interface again")
        again = self.post("/netbird", {"on": True})
        self.assertEqual(200, again.status, "the key that is kept needs no typing")
        self.assertIsNotNone(self.rs.netbird_block())

    def test_netbird_runs_only_while_remote_access_is_on(self):
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        self.connect_netbird()
        self.netbird_says()
        self.assertTrue(self.get("/info").body["netbird"]["on"])
        with mock.patch.object(self.rs.portmap, "release", return_value=[]), mock.patch.object(self.rs.portmap, "reach", return_value={}):
            off = self.post("/enabled", {"on": False}).body
            self.assertIn("FIELDMAP_NETBIRD=off", open(self.rs.NETBIRD_CLIENT).read())
            self.assertFalse(json.load(open(self.rs.SETTINGS))["netbird"])
            self.assertEqual((False, "off"), (off["netbird"]["on"], off["netbird"]["status"]))
            self.assertIn("switch on remote access", off["netbird"]["text"])
            self.assertEqual(401, self.get("/gateway/netbird", {"X-Fieldmap-Fingerprint": device["sha1"]}).status)
            on = self.post("/enabled", {"on": True}).body
            self.rs.time.sleep(0.2)
        self.assertIn("FIELDMAP_NETBIRD=on", open(self.rs.NETBIRD_CLIENT).read())
        self.assertTrue(json.load(open(self.rs.SETTINGS))["netbird"])
        self.assertTrue(on["netbird"]["on"], "the box's address in the NetBird network is the same as before")
        self.assertEqual("starting", on["netbird"]["status"])

    def test_the_two_ways_are_counted_apart(self):
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        self.connect_netbird()
        self.netbird_says()
        mine = {"X-Fieldmap-Fingerprint": device["sha1"]}
        self.assertEqual(204, self.get("/check", {**mine, "X-Fieldmap-Peer": "100.92.200.9"}).status)
        self.assertTrue(self.rs.newest("netbird") > 0)
        self.assertEqual(0, self.rs.newest("direct"))
        self.assertNotEqual("confirmed", self.rs.report()["status"], "nobody came in through the router's port")
        self.assertIn("A device connected to the box through NetBird on", self.rs.netbird_report()["text"])
        self.assertEqual(204, self.get("/check", {**mine, "X-Fieldmap-Peer": "198.51.100.7"}).status)
        self.assertTrue(self.rs.newest("direct") > 0)
        self.post("/way", {"way": "direct"})
        self.assertEqual("confirmed", self.rs.report()["status"])
        self.rs.write_seen()
        saved = self.rs.load_devices()[0]
        self.assertEqual({"direct", "netbird"}, set(saved["way"]))
        self.assertTrue(self.rs.newest("netbird") > 0 and self.rs.newest("direct") > 0, "and after it was written down")
        # A device from before the box knew two ways came in through the router's port.
        self.rs.save_devices([{k: v for k, v in saved.items() if k != "way"}])
        self.assertTrue(self.rs.newest("direct") > 0)
        self.assertEqual(0, self.rs.newest("netbird"))

    def test_one_way_in_at_a_time(self):
        os.environ["REMOTE_ADDRESS"] = "tak.example.com:8443"
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        mine = {"X-Fieldmap-Fingerprint": device["sha1"]}
        self.rs.write_private(self.rs.REACH, json.dumps({"checked": 1, "problem": "", "ipv4": {"ok": True, "external_ip": "203.0.113.30",
                                                                                              "external_port": 41000}, "ipv6": {}}).encode())
        self.assertEqual("direct", self.get("/info").body["way"], "the port on the router is the way in by default")
        self.assertEqual(2, len(self.rs.endpoints()))
        self.assertEqual(400, self.post("/way", {"way": "netbird"}).status, "NetBird needs a setup key first")
        self.assertEqual(400, self.post("/way", {"way": "both"}).status)
        # NetBird chosen: the router's port is given back, no direct address is offered, the firewall closes it.
        with mock.patch.object(self.rs.portmap, "release", return_value=[]) as release, mock.patch.object(self.rs.portmap, "reach") as reach:
            info = self.connect_netbird().body
            self.rs.time.sleep(0.2)
            release.assert_called_once()
            self.assertEqual("direct-off", self.rs.check_router()["problem"])
            reach.assert_not_called()               # and the router is not asked again
        self.assertEqual(("netbird", False), (info["way"], info["direct"]))
        self.assertEqual([], info["endpoints"])
        self.assertEqual(("netbird", True), (json.load(open(self.rs.SETTINGS))["way"], json.load(open(self.rs.SETTINGS))["netbird"]))
        self.assertIn("FIELDMAP_NETBIRD=on", open(self.rs.NETBIRD_CLIENT).read())
        self.assertEqual("off", self.rs.report()["status"])
        self.assertIn("because devices come in through NetBird", self.rs.report()["text"])
        self.netbird_says()
        self.assertIsNotNone(self.get("/gateway/netbird", mine).body["netbird"])
        # Back to the router's port: NetBird stops, its firewall rule goes, devices get no block.
        with mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 2, "ipv4": {"ok": True, "external_ip": "203.0.113.30",
                                                                                                "external_port": 41000}, "ipv6": {}, "problem": ""}) as reach, \
                mock.patch.object(self.rs.threading, "Thread") as later:
            info = self.post("/way", {"way": "direct"}).body
            self.assertEqual("The box asks the router now.", info["reach"]["text"], "not the text of the way that was closed")
            later.call_args.kwargs["target"]()     # the router answers
            self.rs.time.sleep(0.2)
            reach.assert_called()
        self.assertEqual(("direct", True), (info["way"], info["direct"]))
        self.assertIn("FIELDMAP_NETBIRD=off", open(self.rs.NETBIRD_CLIENT).read())
        self.assertFalse(json.load(open(self.rs.SETTINGS))["netbird"])
        self.assertEqual({"netbird": None}, self.get("/gateway/netbird", mine).body)
        self.assertEqual((False, "off", True), (info["netbird"]["on"], info["netbird"]["status"], info["netbird"]["keySet"]))
        self.assertIn("because devices come in through the port on the router", info["netbird"]["text"])
        self.assertEqual(2, len(self.rs.endpoints()))
        # And NetBird again, with the key the box kept.
        self.assertEqual("netbird", self.post("/way", {"way": "netbird"}).body["way"])
        self.assertEqual("netbird", self.post("/netbird", {"on": True}).body["way"])
        self.assertEqual("direct", self.post("/netbird", {"on": False}).body["way"], "{\"on\": false} is the way back to the router's port")

    def test_forgetting_netbird_goes_back_to_the_port_on_the_router(self):
        self.connect_netbird()
        with mock.patch.object(sys, "argv", ["remote-server.py", "netbird-forget"]), contextlib.redirect_stdout(io.StringIO()):
            self.rs.main()
        self.assertEqual("direct", self.rs.way())
        self.assertFalse(json.load(open(self.rs.SETTINGS))["netbird"])
        self.assertFalse(os.path.exists(self.rs.NETBIRD_KEY))

    def test_netbird_says_in_plain_words_why_it_does_not_connect(self):
        self.connect_netbird()
        self.assertEqual("starting", self.rs.netbird_report()["status"])
        with open(self.rs.NETBIRD_PROBLEM, "w") as f:
            f.write("key\nrpc error: code = PermissionDenied desc = couldn't add peer: setup key is invalid\n")
        r = self.rs.netbird_report()
        self.assertEqual("blocked", r["status"])
        self.assertIn("did not accept the setup key", r["text"])
        self.assertNotIn("rpc error", r["text"])
        with open(self.rs.NETBIRD_PROBLEM, "w") as f:
            f.write("server\ncontext deadline exceeded\n")
        self.assertIn("cannot connect to the management server https://api.netbird.io:443", self.rs.netbird_report()["text"])
        with open(self.rs.NETBIRD_PROBLEM, "w") as f:
            f.write("download\n")
        self.assertIn("cannot download NetBird", self.rs.netbird_report()["text"])
        os.remove(self.rs.NETBIRD_PROBLEM)
        old = time.time() - 3600
        os.utime(self.rs.NETBIRD_APPLY, (old, old))
        self.assertIn("did not start", self.rs.netbird_report()["text"], "nothing answered for a long time")
        self.netbird_says(connected=False, error="rpc error: code = Unavailable")
        self.assertIn("cannot connect to the management server", self.rs.netbird_report()["text"])
        # A box that was a member already: NetBird does not look at a new key. The page says so.
        self.connect_netbird(setupKey="E" * 36)
        self.netbird_says()
        with open(self.rs.NETBIRD_CHECK, "w") as f:
            f.write("unchecked\n")
        r = self.rs.netbird_report()
        self.assertEqual(("connected", False), (r["status"], r["keyChecked"]))
        self.assertIn("did not check the new setup key", r["text"])
        os.remove(self.rs.NETBIRD_CHECK)
        # A client that was started for an earlier request does not count: the box starts the new one.
        self.netbird_says()
        self.assertEqual("connected", self.rs.netbird_report()["status"])
        self.connect_netbird(setupKey="D" * 36)
        self.assertEqual("starting", self.rs.netbird_report()["status"])
        # Typing the key again clears what was wrong before.
        with open(self.rs.NETBIRD_PROBLEM, "w") as f:
            f.write("key\n")
        self.connect_netbird(setupKey="C" * 36)
        self.assertFalse(os.path.exists(self.rs.NETBIRD_PROBLEM))

    def test_the_gateway_has_one_address_for_netbird_and_the_service_checks_the_device(self):
        for prefix in ("/maps/", "/"):
            conf = self.rs.gateway_conf(prefix, "http://127.0.0.1:8080/", "8443", True)
            block = re.search(r"    location = /mapremote/netbird \{\n(.*?)\n    \}\n", conf, re.S).group(1)
            self.assertIn("if ($request_method != GET) { return 405; }", block)
            self.assertIn("proxy_pass http://unix:/run/fieldmap-remote/remote.sock:/gateway/netbird;", block)
            self.assertIn('proxy_set_header X-Real-IP "";', block, "a visitor cannot pass for the web server, or for nobody")
            self.assertIn("proxy_set_header X-Fieldmap-Fingerprint $ssl_client_fingerprint;", block)
            self.assertIn("location /mapremote/ { return 404; }", conf, "nothing else of the service is behind the gateway")
            device = re.search(r"    location = /_device \{\n(.*?)\n    \}\n", conf, re.S).group(1)
            self.assertIn("proxy_set_header X-Fieldmap-Peer $remote_addr;", device)

    def test_the_netbird_client_is_kept_to_the_gateway(self):
        script = open(os.path.join(HERE, "netbird-run.sh")).read()
        for flag in ("--disable-firewall", "--disable-dns", "--disable-client-routes", "--disable-server-routes", "--block-lan-access",
                     "--interface-name", "--setup-key-file"):
            self.assertIn(flag, script)
        for never in ("--allow-server-ssh", "--allow-remote-jobs", "--setup-key "):
            self.assertNotIn(never, script, "never on, and the key never on a command line")
        self.assertEqual(0, subprocess.run(["bash", "-n", os.path.join(HERE, "netbird-run.sh")]).returncode)
        firewall = open(os.path.join(HERE, "..", "scripts", "firewall.sh")).read()
        self.assertIn('NB_IF="nb-fieldmap"', firewall)
        self.assertIn('INTERFACE="${FIELDMAP_NETBIRD_INTERFACE:-nb-fieldmap}"', script, "the firewall's rule is for this name")


if __name__ == "__main__":
    unittest.main()
