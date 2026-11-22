#!/usr/bin/env python3
"""Tests for tools/admin-server.py: Pi mode, the RAM check, the free-space reserve and the services a
build stops. Run: python3 -m unittest tools/test_admin_server.py"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = ("PI_MODE", "BUILD_MEMORY", "BUILD_RESERVE_GB", "FIELDMAP_PAUSE_UNITS", "FIELDMAP_DATA")


class AdminServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.data = os.path.join(self.root, "data")
        os.makedirs(os.path.join(self.data, "admin"))
        env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
        env.update(FIELDMAP_DATA=self.data, FIELDMAP_PAUSE_STATE=os.path.join(self.root, "paused.json"),
                   FIELDMAP_PAUSE_HELPER=os.path.join(self.root, "pause-services"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location("admin_server", os.path.join(HERE, "admin-server.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        del os.environ["FIELDMAP_DATA"]          # from .env below, as on a real server
        self.m.ROOT = self.root                  # .env is read from here
        self.ram = 3.7                           # a 4 GB Raspberry Pi
        self.m.ram_gb = lambda: self.ram
        self.write_env("")

    def tearDown(self):
        self.dir.cleanup()

    def write_env(self, text):
        with open(os.path.join(self.root, ".env"), "w") as f:
            f.write(text)

    def selection(self, regions=("us/delaware",), layers=("basemap", "contours", "hillshade", "elevation", "routing"),
                  **options):
        return {"regions": list(regions), "layers": list(layers), "options": options}

    def estimate(self, body, free_gb=500):
        usage = mock.Mock(free=free_gb * 1024 ** 3, total=1000 * 1024 ** 3)
        with mock.patch.object(self.m.shutil, "disk_usage", return_value=usage):
            return self.m.estimate(*self.m.check_selection(body))

    # ---------- Pi mode ----------

    def test_pi_mode_from_ram(self):
        self.assertTrue(self.m.pi_mode())
        self.ram = 7.6                           # an 8 GB machine
        self.assertFalse(self.m.pi_mode())

    def test_pi_mode_from_env(self):
        self.write_env("PI_MODE=no\n")
        self.assertFalse(self.m.pi_mode())
        self.ram = 31
        self.write_env('PI_MODE="yes"\n')
        self.assertTrue(self.m.pi_mode())
        # The environment wins over .env, as in scripts/lib.sh.
        with mock.patch.dict(os.environ, {"PI_MODE": "no"}):
            self.assertFalse(self.m.pi_mode())

    def test_memory_cap_and_reserve(self):
        self.assertEqual(self.m.build_memory_gb(), 2.5)
        self.assertEqual(self.m.reserve_gb(), 5)
        self.write_env("BUILD_MEMORY=2048m\nBUILD_RESERVE_GB=8\n")
        self.assertEqual(self.m.build_memory_gb(), 2)
        self.assertEqual(self.m.reserve_gb(), 8)
        self.ram = 64
        self.write_env("")
        self.assertIsNone(self.m.build_memory_gb())
        self.assertEqual(self.m.reserve_gb(), 0)
        self.write_env("BUILD_MEMORY=12g\n")     # a cap on a large machine too, when set
        self.assertEqual(self.m.build_memory_gb(), 12)

    def test_data_folder_from_env_file(self):
        self.write_env(f"FIELDMAP_DATA={self.data}\n")
        self.assertEqual(self.m.setting("FIELDMAP_DATA"), self.data)
        self.assertEqual(self.m.DATA, self.data)

    # ---------- 30 m only ----------

    def test_pi_mode_refuses_fine_terrain(self):
        for terrain in ("10m", "1m"):
            with self.assertRaises(self.m.BadRequest) as e:
                self.m.check_selection(self.selection(terrain=terrain))
            self.assertIn("Standard", str(e.exception))
        self.m.check_selection(self.selection(terrain="30m"))
        self.ram = 16
        self.m.check_selection(self.selection(terrain="10m"))

    def test_region_file_in_pi_mode(self):
        regions, layers, options = self.m.check_selection(self.selection())
        text = self.m.region_file(regions, options)
        self.assertIn('DEM_SOURCE="copernicus"', text)
        self.assertIn('PLANETILER_MEMORY="1706m"', text)       # two thirds of 2.5 GB
        self.ram = 32
        regions, layers, options = self.m.check_selection(self.selection(terrain="10m"))
        self.assertIn('PLANETILER_MEMORY="16g"', self.m.region_file(regions, options))

    def test_pi_mode_refuses_world_overview_it_cannot_build(self):
        with self.assertRaises(self.m.BadRequest) as e:
            self.m.check_selection(self.selection(world=True))
        self.assertIn("World Overview", str(e.exception))
        open(os.path.join(self.data, "world-z0-8.mbtiles"), "w").close()      # copied in from elsewhere
        self.m.check_selection(self.selection(world=True))
        # Without the base map, the option does not matter.
        os.remove(os.path.join(self.data, "world-z0-8.mbtiles"))
        self.m.check_selection(self.selection(layers=["contours"], world=True))

    # ---------- the RAM check ----------

    def test_small_region_fits_in_pi_mode(self):
        e = self.estimate(self.selection())
        self.assertEqual(e["memory_problem"], "")
        self.assertTrue(e["ok"])
        self.assertTrue(e["pi"])
        self.assertEqual(e["memory_gb"], 2.5)

    def test_large_base_map_is_refused_before_it_starts(self):
        e = self.estimate(self.selection(regions=["germany"]))
        self.assertFalse(e["ok"])
        self.assertTrue(e["space_ok"])
        self.assertIn("too large for this server to build the base map", e["memory_problem"])
        self.assertIn("4.5 GB", e["memory_problem"])
        with self.assertRaises(self.m.BadRequest) as err:
            self.m.check_build(e, {})
        self.assertEqual(str(err.exception), e["memory_problem"])
        # The same regions without the layers that run Planetiler: the terrain works cell by cell.
        e = self.estimate(self.selection(regions=["germany"], layers=["contours", "hillshade"]))
        self.assertEqual(e["memory_problem"], "")
        # The printed map details run Planetiler too.
        e = self.estimate(self.selection(regions=["germany"], layers=["extras"]))
        self.assertIn("printed map details", e["memory_problem"])

    def test_no_ram_check_without_a_cap(self):
        self.ram = 64
        e = self.estimate(self.selection(regions=["germany"]))
        self.assertEqual(e["memory_problem"], "")
        self.assertIsNone(e["memory_gb"])

    def test_machine_with_less_ram_than_the_cap(self):
        self.ram = 1.8
        e = self.estimate(self.selection())
        self.assertIn("1.8 GB of RAM", e["memory_problem"])
        self.assertIn("BUILD_MEMORY", e["memory_problem"])

    def test_pi_pace_is_slower(self):
        body = self.selection(regions=["us/maine"])
        work = lambda e: sum(i["hours"] for i in e["items"] if i["key"] not in ("osm", "dem", "routing"))
        pi = work(self.estimate(body))
        self.ram = 7.6
        pc = work(self.estimate(body))
        self.assertGreater(pi, pc * 2)

    # ---------- the free-space reserve ----------

    def test_reserve_is_kept_free(self):
        e = self.estimate(self.selection(), free_gb=500)
        need = e["need_gb"]
        e = self.estimate(self.selection(), free_gb=need + 4)          # fits, but leaves under 5 GB
        self.assertFalse(e["space_ok"])
        self.assertFalse(e["ok"])
        with self.assertRaises(self.m.BadRequest) as err:
            self.m.check_build(e, {})
        self.assertIn("5 GB must stay free", str(err.exception))
        self.assertTrue(self.estimate(self.selection(), free_gb=need + 6)["ok"])

    # ---------- services stopped during a build ----------

    def test_pause_list_shown_and_confirmed(self):
        self.write_env("FIELDMAP_PAUSE_UNITS=jellyfin calibre-web docker:kiwix nginx\n")
        p = self.m.pause_list()
        self.assertEqual(p["services"], ["jellyfin", "calibre-web", "kiwix"])
        self.assertEqual([r["name"] for r in p["refused"]], ["nginx"])
        self.assertEqual(p["stopped"], [])
        self.assertFalse(p["ready"])
        e = self.estimate(self.selection())
        with self.assertRaises(self.m.BadRequest) as err:
            self.m.check_build(e, {"pause_ok": True})
        self.assertIn("not set up", str(err.exception))
        open(self.m.PAUSE_HELPER, "w").close()
        with self.assertRaises(self.m.BadRequest) as err:
            self.m.check_build(e, {})
        self.assertIn("jellyfin, calibre-web, kiwix", str(err.exception))
        self.m.check_build(e, {"pause_ok": True})
        self.write_env("")
        self.m.check_build(e, {})                # nothing to stop: nothing to confirm

    def test_status_shows_what_is_stopped(self):
        with open(self.m.PAUSE_STATE, "w") as f:
            json.dump({"since": 1, "stopped": [{"kind": "unit", "name": "jellyfin.service", "label": "jellyfin"}]}, f)
        with mock.patch.object(self.m, "on_sd_card", return_value=True):
            s = self.m.status()
        self.assertEqual(s["pause"]["stopped"], ["jellyfin"])
        self.assertTrue(s["pi"])
        self.assertTrue(s["sd_card"])
        self.assertEqual(s["memory_gb"], 2.5)

    def test_restore_runs_the_helper_only_when_something_is_stopped(self):
        calls = []

        def run(args, **_kw):
            calls.append(args)
            os.remove(self.m.PAUSE_STATE)
            return mock.Mock(returncode=0, stdout="Started again: jellyfin.\n")

        with mock.patch.object(self.m.subprocess, "run", side_effect=run):
            self.assertTrue(self.m.restore_services(say=lambda _l: None))
            self.assertEqual(calls, [])
            open(self.m.PAUSE_STATE, "w").close()
            said = []
            self.assertTrue(self.m.restore_services(say=said.append))
        self.assertEqual(calls, [["sudo", "-n", self.m.PAUSE_HELPER, "restore"]])
        self.assertEqual(said, ["Started again: jellyfin."])

    def test_dead_build_starts_the_services_again(self):
        self.m.write_json(self.m.JOB, {"state": "running", "pid": 999999999, "started": 1,
                                       "request": {"layers": ["basemap"], "names": ["Delaware"]}})
        restored = []
        with mock.patch.object(self.m, "restore_services", side_effect=lambda **_kw: restored.append(1)):
            job = self.m.job_status()
            for t in list(self.m.threading.enumerate()):
                if t is not self.m.threading.current_thread() and t.daemon:
                    t.join(5)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(restored, [1])

    # ---------- the drive ----------

    def test_sd_card_from_device_number(self):
        with mock.patch.object(self.m.os, "stat", return_value=mock.Mock(st_dev=os.makedev(179, 2))):
            self.assertTrue(self.m.on_sd_card("/srv/maps"))

    def test_sd_card_from_mount_source(self):
        mountinfo = ("22 1 0:21 / / rw,relatime - ext4 /dev/sda2 rw\n"
                     "30 22 0:33 / /srv/maps rw,relatime - btrfs /dev/mmcblk0p3 rw\n")
        st = mock.Mock(st_dev=os.makedev(0, 33))
        with mock.patch.object(self.m.os, "stat", return_value=st), \
                mock.patch.object(self.m.os.path, "realpath", side_effect=lambda p: p), \
                mock.patch("builtins.open", mock.mock_open(read_data=mountinfo)):
            self.assertTrue(self.m.on_sd_card("/srv/maps/data"))
            self.assertFalse(self.m.on_sd_card("/opt/fieldmap-server/data"))


if __name__ == "__main__":
    unittest.main()
