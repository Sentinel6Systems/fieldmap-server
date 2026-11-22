#!/usr/bin/env python3
"""Tests for the Geofabrik addresses of the builds: the outlines (tools/region-outline.py) and the
extract mirrors (osm_mirror_url in scripts/lib.sh). Run: python3 -m unittest tools/test_geofabrik_paths.py"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    spec = importlib.util.spec_from_file_location("region_outline", os.path.join(HERE, "region-outline.py"))
    ro = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ro)
except ImportError:                          # no GDAL here: the builds run it in the tools container
    ro = None

POLY = "area\n1\n   -80.0   40.0\n   -79.0   40.0\n   -79.0   41.0\n   -80.0   41.0\nEND\nEND\n"


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


@unittest.skipIf(ro is None, "needs GDAL (python3-gdal)")
class Outlines(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual("north-america/us/alaska", ro.plain_path("north-america/us/alaska"))
        self.assertEqual("north-america/us/alaska", ro.plain_path("north-america/us/alaska/alaska"))
        self.assertEqual("europe", ro.plain_path("europe/europe"))
        self.assertEqual("europe", ro.plain_path("/europe/"))

    def fetch(self, area, answers):
        asked = []

        def urlopen(url, timeout):
            asked.append(url)
            data = answers.get(url)
            if data is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return Response(data)

        with tempfile.TemporaryDirectory() as cache, mock.patch.object(ro.urllib.request, "urlopen", urlopen):
            g = ro.fetch_poly(area, cache)
            return g, asked, sorted(os.listdir(cache))

    def test_the_folder_address_comes_first(self):
        url = "https://download.geofabrik.de/north-america/us/alaska/alaska.poly"
        g, asked, files = self.fetch("north-america/us/alaska", {url: POLY.encode()})
        self.assertIsNotNone(g)
        self.assertEqual([url], asked)
        self.assertEqual(["north-america_us_alaska.poly"], files)

    def test_the_old_address_is_tried_next(self):
        url = "https://download.geofabrik.de/north-america/us/alaska.poly"
        g, asked, _ = self.fetch("north-america/us/alaska/alaska", {url: POLY.encode()})
        self.assertIsNotNone(g)
        self.assertEqual(["https://download.geofabrik.de/north-america/us/alaska/alaska.poly", url], asked)

    def test_no_outline(self):
        g, asked, files = self.fetch("north-america/us/nowhere", {})
        self.assertIsNone(g)
        self.assertEqual(2, len(asked))
        self.assertEqual([], files)

    def test_a_missing_outline_stops_the_build(self):
        # Not the whole box instead: for areas far apart that is many times the land asked for.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ro, "fetch_poly", return_value=None):
            a = mock.Mock(bbox=[-80, 40, -70, 45], areas=["north-america/us/nowhere"], poly_cache=d,
                          out=os.path.join(d, "out"), dem_dir=d, usgs_dir=None)
            with self.assertRaises(SystemExit) as e:
                ro.prepare(a)
            self.assertIn("north-america/us/nowhere", str(e.exception))
            self.assertFalse(os.path.exists(a.out))


class Mirrors(unittest.TestCase):
    def url(self, template, path):
        lib = os.path.join(os.path.dirname(HERE), "scripts", "lib.sh")
        script = f'eval "$(sed -n "/^osm_mirror_url()/,/^}}/p" "{lib}")"; osm_mirror_url "$1" "$2"'
        return subprocess.run(["bash", "-c", script, "-", template, path], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_geofabrik_folder_form(self):
        t = "https://download.geofabrik.de/{path}/{name}-latest.osm.pbf"
        want = "https://download.geofabrik.de/north-america/us/new-jersey/new-jersey-latest.osm.pbf"
        self.assertEqual(want, self.url(t, "north-america/us/new-jersey"))
        self.assertEqual(want, self.url(t, "north-america/us/new-jersey/new-jersey"))
        self.assertEqual("https://download.geofabrik.de/europe/europe-latest.osm.pbf", self.url(t, "europe"))

    def test_other_mirrors_get_the_plain_path(self):
        self.assertEqual("https://osm.download.movisda.io/north-america/us/new-jersey-latest.osm.pbf",
                         self.url("https://osm.download.movisda.io/{path}-latest.osm.pbf",
                                  "north-america/us/new-jersey/new-jersey"))
        self.assertEqual("https://geo2day.com/north_america/us/new-jersey.pbf",
                         self.url("https://geo2day.com/{path_}.pbf", "north-america/us/new-jersey"))


if __name__ == "__main__":
    unittest.main()
