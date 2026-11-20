#!/usr/bin/env python3
"""Tests for tools/netbird_isolation.py (network isolation in NetBird), against a fake NetBird
management API that keeps NetBird's rules: a group's member list is replaced as a whole, the All
group holds every peer and cannot be changed, a setup key is shown once, a peer that joins gets
its key's auto-groups, and a peer's network map holds only the peers a policy links it to.

Run: python3 -m unittest tools/test_netbird_isolation.py"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import netbird_isolation as iso  # noqa: E402

TOKEN = "nbp_" + "T" * 36
MANAGEMENT = "https://nb.example.com:443"
PORT = 41234


class FakeNetBird:
    """One NetBird account behind the management API."""

    def __init__(self, role="admin"):
        self.token, self.role = TOKEN, role
        self.peers, self.keys, self.policies = {}, {}, {}
        self.groups = {"g-all": {"id": "g-all", "name": "All", "peers": []}}
        self.settings = {"extra": {"peer_approval_enabled": False}, "peer_expose_enabled": False, "peer_expose_groups": []}
        self.next_ip = 10
        self.calls = []
        self.fail = None            # (method, path prefix): that call answers 500
        self.add_default_policy()

    def add_default_policy(self):
        self.policies["p-default"] = {"id": "p-default", "name": "Default", "enabled": True, "rules": [
            {"id": "r-default", "name": "Default", "enabled": True, "action": "accept", "bidirectional": True, "protocol": "all",
             "sources": ["g-all"], "destinations": ["g-all"]}]}

    # -- what NetBird does outside the API --

    def join(self, key, name, groups_asked=None):
        """A machine registers with a setup key. Whatever it asks for, it gets the key's auto-groups."""
        k = next((k for k in self.keys.values() if k["plain"] == key), None)
        if k is None or k["revoked"] or (k["usage_limit"] and k["used"] >= k["usage_limit"]):
            raise PermissionError("setup key is invalid")
        k["used"] += 1
        pid = "peer-%d" % self.next_ip
        ip = "100.64.0.%d" % self.next_ip
        self.next_ip += 1
        self.peers[pid] = {"id": pid, "ip": ip, "name": name}
        self.groups["g-all"]["peers"].append(pid)
        for gid in k["auto_groups"]:
            self.groups[gid]["peers"].append(pid)
        return self.peers[pid]

    def plain_peer(self, name):
        """A machine that joined with a key that has no auto-groups (the old way, by hand)."""
        pid = "peer-%d" % self.next_ip
        ip = "100.64.0.%d" % self.next_ip
        self.next_ip += 1
        self.peers[pid] = {"id": pid, "ip": ip, "name": name}
        self.groups["g-all"]["peers"].append(pid)
        return self.peers[pid]

    def groups_of(self, pid):
        return {gid for gid, g in self.groups.items() if pid in g["peers"]}

    def reaches(self, src, dst, port, protocol="tcp"):
        """Can peer `src` open a connection to peer `dst` on this port, by NetBird's policies?"""
        for p in self.policies.values():
            if not p["enabled"]:
                continue
            for r in p["rules"]:
                if not r["enabled"] or r["action"] != "accept":
                    continue
                if r["protocol"] not in ("all", protocol) or (r.get("ports") and str(port) not in r["ports"]):
                    continue
                s, d = set(r["sources"]), set(r["destinations"])
                if (self.groups_of(src) & s and self.groups_of(dst) & d) or \
                        (r["bidirectional"] and self.groups_of(src) & d and self.groups_of(dst) & s):
                    return True
        return False

    def in_map(self, a, b):
        """Is peer b in peer a's network map (does a even know b)?"""
        for p in self.policies.values():
            for r in p["rules"] if p["enabled"] else []:
                s, d = set(r["sources"]), set(r["destinations"])
                ga, gb = self.groups_of(a), self.groups_of(b)
                if r["enabled"] and ((ga & s and gb & d) or (ga & d and gb & s)):
                    return True
        return False

    # -- the API --

    def __call__(self, method, url, headers, body):
        path = url.split("/api", 1)[1]
        self.calls.append((method, path))
        if headers.get("Authorization") != "Token " + self.token:
            return 401, {"message": "token invalid"}
        if self.fail and method == self.fail[0] and path.startswith(self.fail[1]):
            return 500, {"message": "internal error"}
        data = json.loads(body) if body else None
        writes = method in ("POST", "PUT", "DELETE")
        if writes and self.role not in ("admin", "owner"):
            return 403, {"message": "permission denied"}
        parts = path.split("?")[0].strip("/").split("/")
        if parts == ["users", "current"]:
            return 200, {"id": "u1", "role": self.role}
        if parts == ["accounts"]:
            return 200, [{"id": "acc", "settings": self.settings}]
        if parts == ["peers"]:
            return 200, [{**p, "groups": [{"id": g, "name": self.groups[g]["name"]} for g in self.groups_of(p["id"])]}
                         for p in self.peers.values()]
        if parts[0] == "groups":
            if method == "GET":
                return 200, [{"id": g["id"], "name": g["name"], "peers": [{"id": p} for p in g["peers"]]} for g in self.groups.values()]
            if method == "POST":
                gid = "g-" + uuid.uuid4().hex[:8]
                self.groups[gid] = {"id": gid, "name": data["name"], "peers": list(data.get("peers") or [])}
                return 200, {"id": gid, "name": data["name"], "peers": []}
            if method == "PUT":
                if parts[1] == "g-all":
                    return 422, {"message": "updating group ALL is not allowed"}
                self.groups[parts[1]].update(name=data["name"], peers=list(data.get("peers") or []))
                return 200, self.groups[parts[1]]
        if parts[0] == "policies":
            def shown(p):
                return {**p, "rules": [{**r, "sources": [{"id": g} for g in r["sources"]],
                                        "destinations": [{"id": g} for g in r["destinations"]]} for r in p["rules"]]}
            if method == "GET":
                return 200, [shown(p) for p in self.policies.values()]
            pid = parts[1] if method == "PUT" else "p-" + uuid.uuid4().hex[:8]
            self.policies[pid] = {"id": pid, **data}
            return 200, shown(self.policies[pid])
        if parts[0] == "setup-keys":
            if method == "GET":
                return 200, [{k: v for k, v in key.items() if k != "plain"} | {"key": key["plain"][:5] + "****"} for key in self.keys.values()]
            if method == "POST":
                kid = "k-" + uuid.uuid4().hex[:8]
                plain = str(uuid.uuid4()).upper()
                self.keys[kid] = {"id": kid, "name": data["name"], "type": data["type"], "auto_groups": data["auto_groups"],
                                  "usage_limit": data["usage_limit"], "expires_in": data["expires_in"], "ephemeral": data["ephemeral"],
                                  "revoked": False, "used": 0, "plain": plain}
                return 200, {"id": kid, "key": plain, "name": data["name"], "auto_groups": data["auto_groups"]}
            if method == "PUT":
                self.keys[parts[1]].update(revoked=data["revoked"], auto_groups=data["auto_groups"])
                return 200, {"id": parts[1]}
        return 404, {"message": "not found"}


class Server:
    """One Fieldmap server's view: its record, its device key, its paired devices."""

    def __init__(self, nb, name):
        self.nb = nb
        self.record = {}
        self.key = ""
        self.peer = nb.plain_peer(name)           # joined NetBird with a key typed by hand
        self.devices = {}                         # paired device id -> NetBird address seen at the gateway

    def keep(self, key):
        self.key = key

    def setup(self, new_key=False):
        iso.setup(self.record, MANAGEMENT, TOKEN, self.peer["ip"], PORT, self.devices, self.keep, transport=self.nb, new_key=new_key)

    def pair_and_join(self, name):
        """A device pairs (gets the block with self.key), joins NetBird, comes in through the gateway."""
        peer = self.nb.join(self.key, "Fieldmap " + name)
        self.devices[name] = peer["ip"]
        return peer


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.nb = FakeNetBird()
        self.a = Server(self.nb, "fieldmap-box")
        self.b = Server(self.nb, "fieldmap-box")
        self.a.setup()
        self.b.setup()

    def disable_default(self):
        self.nb.policies["p-default"]["enabled"] = False

    def test_each_server_gets_its_own_groups_policy_and_key(self):
        a, b = self.a.record, self.b.record
        self.assertRegex(a["id"], r"^[0-9a-f]{10}$")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual((a["serverGroup"], a["deviceGroup"], a["policy"]), (b["serverGroup"], b["deviceGroup"], b["policy"]))
        self.assertNotEqual(self.a.key, self.b.key, "a key per server, never shared")
        names = {g["name"] for g in self.nb.groups.values()}
        self.assertTrue({"fieldmap-server-" + a["id"], "fieldmap-devices-" + a["id"]} <= names)
        self.assertNotIn("fieldmap", names, "no global Fieldmap group")
        policy = self.nb.policies[a["policy"]]
        self.assertEqual("fieldmap-" + a["id"], policy["name"])
        rule = policy["rules"][0]
        self.assertEqual(([a["deviceGroup"]], [a["serverGroup"]], False, "tcp", [str(PORT)]),
                         (rule["sources"], rule["destinations"], rule["bidirectional"], rule["protocol"], rule["ports"]))
        key = self.nb.keys[a["deviceKey"]["id"]]
        self.assertEqual(("reusable", [a["deviceGroup"]], iso.KEY_USES, False), (key["type"], key["auto_groups"], key["usage_limit"], key["ephemeral"]))
        self.assertEqual(iso.KEY_DAYS * 86400, key["expires_in"])
        self.assertIn(self.a.peer["id"], self.nb.groups[a["serverGroup"]]["peers"])
        for p in self.nb.policies.values():
            if p["name"].startswith("fieldmap-"):
                self.assertNotIn("g-all", p["rules"][0]["sources"] + p["rules"][0]["destinations"], "All is never used")

    def test_devices_join_their_own_servers_segment_and_nothing_else(self):
        self.disable_default()
        a1, a2 = self.a.pair_and_join("A1"), self.a.pair_and_join("A2")
        b1 = self.b.pair_and_join("B1")
        # 1, 2: the device group of the server they paired with.
        for d in (a1, a2):
            self.assertIn(self.a.record["deviceGroup"], self.nb.groups_of(d["id"]))
            self.assertNotIn(self.b.record["deviceGroup"], self.nb.groups_of(d["id"]))
        # 3: both reach their server's gateway, and only its gateway port.
        for d in (a1, a2):
            self.assertTrue(self.nb.reaches(d["id"], self.a.peer["id"], PORT))
            self.assertFalse(self.nb.reaches(d["id"], self.a.peer["id"], 22))
            self.assertFalse(self.nb.reaches(d["id"], self.a.peer["id"], PORT, "udp"))
        # 4: devices of one server do not talk to each other (sharing goes through the server).
        self.assertFalse(self.nb.reaches(a1["id"], a2["id"], PORT))
        self.assertFalse(self.nb.in_map(a1["id"], a2["id"]))
        # 5: a device of B reaches nothing of A, and does not even know it is there.
        for target in (self.a.peer, a1, a2):
            self.assertFalse(self.nb.reaches(b1["id"], target["id"], PORT))
            self.assertFalse(self.nb.in_map(b1["id"], target["id"]))
        # 6: server A starts no connection to B's devices (nor to its own).
        for target in (b1, a1):
            self.assertFalse(self.nb.reaches(self.a.peer["id"], target["id"], PORT))
        self.assertFalse(self.nb.reaches(self.a.peer["id"], self.b.peer["id"], PORT))

    def test_a_device_cannot_choose_another_segment(self):
        self.disable_default()
        # 7: the group comes from the key, in NetBird. Whatever the device asks for, A's key gives A's group.
        sneaky = self.nb.join(self.a.key, "Fieldmap Sneaky", groups_asked=[self.b.record["deviceGroup"], self.b.record["serverGroup"]])
        self.assertNotIn(self.b.record["deviceGroup"], self.nb.groups_of(sneaky["id"]))
        self.assertFalse(self.nb.reaches(sneaky["id"], self.b.peer["id"], PORT))
        # And the server's run never takes a group from anything a device sent.
        source = open(os.path.join(HERE, "netbird_isolation.py")).read()
        self.assertNotIn("groups_asked", source)

    def test_running_again_creates_nothing_new(self):
        before = (len(self.nb.groups), len(self.nb.policies), len(self.nb.keys), self.a.key)
        self.nb.calls.clear()
        for _ in range(3):              # 8: restarts, repeated clicks
            self.a.setup()
        self.assertEqual(before, (len(self.nb.groups), len(self.nb.policies), len(self.nb.keys), self.a.key))
        self.assertFalse([c for c in self.nb.calls if c[0] == "POST"], "a repeat run creates nothing")

    def test_a_changed_policy_is_found_and_repaired(self):
        self.disable_default()
        policy = self.nb.policies[self.a.record["policy"]]
        policy["rules"][0]["bidirectional"] = True
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertEqual("open", iso.status(self.a.record, PORT, self.a.peer["ip"])["status"])
        self.a.setup()
        self.assertFalse(self.nb.policies[self.a.record["policy"]]["rules"][0]["bidirectional"])
        self.assertEqual("verified", iso.status(self.a.record, PORT, self.a.peer["ip"])["status"])
        # A new gateway port: the page stops saying verified until the next run moves the policy along.
        self.assertEqual("stale", iso.status(self.a.record, PORT + 1, self.a.peer["ip"])["status"])

    def test_a_new_device_key_keeps_the_devices_that_joined(self):
        self.disable_default()
        a1 = self.a.pair_and_join("A1")
        old_key, old_id = self.a.key, self.a.record["deviceKey"]["id"]
        self.a.setup(new_key=True)      # 9: Replace Device Key
        self.assertNotEqual(old_key, self.a.key)
        self.assertTrue(self.nb.keys[old_id]["revoked"], "the old key is revoked")
        self.assertTrue(self.nb.reaches(a1["id"], self.a.peer["id"], PORT), "a device that joined keeps working")
        with self.assertRaises(PermissionError):
            self.nb.join(old_key, "late")
        a2 = self.a.pair_and_join("A2")
        self.assertIn(self.a.record["deviceGroup"], self.nb.groups_of(a2["id"]))
        self.assertEqual(2, len([k for k in self.nb.keys.values() if k["name"] == "fieldmap-devices-" + self.a.record["id"]]))

    def test_devices_from_before_are_moved_by_their_address(self):
        # 10: a server that used one key typed by hand: its devices joined without a group.
        nb = FakeNetBird()
        old = Server(nb, "fieldmap-box")
        p1, p2 = nb.plain_peer("Fieldmap Phone"), nb.plain_peer("Fieldmap Tablet")
        nb.plain_peer("Fieldmap Phone")                 # same name, another account member: names are never trusted
        old.devices = {"d1": p1["ip"], "d2": p2["ip"], "d3": ""}    # d3 never came in through NetBird
        nb.groups["g-team"] = {"id": "g-team", "name": "team", "peers": [p1["id"]]}
        old.setup()
        group = nb.groups[old.record["deviceGroup"]]["peers"]
        self.assertEqual({p1["id"], p2["id"]}, set(group))
        self.assertEqual([p1["id"]], nb.groups["g-team"]["peers"], "other groups are left alone")
        self.assertEqual(["d3"], old.record["unmatched"])
        self.assertIn(1, [len(old.record["unmatched"])])
        self.assertTrue(any("have not connected through NetBird" in w for w in old.record["warnings"]))

    def test_the_default_policy_and_other_open_doors_are_reported(self):
        # 11: Default All<->All is on in a new account.
        st = iso.status(self.a.record, PORT, self.a.peer["ip"])
        self.assertEqual("open", st["status"])
        self.assertTrue(any("Default" in p for p in st["problems"]))
        self.assertTrue(self.nb.policies["p-default"]["enabled"], "never disabled by the server")
        self.disable_default()
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertEqual("verified", iso.status(self.a.record, PORT, self.a.peer["ip"])["status"])
        # Another policy that names one of our groups, or one of our peers directly.
        self.nb.policies["p-x"] = {"id": "p-x", "name": "admins", "enabled": True, "rules": [
            {"enabled": True, "action": "accept", "bidirectional": True, "protocol": "all",
             "sources": ["g-admins"], "destinations": [self.a.record["deviceGroup"]]}]}
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertIn("admins", " ".join(self.a.record["problems"]))
        del self.nb.policies["p-x"]
        self.nb.policies["p-y"] = {"id": "p-y", "name": "one peer", "enabled": True, "rules": [
            {"enabled": True, "action": "accept", "bidirectional": False, "protocol": "tcp", "sources": ["g-admins"],
             "destinations": [], "destination": {"id": self.a.peer["id"], "type": "peer"}}]}
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertIn("one peer", " ".join(self.a.record["problems"]))
        # A stranger in our server group.
        del self.nb.policies["p-y"]
        self.nb.groups[self.a.record["serverGroup"]]["peers"].append(self.b.peer["id"])
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertEqual("open", iso.status(self.a.record, PORT, self.a.peer["ip"])["status"])

    def test_warnings_that_do_not_open_the_segment(self):
        self.disable_default()
        self.a.pair_and_join("A1")
        removed = self.a.pair_and_join("A2")
        del self.a.devices["A2"]                        # removed on the server, still a NetBird peer
        self.nb.settings["extra"]["peer_approval_enabled"] = True
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        text = " ".join(self.a.record["warnings"])
        self.assertIn(removed["name"], text)
        self.assertIn("Peer approval", text)
        self.assertEqual("verified", iso.status(self.a.record, PORT, self.a.peer["ip"])["status"])
        late = iso.status(self.a.record, PORT, self.a.peer["ip"], now=self.a.record["lastCheck"] + (iso.CHECK_DAYS + 1) * 86400)
        self.assertEqual("stale", late["status"])
        self.a.record["deviceKey"]["expires"] = self.a.record["lastCheck"] + 86400
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertIn("expires", " ".join(self.a.record["warnings"]))

    def test_a_failure_anywhere_never_says_verified(self):
        # 12: every call of a run fails once, in turn.
        self.disable_default()
        self.a.pair_and_join("A1")
        fresh = Server(self.nb, "fieldmap-box")
        fresh.devices = dict(self.a.devices)
        self.nb.groups[self.a.record["deviceGroup"]]["peers"].clear()   # so that moving it is a call too
        self.nb.calls.clear()
        fresh.setup()
        # Every call a first run makes (the account settings only feed warnings: tested below).
        # Ids differ from run to run: a PUT to any group (or key, or policy) counts as one step.
        steps = sorted({(m, re.sub(r"/[^/]+$", "/", p.split("?")[0]) if m == "PUT" else p.split("?")[0])
                        for m, p in self.nb.calls} - {("GET", "/accounts")})
        self.assertGreaterEqual(len(steps), 9)
        for method, path in steps:
            with self.subTest(call=(method, path)):
                self.nb.fail = (method, path)
                record = {}
                with self.assertRaises(iso.IsolationError):
                    iso.setup(record, MANAGEMENT, TOKEN, self.a.peer["ip"], PORT, self.a.devices, lambda k: None,
                              transport=self.nb)
                self.assertNotEqual("verified", iso.status(record, PORT, self.a.peer["ip"])["status"])
                self.assertTrue(record["lastError"])
                self.assertNotIn(TOKEN, json.dumps(record))
                for leftover in [g for g in self.nb.groups.values() if g["name"].startswith("fieldmap-") and not g["peers"]]:
                    del self.nb.groups[leftover["id"]]      # what a half run made would block the next name otherwise
                for pid in [p["id"] for p in self.nb.policies.values() if p["name"] == "fieldmap-" + record.get("id", "-")]:
                    del self.nb.policies[pid]
        self.nb.fail = ("GET", "/accounts")
        iso.check(self.a.record, MANAGEMENT, TOKEN, PORT, self.a.devices, transport=self.nb)
        self.assertIn("settings could not be read", " ".join(self.a.record["warnings"]))
        self.nb.fail = None

    def test_the_token_is_checked_before_anything_changes(self):
        nb = FakeNetBird(role="auditor")
        s = Server(nb, "fieldmap-box")
        with self.assertRaises(iso.IsolationError) as e:
            s.setup()
        self.assertIn("Admin", str(e.exception))
        self.assertEqual([("GET", "/users/current")], nb.calls, "nothing else was asked or changed")
        nb.role = "admin"
        s.setup()
        nb.role = "auditor"         # a read-only token is enough to check
        nb.policies["p-default"]["enabled"] = False
        iso.check(s.record, MANAGEMENT, TOKEN, PORT, s.devices, transport=nb)
        self.assertTrue(s.record["checkOk"])
        wrong = Server(nb, "x")
        nb.token = "nbp_other"
        with self.assertRaises(iso.IsolationError) as e:
            wrong.setup()
        self.assertIn("did not accept", str(e.exception))

    def test_names_that_are_not_ours_are_never_taken_over(self):
        nb = FakeNetBird()
        s = Server(nb, "fieldmap-box")
        with mock.patch.object(iso, "new_id", return_value="0123456789"):
            nb.groups["g-x"] = {"id": "g-x", "name": "fieldmap-server-0123456789", "peers": []}
            with self.assertRaises(iso.IsolationError) as e:
                s.setup()
        self.assertIn("not created by this server", str(e.exception))
        self.assertEqual([], nb.groups["g-x"]["peers"])

    def test_the_server_must_be_a_member_of_this_account(self):
        nb = FakeNetBird()
        record = {}
        with self.assertRaises(iso.IsolationError):
            iso.setup(record, MANAGEMENT, TOKEN, "100.64.9.9", PORT, {}, lambda k: None, transport=nb)
        with self.assertRaises(iso.IsolationError):
            iso.setup(record, MANAGEMENT, TOKEN, "", PORT, {}, lambda k: None, transport=nb)
        self.assertEqual(1, len(nb.groups), "nothing made")

    def test_tokens_and_addresses_as_typed(self):
        self.assertEqual(TOKEN, iso.clean_token("  " + TOKEN + "\n"))
        for bad in ("", "short", "nbp_" + "x" * 20 + ";rm", "Token " + TOKEN):
            self.assertEqual("", iso.clean_token(bad), bad)
        self.assertEqual("https://api.netbird.io:443/api", iso.api_base("https://api.netbird.io:443"))
        self.assertEqual("http://192.0.2.10:8080/api", iso.api_base("http://192.0.2.10:8080"))


# ---------- the service: the page's calls, the token never kept ----------

def load_service(folder):
    import importlib.util
    os.environ["REMOTE_DIR"] = folder
    spec = importlib.util.spec_from_file_location("remote_server_iso", os.path.join(HERE, "remote-server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServiceTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, HERE)
        from test_remote_server import FakeRequest, phone_key
        self.FakeRequest, self.phone_key = FakeRequest, phone_key
        self.tmp = tempfile.TemporaryDirectory()
        self.rs = load_service(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            self.rs.init("/maps/", "http://127.0.0.1:8080/", "8443", True)
        self.patches = [mock.patch.object(self.rs.portmap, "global_ipv6", return_value=[]),
                        mock.patch.object(self.rs.portmap, "release", return_value=[]),
                        mock.patch.object(self.rs.portmap, "reach", return_value={"checked": 1, "ipv4": {}, "ipv6": {}, "problem": "router-closed"})]
        for p in self.patches:
            p.start()
        os.environ["REMOTE_PORT"] = str(PORT)
        self.rs.write_private(self.rs.SETTINGS, json.dumps({"enabled": True}).encode())
        self.nb = FakeNetBird()
        self.box = self.nb.plain_peer("fieldmap-box")
        self.post("/netbird", {"on": True, "setupKey": "test-setup-key-for-fieldmap-0001", "management": MANAGEMENT})
        self.netbird_says(self.box["ip"])
        self.transport = mock.patch.object(iso, "http_transport", self.nb)
        self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.rs.time.sleep(0.05)
        for p in self.patches:
            p.stop()
        os.environ.pop("REMOTE_PORT", None)
        self.tmp.cleanup()

    def netbird_says(self, ip):
        with open(self.rs.NETBIRD_STATUS, "w") as f:
            json.dump({"management": {"connected": True}, "netbirdIp": ip + "/16"}, f)
        with open(self.rs.NETBIRD_APPLY) as src, open(self.rs.NETBIRD_STATUS_FOR, "w") as dst:
            dst.write(src.read())
        self.rs._network.update(at=0.0, network=None)

    def post(self, path, body, headers=None):
        return self.FakeRequest(self.rs, "POST", path, headers if headers is not None else {"X-Real-IP": "192.168.1.20"}, body)

    def get(self, path, headers=None):
        return self.FakeRequest(self.rs, "GET", path, headers if headers is not None else {"X-Real-IP": "192.168.1.20"})

    def every_file(self):
        out = ""
        for root, _, files in os.walk(self.tmp.name):
            for name in files:
                try:
                    out += open(os.path.join(root, name), errors="replace").read()
                except OSError:
                    pass
        return out

    def test_setup_from_the_page_gives_devices_the_servers_own_key(self):
        _, public = self.phone_key()
        device = self.rs.enrol("Phone", public)
        # The device came in through NetBird once: the gateway noted where from.
        phone = self.nb.plain_peer("Fieldmap Phone")
        self.assertTrue(self.rs.allowed(device["sha1"], phone["ip"]))
        self.rs.write_seen()
        self.assertEqual(phone["ip"], self.rs.load_devices()[0]["netbirdAddress"])
        rev = self.rs.netbird_block()["rev"]
        out = io.StringIO()
        with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
            done = self.post("/netbird/isolation", {"action": "setup", "token": TOKEN})
        self.assertEqual(200, done.status, done.out)
        self.assertIn("Revoke the setup key you entered by hand", done.body["note"])
        record = self.rs.load_isolation()
        self.assertIn(phone["id"], self.nb.groups[record["deviceGroup"]]["peers"], "moved into the device group")
        block = self.rs.netbird_block()
        key = self.nb.keys[record["deviceKey"]["id"]]["plain"]
        self.assertEqual(key, block["setupKey"], "paired devices now get the server's own key")
        self.assertNotEqual(rev, block["rev"], "so they pick it up")
        iso_info = self.get("/info").body["netbird"]["isolation"]
        self.assertEqual(("open", True), (iso_info["status"], iso_info["managed"]), "Default policy still on")
        # The token: in no file, no answer, no log line; the key: in its own file only.
        self.assertNotIn(TOKEN, self.every_file())
        self.assertNotIn(TOKEN, json.dumps(done.body) + out.getvalue())
        self.assertNotIn(key, json.dumps(self.get("/info").body) + out.getvalue())
        self.assertEqual(0o600, os.stat(self.rs.NETBIRD_KEY).st_mode & 0o777)
        # A key typed by hand would leave the segment: refused while isolation manages the key.
        self.assertEqual(409, self.post("/netbird", {"on": True, "setupKey": "test-setup-key-for-fieldmap-0002"}).status)
        self.assertEqual(409, self.post("/netbird", {"on": True, "management": "https://other.example.com"}).status)
        self.assertEqual(200, self.post("/netbird", {"on": True, "management": MANAGEMENT}).status)
        # Check with the Default policy off.
        self.nb.policies["p-default"]["enabled"] = False
        self.assertEqual(200, self.post("/netbird/isolation", {"action": "check", "token": TOKEN}).status)
        self.assertEqual("verified", self.get("/info").body["netbird"]["isolation"]["status"])

    def test_the_page_calls_are_for_the_local_network_and_the_access_code(self):
        self.assertEqual(403, self.post("/netbird/isolation", {"action": "setup", "token": TOKEN}, {"X-Real-IP": "203.0.113.30"}).status)
        os.environ["REMOTE_PIN"] = "4321"
        try:
            self.assertEqual(403, self.post("/netbird/isolation", {"action": "setup", "token": TOKEN}).status)
        finally:
            os.environ.pop("REMOTE_PIN", None)
        self.assertEqual(400, self.post("/netbird/isolation", {"action": "setup", "token": "x"}).status)
        self.assertEqual(400, self.post("/netbird/isolation", {"action": "delete", "token": TOKEN}).status)
        self.assertEqual(409, self.post("/netbird/isolation", {"action": "check", "token": TOKEN}).status, "not set up yet")
        self.assertEqual([], [c for c in self.nb.calls if c[0] != "GET"])

    def test_a_failed_run_is_kept_and_shown(self):
        self.nb.fail = ("POST", "/policies")
        failed = self.post("/netbird/isolation", {"action": "setup", "token": TOKEN})
        self.assertEqual(409, failed.status)
        st = self.get("/info").body["netbird"]["isolation"]
        self.assertEqual("failed", st["status"])
        self.assertIn("policy", st["text"])
        self.assertEqual("test-setup-key-for-fieldmap-0001", self.rs.netbird_key(), "the key in use stays until a run made a new one")
        self.nb.fail = None
        self.assertEqual(200, self.post("/netbird/isolation", {"action": "setup", "token": TOKEN}).status, "retry works")
        self.assertEqual(2, len([g for g in self.nb.groups.values() if g["name"].startswith("fieldmap-")]), "and made nothing twice")


if __name__ == "__main__":
    unittest.main()
