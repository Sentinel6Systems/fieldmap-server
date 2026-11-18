#!/usr/bin/env python3
"""Tests for tools/remote-server.py. Run: python3 -m unittest tools/test_remote_server.py"""
import importlib.util
import os
import subprocess
import tempfile
import unittest

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


class RemoteServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rs = load(self.tmp.name)
        self.rs.init("/maps/", "http://127.0.0.1:8080/", "127.0.0.1:8443")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("REMOTE_ADDRESS", None)

    def test_init_makes_everything_once(self):
        self.assertTrue(self.rs.ready())
        before = open(self.rs.CA_CRT).read()
        self.rs.init("/", "http://tileserver:8080/", "8443")
        self.assertEqual(before, open(self.rs.CA_CRT).read(), "a second init keeps the authority")
        self.assertEqual(0o600, os.stat(self.rs.CA_KEY).st_mode & 0o777)
        self.assertFalse(os.path.exists(os.path.join(self.rs.GATEWAY, "ca.key")), "the gateway never gets the authority's key")

    def test_a_phone_is_let_in_with_a_certificate_for_its_own_key(self):
        private, public = phone_key()
        device = self.rs.enrol("Test phone", public)
        self.assertTrue(self.rs.allowed(device["sha1"]))
        self.assertTrue(self.rs.allowed(device["sha1"].upper()))
        # The certificate verifies against the box's authority, is for the phone's key, and is for clients only.
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(device["certificate"])
            f.flush()
            subprocess.run(["openssl", "verify", "-CAfile", self.rs.CA_CRT, "-purpose", "sslclient", f.name], capture_output=True, check=True)
            in_cert = subprocess.run(["openssl", "x509", "-in", f.name, "-noout", "-pubkey"], capture_output=True, check=True).stdout.decode()
            as_server = subprocess.run(["openssl", "verify", "-CAfile", self.rs.CA_CRT, "-purpose", "sslserver", f.name], capture_output=True)
        self.assertEqual(public.strip(), in_cert.strip())
        self.assertNotEqual(0, as_server.returncode, "a phone's certificate cannot pose as the box")

    def test_the_same_key_twice_is_one_device(self):
        _, public = phone_key()
        first = self.rs.enrol("Phone", public)
        again = self.rs.enrol("Phone again", public)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(1, len(self.rs.load_devices()))

    def test_a_removed_phone_is_locked_out(self):
        _, public = phone_key()
        device = self.rs.enrol("Phone", public)
        self.assertTrue(self.rs.remove(device["id"]))
        self.assertFalse(self.rs.allowed(device["sha1"]))
        self.assertFalse(self.rs.remove(device["id"]))

    def test_unknown_and_empty_fingerprints_are_refused(self):
        self.assertFalse(self.rs.allowed(""))
        self.assertFalse(self.rs.allowed(None))
        self.assertFalse(self.rs.allowed("0" * 40))

    def test_only_p256_public_keys_are_taken(self):
        with self.assertRaises(ValueError):
            self.rs.enrol("Phone", "not a key")
        rsa = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
        rsa_public = subprocess.run(["openssl", "pkey", "-pubout"], input=rsa, capture_output=True, check=True).stdout.decode()
        with self.assertRaises(ValueError):
            self.rs.enrol("Phone", rsa_public)

    def test_names_are_cleaned(self):
        self.assertEqual("Phone", self.rs.clean_name(""))
        self.assertEqual("ab", self.rs.clean_name(" a\x00\nb "))
        self.assertEqual(40, len(self.rs.clean_name("x" * 100)))

    def test_only_the_local_network_may_enrol(self):
        lan = self.rs.lan_only
        self.assertTrue(lan({"X-Real-IP": "192.168.1.20"}))
        self.assertTrue(lan({"X-Real-IP": "10.0.0.7"}))
        self.assertTrue(lan({"X-Real-IP": "172.20.1.1"}))
        self.assertTrue(lan({"X-Real-IP": "127.0.0.1"}))
        self.assertTrue(lan({"X-Real-IP": "fd00::5"}))
        self.assertTrue(lan({"X-Real-IP": "::ffff:192.168.1.20"}))
        self.assertFalse(lan({"X-Real-IP": "203.0.113.30"}), "an address on the internet")
        self.assertFalse(lan({"X-Real-IP": "2001:db8::1"}), "an IPv6 address that is not local")
        self.assertFalse(lan({"X-Real-IP": "100.64.0.9"}), "a relay network's address is not the local network")
        self.assertFalse(lan({}), "not through the box's web server")
        self.assertFalse(lan({"X-Real-IP": "nonsense"}))
        self.assertFalse(lan({"X-Real-IP": "192.168.1.20", "X-Forwarded-For": "203.0.113.30"}), "came through a proxy or tunnel")
        self.assertFalse(lan({"X-Real-IP": "127.0.0.1", "Tailscale-Funnel-Request": "?1"}))

    def test_the_outside_address_comes_from_the_file_or_the_setting(self):
        self.assertIsNone(self.rs.remote_address())
        with open(self.rs.ADDRESS, "w") as f:
            f.write("box.example.ts.net:443\n")
        self.assertEqual({"host": "box.example.ts.net", "port": 443}, self.rs.remote_address())
        os.environ["REMOTE_ADDRESS"] = "tak.example.com:8443"
        self.assertEqual({"host": "tak.example.com", "port": 8443}, self.rs.remote_address())
        os.environ["REMOTE_ADDRESS"] = "not an address"
        self.assertIsNone(self.rs.remote_address())

    def test_the_gateway_configuration_demands_a_certificate_and_serves_only_the_maps(self):
        conf = self.rs.gateway_conf("/maps/", "http://127.0.0.1:8080/", "127.0.0.1:8443")
        self.assertIn("ssl_verify_client       on;", conf)
        self.assertIn("auth_request /_device;", conf)
        self.assertIn("location /maps/ {", conf)
        self.assertIn("location / { return 404; }", conf)
        self.assertNotIn("location / { return 404; }", self.rs.gateway_conf("/", "http://tileserver:8080/", "8443"))
        for bad in (("maps/", "http://127.0.0.1:8080/", "8443"), ("/maps/", "127.0.0.1:8080", "8443"), ("/maps/", "http://127.0.0.1:8080/", "x")):
            with self.assertRaises(ValueError):
                self.rs.gateway_conf(*bad)


if __name__ == "__main__":
    unittest.main()
