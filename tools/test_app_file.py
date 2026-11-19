#!/usr/bin/env python3
"""Tests for tools/app-file.py. Run: python3 -m unittest tools/test_app_file.py"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(folder):
    os.environ["APP_DIR"] = folder
    spec = importlib.util.spec_from_file_location("app_file", os.path.join(HERE, "app-file.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apk_bytes(filler=b"x"):
    """Something with the inside of an Android app."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("classes.dex", filler * 1000)
    return out.getvalue()


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class AppFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "app")
        self.af = load(self.folder)
        self.apk = apk_bytes()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("APP_DIR", None)

    def release(self, data=None, digest=None, name="fieldmap-1.2.0.apk", tag="v1.2.0"):
        """A pretend GitHub: the release listing, then the file."""
        data = self.apk if data is None else data
        digest = "sha256:" + hashlib.sha256(data).hexdigest() if digest is None else digest
        listing = {"tag_name": tag, "assets": [{"name": "fieldmap-debug.apk", "size": 5, "browser_download_url": "https://example.com/debug"},
                                               {"name": name, "size": len(data), "digest": digest, "browser_download_url": "https://example.com/apk"}]}
        self.asked = []

        def urlopen(request, timeout=None):
            self.asked.append(request.full_url)
            if "api.github.com" in request.full_url:
                return Answer(json.dumps(listing).encode())
            if request.full_url == "https://example.com/apk":
                return Answer(data)
            raise AssertionError("asked for " + request.full_url)
        return mock.patch.object(self.af.urllib.request, "urlopen", side_effect=urlopen)

    def test_nothing_published_yet_changes_nothing(self):
        gone = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch.object(self.af.urllib.request, "urlopen", side_effect=gone):
            self.assertIn("No release of the Fieldmap app is published yet", self.af.fetch())
        self.assertIsNone(self.af.current())
        self.assertFalse(os.path.exists(self.af.APK))

    def test_the_newest_release_is_put_in_place_once(self):
        with self.release():
            self.assertIn("now v1.2.0", self.af.fetch())
        info = self.af.current()
        self.assertEqual(("v1.2.0", "github", len(self.apk)), (info["version"], info["source"], info["size"]))
        with open(self.af.APK, "rb") as f:
            self.assertEqual(self.apk, f.read())
        self.assertNotIn("https://example.com/debug", self.asked, "never a debug build")
        with self.release():
            self.assertIn("is the newest", self.af.fetch())
        self.assertEqual(["https://api.github.com/repos/Sentinel6Systems/fieldmap-android/releases/latest"], self.asked,
                         "the same file is not downloaded again")
        newer = apk_bytes(b"y")
        with self.release(newer, tag="v1.3.0"):
            self.assertIn("now v1.3.0", self.af.fetch())
        self.assertEqual("v1.3.0", self.af.current()["version"])
        self.assertEqual([], [n for n in os.listdir(self.folder) if n not in ("fieldmap.apk", "app.json")], "nothing left lying about")

    def test_a_file_that_is_not_the_published_one_is_refused(self):
        with self.release(digest="sha256:" + "0" * 64), self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            self.af.fetch()
        with self.release(data=b"not an app at all", digest=""), self.assertRaisesRegex(ValueError, "not an Android app"):
            self.af.fetch()
        self.assertIsNone(self.af.current())
        self.assertEqual([], os.listdir(self.folder), "nothing left lying about")

    def test_a_file_added_by_hand_is_left_alone_by_fetch(self):
        path = os.path.join(self.tmp.name, "my-build.apk")
        with open(path, "wb") as f:
            f.write(self.apk)
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(self.af.sys, "argv", ["app-file.py", "add", path]):
                self.af.main()
        info = self.af.current()
        self.assertEqual(("my-build", "by hand"), (info["version"], info["source"]))
        with self.release():
            self.assertIn("added by hand", self.af.fetch())
        self.assertEqual([], self.asked, "GitHub is not even asked")
        self.assertEqual("by hand", self.af.current()["source"])

    def test_only_an_android_app_can_be_added(self):
        path = os.path.join(self.tmp.name, "notes.apk")
        with open(path, "wb") as f:
            f.write(b"text")
        with mock.patch.object(self.af.sys, "argv", ["app-file.py", "add", path]), self.assertRaises(SystemExit) as stop:
            self.af.main()
        self.assertIn("not an Android app", str(stop.exception))
        self.assertIsNone(self.af.current())

    def test_versions_are_plain(self):
        self.assertEqual("v1.2.0", self.af.clean_version("v1.2.0"))
        self.assertEqual("v1.2.0scriptx", self.af.clean_version('v1.2.0"<script>x'))
        self.assertRegex(self.af.clean_version(""), r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
