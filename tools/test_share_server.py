#!/usr/bin/env python3
"""Tests for tools/share-server.py. Run: python3 -m unittest tools/test_share_server.py"""
import base64
import contextlib
import http.client
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(folder, name="share-server.py", variable="SHARE_DIR"):
    """A service as a module, keeping its files in `folder`."""
    os.environ[variable] = folder
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], os.path.join(HERE, name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRequest:
    """Just enough of BaseHTTPRequestHandler to hand the handler one request. `body` is sent as
    JSON, `raw` as it is."""

    def __init__(self, ss, method, path, headers=None, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else b""
        self.headers = dict(headers or {})
        if data:
            self.headers["Content-Length"] = str(len(data))
        handler = ss.Handler.__new__(ss.Handler)
        handler.command, handler.path, handler.headers, handler.rfile = method, path, self.headers, io.BytesIO(data)
        self.out = {}
        handler.send = lambda status, payload=None: self.out.update(status=status, body=payload)
        handler.answer()

    @property
    def status(self):
        return self.out["status"]

    @property
    def body(self):
        return self.out["body"]


HOME = {"X-Real-IP": "192.168.1.20"}
OTHER = {"X-Real-IP": "192.168.1.21"}
OUTSIDE = {"X-Fieldmap-Fingerprint": "ab" * 20}
UID = "test-phone-0001"
MARKER = "test-marker-0001"


def position(**changes):
    return {"uid": UID, "name": "Blue", "lat": 1.5, "lon": 2.5, "alt": 120.0, "acc": 4.0, "course": 90.0, "speed": 1.2,
            "time": "2026-01-31T12:00:00Z", **changes}


def marker(**properties):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [2.5, 1.5]},
            "properties": {"name": "P1", "stroke": "#d32f2f", "marker-color": "#d32f2f",
                           "fieldmap": {"kind": "marker", "label": "P1", "color": "#d32f2f", "id": MARKER},
                           "by": UID, "updated": "2026-01-31T12:00:00Z", **properties}}


class ShareServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ss = load(self.tmp.name)
        self.ss.load_settings()
        self.ss.load_markers()
        self.ss.load_messages()
        self.time = 1000.0
        self.clock = mock.patch.object(self.ss, "clock", side_effect=lambda: self.time)
        self.clock.start()
        self.no_v6 = mock.patch.object(self.ss.portmap, "global_ipv6", return_value=[])
        self.no_v6.start()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        # Most tests are about a box whose owner switched sharing on (it is off by default).
        self.ss.set_enabled(True)

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.no_v6.stop()
        self.clock.stop()
        self.tmp.cleanup()
        os.environ.pop("REMOTE_PIN", None)

    def ask(self, method, path, body=None, headers=HOME, raw=None):
        return FakeRequest(self.ss, method, path, headers, body, raw)

    def on_disk(self):
        """Everything the service has written, as one text."""
        out = ""
        for folder, _, files in os.walk(self.tmp.name):
            for name in files:
                with open(os.path.join(folder, name), errors="replace") as f:
                    out += f.read()
        return out

    # ---------- the switch ----------

    def test_off_by_default_and_then_nothing_is_taken_or_given(self):
        os.remove(self.ss.SETTINGS)
        self.assertFalse(self.ss.load_settings())
        info = self.ask("GET", "/info")
        self.assertEqual((200, {"version": 1, "enabled": False, "interval": 5, "stale": 120}), (info.status, info.body))
        for method, path, body in (("GET", "/state", None), ("POST", "/position", position()), ("POST", "/leave", {"uid": UID}),
                                   ("PUT", "/markers/" + MARKER, marker()), ("DELETE", "/markers/" + MARKER, None)):
            for headers in (HOME, OUTSIDE):
                r = self.ask(method, path, body, headers)
                self.assertEqual((503, {"enabled": False}), (r.status, r.body), path)
        self.assertEqual({}, self.ss.positions)
        self.assertEqual({}, self.ss.markers)

    def test_the_phones_page_switches_it_and_sees_who_is_sharing(self):
        self.ss.set_enabled(False)
        r = self.ask("POST", "/admin", {"on": True})
        self.assertEqual(200, r.status)
        self.assertTrue(r.body["enabled"])
        with open(self.ss.SETTINGS) as f:
            self.assertTrue(json.load(f)["enabled"], "kept for after a restart")
        self.ask("POST", "/position", position())
        self.ask("POST", "/position", position(uid=UID[:-1] + "2", name="amber"), OUTSIDE)
        self.time += 7
        r = self.ask("GET", "/admin")
        self.assertEqual("fieldmap-share", r.body["service"])
        self.assertEqual([{"name": "amber", "age": 7, "outside": True}, {"name": "Blue", "age": 7, "outside": False}], r.body["people"])
        self.assertNotIn("lat", json.dumps(r.body), "names and ages, no places")
        self.assertEqual(400, self.ask("POST", "/admin", {"on": "yes"}).status)
        self.assertEqual(400, self.ask("POST", "/admin", {}).status)

    def test_switching_off_drops_every_position_and_keeps_the_markers(self):
        self.ask("POST", "/position", position())
        self.ask("PUT", "/markers/" + MARKER, marker())
        self.assertFalse(self.ask("POST", "/admin", {"on": False}).body["enabled"])
        self.assertEqual({}, self.ss.positions)
        self.assertEqual(503, self.ask("GET", "/state").status)
        self.ss.post_position(self.ss.clean_position(position()), ("home", "192.168.1.20"), False)
        self.assertEqual({}, self.ss.positions, "nothing is kept while off")
        self.ask("POST", "/admin", {"on": True})
        state = self.ask("GET", "/state").body
        self.assertEqual([], state["positions"])
        self.assertEqual([MARKER], [m["id"] for m in state["markers"]])

    def test_the_box_code_guards_the_switch(self):
        os.environ["REMOTE_PIN"] = "4711"
        self.assertTrue(self.ask("GET", "/admin").body["pin"])
        r = self.ask("POST", "/admin", {"on": False})
        self.assertEqual(403, r.status)
        self.assertTrue(r.body["pin"])
        self.assertTrue(self.ss.on)
        self.assertEqual(200, self.ask("POST", "/admin", {"on": False, "pin": " 4711 "}).status)
        self.assertFalse(self.ss.on)
        self.ss.buckets.clear()
        for _ in range(10):
            self.assertEqual(403, self.ask("POST", "/admin", {"on": True, "pin": "0000"}).status)
        self.assertEqual(429, self.ask("POST", "/admin", {"on": True, "pin": "4711"}).status, "the code cannot be found by trying")

    # ---------- the mesh radio channel ----------

    def test_the_mesh_channel_goes_to_local_and_paired_devices_while_sharing_is_on(self):
        for headers in (HOME, OUTSIDE):
            r = self.ask("GET", "/mesh", headers=headers)
            self.assertEqual(200, r.status)
            self.assertEqual({"version", "name", "psk", "preset", "hopLimit", "moving", "still", "move", "rev"}, set(r.body))
            self.assertEqual((60, 600, 15), (r.body["moving"], r.body["still"], r.body["move"]))
            self.assertRegex(r.body["name"], r"^FM-[0-9a-f]{6}$")
            self.assertLessEqual(len(r.body["name"]), 11, "Meshtastic takes channel names up to 11 characters")
            self.assertEqual(32, len(base64.b64decode(r.body["psk"])), "AES-256")
            self.assertEqual(("LONG_FAST", 3), (r.body["preset"], r.body["hopLimit"]))
            self.assertGreater(r.body["moving"], self.ss.INTERVAL, "slower than through the server")
        self.assertEqual(403, self.ask("GET", "/mesh", headers={"X-Real-IP": "203.0.113.9"}).status)
        self.assertEqual(405, self.ask("POST", "/mesh", {}).status)
        self.ss.set_enabled(False)
        self.assertEqual(503, self.ask("GET", "/mesh").status)
        self.assertNotIn("mesh", self.ask("GET", "/info").body)

    def test_info_tells_a_change_of_the_channel_but_never_the_key(self):
        channel = self.ask("GET", "/mesh").body
        info = self.ask("GET", "/info").body
        self.assertEqual(channel["rev"], info["mesh"])
        self.assertNotIn(channel["psk"], json.dumps(info))
        self.assertNotIn(channel["psk"], json.dumps(self.ask("GET", "/admin").body))

    def test_the_channel_is_kept_and_a_new_key_replaces_it(self):
        first = self.ask("GET", "/mesh").body
        self.ss.mesh = {}
        self.assertEqual(first, self.ask("GET", "/mesh").body, "read again after a restart")
        self.assertEqual(0o600, os.stat(self.ss.MESH).st_mode & 0o777)
        r = self.ask("POST", "/admin", {"newMeshKey": True})
        self.assertEqual(200, r.status)
        self.assertTrue(r.body["enabled"], "the switch is left as it was")
        second = self.ask("GET", "/mesh").body
        self.assertNotEqual(first["psk"], second["psk"])
        self.assertNotEqual(first["rev"], second["rev"])
        self.assertEqual(403, self.ask("POST", "/admin", {"newMeshKey": True}, OUTSIDE).status)
        self.assertEqual(400, self.ask("POST", "/admin", {"newMeshKey": "yes"}).status)

    def test_the_box_code_guards_a_new_channel_key(self):
        os.environ["REMOTE_PIN"] = "4711"
        before = self.ask("GET", "/mesh").body["psk"]
        self.assertEqual(403, self.ask("POST", "/admin", {"newMeshKey": True}).status)
        self.assertEqual(before, self.ask("GET", "/mesh").body["psk"])
        self.assertEqual(200, self.ask("POST", "/admin", {"newMeshKey": True, "pin": "4711"}).status)
        self.assertNotEqual(before, self.ask("GET", "/mesh").body["psk"])

    def test_a_channel_file_that_cannot_be_read_gives_a_new_channel(self):
        with open(self.ss.MESH, "w") as f:
            f.write('{"name": "FM-000000", "psk": "c2hvcnQ=", "rev": "x", "made": 1}')
        self.ss.mesh = {}
        self.assertEqual(32, len(base64.b64decode(self.ask("GET", "/mesh").body["psk"])))

    # ---------- messages ----------

    def message(self, **changes):
        return {"id": "test-msg-0001", "from": UID, "name": "Blue", "to": "test-phone-0002", "text": "Meet at the gate", "time": "2026-01-31T12:00:00Z", **changes}

    def test_a_message_waits_for_its_device_and_its_sender_learns_it_was_delivered(self):
        r = self.ask("POST", "/messages", self.message())
        self.assertEqual(200, r.status)
        seq = r.body["seq"]
        mine = self.ask("GET", "/messages?for=" + UID).body
        self.assertEqual([], mine["messages"], "not to the sender itself")
        self.assertEqual([], mine["delivered"])
        theirs = self.ask("GET", "/messages?for=test-phone-0002", headers=OUTSIDE).body
        self.assertEqual(["test-msg-0001"], [m["id"] for m in theirs["messages"]])
        self.assertEqual({"seq", "id", "from", "name", "to", "text", "time"}, set(theirs["messages"][0]))
        self.assertEqual([], self.ask("GET", "/messages?for=test-phone-0002&since=%d" % seq).body["messages"], "nothing new after it")
        self.assertEqual([], self.ask("GET", "/messages?for=test-phone-0003").body["messages"], "not to anybody else")
        self.assertEqual(204, self.ask("POST", "/messages/delivered", {"ids": ["test-msg-0001"], "by": "test-phone-0002"}).status)
        self.assertEqual([], self.ask("GET", "/messages?for=test-phone-0002").body["messages"], "done with")
        self.assertEqual(["test-msg-0001"], self.ask("GET", "/messages?for=" + UID).body["delivered"])

    def test_a_message_to_everybody_reaches_all_but_its_sender_and_the_same_one_twice_is_kept_once(self):
        first = self.ask("POST", "/messages", self.message(to="*")).body["seq"]
        self.assertEqual(first, self.ask("POST", "/messages", self.message(to="*")).body["seq"])
        for uid in ("test-phone-0002", "test-phone-0003"):
            self.assertEqual(1, len(self.ask("GET", "/messages?for=" + uid).body["messages"]))
        self.assertEqual(0, len(self.ask("GET", "/messages?for=" + UID).body["messages"]))
        # A delivery receipt is only for a message to that one device.
        self.ask("POST", "/messages/delivered", {"ids": ["test-msg-0001"], "by": "test-phone-0002"})
        self.assertEqual(1, len(self.ask("GET", "/messages?for=test-phone-0003").body["messages"]))

    def test_a_relay_asks_for_several_devices(self):
        self.ask("POST", "/messages", self.message(id="test-msg-0001", to="test-phone-0002"))
        self.ask("POST", "/messages", self.message(id="test-msg-0002", to="test-phone-0003"))
        both = self.ask("GET", "/messages?for=test-phone-0002,test-phone-0003").body["messages"]
        self.assertEqual(["test-msg-0001", "test-msg-0002"], [m["id"] for m in both])
        self.assertEqual(400, self.ask("GET", "/messages?for=" + ",".join("test-phone-%04d" % i for i in range(9))).status)

    def test_messages_are_kept_on_the_disk_for_a_week_and_never_in_the_log(self):
        self.ask("POST", "/messages", self.message())
        self.assertEqual(0o600, os.stat(self.ss.MESSAGES).st_mode & 0o777)
        self.ss.messages = {"seq": 0, "list": [], "delivered": {}}
        self.assertEqual(1, len(self.ask("GET", "/messages?for=test-phone-0002").body["messages"]), "read again after a restart")
        with mock.patch.object(self.ss.time, "time", return_value=self.ss.time.time() + 8 * 86400):
            self.assertEqual([], self.ask("GET", "/messages?for=test-phone-0002").body["messages"])

    def test_messages_are_checked_and_only_while_sharing_is_on(self):
        for bad in (self.message(text=""), self.message(text="x" * 141), self.message(text="two\nlines"), self.message(to="nobody!"),
                    self.message(id="x"), self.message(name=""), self.message(time="yesterday"), ["not", "an", "object"]):
            self.assertEqual(400, self.ask("POST", "/messages", bad).status, bad)
        self.assertEqual(200, self.ask("POST", "/messages", self.message(text="x" * 140)).status)
        self.assertEqual(400, self.ask("GET", "/messages").status)
        self.assertEqual(400, self.ask("GET", "/messages?for=x").status)
        self.assertTrue(self.ask("GET", "/info").body["messages"])
        self.ss.set_enabled(False)
        self.assertEqual(503, self.ask("POST", "/messages", self.message(id="test-msg-0002")).status)
        self.assertEqual(503, self.ask("GET", "/messages?for=test-phone-0002").status)
        self.assertEqual(403, self.ask("GET", "/messages?for=test-phone-0002", headers={"X-Real-IP": "203.0.113.9"}).status)

    # ---------- who may ask ----------

    def test_only_the_local_network_and_paired_phones_get_an_answer(self):
        for headers in ({"X-Real-IP": "203.0.113.30"}, {"X-Real-IP": "192.168.1.20", "X-Forwarded-For": "203.0.113.30"},
                        {"X-Real-IP": "192.168.1.20", "Tailscale-Funnel-Request": "?1"}, {"X-Real-IP": "nonsense"},
                        {"X-Fieldmap-Fingerprint": "not a certificate"}, {"X-Fieldmap-Fingerprint": ""}):
            for method, path in (("GET", "/info"), ("GET", "/state"), ("GET", "/admin")):
                self.assertEqual(403, self.ask(method, path, headers=headers).status, (headers, path))
        for headers in (HOME, {"X-Real-IP": "10.1.2.3"}, {"X-Real-IP": "::ffff:192.168.1.20"}, {"X-Real-IP": "fd00::5"}, OUTSIDE, {}):
            self.assertEqual(200, self.ask("GET", "/state", headers=headers).status, headers)

    def test_the_switch_and_the_list_are_never_given_outside(self):
        self.assertEqual(403, self.ask("GET", "/admin", headers=OUTSIDE).status)
        self.assertEqual(403, self.ask("POST", "/admin", {"on": False}, OUTSIDE).status)
        self.assertTrue(self.ss.on)
        # A phone outside cannot pass for one at home: the gateway's mark outweighs nothing here,
        # because the web server, and only it, sets X-Real-IP.
        self.assertEqual(("outside", "ab" * 20), self.ss.caller(OUTSIDE))
        self.assertEqual(("home", "192.168.1.20"), self.ss.caller({**HOME, **OUTSIDE}))
        self.assertEqual(("box", ""), self.ss.caller({}))

    def test_phones_at_home_with_a_global_ipv6_address_count_as_local(self):
        self.assertEqual(403, self.ask("GET", "/state", headers={"X-Real-IP": "2001:db8:77::20"}).status)
        self.ss.own_networks[:] = [0.0, []]
        with mock.patch.object(self.ss.portmap, "global_ipv6", return_value=["2001:db8:77::10"]) as asked:
            for _ in range(3):
                self.assertEqual(200, self.ask("GET", "/state", headers={"X-Real-IP": "2001:db8:77::20"}).status)
            self.assertEqual(1, asked.call_count, "the machine's networks are not asked for on every request")

    def test_the_local_network_rule_is_the_one_of_the_remote_access_service(self):
        with tempfile.TemporaryDirectory() as folder:
            rs = load(folder, "remote-server.py", "REMOTE_DIR")
        self.assertEqual(rs.LOCAL_NETWORKS, self.ss.LOCAL_NETWORKS)
        self.assertEqual(rs.FORWARDED, self.ss.FORWARDED)
        with mock.patch.object(rs.portmap, "global_ipv6", return_value=[]):
            for real in ("192.168.1.20", "10.0.0.1", "172.31.255.1", "169.254.1.1", "127.0.0.1", "100.64.0.1", "203.0.113.30",
                         "198.51.100.7", "::1", "fe80::1", "fd00::1", "2001:db8::1", "::ffff:10.0.0.1", "::ffff:203.0.113.30", "x", ""):
                for extra in ({}, {"X-Forwarded-For": "10.0.0.1"}, {"Forwarded": "for=10.0.0.1"}):
                    headers = {"X-Real-IP": real, **extra}
                    self.assertEqual(rs.lan(headers), self.ss.caller(headers) is not None, headers)

    # ---------- positions ----------

    def test_a_position_is_seen_by_everybody_until_it_is_stale_or_leaves(self):
        self.assertEqual(204, self.ask("POST", "/position", position()).status)
        self.assertEqual(204, self.ask("POST", "/position", position(uid=UID[:-1] + "2", name="Amber", lat=-3, lon=4), OUTSIDE).status)
        self.time += 3
        state = self.ask("GET", "/state", headers=OTHER).body
        self.assertEqual([{**position(), "age": 3}, {**position(uid=UID[:-1] + "2", name="Amber", lat=-3, lon=4), "age": 3}], state["positions"])
        self.assertEqual(2, len(self.ask("GET", "/state", headers=OUTSIDE).body["positions"]), "also the caller's own")
        self.assertEqual(204, self.ask("POST", "/leave", {"uid": UID}).status)
        self.assertEqual(["Amber"], [p["name"] for p in self.ask("GET", "/state").body["positions"]])
        self.assertEqual(204, self.ask("POST", "/leave", {"uid": UID}).status, "leaving twice is fine")
        self.time += 116
        self.assertEqual(1, len(self.ask("GET", "/state").body["positions"]))
        self.time += 2
        self.assertEqual([], self.ask("GET", "/state").body["positions"], "dropped 120 seconds after its last post")

    def test_a_new_post_replaces_the_old_one_and_only_known_fields_are_kept(self):
        self.ask("POST", "/position", position())
        self.time += 50
        slim = {"uid": UID, "name": "  Blue 2 ", "lat": 0, "lon": -180, "alt": None, "time": "2026-01-31T12:00:50.123+00:00",
                "token": "x", "note": {"deep": [1, 2]}}
        self.assertEqual(204, self.ask("POST", "/position", slim).status)
        self.assertEqual([{"uid": UID, "name": "Blue 2", "lat": 0, "lon": -180, "time": "2026-01-31T12:00:50.123+00:00", "age": 0}],
                         self.ask("GET", "/state").body["positions"])

    def test_positions_are_checked_strictly(self):
        bad = [{"uid": "short"}, {"uid": "has space in it"}, {"uid": "a" * 65}, {"uid": 12345678}, {"uid": None},
               {"name": ""}, {"name": "   "}, {"name": "x" * 25}, {"name": "two\nlines"}, {"name": "a‮b"}, {"name": "a\x00b"}, {"name": 7},
               {"lat": 90.1}, {"lat": "1.5"}, {"lat": True}, {"lat": None}, {"lon": -180.5}, {"lon": [1]}, {"lon": 10 ** 400},
               {"alt": 200000}, {"acc": -1}, {"course": 361}, {"speed": "fast"}, {"speed": -0.1},
               {"time": "yesterday"}, {"time": "2026-01-31 12:00:00"}, {"time": 1769860800}, {"time": "2026-01-31T12:00:00Z" + "x" * 30}]
        for change in bad:
            r = self.ask("POST", "/position", position(**change))
            self.assertEqual(400, r.status, change)
            self.assertTrue(r.body["error"], "in plain words")
        for field in ("uid", "name", "lat", "lon", "time"):
            body = position()
            del body[field]
            self.assertEqual(400, self.ask("POST", "/position", body).status, field)
        for raw in (b"[1, 2]", b'"text"', b"{not json", b'{"uid": "' + UID.encode() + b'", "name": "n", "lat": NaN, "lon": 1, "time": "2026-01-31T12:00:00Z"}',
                    b'{"lat": 1e400}', b"[" * 1020 + b"]" * 1020, b"\xff\xfe"):
            self.assertEqual(400, self.ask("POST", "/position", raw=raw).status, raw[:30])
        self.assertEqual(400, self.ask("POST", "/position").status, "nothing sent")
        self.assertEqual(400, self.ask("POST", "/leave", {"uid": "x"}).status)
        self.assertEqual(400, self.ask("POST", "/leave", ["x"]).status)
        self.assertEqual({}, self.ss.positions)

    def test_positions_are_never_written_to_the_disk(self):
        self.ask("POST", "/position", position(name="Kept in memory", lat=12.345678, lon=-98.765432))
        self.ask("PUT", "/markers/" + MARKER, marker())
        self.ask("POST", "/admin", {"on": True})
        text = self.on_disk()
        self.assertIn(MARKER, text)
        for secret in ("Kept in memory", "12.345678", "98.765432", "192.168.1.20"):
            self.assertNotIn(secret, text)

    def test_the_room_takes_only_so_many_people(self):
        with mock.patch.object(self.ss, "MAX_PEOPLE", 3):
            for n in range(3):
                self.assertEqual(204, self.ask("POST", "/position", position(uid=f"{UID[:-1]}{n}"), {"X-Real-IP": f"192.168.1.{n + 1}"}).status)
            full = self.ask("POST", "/position", position(uid=UID[:-1] + "9"), OTHER)
            self.assertEqual(507, full.status)
            self.assertEqual(204, self.ask("POST", "/position", position(uid=UID[:-1] + "0"), OTHER).status, "those who are in can go on")
            self.time += 121
            self.assertEqual(204, self.ask("POST", "/position", position(uid=UID[:-1] + "9"), OTHER).status, "room again when the others are stale")

    def test_one_phone_cannot_fill_the_room(self):
        for n in range(self.ss.MAX_PER_PHONE):
            self.assertEqual(204, self.ask("POST", "/position", position(uid=f"{UID[:-1]}{n}"), OUTSIDE).status)
        self.assertEqual(507, self.ask("POST", "/position", position(uid=UID[:-1] + "9"), OUTSIDE).status)
        self.assertEqual(204, self.ask("POST", "/position", position(uid=UID[:-1] + "9"), {"X-Fieldmap-Fingerprint": "cd" * 20}).status)

    # ---------- shared markers ----------

    def test_a_shared_marker_reaches_the_others_and_only_changes_are_sent_after(self):
        start = self.ask("GET", "/state").body
        self.assertTrue(start["full"])
        self.assertGreater(start["rev"], 1_000_000_000, "revs start from the clock")
        put = self.ask("PUT", "/markers/" + MARKER, marker())
        self.assertEqual((200, {"rev": start["rev"] + 1}), (put.status, put.body))
        changed = self.ask("GET", f"/state?since={start['rev']}", headers=OUTSIDE).body
        self.assertFalse(changed["full"])
        self.assertEqual([{**marker(), "id": MARKER}], changed["markers"])
        self.assertEqual([], changed["removed"])
        nothing = self.ask("GET", f"/state?since={put.body['rev']}").body
        self.assertEqual((False, [], []), (nothing["full"], nothing["markers"], nothing["removed"]))
        # The last one sent wins.
        moved = marker(name="Moved")
        moved["geometry"]["coordinates"] = [3, 4, 250.5]
        moved["id"] = MARKER
        self.assertEqual(start["rev"] + 2, self.ask("PUT", "/markers/" + MARKER, moved, OUTSIDE).body["rev"])
        self.assertEqual([moved], self.ask("GET", f"/state?since={put.body['rev']}").body["markers"])
        self.assertEqual(1, len(self.ask("GET", "/state").body["markers"]))

    def test_a_removed_marker_is_told_to_phones_that_had_it(self):
        first = self.ask("PUT", "/markers/" + MARKER, marker()).body["rev"]
        other = MARKER[:-1] + "2"
        self.ask("PUT", "/markers/" + other, marker())
        gone = self.ask("DELETE", "/markers/" + MARKER)
        self.assertEqual((200, {"rev": first + 2}), (gone.status, gone.body))
        state = self.ask("GET", f"/state?since={first}").body
        self.assertEqual(([other], [MARKER]), ([m["id"] for m in state["markers"]], state["removed"]))
        again = self.ask("DELETE", "/markers/" + MARKER)
        self.assertEqual((200, {"rev": first + 2}), (again.status, again.body), "one that is not there: nothing changes")
        full = self.ask("GET", "/state").body
        self.assertEqual((True, [other], []), (full["full"], [m["id"] for m in full["markers"]], full["removed"]))
        # Shared again under the same id: it is a marker again, not a removed one.
        self.ask("PUT", "/markers/" + MARKER, marker())
        state = self.ask("GET", f"/state?since={first}").body
        self.assertEqual(({other, MARKER}, []), ({m["id"] for m in state["markers"]}, state["removed"]))

    def test_markers_survive_a_restart_and_positions_do_not(self):
        self.ask("POST", "/position", position())
        first = self.ask("PUT", "/markers/" + MARKER, marker()).body["rev"]
        other = MARKER[:-1] + "2"
        self.ask("PUT", "/markers/" + other, marker())
        self.ask("DELETE", "/markers/" + other)
        self.assertEqual(0o600, os.stat(self.ss.MARKERS).st_mode & 0o777)
        again = load(self.tmp.name)
        again.load_settings()
        again.load_markers()
        self.assertTrue(again.on)
        self.assertEqual({}, again.positions)
        self.assertEqual(first + 2, again.rev, "the count goes on where it was")
        state = again.state(first)
        self.assertEqual((False, [], [other]), (state["full"], state["markers"], state["removed"]), "a restart alone does not make phones start over")
        self.assertEqual([{**marker(), "id": MARKER}], again.state()["markers"])

    def test_a_phone_that_was_away_too_long_or_knew_another_box_starts_over(self):
        self.ask("PUT", "/markers/" + MARKER, marker())
        rev = self.ss.rev
        for since in (0, 1, rev - 5, rev + 1, 10 ** 17):
            state = self.ask("GET", f"/state?since={since}").body
            self.assertEqual((True, 1), (state["full"], len(state["markers"])), since)
        with mock.patch.object(self.ss, "MAX_REMOVED", 3):
            for n in range(5):
                self.ask("PUT", f"/markers/{MARKER[:-1]}{n}", marker())
                self.ask("DELETE", f"/markers/{MARKER[:-1]}{n}")
        self.assertEqual(3, len(self.ss.removed))
        self.assertTrue(self.ask("GET", f"/state?since={rev}").body["full"], "the box no longer knows what was removed since then")
        recent = self.ask("GET", f"/state?since={self.ss.rev - 3}").body
        self.assertEqual((False, [MARKER[:-1] + "3", MARKER[:-1] + "4"]), (recent["full"], recent["removed"]))
        for bad in ("since=", "since=-1", "since=abc", "since=1&x=2", "x=1", "since=" + "9" * 19):
            self.assertEqual(400, self.ask("GET", "/state?" + bad).status, bad)

    def test_markers_are_checked_strictly(self):
        def changed(**parts):
            return {**marker(), **parts}
        bad = [changed(type="FeatureCollection"), changed(id="another-id-0001"), changed(geometry=None),
               changed(geometry={"type": "LineString", "coordinates": [[1, 2], [3, 4]]}),
               changed(geometry={"type": "Point", "coordinates": [181, 0]}), changed(geometry={"type": "Point", "coordinates": [0, 91]}),
               changed(geometry={"type": "Point", "coordinates": [0]}), changed(geometry={"type": "Point", "coordinates": [0, 0, 0, 0]}),
               changed(geometry={"type": "Point", "coordinates": ["0", "0"]}), changed(geometry={"type": "Point", "coordinates": {"lon": 0}}),
               changed(properties=None), changed(properties=[]),
               marker(by="x"), marker(by=None), marker(updated="now"), marker(note="x" * 301), marker(note="two\nlines"),
               marker(note=[1, 2]), marker(fieldmap={"deeper": {"still": 1}}), marker(fieldmap={"list": []}), marker(size=1e300),
               marker(**{"k" * 41: 1}), marker(**{"": 1}), marker(**{f"k{n}": n for n in range(30)})]
        for body in bad:
            r = self.ask("PUT", "/markers/" + MARKER, body)
            self.assertEqual(400, r.status, json.dumps(body)[:120])
            self.assertTrue(r.body["error"])
        for body in (marker(), {k: v for k, v in marker().items() if k != "properties"}):
            if "properties" in body:
                del body["properties"]["by"]
            self.assertEqual(400, self.ask("PUT", "/markers/" + MARKER, body).status)
        self.assertEqual({}, self.ss.markers)
        self.assertFalse(os.path.exists(self.ss.MARKERS))
        # Kept: plain values and the print tool's own object; dropped: anything else beside them.
        fine = {**marker(note="", count=3, shown=True, none=None), "bbox": [0, 0, 1, 1], "extra": {"x": 1}}
        self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER, fine).status)
        self.assertEqual({**marker(note="", count=3, shown=True, none=None), "id": MARKER}, self.ss.markers[MARKER]["feature"])
        for path in ("/markers/short", "/markers/" + "a" * 65, "/markers/has.dot.in.it", "/markers/" + MARKER + "/x", "/markers/"):
            self.assertEqual(404, self.ask("PUT", path, marker()).status, path)

    def test_the_box_takes_only_so_many_markers(self):
        with mock.patch.object(self.ss, "MAX_MARKERS", 2):
            for n in range(2):
                self.assertEqual(200, self.ask("PUT", f"/markers/{MARKER[:-1]}{n}", marker()).status)
            self.assertEqual(507, self.ask("PUT", "/markers/" + MARKER[:-1] + "9", marker()).status)
            self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER[:-1] + "0", marker(name="changed")).status, "one that is there can change")
        with mock.patch.object(self.ss, "MAX_MARKER_BYTES", 1000):
            self.assertEqual(507, self.ask("PUT", "/markers/" + MARKER[:-1] + "9", marker()).status)

    def test_a_change_that_cannot_be_written_down_is_not_made(self):
        first = self.ask("PUT", "/markers/" + MARKER, marker()).body["rev"]
        with mock.patch.object(self.ss, "write_private", side_effect=OSError(28, "No space left on device")):
            full = self.ask("PUT", "/markers/" + MARKER[:-1] + "2", marker())
            self.assertEqual(507, full.status)
            self.assertEqual(507, self.ask("DELETE", "/markers/" + MARKER).status)
            state = self.ask("GET", "/state").body
            self.assertEqual((first, [MARKER], []), (state["rev"], [m["id"] for m in state["markers"]], state["removed"]))
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertEqual(500, self.ask("POST", "/admin", {"on": False}).status)
            self.assertIn("No space left", said.getvalue(), "the box's own log says why")
            self.assertTrue(self.ss.on)
        self.assertEqual(first + 1, self.ask("DELETE", "/markers/" + MARKER).body["rev"], "and works again when the disk does")

    def test_a_file_that_cannot_be_read_is_put_aside(self):
        self.ask("PUT", "/markers/" + MARKER, marker())
        with open(self.ss.MARKERS, "w") as f:
            f.write("{half a fi")
        self.ss.load_markers()
        self.assertEqual({}, self.ss.markers)
        self.assertTrue(os.path.exists(self.ss.MARKERS + ".bad"))
        self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER, marker()).status)
        # One marker that no longer passes the check is left out; the rest stay.
        with open(self.ss.MARKERS) as f:
            data = json.load(f)
        data["markers"]["bad-marker-0001"] = {"rev": 1, "feature": {"type": "Feature"}}
        with open(self.ss.MARKERS, "w") as f:
            json.dump(data, f)
        self.ss.load_markers()
        self.assertEqual([MARKER], list(self.ss.markers))

    # ---------- limits ----------

    def test_what_is_sent_may_only_be_so_large(self):
        self.assertEqual(413, self.ask("POST", "/position", raw=b" " * 2049).status)
        self.assertEqual(413, self.ask("POST", "/leave", raw=b" " * 2049).status)
        self.assertEqual(413, self.ask("PUT", "/markers/" + MARKER, raw=b" " * 8193).status)
        self.assertEqual(413, self.ask("POST", "/admin", raw=b" " * 1025).status)
        padded = json.dumps(marker()).encode().ljust(8192)
        self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER, raw=padded).status)
        self.assertEqual(400, self.ask("POST", "/position", headers={**HOME, "Content-Length": "many"}).status)
        self.assertEqual(400, self.ask("POST", "/position", headers={**HOME, "Content-Length": "-5"}).status)

    def test_only_the_listed_addresses_and_methods(self):
        for method, path in (("GET", "/"), ("GET", "/markers"), ("GET", "/settings.json"), ("GET", "/../remote/ca.key"), ("POST", "/enrol"),
                             ("GET", "/info/"), ("GET", "/state/x"), ("GET", "/info" + "x" * 300)):
            self.assertEqual(404, self.ask(method, path).status, path)
        for method, path in (("POST", "/info"), ("POST", "/state"), ("GET", "/position"), ("PUT", "/position"), ("DELETE", "/leave"),
                             ("GET", "/markers/" + MARKER), ("POST", "/markers/" + MARKER), ("DELETE", "/admin")):
            self.assertEqual(405, self.ask(method, path, {}).status, (method, path))
        for path in ("/info?x=1", "/position?uid=" + UID, "/markers/" + MARKER + "?force=1", "/admin?on=true"):
            self.assertEqual(400, self.ask("POST" if "position" in path else "PUT" if "markers" in path else "GET", path, {}).status, path)

    def test_a_caller_that_asks_too_often_is_told_to_wait(self):
        for _ in range(60):
            self.assertEqual(200, self.ask("GET", "/state").status)
        self.assertEqual(429, self.ask("GET", "/state").status)
        self.assertEqual(429, self.ask("GET", "/info").status)
        self.assertEqual(200, self.ask("GET", "/state", headers=OTHER).status, "others are not held up")
        self.assertEqual(200, self.ask("GET", "/state", headers=OUTSIDE).status)
        self.time += 1
        for _ in range(10):
            self.assertEqual(200, self.ask("GET", "/state").status, "ten a second")
        self.assertEqual(429, self.ask("GET", "/state").status)

    def test_marker_changes_are_counted_apart_because_each_is_written_down(self):
        with mock.patch.object(self.ss, "RATE_ALL", (1000, 1000)):
            for n in range(60):
                self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER, marker(count=n)).status)
            self.assertEqual(429, self.ask("PUT", "/markers/" + MARKER, marker()).status)
            self.assertEqual(429, self.ask("DELETE", "/markers/" + MARKER).status)
            self.assertEqual(200, self.ask("GET", "/state").status, "reading goes on")
            self.time += 2
            self.assertEqual(200, self.ask("PUT", "/markers/" + MARKER, marker()).status, "one a second after that")

    def test_the_list_of_callers_does_not_grow_without_end(self):
        for n in range(5001):
            self.ss.allow("all", ("home", str(n)), (10, 60))
        self.time += 601
        self.ss.allow("all", ("home", "last"), (10, 60))
        self.assertEqual(1, len(self.ss.buckets))


class OverTheSocketTest(unittest.TestCase):
    """The service as the web server and the gateway reach it: over its socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ss = load(self.tmp.name)
        self.ss.load_markers()
        with contextlib.redirect_stdout(io.StringIO()):
            self.ss.set_enabled(True)
        self.back = os.getcwd()
        os.chdir(self.tmp.name)             # a socket's path may only be so long
        self.server = self.ss.SocketServer("share.sock", self.ss.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.errors = mock.patch.object(self.ss.sys, "stderr", io.StringIO())
        self.log = self.errors.start()

    def tearDown(self):
        self.errors.stop()
        self.server.shutdown()
        self.server.server_close()
        os.chdir(self.back)
        self.tmp.cleanup()

    def connect(self):
        connection = http.client.HTTPConnection("box")
        connection.sock = socket.socket(socket.AF_UNIX)
        connection.sock.connect(os.path.join(self.tmp.name, "share.sock"))
        self.addCleanup(connection.close)
        return connection

    def ask(self, method, path, body=None, headers=HOME, connection=None):
        once = connection is None
        connection = connection or self.connect()
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = connection.getresponse()
        text = r.read()
        if once:
            connection.close()
        return r, json.loads(text) if text and r.getheader("Content-Type") == "application/json" else text

    def test_the_answers_as_the_app_gets_them(self):
        one = self.connect()
        r, body = self.ask("POST", "/position", position(), connection=one)
        self.assertEqual((204, b"", "0", "no-store"), (r.status, body, r.getheader("Content-Length"), r.getheader("Cache-Control")))
        r, body = self.ask("GET", "/state", connection=one)                # the same connection again
        self.assertEqual((200, "Blue"), (r.status, body["positions"][0]["name"]))
        self.assertEqual("fieldmap-share", r.getheader("Server"), "says nothing about what runs it")
        r, body = self.ask("PUT", "/markers/" + MARKER, marker(), OUTSIDE)
        self.assertEqual(200, r.status)
        r, body = self.ask("GET", f"/state?since={body['rev'] - 1}", headers=OUTSIDE)
        self.assertEqual([MARKER], [m["id"] for m in body["markers"]])
        for method in ("HEAD", "OPTIONS", "PATCH", "TRACE"):
            r, _ = self.ask(method, "/info")
            self.assertEqual(501, r.status, method)

    def test_too_much_is_refused_without_being_read(self):
        connection = self.connect()
        connection.putrequest("POST", "/position")
        connection.putheader("X-Real-IP", "192.168.1.20")
        connection.putheader("Content-Length", str(50_000_000))
        connection.endheaders()
        r = connection.getresponse()
        self.assertEqual(413, r.status)
        self.assertEqual("close", r.getheader("Connection"), "and the connection is hung up")

    def test_the_log_names_no_one(self):
        self.ask("POST", "/position", position(name="Not for the log"))
        self.ask("GET", "/state")
        self.ask("POST", "/position", position(lat=99, name="Not for the log"))
        self.ask("GET", "/nothing-here")
        log = self.log.getvalue()
        self.assertIn("400 POST /position", log)
        self.assertIn("404 GET /nothing-here", log)
        self.assertEqual(2, len(log.splitlines()), "only what went wrong")
        for secret in ("Not for the log", UID, "192.168.1.20"):
            self.assertNotIn(secret, log)

    def test_only_so_many_requests_are_answered_at_once(self):
        held = [self.ss.Server.busy.acquire(blocking=False) for _ in range(self.ss.MAX_BUSY)]
        try:
            with self.assertRaises((http.client.HTTPException, OSError)):
                self.ask("GET", "/info")
        finally:
            for _ in held:
                self.ss.Server.busy.release()
        r, _ = self.ask("GET", "/info")
        self.assertEqual(200, r.status)


if __name__ == "__main__":
    unittest.main()
