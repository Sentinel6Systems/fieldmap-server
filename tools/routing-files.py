#!/usr/bin/env python3
"""fieldmap-server: the routing data for the Fieldmap app (BRouter's .rd5 files).

The app plans routes along roads and trails without a connection, with BRouter on the device.
BRouter reads one file per 5 x 5 degree square, named after its south-west corner: W80_N35.rd5
is the square from longitude -80 to -75 and latitude 35 to 40. scripts/build-routing.sh
downloads the squares a region touches into data/routing/, and the web server serves them,
with this tool's index.json, at /routing/.

  routing-files.py squares <min-lon> <min-lat> <max-lon> <max-lat>
        the names of the squares that overlap the box, one a line (a box whose min-lon is
        larger than its max-lon crosses the 180 degree line: both sides)
  routing-files.py index <dir> <min-lon> <min-lat> <max-lon> <max-lat>
        write <dir>/index.json for the squares of the box that are in <dir> (squares with no
        land have no file), and delete the .rd5 files of other squares
  routing-files.py install <from-dir> <to-dir>
        put the files of a staged build in place: the squares first, then the index, then the
        old squares the new index does not list go. The folder itself stays (the web server's
        containers have it mounted), and <from-dir> is removed.

index.json: {"version": 1, "source": <URL>, "made": <UTC>, "files": [{"name", "west", "south",
"east", "north", "size", "sha256"}]}. The app downloads the files an offline area touches.
"""
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sys

SOURCE = "https://brouter.de/brouter/segments4/"
SIZE = 5                        # degrees: the side of a square
NAME = re.compile(r"^([EW])([0-9]{1,3})_([NS])([0-9]{1,2})\.rd5$")
INDEX = "index.json"


def name(west, south):
    """The file name of the square whose south-west corner is (west, south)."""
    return f"{'W' if west < 0 else 'E'}{abs(west)}_{'S' if south < 0 else 'N'}{abs(south)}.rd5"


def corner(file_name):
    """(west, south) of a square's file name; ValueError for any other name."""
    m = NAME.match(file_name)
    if not m:
        raise ValueError(f"not a routing file name: {file_name}")
    west = int(m.group(2)) * (-1 if m.group(1) == "W" else 1)
    south = int(m.group(4)) * (-1 if m.group(3) == "S" else 1)
    if west % SIZE or south % SIZE or not -180 <= west < 180 or not -90 <= south < 90:
        raise ValueError(f"not a routing file name: {file_name}")
    return west, south


def starts(low, high, first, last):
    """The corners (multiples of SIZE, from first to last) of the squares that overlap low..high.
    A square that only touches the edge of the range is left out, unless the range is a line."""
    a = math.floor(low / SIZE) * SIZE
    b = math.ceil(high / SIZE) * SIZE
    if b == a:
        b = a + SIZE
    return [v for v in range(a, b, SIZE) if first <= v <= last]


def squares(min_lon, min_lat, max_lon, max_lat):
    """The names of the squares that overlap the box, south to north and west to east."""
    for v in (min_lon, max_lon):
        if not -180 <= v <= 180:
            raise ValueError("a longitude must be from -180 to 180")
    for v in (min_lat, max_lat):
        if not -90 <= v <= 90:
            raise ValueError("a latitude must be from -90 to 90")
    if min_lat > max_lat:
        raise ValueError("min-lat must not be larger than max-lat")
    spans = [(min_lon, max_lon)] if min_lon <= max_lon else [(min_lon, 180), (-180, max_lon)]
    lons = []
    for w, e in spans:
        lons += [v for v in starts(w, e, -180, 180 - SIZE) if v not in lons]
    return [name(w, s) for s in starts(min_lat, max_lat, -90, 90 - SIZE) for w in lons]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    with open(path + ".tmp", "w") as f:
        json.dump(value, f, indent=1)
        f.write("\n")
    os.replace(path + ".tmp", path)


def index(folder, box, now=None):
    """Writes folder/index.json for the box's squares that are in the folder, and deletes the
    .rd5 files of other squares. Returns the index."""
    wanted = squares(*box)
    files = []
    for n in wanted:
        path = os.path.join(folder, n)
        if not os.path.isfile(path):
            continue
        west, south = corner(n)
        files.append({"name": n, "west": west, "south": south, "east": west + SIZE, "north": south + SIZE,
                      "size": os.path.getsize(path), "sha256": sha256(path)})
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"version": 1, "source": SOURCE, "made": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files}
    write_json(os.path.join(folder, INDEX), out)
    for n in os.listdir(folder):
        if NAME.match(n) and n not in wanted:
            os.remove(os.path.join(folder, n))
    return out


def install(src, dest):
    """Puts a staged build's files (src) in place in dest; see the module's help."""
    with open(os.path.join(src, INDEX)) as f:
        listed = [e["name"] for e in json.load(f)["files"]]
    for n in listed:
        corner(n)
    os.makedirs(dest, exist_ok=True)
    for n in listed:
        os.replace(os.path.join(src, n), os.path.join(dest, n))
    os.replace(os.path.join(src, INDEX), os.path.join(dest, INDEX))
    for n in os.listdir(dest):
        if (NAME.match(n) and n not in listed) or n.endswith(".part"):
            os.remove(os.path.join(dest, n))
    shutil.rmtree(src)
    return listed


def numbers(args):
    try:
        return [float(a) for a in args]
    except ValueError:
        raise ValueError("the box is four numbers: min-lon min-lat max-lon max-lat")


def main():
    args = sys.argv[1:]
    try:
        if args[:1] == ["squares"] and len(args) == 5:
            print("\n".join(squares(*numbers(args[1:]))))
        elif args[:1] == ["index"] and len(args) == 6:
            out = index(args[1], numbers(args[2:]))
            mb = sum(e["size"] for e in out["files"]) / 1e6
            print(f"Routing data: {len(out['files'])} file{'' if len(out['files']) == 1 else 's'}, {mb:.0f} MB "
                  f"({os.path.join(args[1], INDEX)}).")
        elif args[:1] == ["install"] and len(args) == 3:
            listed = install(args[1], args[2])
            print(f"Routing data: {len(listed)} file{'' if len(listed) == 1 else 's'} in place.")
        else:
            sys.exit(__doc__.split("\n\n")[2])
    except (OSError, ValueError, KeyError) as e:
        sys.exit("Error: %s" % e)


if __name__ == "__main__":
    main()
