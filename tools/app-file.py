#!/usr/bin/env python3
"""fieldmap-server: the Fieldmap Android app, kept on the box.

Devices on the box's network download the app from the Devices page (phones/, at /phones/),
and install it from there: no app store, no account. This tool puts the app file in place:
the newest published release, or a file of your own. tools/remote-server.py serves it.

  app-file.py fetch                 the newest release published on GitHub, when it is newer
  app-file.py add <file.apk> [version]   a file of your own instead
  app-file.py status                which app the box offers
  app-file.py remove                offer none

It keeps data/app/fieldmap.apk and data/app/app.json (version, SHA-256, size, where from).
A file added by hand stays until it is removed: "fetch" leaves it alone.

Settings in the environment (scripts/app.sh reads them from .env):
  APP_REPO   <owner>/<repo> on GitHub whose latest release carries the app (.apk)
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.environ.get("APP_DIR") or os.path.join(ROOT, "data", "app")
APK = os.path.join(APP, "fieldmap.apk")
INFO = os.path.join(APP, "app.json")
REPO = os.environ.get("APP_REPO") or "Sentinel6Systems/fieldmap-android"
MAX_BYTES = 300 << 20           # an app file is a few tens of MB
TIMEOUT = 60


def current():
    """What the box offers now: app.json, or None when there is no app file."""
    try:
        with open(INFO) as f:
            info = json.load(f)
        if os.path.getsize(APK) != info.get("size"):
            return None
        return info
    except (OSError, ValueError):
        return None


def clean_version(text):
    text = re.sub(r"[^A-Za-z0-9._+-]", "", str(text or ""))[:32]
    return text or datetime.date.today().isoformat()


def check_apk(path):
    """ValueError in plain words unless the file is an Android app."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except (OSError, zipfile.BadZipFile):
        raise ValueError("That file is not an Android app (.apk).")
    if "AndroidManifest.xml" not in names or not any(n.startswith("classes") and n.endswith(".dex") for n in names):
        raise ValueError("That file is not an Android app (.apk).")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def put(path, version, source):
    """Puts a checked app file in place, all at once."""
    os.makedirs(APP, exist_ok=True)
    info = {"version": clean_version(version), "sha256": sha256(path), "size": os.path.getsize(path), "source": source,
            "added": datetime.date.today().isoformat()}
    tmp = APK + ".tmp"
    shutil.copyfile(path, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, APK)
    with open(INFO + ".tmp", "w") as f:
        json.dump(info, f)
    os.replace(INFO + ".tmp", INFO)
    return info


def get(url, accept="application/vnd.github+json"):
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "fieldmap-server"})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


def fetch():
    """The newest published release, when it differs from what the box has. Returns a line to print."""
    have = current()
    if have and have.get("source") == "by hand":
        return "The app on this box was added by hand (%s); left as it is. To take the published one: ./scripts/app.sh remove, then fetch." % have["version"]
    try:
        with get("https://api.github.com/repos/%s/releases/latest" % REPO) as r:
            release = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "No release of the Fieldmap app is published yet (%s). The Devices page says so; ./scripts/app.sh add <file.apk> puts one in by hand." % REPO
        raise ValueError("GitHub answered %s." % e.code)
    except (OSError, ValueError) as e:
        raise ValueError("Could not ask GitHub for the newest app: %s" % e)
    apks = [a for a in release.get("assets", []) if str(a.get("name", "")).endswith(".apk") and "debug" not in a.get("name", "")]
    if not apks:
        return "The newest release (%s) has no app file in it." % release.get("tag_name", "?")
    asset = apks[0]
    want = str(asset.get("digest") or "")
    want = want[7:] if want.startswith("sha256:") else ""
    version = release.get("tag_name") or asset["name"]
    if have and want and have.get("sha256") == want:
        return "The app on this box is the newest: %s." % have["version"]
    if not 0 < int(asset.get("size") or 0) <= MAX_BYTES:
        raise ValueError("The app file in the release is not a size an app can be.")
    os.makedirs(APP, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=APP, suffix=".download", delete=False) as tmp:
        try:
            with get(asset["browser_download_url"], "application/octet-stream") as r:
                got = 0
                for block in iter(lambda: r.read(1 << 20), b""):
                    got += len(block)
                    if got > MAX_BYTES:
                        raise ValueError("The app file is larger than it said.")
                    tmp.write(block)
            tmp.close()
            if want and sha256(tmp.name) != want:
                raise ValueError("The downloaded app file is not the one the release lists (its SHA-256 differs). Nothing was changed.")
            check_apk(tmp.name)
            info = put(tmp.name, version, "github")
        except (OSError, urllib.error.URLError) as e:
            raise ValueError("Could not download the app: %s" % e)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    return "The app on this box is now %s (%d MB)." % (info["version"], round(info["size"] / 1e6))


def main():
    args = sys.argv[1:]
    try:
        if args[:1] == ["fetch"]:
            print(fetch())
        elif args[:1] == ["add"] and len(args) in (2, 3):
            check_apk(args[1])
            version = args[2] if len(args) == 3 else re.sub(r"\.apk$", "", os.path.basename(args[1]))
            info = put(args[1], version, "by hand")
            print("The app on this box is now %s (%d MB, added by hand)." % (info["version"], round(info["size"] / 1e6)))
        elif args == ["remove"]:
            for path in (APK, INFO):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            print("The box offers no app now.")
        elif args == ["status"]:
            info = current()
            print("App:            " + ("%s, %d MB, %s, SHA-256 %s" % (info["version"], round(info["size"] / 1e6),
                                                                      "added by hand" if info["source"] == "by hand" else "published release",
                                                                      info["sha256"]) if info else "none (./scripts/app.sh fetch, or add <file.apk>)"))
        else:
            sys.exit(__doc__.split("\n\n")[2])
    except ValueError as e:
        sys.exit("Error: %s" % e)


if __name__ == "__main__":
    main()
