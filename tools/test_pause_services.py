#!/usr/bin/env python3
"""Tests for tools/pause-services.py. Run: python3 -m unittest tools/test_pause_services.py"""
import importlib.util
import json
import os
import stat
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pause_services", os.path.join(HERE, "pause-services.py"))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


class Machine:
    """systemctl and docker as the helper sees them: what runs, and every command it was given."""

    def __init__(self, running=(), fail=()):
        self.running, self.fail, self.calls = set(running), set(fail), []
        self.state_path = None
        self.state_at_stop = {}

    def __call__(self, args):
        self.calls.append(args)
        name = args[-1]
        if args[:2] == ["systemctl", "is-active"]:
            return (0 if name in self.running else 3), ""
        if args[:2] == ["docker", "inspect"]:
            return (0, "true") if name in self.running else (1, "No such object")
        if args[1] in ("stop", "start"):
            if self.state_path and args[1] == "stop":
                self.state_at_stop[name] = ps.read_state(self.state_path)["stopped"]
            if name in self.fail:
                return 1, f"Failed to {args[1]} {name}"
            (self.running.discard if args[1] == "stop" else self.running.add)(name)
            return 0, ""
        raise AssertionError(args)


class PauseServicesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.dir.name, ".env")
        self.state = os.path.join(self.dir.name, "state", "paused.json")
        self.said = []

    def tearDown(self):
        self.dir.cleanup()

    def write_env(self, text):
        with open(self.env, "w") as f:
            f.write(text)

    def test_parse_units_and_containers(self):
        services, refused = ps.parse("jellyfin calibre-web.service, docker:kiwix  jellyfin")
        self.assertEqual(services, [
            {"kind": "unit", "name": "jellyfin.service", "label": "jellyfin"},
            {"kind": "unit", "name": "calibre-web.service", "label": "calibre-web"},
            {"kind": "container", "name": "kiwix", "label": "kiwix"}])
        self.assertEqual(refused, [])

    def test_never_stops_what_the_server_needs(self):
        value = ("nginx ssh sshd.service docker containerd systemd-networkd NetworkManager dnsmasq hostapd "
                 "fieldmap-admin fieldmap-share.service tileserver docker:tileserver docker:fieldmap-brouter "
                 "tailscaled getty@tty1")
        services, refused = ps.parse(value)
        self.assertEqual(services, [])
        self.assertEqual(len(refused), len(value.split()))
        self.assertTrue(all(r["why"] == "the server needs it" for r in refused))

    def test_refuses_names_that_are_not_services(self):
        services, refused = ps.parse("--now ../x multi-user.target data.mount docker: ok")
        self.assertEqual([s["name"] for s in services], ["ok.service"])
        self.assertEqual([r["name"] for r in refused], ["--now", "../x", "multi-user.target", "data.mount", "docker:"])

    def test_setting_read_from_env_file_only(self):
        self.write_env('PI_MODE=yes\nFIELDMAP_PAUSE_UNITS="old"\nFIELDMAP_PAUSE_UNITS="jellyfin docker:kiwix"\n')
        os.environ["FIELDMAP_PAUSE_UNITS"] = "nothing-from-the-environment"
        try:
            services, _ = ps.configured(self.env)
        finally:
            del os.environ["FIELDMAP_PAUSE_UNITS"]
        self.assertEqual([s["label"] for s in services], ["jellyfin", "kiwix"])
        self.assertEqual(ps.configured(os.path.join(self.dir.name, "missing")), ([], []))

    def test_stop_records_each_service_before_stopping_it(self):
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin calibre-web docker:kiwix\n")
        m = Machine(running={"jellyfin.service", "kiwix"})
        m.state_path = self.state
        self.assertEqual(ps.stop(self.env, self.state, m, self.said.append), 0)
        # Already on the list when it was stopped: a crash then still starts it again.
        self.assertEqual([s["name"] for s in m.state_at_stop["jellyfin.service"]], ["jellyfin.service"])
        self.assertEqual([s["name"] for s in m.state_at_stop["kiwix"]], ["jellyfin.service", "kiwix"])
        self.assertEqual(m.running, set())
        self.assertIn(["docker", "stop", "kiwix"], m.calls)
        self.assertNotIn(["systemctl", "stop", "calibre-web.service"], m.calls)      # was not running
        self.assertIn("Not running: calibre-web.", self.said)
        state = ps.read_state(self.state)
        self.assertEqual([s["name"] for s in state["stopped"]], ["jellyfin.service", "kiwix"])
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o644)

    def test_restore_starts_again_and_removes_the_state_file(self):
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin docker:kiwix\n")
        m = Machine(running={"jellyfin.service", "kiwix"})
        ps.stop(self.env, self.state, m, self.said.append)
        self.assertEqual(ps.restore(self.state, m, self.said.append), 0)
        self.assertEqual(m.running, {"jellyfin.service", "kiwix"})
        # Last stopped, first started.
        starts = [c for c in m.calls if c[1] == "start"]
        self.assertEqual(starts, [["docker", "start", "kiwix"], ["systemctl", "start", "jellyfin.service"]])
        self.assertFalse(os.path.exists(self.state))
        # Again: nothing to do.
        m.calls.clear()
        self.assertEqual(ps.restore(self.state, m, self.said.append), 0)
        self.assertEqual(m.calls, [])

    def test_restore_keeps_what_did_not_start(self):
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin calibre-web\n")
        m = Machine(running={"jellyfin.service", "calibre-web.service"})
        ps.stop(self.env, self.state, m, self.said.append)
        m.fail = {"calibre-web.service"}
        self.assertEqual(ps.restore(self.state, m, self.said.append), 1)
        self.assertEqual([s["name"] for s in ps.read_state(self.state)["stopped"]], ["calibre-web.service"])
        m.fail = set()
        self.assertEqual(ps.restore(self.state, m, self.said.append), 0)
        self.assertFalse(os.path.exists(self.state))

    def test_restore_after_a_crash_uses_only_the_state_file(self):
        """A reboot in the middle of a build: the list in .env may have changed since; only what
        the state file says was stopped is started."""
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin\n")
        m = Machine(running={"jellyfin.service"})
        ps.stop(self.env, self.state, m, self.said.append)
        self.write_env("FIELDMAP_PAUSE_UNITS=something-else\n")
        m2 = Machine()
        self.assertEqual(ps.restore(self.state, m2, self.said.append), 0)
        self.assertEqual(m2.calls, [["systemctl", "start", "jellyfin.service"]])

    def test_a_failed_stop_is_reported_and_kept_for_restore(self):
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin calibre-web\n")
        m = Machine(running={"jellyfin.service", "calibre-web.service"}, fail={"calibre-web.service"})
        self.assertEqual(ps.stop(self.env, self.state, m, self.said.append), 1)
        self.assertEqual([s["name"] for s in ps.read_state(self.state)["stopped"]],
                         ["jellyfin.service", "calibre-web.service"])

    def test_second_stop_keeps_what_an_earlier_build_left_stopped(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            json.dump({"since": 1, "stopped": [{"kind": "unit", "name": "old.service", "label": "old"}]}, f)
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin\n")
        m = Machine(running={"jellyfin.service"})
        ps.stop(self.env, self.state, m, self.said.append)
        self.assertEqual([s["name"] for s in ps.read_state(self.state)["stopped"]], ["old.service", "jellyfin.service"])

    def test_state_file_entries_are_checked(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            json.dump({"stopped": [{"kind": "unit", "name": "--force"}, {"kind": "shell", "name": "x"},
                                   {"kind": "container", "name": "kiwix"}]}, f)
        self.assertEqual(ps.read_state(self.state)["stopped"], [{"kind": "container", "name": "kiwix"}])
        with open(self.state, "w") as f:
            f.write("not json")
        self.assertEqual(ps.read_state(self.state)["stopped"], [])

    def test_installed_copy_reads_the_repository_env(self):
        """scripts/pause-setup.sh rewrites the ENV_FILE line of the root copy: it must be there, once."""
        with open(os.path.join(HERE, "pause-services.py")) as f:
            lines = [l for l in f if l.startswith("ENV_FILE = ")]
        self.assertEqual(len(lines), 1)
        with open(os.path.join(HERE, "..", "scripts", "pause-setup.sh")) as f:
            setup = f.read()
        self.assertIn('s#^ENV_FILE = .*#ENV_FILE = \\"$REPO/.env\\"#', setup)
        self.assertIn("NOPASSWD: $HELPER stop, $HELPER restore", setup)


if __name__ == "__main__":
    unittest.main()
