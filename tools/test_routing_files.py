#!/usr/bin/env python3
"""Tests for tools/routing-files.py. Run: python3 -m unittest tools/test_routing_files.py"""
import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("routing_files", os.path.join(HERE, "routing-files.py"))
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)


class Squares(unittest.TestCase):
    def test_names_are_the_south_west_corner(self):
        self.assertEqual(["W80_N35.rd5"], rf.squares(-79, 36, -76, 39))
        self.assertEqual(["E5_N45.rd5"], rf.squares(5.5, 45.5, 9.5, 49.5))
        self.assertEqual(["W115_N35.rd5"], rf.squares(-114.98, 36, -114.98, 36))
        self.assertEqual(["E0_N0.rd5"], rf.squares(0, 0, 1, 1))
        self.assertEqual(["W5_S5.rd5"], rf.squares(-4, -4, -1, -1))
        self.assertEqual(["W180_S90.rd5"], rf.squares(-180, -90, -179, -89))
        self.assertEqual(["E175_N85.rd5"], rf.squares(179, 89, 180, 90))

    def test_a_box_gets_every_square_it_overlaps(self):
        self.assertEqual(["W85_N35.rd5", "W80_N35.rd5", "W85_N40.rd5", "W80_N40.rd5"], rf.squares(-81, 38, -79, 41))
        # An edge on a square's border does not take in the square beyond it.
        self.assertEqual(["W80_N35.rd5"], rf.squares(-80, 35, -75, 40))
        self.assertEqual(72 * 36, len(rf.squares(-180, -90, 180, 90)))

    def test_a_box_across_the_180_degree_line_covers_both_sides(self):
        self.assertEqual(["E170_N50.rd5", "E175_N50.rd5", "W180_N50.rd5"], rf.squares(172, 51, -178, 54))
        self.assertEqual(["E175_S20.rd5", "W180_S20.rd5"], rf.squares(178, -18, -179, -16))

    def test_names_and_corners_go_both_ways(self):
        for n in rf.squares(-180, -90, 180, 90):
            self.assertEqual(n, rf.name(*rf.corner(n)))
        for bad in ("W80_N35.rd5.part", "index.json", "W81_N35.rd5", "E180_N0.rd5", "W80_N90.rd5", "../W80_N35.rd5", "w80_n35.rd5"):
            with self.assertRaises(ValueError):
                rf.corner(bad)

    def test_a_wrong_box_is_refused(self):
        for bad in ((-181, 0, 0, 1), (0, -91, 1, 0), (0, 5, 1, 4), (0, 0, 1, 91)):
            with self.assertRaises(ValueError):
                rf.squares(*bad)


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, folder, name, data):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as f:
            f.write(data)

    def test_the_index_lists_the_files_on_hand_and_drops_other_squares(self):
        self.put(self.dir, "W85_N35.rd5", b"one")
        self.put(self.dir, "W80_N40.rd5", b"three")
        self.put(self.dir, "E5_N45.rd5", b"from an earlier region")
        self.put(self.dir, "W80_N35.rd5.part", b"half")
        made = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
        out = rf.index(self.dir, (-81, 38, -79, 41), made)
        with open(os.path.join(self.dir, "index.json")) as f:
            self.assertEqual(out, json.load(f))
        self.assertEqual({"version": 1, "source": "https://brouter.de/brouter/segments4/", "made": "2026-01-02T03:04:05Z",
                          "files": [{"name": "W85_N35.rd5", "west": -85, "south": 35, "east": -80, "north": 40, "size": 3,
                                     "sha256": hashlib.sha256(b"one").hexdigest()},
                                    {"name": "W80_N40.rd5", "west": -80, "south": 40, "east": -75, "north": 45, "size": 5,
                                     "sha256": hashlib.sha256(b"three").hexdigest()}]}, out)
        self.assertEqual(["W80_N35.rd5.part", "W80_N40.rd5", "W85_N35.rd5", "index.json"], sorted(os.listdir(self.dir)))

    def test_an_area_of_open_sea_has_an_empty_index(self):
        out = rf.index(self.dir, (-40, 30, -35, 35))
        self.assertEqual([], out["files"])
        self.assertRegex(out["made"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_a_staged_build_replaces_the_files_and_keeps_the_folder(self):
        new, dest = os.path.join(self.dir, "incoming", "routing"), os.path.join(self.dir, "routing")
        self.put(dest, "E5_N45.rd5", b"old region")
        self.put(dest, "W80_N35.rd5", b"old copy")
        self.put(dest, "W80_N35.rd5.part", b"left over")
        self.put(dest, "index.json", b"{}")
        inode = os.stat(dest).st_ino
        self.put(new, "W80_N35.rd5", b"new copy")
        self.put(new, "W85_N35.rd5", b"new square")
        rf.index(new, (-85, 35, -76, 39))
        self.assertEqual(["W85_N35.rd5", "W80_N35.rd5"], rf.install(new, dest))
        self.assertEqual(inode, os.stat(dest).st_ino, "the web server's containers have this folder mounted")
        self.assertFalse(os.path.exists(new))
        self.assertEqual(["W80_N35.rd5", "W85_N35.rd5", "index.json"], sorted(os.listdir(dest)))
        with open(os.path.join(dest, "W80_N35.rd5"), "rb") as f:
            self.assertEqual(b"new copy", f.read())
        with open(os.path.join(dest, "index.json")) as f:
            self.assertEqual(["W85_N35.rd5", "W80_N35.rd5"], [e["name"] for e in json.load(f)["files"]])

    def test_install_refuses_names_that_are_not_squares(self):
        new = os.path.join(self.dir, "incoming")
        self.put(new, "index.json", json.dumps({"files": [{"name": "../index.html"}]}).encode())
        with self.assertRaises(ValueError):
            rf.install(new, os.path.join(self.dir, "routing"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "routing")))


class CommandLine(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "routing-files.py"), *args], capture_output=True, text=True)

    def test_squares_one_a_line(self):
        r = self.run_tool("squares", "-81", "38", "-79", "41")
        self.assertEqual(0, r.returncode)
        self.assertEqual("W85_N35.rd5\nW80_N35.rd5\nW85_N40.rd5\nW80_N40.rd5\n", r.stdout)

    def test_mistakes_are_said_in_plain_words(self):
        r = self.run_tool("squares", "x", "38", "-79", "41")
        self.assertNotEqual(0, r.returncode)
        self.assertIn("four numbers", r.stderr)
        r = self.run_tool("squares", "1")
        self.assertNotEqual(0, r.returncode)
        self.assertIn("routing-files.py squares", r.stderr)



class RouteService(unittest.TestCase):
    """The route service for the browser (brouter/): the web servers pass on route requests only."""
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def block(self, path):
        import re
        text = open(os.path.join(self.REPO, path)).read()
        m = re.search(r"location = /routing/route \{(.*?)\n\s*\}", text, re.S)
        self.assertIsNotNone(m, path)
        return text, m.group(1)

    def test_only_routes_with_the_apps_profiles_and_up_to_30_points(self):
        import re
        for path in ("web/nginx.conf", "pi/nginx/routing-nginx.conf"):
            text, block = self.block(path)
            self.assertIn("limit_except GET { deny all; }", block)
            self.assertIn('"^(car-vario|hiking-mountain)$"', block)
            # BRouter gets a request it builds from the two checked values only: never the
            # caller's path (its profile upload) or other arguments.
            self.assertRegex(block, r"/brouter\?lonlats=\$arg_lonlats&profile=\$arg_profile&alternativeidx=0&format=geojson;")
            self.assertNotIn("$args", block)
            self.assertNotIn("$request_uri", block)
            self.assertEqual(1, text.count("/brouter"), path)
            # No way found (BRouter's own 400 or 500): a plain 404; no service: 503.
            self.assertIn("error_page 400 404 500 = @no_way;", block)
            self.assertIn("location @no_way { return 404; }", text)
            self.assertIn("location @no_router { return 503; }", text)
            pattern = re.search(r'\$arg_lonlats !~ "(.*?)"\)', block).group(1)
            ok = ["-149.1568,61.1648|-149.0,61.0", "7.0,45.0%7C7.1,45.1", "|".join(["1.5,2.5"] * 30)]
            bad = ["-149.1,61.1", "|".join(["1.5,2.5"] * 31), "1,2|3,4&profile=x", "1,2|../x", "1,2;3,4", "1,2|3,4|", ""]
            for s in ok:
                self.assertRegex(s, pattern)
            for s in bad:
                self.assertNotRegex(s, pattern)

    def test_the_service_reads_the_routing_data_only(self):
        for path in ("docker-compose.yml", "pi/tileserver/docker-compose.pi.yml"):
            text = open(os.path.join(self.REPO, path)).read()
            service = text.split("  brouter:\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("build: ./brouter", service)
            self.assertIn("- ./data/routing:/segments:ro", service)
            self.assertNotIn("ports:", service)          # not reachable but through the web server
        docker = open(os.path.join(self.REPO, "brouter/Dockerfile")).read()
        self.assertIn("sha256sum -c", docker)
        self.assertIn("USER nobody", docker)


if __name__ == "__main__":
    unittest.main()
