#!/usr/bin/env python3
"""Tests for tools/remote-server.py. Run: python3 -m unittest tools/test_remote_server.py"""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
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
        handler.send = lambda status, payload=None: self.out.update(status=status, body=payload)
        handler.send_response = lambda status: self.out.update(status=status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
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
        # Most tests are about a box whose owner switched remote access on (it is off by default).
        self.switch_on()

    def switch_on(self):
        self.rs.write_private(self.rs.SETTINGS, json.dumps({"enabled": True}).encode())

    def tearDown(self):
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
        for path in ("/fieldmap.css", "/nav.js", "/index.html"):
            self.assertEqual(200, self.get(path).status, path)
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
            self.assertIn("location /mapshare/ { return 404; }", conf, "the Phones page's switch is not among them")
            self.assertNotIn("admin", conf)
            self.assertEqual(1, conf.count("location / {"), "one catch-all only, or nginx refuses to start")

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
        self.assertIn("switched itself off", self.rs.report()["text"])
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


if __name__ == "__main__":
    unittest.main()
