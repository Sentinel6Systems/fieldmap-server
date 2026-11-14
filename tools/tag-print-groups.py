#!/usr/bin/env python3
"""Tag every layer of styles/topo-print.json with metadata "fieldmap:group", the same
groups tools/make-topo-usgs.py uses, so the print tool's Layers panel works for both
styles. topo-print is edited by hand: run this again after adding or renaming layers.
Run from the repository root:

    python3 tools/tag-print-groups.py

Layers that match no rule are reported and left untagged (they are always drawn).
"""
import json
import re
import sys

PATH = "styles/topo-print.json"

# First match wins: (group, layer id pattern).
RULES = [
    ("base", r"^background$"),
    ("hillshade", r"^hillshade$"),
    ("contours", r"^contours"),
    ("woodland", r"^landcover-wood$"),
    ("ground", r"^landcover-"),
    ("parks", r"^park-"),
    ("landuse", r"^landuse-"),
    ("peaks", r"^mountain-(peak|cliff)$"),
    ("terrain-names", r"^mountain-ridge-label$"),
    ("seasonal-water", r"-intermittent"),
    ("water-names", r"^(waterway-name|water-name)"),
    ("water", r"^(water|waterway)"),
    ("airfields", r"^(aeroway-|airport-label)"),
    ("railways", r"(railway|cablecar)"),
    ("trail-names", r"^highway-name-path$"),
    ("road-names", r"^highway-(shield|name)"),
    ("trails", r"^(tunnel-path|bridge-path|highway-path|highway-track$)"),
    ("roads", r"^(tunnel-|bridge-|highway-|pier-|road_oneway|ferry$)"),
    ("buildings", r"^building$"),
    ("boundaries", r"^boundary-"),
    ("pois", r"^poi-"),
    ("place-names", r"^(state-labels|city-labels|place-)"),
]


def group_for(layer_id):
    for group, pattern in RULES:
        if re.search(pattern, layer_id):
            return group
    return None


style = json.load(open(PATH))
missing = []
for layer in style["layers"]:
    group = group_for(layer["id"])
    if group is None:
        missing.append(layer["id"])
        continue
    layer.setdefault("metadata", {})["fieldmap:group"] = group
with open(PATH, "w") as f:
    f.write(json.dumps(style, indent=2))
print(f"{PATH}: {len(style['layers']) - len(missing)} layers tagged")
if missing:
    print("untagged:", ", ".join(missing))
    sys.exit(1)
