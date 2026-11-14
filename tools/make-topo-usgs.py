#!/usr/bin/env python3
"""Generate styles/topo-usgs.json: a paper style in the manner of a USGS
topographic quadrangle (white paper, green woodland, brown contours, red and
black cased roads, railway ties, blue water with italic names, banded
protected-land and military boundaries, monospace numbers).

It reads the same three tile sets as topo-print (basemap, contours, hillshade),
so the two can be swapped freely. Run from the repository root:

    python3 tools/make-topo-usgs.py

Sizes are given in millimetres on paper at the reference print zoom (13.25,
which is 1:24,000 at mid latitudes; the print tool draws 100 CSS px per inch)
and scale gently with zoom, so smaller scales get proportionally finer lines.
Every layer carries metadata "fieldmap:group", for switching groups on and off.
"""
import json

OMT, TER = "openmaptiles", "terrain"
PX_PER_MM = 100 / 25.4

# Fonts
REG, BOLD, ITAL = ["Roboto Condensed Regular"], ["Roboto Condensed Bold"], ["Roboto Condensed Italic"]
MONO, MONOB = ["Atkinson Hyperlegible Mono Regular"], ["Atkinson Hyperlegible Mono Bold"]

# Colours
C = dict(
    paper="#ffffff", wood="#86b04a", ice="#e6f1fa", contour="#b5803f", contour_text="#8a5a26",
    water="#bcd6ee", shore="#2a6db8", water_text="#1b4a86", red="#cf2a33", red2="#9c1f2b",
    black="#000000", casing="#ffffff", grey="#8c8c8c", closed="#9a9a96", park="#6d3fa6",
    military="#b0307a", admin="#3f3a4d", built="#ead9d4", industrial="#ddd3ea", commercial="#f3dfcb",
    civic="#ece1bd", peak="#6b4a2b",
)

layers = []


def add(id, type, group, source=None, source_layer=None, filter=None, minzoom=None, maxzoom=None, layout=None, paint=None):
    l = {"id": id, "type": type, "metadata": {"fieldmap:group": group}}
    if source: l["source"] = source
    if source_layer: l["source-layer"] = source_layer
    if minzoom is not None: l["minzoom"] = minzoom
    if maxzoom is not None: l["maxzoom"] = maxzoom
    if filter is not None: l["filter"] = filter
    if layout: l["layout"] = layout
    if paint: l["paint"] = paint
    layers.append(l)


def w(mm, ramp=False):
    """Line width: `mm` on paper at z13.25, finer when zoomed out, wider when zoomed in."""
    px = mm * PX_PER_MM
    def out(f):
        v = round(px * f, 3)
        return ["case", ["==", ["get", "ramp"], 1], round(v * 0.7, 3), v] if ramp else v
    return ["interpolate", ["exponential", 1.4], ["zoom"], 9, out(0.45), 11.25, out(0.65), 13.25, out(1.0), 15, out(1.7), 18, out(4.5)]


def size(px, lo=0.9, hi=1.15):
    return ["interpolate", ["linear"], ["zoom"], 11, round(px * lo, 2), 13.25, px, 15, round(px * hi, 2)]


def cls(*names):
    return ["match", ["get", "class"], list(names), True, False]


def halo(color="#ffffff", width=1.4):
    return {"text-halo-color": color, "text-halo-width": width}


# Labels are English only: the English name, else the Latin-script name. Names that exist
# only in another script are not shown.
NAME = ["coalesce", ["get", "name:en"], ["get", "name:latin"], ""]
LINE = ["==", ["geometry-type"], "LineString"]
POLY = ["==", ["geometry-type"], "Polygon"]
POINT = ["==", ["geometry-type"], "Point"]
BRUNNEL = ["coalesce", ["get", "brunnel"], ""]
SEASONAL = ["==", ["get", "intermittent"], 1]
PERENNIAL = ["!=", ["get", "intermittent"], 1]

# ------------------------------------------------------------------ ground

add("paper", "background", "base", paint={"background-color": C["paper"]})
add("hillshade", "raster", "hillshade", source="hillshade", minzoom=5, maxzoom=18,
    paint={"raster-opacity": 0.55, "raster-brightness-min": 0.15, "raster-brightness-max": 0.85,
           "raster-saturation": 0.0, "raster-contrast": 0.5, "raster-resampling": "linear"})

add("built-up", "fill", "landuse", OMT, "landuse", ["match", ["get", "class"], ["residential", "suburb", "neighbourhood"], True, False],
    maxzoom=13, paint={"fill-color": C["built"], "fill-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0.8, 13, 0.0]})
add("landuse-industrial", "fill", "landuse", OMT, "landuse", cls("industrial", "garages", "railway"),
    paint={"fill-color": C["industrial"], "fill-opacity": 0.55})
add("landuse-commercial", "fill", "landuse", OMT, "landuse", cls("commercial", "retail"),
    paint={"fill-color": C["commercial"], "fill-opacity": 0.55})
add("landuse-civic", "fill", "landuse", OMT, "landuse", cls("school", "college", "university", "kindergarten", "hospital"),
    paint={"fill-color": C["civic"], "fill-opacity": 0.55})
add("landuse-sports", "fill", "landuse", OMT, "landuse", cls("stadium", "pitch", "playground", "track"),
    paint={"fill-color": "#d6e8c2", "fill-opacity": 0.6})

add("wood", "fill", "woodland", OMT, "landcover", cls("wood"),
    paint={"fill-color": C["wood"], "fill-opacity": 0.38, "fill-antialias": False})
add("ice", "fill", "ground", OMT, "landcover", cls("ice"),
    paint={"fill-color": C["ice"], "fill-opacity": 0.8, "fill-outline-color": C["shore"]})
add("sand", "fill", "ground", OMT, "landcover", cls("sand"), paint={"fill-color": "#f6ecc9", "fill-opacity": 0.6})
add("sand-pattern", "fill", "ground", OMT, "landcover", cls("sand"), minzoom=11, paint={"fill-pattern": "pat_sand"})
add("rock-pattern", "fill", "ground", OMT, "landcover", cls("rock"), minzoom=11, paint={"fill-pattern": "pat_rock"})
add("wetland", "fill", "ground", OMT, "landcover", cls("wetland"), paint={"fill-color": "#d4e6f4", "fill-opacity": 0.35})
add("wetland-pattern", "fill", "ground", OMT, "landcover", cls("wetland"), minzoom=11, paint={"fill-pattern": "pat_marsh"})

add("cemetery", "fill", "landuse", OMT, "landuse", cls("cemetery"), minzoom=12, paint={"fill-pattern": "pat_cemetery"})
add("cemetery-edge", "line", "landuse", OMT, "landuse", cls("cemetery"), minzoom=12,
    paint={"line-color": C["black"], "line-width": w(0.12)})
add("quarry", "fill", "landuse", OMT, "landuse", cls("quarry"), paint={"fill-color": "#d8d0c6", "fill-opacity": 0.6})
add("quarry-edge", "line", "landuse", OMT, "landuse", cls("quarry"),
    paint={"line-color": "#6b5a4a", "line-width": w(0.3), "line-dasharray": [3, 1.5]})
add("dam-area", "fill", "landuse", OMT, "landuse", cls("dam"), paint={"fill-color": "#3c3c3c", "fill-opacity": 0.8})

# Banded boundaries: a wide translucent band just inside the edge, and the edge itself.
def banded(prefix, group, layer, flt, color, dash):
    add(f"{prefix}-band", "line", group, OMT, layer, flt, minzoom=8, layout={"line-join": "round"},
        paint={"line-color": color, "line-opacity": 0.17, "line-width": w(2.2), "line-offset": w(1.1)})
    edge = {"line-color": color, "line-width": w(0.22)}
    if dash: edge["line-dasharray"] = dash
    add(f"{prefix}-edge", "line", group, OMT, layer, flt, minzoom=7, layout={"line-join": "round"}, paint=edge)

banded("park", "parks", "park", POLY, C["park"], [4, 2])
banded("military", "landuse", "landuse", cls("military"), C["military"], None)

# ------------------------------------------------------------------ contours

add("contour", "line", "contours", TER, "contours_minor", minzoom=12,
    paint={"line-color": C["contour"], "line-opacity": 0.85, "line-width": w(0.11)})
add("contour-index", "line", "contours", TER, "contours_major", minzoom=9,
    paint={"line-color": C["contour"], "line-opacity": 0.9, "line-width": w(0.26)})

# ------------------------------------------------------------------ water

add("water", "fill", "water", OMT, "water", PERENNIAL, paint={"fill-color": C["water"]})
add("water-seasonal", "fill", "water", OMT, "water", SEASONAL, paint={"fill-color": C["water"], "fill-opacity": 0.45})
add("water-shore", "line", "water", OMT, "water", PERENNIAL, minzoom=9,
    paint={"line-color": C["shore"], "line-width": w(0.14)})
add("water-shore-seasonal", "line", "water", OMT, "water", SEASONAL, minzoom=9,
    paint={"line-color": C["shore"], "line-width": w(0.14), "line-dasharray": [4, 2]})

STREAM_W = ["match", ["get", "class"], "river", 0.9, "canal", 0.6, 1]
for name, flt, mm, dash in (
    ("river", ["all", cls("river", "canal"), PERENNIAL, ["!=", BRUNNEL, "tunnel"]], 0.34, None),
    ("stream", ["all", ["!", cls("river", "canal")], PERENNIAL, ["!=", BRUNNEL, "tunnel"]], 0.15, None),
    ("river-seasonal", ["all", cls("river", "canal"), SEASONAL], 0.3, [8, 3, 1, 3]),
    ("stream-seasonal", ["all", ["!", cls("river", "canal")], SEASONAL], 0.17, [8, 3, 1, 3]),
    ("waterway-tunnel", ["==", BRUNNEL, "tunnel"], 0.15, [2, 2]),
):
    p = {"line-color": C["shore"], "line-width": w(mm)}
    if dash: p["line-dasharray"] = dash
    add(f"waterway-{name}" if not name.startswith("waterway") else name, "line",
        "seasonal-water" if "seasonal" in name else "water", OMT, "waterway", flt,
        layout={"line-cap": "butt" if dash else "round", "line-join": "round"}, paint=p)

# ------------------------------------------------------------------ aeroway, areas under roads

add("aeroway-area", "fill", "airfields", OMT, "aeroway", ["all", POLY, cls("runway", "taxiway", "apron")], minzoom=11,
    paint={"fill-color": ["match", ["get", "class"], "apron", "#dcdcdc", C["grey"]], "fill-outline-color": "#3c3c3c"})
add("aeroway-line", "line", "airfields", OMT, "aeroway", ["all", LINE, cls("runway", "taxiway")], minzoom=10,
    paint={"line-color": C["grey"], "line-width": ["interpolate", ["exponential", 1.5], ["zoom"], 10,
           ["match", ["get", "class"], "runway", 1.5, 0.5], 13.25, ["match", ["get", "class"], "runway", 5, 1.6], 16,
           ["match", ["get", "class"], "runway", 30, 8]]})
add("pier-area", "fill", "roads", OMT, "transportation", ["all", POLY, cls("pier")], paint={"fill-color": "#cfcfcf", "fill-outline-color": "#3c3c3c"})
add("pier-line", "line", "roads", OMT, "transportation", ["all", LINE, cls("pier")], paint={"line-color": "#3c3c3c", "line-width": w(0.3)})
add("road-area", "fill", "roads", OMT, "transportation", ["all", POLY, ["!", cls("pier")]], minzoom=13,
    paint={"fill-color": "#ececec", "fill-outline-color": "#9a9a9a"})

# ------------------------------------------------------------------ buildings

add("building", "fill", "buildings", OMT, "building", minzoom=13,
    paint={"fill-color": "#1a1a1a", "fill-outline-color": "#000000", "fill-opacity": 0.9})

# ------------------------------------------------------------------ roads and rail

# class group, fill colour, fill mm, extra mm for the black outline (0 = none), extra mm for the white casing, minzoom
ROADS = [
    ("track",     ["track"],                None,       0.20, 0,    0.30, 11),
    ("service",   ["service"],              C["black"], 0.16, 0,    0.26, 13),
    ("minor",     ["minor"],                C["black"], 0.27, 0,    0.32, 11),
    ("tertiary",  ["tertiary"],             C["black"], 0.36, 0,    0.36, 10),
    ("secondary", ["secondary"],            C["red2"],  0.44, 0.16, 0.44, 8),
    ("primary",   ["primary"],              C["red"],   0.54, 0.16, 0.46, 6),
    ("motorway",  ["motorway", "trunk"],    C["red"],   0.68, 0.18, 0.50, 4),
]
ACCESS = ["case", ["==", ["get", "access"], "no"], C["closed"], C["black"]]
UNPAVED = ["==", ["get", "surface"], "unpaved"]

for level, lflt, alpha in (
    ("tunnel", ["==", BRUNNEL, "tunnel"], 0.4),
    ("ground", ["match", BRUNNEL, ["tunnel", "bridge"], False, True], 1),
    ("bridge", ["==", BRUNNEL, "bridge"], 1),
):
    def road(suffix, name, classes, minzoom, paint, extra=None, cap="butt"):
        flt = ["all", LINE, lflt, cls(*classes)] + ([extra] if extra else [])
        add(f"{level}-{name}{suffix}", "line", "trails" if name in ("track", "path") else "roads", OMT, "transportation", flt,
            minzoom=minzoom, layout={"line-cap": cap, "line-join": "round"}, paint=paint)

    if level == "bridge":   # black edges either side of the deck
        road("-deck", "path", ["path"], 12, {"line-color": C["black"], "line-width": w(0.16 + 0.55)})
        for name, classes, col, mm, ol, cs, mz in ROADS:
            road("-deck", name, classes, max(mz, 11), {"line-color": C["black"], "line-width": w(mm + cs + 0.3, True)})
    for name, classes, col, mm, ol, cs, mz in ROADS:
        p = {"line-color": C["casing"], "line-width": w(mm + cs, True)}
        if level == "tunnel": p["line-dasharray"] = [3, 1.5]
        road("-casing", name, classes, mz, p)
    if level == "bridge":
        road("-casing", "path", ["path"], 12, {"line-color": C["casing"], "line-width": w(0.16 + 0.3)})
    for name, classes, col, mm, ol, cs, mz in ROADS:
        if ol:
            road("-outline", name, classes, mz, {"line-color": C["black"], "line-opacity": 0.7 * alpha, "line-width": w(mm + ol, True)})
    road("", "path", ["path"], 11, {"line-color": ACCESS, "line-opacity": alpha, "line-width": w(0.15), "line-dasharray": [3, 2]})
    for name, classes, col, mm, ol, cs, mz in ROADS:
        if name == "track":
            road("", name, classes, mz, {"line-color": ACCESS, "line-opacity": alpha, "line-width": w(mm), "line-dasharray": [5, 2.5]})
            continue
        road("", name, classes, mz, {"line-color": col, "line-opacity": alpha, "line-width": w(mm, True)}, cap="round" if level == "ground" else "butt")
        if name in ("service", "minor", "tertiary"):   # unpaved: broken white centre
            road("-unpaved", name, classes, 12, {"line-color": C["casing"], "line-width": w(mm * 0.5), "line-dasharray": [3, 3]}, extra=UNPAVED)

    # Railways: main lines with cross-ties, sidings and yards thin, transit lighter.
    def rail(suffix, flt, paint, minzoom=None):
        add(f"{level}-rail{suffix}", "line", "railways", OMT, "transportation", ["all", LINE, lflt] + flt, minzoom=minzoom,
            layout={"line-join": "round"}, paint=paint)
    MAIN = [cls("rail"), ["!", ["has", "service"]]]
    rail("-yard", [cls("rail"), ["has", "service"]], {"line-color": C["black"], "line-opacity": 0.75 * alpha, "line-width": w(0.13)}, 12)
    rail("-transit", [cls("transit")], {"line-color": "#444444", "line-opacity": alpha, "line-width": w(0.2), "line-dasharray": [4, 1]}, 11)
    rail("-casing", MAIN, {"line-color": C["casing"], "line-width": w(0.55)})
    rail("", MAIN, {"line-color": C["black"], "line-opacity": alpha, "line-width": w(0.26)})
    rail("-ties", MAIN, {"line-color": C["black"], "line-opacity": alpha, "line-width": w(1.0), "line-dasharray": [0.13, 1.3]}, 11)

add("ferry", "line", "roads", OMT, "transportation", ["all", LINE, cls("ferry")],
    paint={"line-color": C["shore"], "line-width": w(0.18), "line-dasharray": [6, 3]})
add("aerialway", "line", "roads", OMT, "transportation", ["all", LINE, cls("aerialway")], minzoom=12,
    paint={"line-color": C["black"], "line-width": w(0.13)})
add("aerialway-ticks", "line", "roads", OMT, "transportation", ["all", LINE, cls("aerialway")], minzoom=12,
    paint={"line-color": C["black"], "line-width": w(0.8), "line-dasharray": [0.15, 2.5]})

# ------------------------------------------------------------------ boundaries

LAND = ["!=", ["get", "maritime"], 1]
def admin(name, level, mm, dash, ribbon=None, minzoom=0):
    flt = ["all", ["==", ["get", "admin_level"], level], LAND]
    if ribbon:
        add(f"admin-{name}-ribbon", "line", "boundaries", OMT, "boundary", flt, minzoom=minzoom,
            paint={"line-color": C["admin"], "line-opacity": 0.16, "line-width": w(ribbon)})
    add(f"admin-{name}", "line", "boundaries", OMT, "boundary", flt, minzoom=minzoom, layout={"line-join": "round"},
        paint={"line-color": C["admin"], "line-width": w(mm), "line-dasharray": dash})
admin("county", 6, 0.3, [7, 2.5], ribbon=1.4, minzoom=9)
admin("state", 4, 0.4, [6, 2, 1, 2], ribbon=1.9, minzoom=3)
admin("country", 2, 0.55, [7, 2, 1, 2, 1, 2], ribbon=2.2)
add("admin-local", "line", "boundaries", OMT, "boundary", ["all", [">=", ["get", "admin_level"], 7], ["<=", ["get", "admin_level"], 8], LAND],
    minzoom=12, paint={"line-color": C["admin"], "line-width": w(0.14), "line-dasharray": [1, 3]})
add("admin-maritime", "line", "boundaries", OMT, "boundary", ["all", ["<=", ["get", "admin_level"], 4], ["==", ["get", "maritime"], 1]],
    paint={"line-color": C["shore"], "line-opacity": 0.5, "line-width": w(0.2), "line-dasharray": [6, 3]})

# ------------------------------------------------------------------ terrain features

add("cliff", "line", "peaks", OMT, "mountain_peak", ["all", LINE, cls("cliff", "arete")], minzoom=12,
    paint={"line-color": C["peak"], "line-width": w(0.2)})

# ------------------------------------------------------------------ symbols and text

def sym(id, group, layer, flt, layout, paint, minzoom=None, maxzoom=None, source=OMT):
    add(id, "symbol", group, source, layer, flt, minzoom, maxzoom, layout, paint)

# Flow arrows: rivers and canals from 1:50k, named streams from 1:24k, every stream only at 1:12k.
sym("flow-arrow", "flow-arrows", "waterway",
    ["all", LINE, PERENNIAL, ["!=", BRUNNEL, "tunnel"],
     ["any", cls("river", "canal"), ["all", ["has", "name"], [">=", ["zoom"], 13]], [">=", ["zoom"], 14]]],
    {"symbol-placement": "line", "symbol-spacing": ["interpolate", ["linear"], ["zoom"], 12, 260, 15, 420],
     "icon-image": "sym_flow", "icon-size": ["match", ["get", "class"], "river", 1, 0.75],
     "icon-rotation-alignment": "map", "icon-padding": 12}, {}, minzoom=12)

sym("contour-label", "contours", "contours_major", None,
    {"symbol-placement": "line", "symbol-spacing": 420,
     "text-field": ["to-string", ["coalesce", ["get", "elev_ft"], ["get", "elev_m"]]],
     "text-font": MONO, "text-size": size(8.5), "text-keep-upright": True, "text-rotation-alignment": "map", "text-padding": 4},
    {"text-color": C["contour_text"], **halo(width=1.6)}, minzoom=12, source=TER)

sym("waterway-name", "water-names", "waterway", ["all", LINE, ["has", "name"], ["!", cls("river")]],
    {"symbol-placement": "line", "symbol-spacing": 380, "text-field": NAME, "text-font": ITAL, "text-size": size(9),
     "text-letter-spacing": 0.08, "text-offset": [0, -0.7], "text-rotation-alignment": "map"},
    {"text-color": C["water_text"], **halo()}, minzoom=13)
sym("river-name", "water-names", "waterway", ["all", LINE, ["has", "name"], cls("river")],
    {"symbol-placement": "line", "symbol-spacing": 420, "text-field": NAME, "text-font": ITAL, "text-size": size(10.5),
     "text-letter-spacing": 0.12, "text-offset": [0, -0.8], "text-rotation-alignment": "map"},
    {"text-color": C["water_text"], **halo()}, minzoom=10.5)
sym("lake-name-line", "water-names", "water_name", LINE,
    {"symbol-placement": "line", "symbol-spacing": 500, "text-field": NAME, "text-font": ITAL, "text-size": size(11),
     "text-letter-spacing": 0.15, "text-rotation-alignment": "map"},
    {"text-color": C["water_text"], **halo()}, minzoom=9)
sym("water-name", "water-names", "water_name", POINT,
    {"text-field": NAME, "text-font": ITAL, "text-max-width": 6, "text-letter-spacing": 0.1,
     "text-size": ["match", ["get", "class"], ["ocean", "sea"], 14, 10.5]},
    {"text-color": C["water_text"], **halo()})

sym("oneway", "roads", "transportation", ["all", LINE, ["==", ["get", "oneway"], 1], cls("motorway", "trunk", "primary", "secondary", "tertiary", "minor")],
    {"symbol-placement": "line", "symbol-spacing": 220, "icon-image": "oneway", "icon-size": 0.6, "icon-rotation-alignment": "map",
     "icon-padding": 2}, {"icon-opacity": 0.6}, minzoom=15)

ROAD_TEXT = {"symbol-placement": "line", "text-field": NAME, "text-rotation-alignment": "map", "text-letter-spacing": 0.03}
sym("trail-name", "trail-names", "transportation_name", cls("path", "track"),
    {**ROAD_TEXT, "text-font": ITAL, "text-size": size(8.5), "symbol-spacing": 300, "text-offset": [0, -0.75]},
    {"text-color": C["black"], **halo()}, minzoom=13)
sym("road-name-minor", "road-names", "transportation_name", cls("minor", "service"),
    {**ROAD_TEXT, "text-font": REG, "text-size": size(8.5), "symbol-spacing": 280, "text-offset": [0, -0.8]},
    {"text-color": C["black"], **halo()}, minzoom=13)
sym("road-name-major", "road-names", "transportation_name", cls("motorway", "trunk", "primary", "secondary", "tertiary"),
    {**ROAD_TEXT, "text-font": REG, "text-size": size(9.5), "symbol-spacing": 340, "text-offset": [0, -0.9]},
    {"text-color": C["black"], **halo()}, minzoom=11)

# Route shields: the three shield families in the topo sprite, numbers in the monospace face.
def shield(id, flt, image, minzoom, color):
    sym(id, "road-names", "transportation_name", ["all", LINE, ["has", "ref"], ["<=", ["get", "ref_length"], 6], flt],
        {"symbol-placement": "line", "symbol-spacing": 420, "icon-image": image, "icon-rotation-alignment": "viewport",
         "icon-size": 0.95, "text-field": ["get", "ref"], "text-font": MONOB, "text-size": 8, "text-rotation-alignment": "viewport",
         "text-letter-spacing": -0.03, "icon-padding": 6}, {"text-color": color}, minzoom=minzoom)
shield("shield-interstate", ["==", ["get", "network"], "us-interstate"], ["concat", "us-interstate_", ["to-string", ["get", "ref_length"]]], 7, "#000000")   # the sprite shield is white
shield("shield-us", ["match", ["get", "network"], ["us-highway", "us-state"], True, False],
       ["concat", ["get", "network"], "_", ["to-string", ["get", "ref_length"]]], 9, "#000000")
shield("shield-other", ["match", ["coalesce", ["get", "network"], ""], ["us-interstate", "us-highway", "us-state"], False, True],
       ["concat", "road_", ["to-string", ["get", "ref_length"]]], 9, "#000000")

sym("ridge-name", "terrain-names", "mountain_peak", ["all", LINE, ["has", "name"]],
    {"symbol-placement": "line", "symbol-spacing": 450, "text-field": NAME, "text-font": ITAL, "text-size": size(9),
     "text-letter-spacing": 0.2}, {"text-color": C["peak"], **halo()}, minzoom=12)

HEIGHT = ["case", ["has", "ele_ft"], ["to-string", ["get", "ele_ft"]], ""]
sym("spot-height", "peaks", "mountain_peak", ["all", POINT, ["!", ["has", "name"]], ["has", "ele_ft"]],
    {"icon-image": "sym_spot", "text-field": HEIGHT, "text-font": MONO, "text-size": size(8.5), "text-optional": True,
     "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 0.5, "text-justify": "auto",
     "symbol-sort-key": ["get", "rank"]}, {"text-color": "#4a2f17", **halo(width=1.6)}, minzoom=12)
sym("peak", "peaks", "mountain_peak", ["all", POINT, ["has", "name"]],
    {"icon-image": "sym_peak", "text-optional": True, "text-max-width": 7,
     "text-field": ["format", NAME, {}, ["case", ["has", "ele_ft"], "\n", ""], {}, HEIGHT, {"text-font": ["literal", MONO], "font-scale": 0.92}],
     "text-font": ITAL, "text-size": size(9.5), "text-variable-anchor": ["top", "bottom", "left", "right"],
     "text-radial-offset": 0.6, "text-justify": "auto", "symbol-sort-key": ["get", "rank"]},
    {"text-color": C["peak"], **halo(width=1.6)}, minzoom=10)

EMERGENCY = ["hospital", "fire_station", "police", "pharmacy", "shelter", "drinking_water", "fuel", "ranger_station"]
POI_LAYOUT = {"icon-image": ["concat", ["get", "class"], "_11"], "icon-size": 0.8, "text-field": NAME, "text-anchor": "top",
              "text-offset": [0, 0.7], "text-max-width": 8, "text-optional": True, "text-padding": 2}
sym("poi-minor", "pois", "poi", ["all", POINT, [">=", ["get", "rank"], 13], ["match", ["get", "class"], EMERGENCY, False, True]],
    {**POI_LAYOUT, "text-font": REG, "text-size": 8}, {"text-color": "#222222", **halo()}, minzoom=14.5)
sym("poi", "pois", "poi", ["all", POINT, ["<=", ["get", "rank"], 12], ["has", "name"], ["match", ["get", "class"], EMERGENCY, False, True]],
    {**POI_LAYOUT, "text-font": REG, "text-size": 8.5}, {"text-color": "#222222", **halo()}, minzoom=13)
sym("poi-emergency", "pois", "poi", ["all", POINT, ["match", ["get", "class"], EMERGENCY, True, False]],
    {**POI_LAYOUT, "icon-size": 0.95, "text-font": BOLD, "text-size": 8.5,
     "symbol-sort-key": ["match", ["get", "class"], "hospital", 0, "fire_station", 1, "police", 2, "shelter", 3, "drinking_water", 4, "pharmacy", 5, 6]},
    {"text-color": "#8b0000", **halo(width=1.6)}, minzoom=12)

AIRPORT = {"text-anchor": "top", "text-offset": [0, 0.7], "text-max-width": 9, "text-optional": True}
sym("airfield", "airfields", "aerodrome_label", ["!", ["has", "iata"]],
    {**AIRPORT, "icon-image": "airfield_11", "text-size": size(8.5), "text-font": REG,
     "text-field": ["format", NAME, {}, ["case", ["has", "icao"], "\n", ""], {}, ["coalesce", ["get", "icao"], ""], {"text-font": ["literal", MONO]}]},
    {"text-color": C["black"], **halo()}, minzoom=11)
sym("airport", "airfields", "aerodrome_label", ["has", "iata"],
    {**AIRPORT, "icon-image": "airport_11", "text-size": size(10.5), "text-font": BOLD,
     "text-field": ["format", NAME, {}, "\n", {}, ["get", "iata"], {"text-font": ["literal", MONOB]}]},
    {"text-color": C["black"], **halo(width=1.8)}, minzoom=9)

sym("park-name", "parks", "park", ["all", POINT, ["has", "name"]],
    {"text-field": NAME, "text-font": ITAL, "text-size": size(10), "text-max-width": 8, "text-letter-spacing": 0.05,
     "symbol-sort-key": ["get", "rank"]}, {"text-color": "#4f2a80", **halo(width=1.6)}, minzoom=9)

# Places. Lower-ranked places appear as you zoom in; bigger places are placed first.
def place(id, flt, font, px, minzoom, maxzoom=None, color="#000000", upper=False, spacing=0.02, extra=None):
    lay = {"text-field": NAME, "text-font": font, "text-size": px, "text-max-width": 7, "text-letter-spacing": spacing,
           "symbol-sort-key": ["coalesce", ["get", "rank"], 20]}
    if upper: lay["text-transform"] = "uppercase"
    if extra: lay.update(extra)
    sym(id, "place-names", "place", flt, lay, {"text-color": color, **halo(width=1.8)}, minzoom=minzoom, maxzoom=maxzoom)

place("place-locality", cls("locality", "isolated_dwelling"), ITAL, 8.5, 12.5, color="#333333")
place("place-island", cls("island", "islet"), ITAL, 9.5, 10, color="#333333")
place("place-neighbourhood", cls("neighbourhood", "quarter"), REG, 9, 13, color="#555555", upper=True, spacing=0.1)
place("place-suburb", cls("suburb"), REG, 10, 11.5, color="#444444", upper=True, spacing=0.1)
place("place-hamlet", cls("hamlet"), REG, 9.5, 12)
place("place-village", cls("village"), REG, size(11), 10)
place("place-town", cls("town"), BOLD, ["interpolate", ["linear"], ["zoom"], 7, 9.5, 13.25, 13], 7, upper=True)
place("place-city", ["all", cls("city"), ["<=", ["coalesce", ["get", "rank"], 10], ["step", ["zoom"], 2, 4, 4, 5, 6, 6, 20]]],
      BOLD, ["interpolate", ["linear"], ["zoom"], 3, 9.5, 8, 12, 13.25, 16], 2, upper=True, spacing=0.05)
place("place-state", cls("state", "province"), ITAL, ["interpolate", ["linear"], ["zoom"], 3, 9, 7, 13], 3, maxzoom=9,
      color=C["admin"], upper=True, spacing=0.2)
place("place-country", cls("country"), BOLD, ["interpolate", ["linear"], ["zoom"], 1, 9, 6, 15], 0, maxzoom=8,
      color=C["admin"], upper=True, spacing=0.15)
place("place-continent", cls("continent"), BOLD, 12, 0, maxzoom=2, color=C["admin"], upper=True, spacing=0.3)

style = {
    "version": 8,
    "name": "Topo USGS",
    "metadata": {
        "fieldmap:generator": "tools/make-topo-usgs.py",
        "fieldmap:reference-zoom": 13.25,
        "fieldmap:px-per-mm": round(PX_PER_MM, 4),
        "fieldmap:attribution": "© OpenMapTiles © OpenStreetMap contributors",
    },
    "center": [0.0, 0.0],
    "zoom": 2,
    "bearing": 0,
    "pitch": 0,
    "sources": {
        "openmaptiles": {"type": "vector", "url": "mbtiles://basemap"},
        "terrain": {"type": "vector", "url": "mbtiles://contours"},
        "hillshade": {"type": "raster", "url": "mbtiles://hillshade"},
    },
    "sprite": "/sprites/usgs",
    "glyphs": "{fontstack}/{range}.pbf",
    "layers": layers,
}
ids = [l["id"] for l in layers]
assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
with open("styles/topo-usgs.json", "w") as f:
    f.write(json.dumps(style, indent=2) + "\n")
groups = sorted({l["metadata"]["fieldmap:group"] for l in layers})
print(f"styles/topo-usgs.json: {len(layers)} layers in {len(groups)} groups: {', '.join(groups)}")
