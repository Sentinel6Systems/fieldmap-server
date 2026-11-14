// Layer switches for the print styles. Every layer of topo-print and topo-usgs carries
// metadata["fieldmap:group"]; the panel switches whole groups on and off, and three
// controls change how the map is drawn: ink, hillshade and contours. applyLayers() turns
// a style JSON plus these settings into the style that is drawn on screen and on paper.

const GROUP = "fieldmap:group";

// Panel sections, in order. Groups a style does not have are hidden.
// hillshade and contours have their own three-way controls, base is never switched off.
export const SECTIONS = [
  ["Terrain", [["peaks", "Peaks & spot heights"], ["terrain-names", "Terrain names"]]],
  ["Cover", [["woodland", "Woodland"], ["ground", "Wetland, sand, rock, ice"], ["landuse", "Built-up & land use"], ["parks", "Parks & protected land"], ["public-land", "Public land access (PAD-US)"]]],
  ["Water", [["water", "Water"], ["seasonal-water", "Seasonal water"], ["flow-arrows", "Flow arrows"], ["springs", "Springs & waterfalls"], ["water-names", "Water names"], ["flood", "Flood zones (FEMA)"]]],
  ["Transport", [["roads", "Roads"], ["trails", "Trails & tracks"], ["railways", "Railways"], ["airfields", "Airfields"], ["road-names", "Road names & shields"], ["trail-names", "Trail names"]]],
  ["Structures", [["buildings", "Buildings"], ["power", "Power lines & plants"], ["pipelines", "Pipelines"], ["towers", "Towers & masts"], ["barriers", "Fences, walls & gates"], ["obstacles", "Obstacles (FAA)"], ["pois", "Points of interest"]]],
  ["Places", [["place-names", "Place names"], ["landmarks", "Landmarks"], ["survey-marks", "Survey marks (NGS)"], ["boundaries", "Admin boundaries"]]],
];
export const OWN_CONTROLS = new Set(["base", "hillshade", "contours"]);

export const DEFAULTS = { ink: "colour", hillshade: "sharp", contours: "all", grid: true, off: [] };

/** The set of groups present in a style. */
export function groupsOf(style) {
  return new Set((style.layers || []).map((l) => l.metadata && l.metadata[GROUP]).filter(Boolean));
}

/** A copy of `style` with the layer settings applied. */
export function applyLayers(style, s) {
  const off = new Set(s.off || []);
  if (s.hillshade === "off") off.add("hillshade");
  if (s.contours === "off") off.add("contours");
  const layers = [];
  for (const l0 of style.layers || []) {
    const group = l0.metadata && l0.metadata[GROUP];
    if (group && off.has(group)) continue;
    // Index contours only: drop the minor lines, keep index lines and their labels.
    if (group === "contours" && s.contours === "index" && l0["source-layer"] === "contours_minor") continue;
    let l = l0;
    if (group === "hillshade" && s.hillshade === "soft") l = softHillshade(l);
    if (s.ink === "bw") l = greyLayer(l);
    layers.push(l);
  }
  return { ...style, layers };
}

function softHillshade(l) {
  const p = { ...l.paint };
  p["raster-opacity"] = scale(p["raster-opacity"] ?? 1, 0.5);
  p["raster-contrast"] = scale(p["raster-contrast"] ?? 0, 0.4);
  return { ...l, paint: p };
}
function scale(v, k) { return typeof v === "number" ? Math.round(v * k * 1000) / 1000 : v; }

/* ---------- black and white ink ---------- */
// Every colour becomes a grey of the same lightness. Lines and text are darkened a
// little so red roads and blue water lines stay distinct from paper-coloured fills.
// Raster sprites (patterns, icons) keep their colours; hillshade loses its tint.
function greyLayer(l) {
  if (!l.paint) return l;
  const p = {};
  const darken = l.type === "line" || l.type === "symbol";
  for (const [k, v] of Object.entries(l.paint)) p[k] = k.endsWith("-color") ? greyValue(v, darken && !k.includes("halo")) : v;
  if (l.type === "raster") p["raster-saturation"] = -1;
  return { ...l, paint: p };
}
function greyValue(v, darken) {
  if (typeof v === "string") { const g = toGrey(v, darken); return g ?? v; }
  if (Array.isArray(v)) return v.map((x) => greyValue(x, darken));
  if (v && typeof v === "object" && Array.isArray(v.stops)) return { ...v, stops: v.stops.map(([z, c]) => [z, greyValue(c, darken)]) };
  return v;
}
function toGrey(str, darken) {
  const c = parseColour(str);
  if (!c) return null;
  let y = 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  y = Math.pow(y, 1 / 2.2);                       // back to sRGB lightness
  if (darken) y *= 0.75;
  const g = Math.round(Math.min(1, y) * 255);
  return c.a < 1 ? `rgba(${g},${g},${g},${c.a})` : `rgb(${g},${g},${g})`;
}
function lin(u) { u /= 255; return u <= 0.04045 ? u / 12.92 : Math.pow((u + 0.055) / 1.055, 2.4); }

const NAMED = { black: "#000", white: "#fff", red: "#f00", blue: "#00f", green: "#008000", gray: "#808080", grey: "#808080", transparent: "rgba(0,0,0,0)" };
function parseColour(s) {
  s = s.trim().toLowerCase();
  if (NAMED[s]) s = NAMED[s];
  let m = s.match(/^#([0-9a-f]{3,8})$/);
  if (m) {
    let h = m[1];
    if (h.length === 3 || h.length === 4) h = [...h].map((x) => x + x).join("");
    if (h.length !== 6 && h.length !== 8) return null;
    return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1 };
  }
  m = s.match(/^rgba?\(([^)]+)\)$/);
  if (m) {
    const [r, g, b, a = 1] = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat);
    return { r, g, b, a };
  }
  m = s.match(/^hsla?\(([^)]+)\)$/);
  if (m) {
    const [h, sat, l, a = 1] = m[1].split(/[ ,\/]+/).filter(Boolean).map(parseFloat);
    return { ...hslToRgb(h, sat / 100, l / 100), a };
  }
  return null;
}
function hslToRgb(h, s, l) {
  const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  return { r: Math.round(f(0) * 255), g: Math.round(f(8) * 255), b: Math.round(f(4) * 255) };
}
