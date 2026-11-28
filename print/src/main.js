import * as maplibregl from "maplibre-gl";
// MapLibre 6 loads its worker from a separate file; tell it where (same folder as app.js).
maplibregl.setWorkerUrl(new URL("maplibre-gl-worker.js", document.baseURI).href);
import { renderSheet, atlasCells } from "./render.js";
import { layout, footprintMeters, SCALES, PAPERS, ATLAS_GRIDS } from "./layout.js";
import { zoomForScale, viewportBounds, inverse, fmtDistance, fmtBearing, project, unproject } from "./geo.js";
import { toMGRS } from "./utm.js";
import { gridFC, metresPerPixel } from "./grid.js";
import { arrowInfo as arrowInfoFor } from "./features.js";
import { PALETTE, swatchRow } from "./palette.js";
import { toGeoJSON, fromGeoJSON, fileName } from "./drawings.js";
import { UNITS, defaultUnit, unitMetres, shapeRing, shapeCenter, shapeSummary, shapeLegs, shapeInsideText, fmtLength, SHAPE_NAMES } from "./shapes.js";
import { SECTIONS, OWN_CONTROLS, DEFAULTS as LAYER_DEFAULTS, applyLayers, groupsOf } from "./layers.js";
import { initTerrain, TERRAIN_SOURCES } from "./terrain/panel.js";
import { initWmmNotice } from "./wmm.js";
import { startLive, conversations, partnerOf, ageText, statusText, cleanText, MAX_NAME, BASE } from "./live.js";
import { sharedFC, peopleFC, describe, kindText, personImage, PERSON_COLORS, itemFromFeature, featureFromItem, itemKey, ROUTE_TYPES_LIST, ROUTE_OPTIONS, DIFFICULTY, ALTERNATIVES, cleanOptions, SPEEDS, MAX_ROUTE_POINTS, shareableShape, chevronImage, CHEVRON, arrowHead } from "./shared.js";
import { createRouter, routesFC, routeLegs, routeStats, durationText, ROAD_CLASSES } from "./routing.js";

// The map at /map/: what the devices share (live.js, shared.js), drawing, terrain analysis, and
// printing (the Print tab: the sheet on the map, render.js). Opened with ?print (the print
// tool's old address), it starts on the Print tab.
const qs = new URLSearchParams(location.search);

const LS = {
  markers: "fieldmap.print.markers",
  arrows: "fieldmap.print.arrows",
  shapes: "fieldmap.print.shapes",
  routes: "fieldmap.print.routes",
  groups: "fieldmap.print.groups",
  settings: "fieldmap.print.settings",
};

const $ = (id) => document.getElementById(id);
const state = Object.assign(
  { scale: 24000, paper: "letter", orientation: "landscape", dpi: 300, title: "", listPage: false, color: PALETTE[0][0], atlas: 1, binder: false, style: "topo-print", units: "imperial", shapeMeasure: true },
  JSON.parse(localStorage.getItem(LS.settings) || "{}")
);
// Settings saved before version 2 always carried binder: true (the old default); start them off.
if ((state.settingsVersion || 1) < 2) { state.binder = false; state.settingsVersion = 2; }
state.layers = { ...LAYER_DEFAULTS, ...(state.layers || {}) };

// Print styles, first in the menu. The others the tile server has follow, except the print
// scale bands (below) and the transparent overlay. ?style=<id> forces any style on the server
// for this visit. A style that is not a print style prints in the first print style.
const PRINT_STYLES = [["topo-print", "Standard"], ["topo-usgs", "USGS Quadrangle"]];
const printable = (id) => PRINT_STYLES.some(([p]) => p === id);
const BAND = /-(12k|24k|50k|100k)$/;
// Tile server base. Tried in order: ?tiles=<url>, then /maps on this host (installed beside a
// machine's own nginx, scripts/install-nginx.sh), then this host's root (the docker-compose stack, where nginx fronts
// TileServer GL), then port 8080 on this host.
async function findTiles() {
  const candidates = qs.get("tiles") ? [qs.get("tiles")] : [`${location.origin}/maps`, location.origin, `${location.protocol}//${location.hostname}:8080`];
  for (const base of candidates) {
    const b = base.replace(/\/$/, "");
    try { const r = await fetch(`${b}/styles/${PRINT_STYLES[0][0]}/style.json`, { cache: "no-store" }); if (r.ok) return b; } catch {}
  }
  return candidates[0].replace(/\/$/, "");
}
const TILES = await findTiles();
const styleUrlFor = (id) => `${TILES}/styles/${id}/style.json`;
let serverStyles = new Set();
async function availableStyles() {
  try {
    const r = await fetch(`${TILES}/styles.json`, { cache: "no-store" });
    const list = await r.json();
    serverStyles = new Set(list.map((s) => s.id));
    const others = list.filter((s) => !printable(s.id) && !BAND.test(s.id) && s.id !== "overlay").map((s) => [s.id, s.name || s.id]);
    return [...PRINT_STYLES.filter(([id]) => serverStyles.has(id)), ...others];
  } catch { return [PRINT_STYLES[0]]; }
}
// Print scale bands with their own style (<style>-12k, -24k, -50k, -100k): line weights
// fixed for the scale, so a sheet looks the same at any latitude. Used when the server has one.
const band = (scale) => (scale <= 16000 ? "12k" : scale <= 36000 ? "24k" : scale <= 75000 ? "50k" : "100k");
/** The style a sheet is printed in: the map's, when it is a print style, else the first print style. */
const printStyleId = () => (printable(STYLE) || !serverStyles.has(PRINT_STYLES[0][0]) ? STYLE : PRINT_STYLES[0][0]);
async function printStyle() {
  const base = printStyleId();
  const id = `${base}-${band(state.scale)}`;
  try {
    if (serverStyles.has(id)) return applyLayers(await fetchStyle(styleUrlFor(id)), state.layers);
    if (base !== STYLE) return applyLayers(await fetchStyle(styleUrlFor(base)), state.layers);
  } catch (e) { console.warn("print style:", e); }
  return drawnStyle();
}
const styles = await availableStyles();
let STYLE = qs.get("style") || (styles.some(([id]) => id === state.style) ? state.style : (styles[0] || PRINT_STYLES[0])[0]);
let styleUrl = styleUrlFor(STYLE);

// The style as served, with relative sprite, glyph and source URLs made absolute so the
// style can be handed to MapLibre as an object (the layer switches rewrite it).
async function fetchStyle(url) {
  const st = await (await fetch(url, { cache: "no-store" })).json();
  const abs = (u) => (typeof u !== "string" || /^[a-z][a-z0-9+.-]*:/i.test(u)) ? u
    : u.startsWith("/") ? new URL(url).origin + u : url.replace(/[^/]*$/, "") + u;
  if (st.sprite) st.sprite = Array.isArray(st.sprite) ? st.sprite.map((s) => ({ ...s, url: abs(s.url) })) : abs(st.sprite);
  if (st.glyphs) st.glyphs = abs(st.glyphs);
  for (const src of Object.values(st.sources || {})) {
    if (src.url) src.url = abs(src.url);
    if (src.tiles) src.tiles = src.tiles.map(abs);
  }
  return st;
}
let rawStyle = await fetchStyle(styleUrl).catch((e) => { console.error("style:", e); return { version: 8, sources: {}, layers: [] }; });
const drawnStyle = () => applyLayers(rawStyle, state.layers);

let markers = JSON.parse(localStorage.getItem(LS.markers) || "[]");
let arrows = JSON.parse(localStorage.getItem(LS.arrows) || "[]");
let shapes = JSON.parse(localStorage.getItem(LS.shapes) || "[]");
let routes = JSON.parse(localStorage.getItem(LS.routes) || "[]");
// The groups the items are kept in ("Water Sources", "Egress Routes"), in the order they were
// made: [{ name, hidden, closed }]. An item's group is its "group" (none: Ungrouped, name "").
// A hidden group is off the map and off the printed sheets. Kept in this browser; a shared item
// carries its group's name to the other devices.
let groups = JSON.parse(localStorage.getItem(LS.groups) || "[]");
const KINDS = ["marker", "arrow", "route", "shape"];
const listOf = (kind) => ({ marker: markers, arrow: arrows, route: routes, shape: shapes }[kind]);
function setList(kind, list) {
  if (kind === "marker") markers = list; else if (kind === "arrow") arrows = list; else if (kind === "route") routes = list; else shapes = list;
}
let config = {};
// null | "marker" | "arrow-start" | "arrow-end" | "route" | "circle-center" | "circle-edge" | "rect-start" | "rect-end" | "polygon" | "me"
let mode = null;
let pendingArrow = null;    // {a:{lng,lat}} while placing the end
let pendingRoute = null;    // [{lng,lat}, ...] while a route's points are tapped
let pendingShape = null;    // {type, center | points} while a shape is being drawn
let selected = null;        // {kind:"marker"|"arrow"|"route"|"shape", id}
let paletteOpenFor = null;  // id of the row whose swatches are open
// The panel's tab: layers | markers | print | terrain. The sheet is on the map only on the Print tab.
let tab = qs.has("print") ? "print" : ["layers", "markers", "print"].includes(state.tab) ? state.tab : "layers";
// The page shows the map, or Messages (the header's Messages page, ?messages) in its place.
let view = qs.has("messages") ? "messages" : "map";

fetch("config.json").then((r) => (r.ok ? r.json() : {})).then((c) => (config = c)).catch(() => {});

/* ---------- controls ---------- */
const scaleSel = $("scale");
for (const s of SCALES) scaleSel.add(new Option(`1:${s.toLocaleString("en-US")}`, s));
scaleSel.value = state.scale;
const styleSel = $("style");
for (const [id, label] of styles) styleSel.add(new Option(label, id));
if (!styles.some(([id]) => id === STYLE)) styleSel.add(new Option(STYLE, STYLE));
styleSel.value = STYLE;
const paperSel = $("paper");
for (const [k, p] of Object.entries(PAPERS)) paperSel.add(new Option(p.label, k));
paperSel.value = state.paper;
$("orientation").value = state.orientation;
const atlasSel = $("atlas");
for (const g of ATLAS_GRIDS) atlasSel.add(new Option(g === 1 ? "Single Sheet" : `Atlas ${g} × ${g} (${g * g} pages + index)`, g));
atlasSel.value = state.atlas || 1;
$("binder").checked = !!state.binder;
$("dpi").value = state.dpi;
$("title").value = state.title;
$("list-page").checked = !!state.listPage;
$("units").value = state.units;
$("shape-measure").checked = state.shapeMeasure !== false;

function saveSettings() { localStorage.setItem(LS.settings, JSON.stringify(state)); }

/* ---------- map ---------- */
const start = location.hash.match(/^#(\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)/);
const map = new maplibregl.Map({
  container: "map",
  style: drawnStyle(),
  center: start ? [+start[3], +start[2]] : undefined,
  zoom: start ? +start[1] : undefined,
  hash: true,
  attributionControl: false,
  maxZoom: 19,
});
window.__printMap = map; // for debugging in the console
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
// Browsers give a page the device's location only over https: on a plain http address the
// button could never work, so it is left out.
if (window.isSecureContext) map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-left");
map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

// Without a place in the address, start on the area the base map covers. A new install has
// empty placeholder files (scripts/data-placeholders.sh): say that no maps are installed.
(async () => {
  try {
    const source = Object.values(rawStyle.sources || {}).find((s) => s.type === "vector" && s.url);
    const data = source ? await (await fetch(source.url, { cache: "no-store" })).json() : null;
    if (data && data.name === "empty placeholder") $("no-maps").hidden = false;
    const b = data && data.bounds;
    if (!start && b && b[2] - b[0] < 300) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 40, duration: 0 });
  } catch { /* the map stays where it is */ }
})();

/* ---------- Add: what can be put on the map ---------- */
const ICONS = {
  marker: '<circle cx="12" cy="12" r="5.5" fill="currentColor"/>',
  arrow: '<path d="M5 19 19 5M10 5h9v9" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  route: '<path d="M5 19c3-6 6 1 9-5s3-7 5-9" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="0.1 4.2"/><circle cx="5" cy="19" r="2.4" fill="currentColor"/><circle cx="19" cy="5" r="2.4" fill="currentColor"/>',
  circle: '<circle cx="12" cy="12" r="7.5" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  rect: '<rect x="4.5" y="6.5" width="15" height="11" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  polygon: '<path d="M12 4 20 10 17 19H7L4 10Z" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>',
};
const icon = (key) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[key]}</svg>`;
// The Add menu at the top of the Markers tab: one entry per kind of item.
const TOOLS = [["marker", "Marker"], ["arrow", "Arrow"], ["route", "Route"], ["circle", "Circle"], ["rect", "Rectangle"], ["polygon", "Polygon"]];
for (const [key, title] of TOOLS) {
  const b = document.createElement("button");
  b.type = "button"; b.id = `add-${key}`; b.setAttribute("role", "menuitem");
  b.innerHTML = `${icon(key)}<span>${title}</span>`;
  $("add-menu").appendChild(b);
}
function openAddMenu(open) {
  $("add-menu").hidden = !open;
  $("add-button").ariaExpanded = String(open);
}
$("add-button").onclick = (e) => { e.stopPropagation(); openAddMenu($("add-menu").hidden); };
document.addEventListener("click", (e) => { if (!e.target.closest(".add-menu")) openAddMenu(false); });

let footprintCenter = null; // {lng,lat}; set on load to the centre of the visible map area

/** Padding that keeps the panel from covering the map area we care about. */
function panelPadding() {
  const panel = $("panel");
  const pad = { top: 30, right: 30, bottom: 30, left: 30 };
  if (document.body.classList.contains("panel-collapsed")) return pad;
  if (window.innerWidth > 640) pad.right += panel.offsetWidth + 12;
  else pad.bottom += panel.offsetHeight + 8;
  return pad;
}
function visibleCenter() {
  const p = panelPadding();
  const w = map.getContainer().clientWidth, h = map.getContainer().clientHeight;
  const c = map.unproject([(p.left + (w - p.right)) / 2, (p.top + (h - p.bottom)) / 2]);
  return { lng: c.lng, lat: c.lat };
}

map.on("load", () => {
  // The MGRS grid (grid.js), under everything drawn on the map.
  map.addSource("grid", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "grid-line", type: "line", source: "grid", filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": "#1f5fbf", "line-opacity": 0.7, "line-width": ["match", ["get", "kind"], "minor", 0.8, 1.6] } });
  // Each line's digits at the top and left edges of the view, upright.
  map.addLayer({ id: "grid-label", type: "symbol", source: "grid", filter: ["==", ["get", "kind"], "edge"],
    layout: { "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 12,
      "text-anchor": ["get", "anchor"], "text-offset": ["match", ["get", "anchor"], "top", ["literal", [0, 0.3]], ["literal", [0.3, 0]]],
      "text-allow-overlap": true, "text-ignore-placement": true },
    paint: { "text-color": "#1f5fbf", "text-halo-color": "#fff", "text-halo-width": 2 } });
  // The 100 km square's name (13S DC), in view at every zoom.
  map.addLayer({ id: "grid-name", type: "symbol", source: "grid", filter: ["==", ["get", "kind"], "name"],
    layout: { "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 15, "text-allow-overlap": false },   // where zones meet: one name shows
    paint: { "text-color": "#1f5fbf", "text-halo-color": "#fff", "text-halo-width": 2.5 } });
  map.on("moveend", drawGrid);

  map.addSource("footprint", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "footprint-fill", type: "fill", source: "footprint", paint: { "fill-color": "#2d8cf0", "fill-opacity": 0.12 } });
  map.addLayer({ id: "footprint-line", type: "line", source: "footprint", paint: { "line-color": "#2d8cf0", "line-width": 2 } });
  map.addLayer({
    id: "footprint-label", type: "symbol", source: "footprint", filter: ["has", "name"],
    layout: { "text-field": ["get", "name"], "text-font": pickFont(), "text-size": 16, "text-allow-overlap": true },
    paint: { "text-color": "#1b5fa8", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });

  map.addSource("shapes", { type: "geojson", data: shapesFC() });
  map.addLayer({ id: "shapes-fill", type: "fill", source: "shapes", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": ["get", "color"], "fill-opacity": 0.12 } });
  map.addLayer({
    id: "shapes-halo", type: "line", source: "shapes", filter: ["==", ["geometry-type"], "Polygon"],
    layout: { "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 5, "line-opacity": 0.6 },
  });
  map.addLayer({
    id: "shapes-line", type: "line", source: "shapes", filter: ["==", ["geometry-type"], "Polygon"],
    layout: { "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 2.5 },
  });
  map.addLayer({
    id: "shapes-legs", type: "symbol", source: "shapes", filter: ["==", ["geometry-type"], "LineString"],
    layout: { "symbol-placement": "line-center", "text-field": ["get", "leg"], "text-font": pickFont(), "text-size": 11,
      "text-allow-overlap": true, "text-keep-upright": true, "text-offset": [0, -0.8] },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.6 },
  });
  map.addLayer({
    id: "shapes-label", type: "symbol", source: "shapes", filter: ["==", ["geometry-type"], "Point"],
    layout: { "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 12, "text-allow-overlap": true, "text-max-width": 14 },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.6 },
  });

  map.addSource("arrows", { type: "geojson", data: arrowsFC() });
  map.addLayer({
    id: "arrows-line", type: "line", source: "arrows", filter: ["==", ["get", "part"], "line"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 3 },
  });
  map.addLayer({
    id: "arrows-halo", type: "line", source: "arrows", filter: ["==", ["get", "part"], "line"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#fff", "line-width": 6, "line-opacity": 0.6 },
  }, "arrows-line");
  map.addLayer({
    id: "arrows-label", type: "symbol", source: "arrows", filter: ["==", ["get", "part"], "label"],
    layout: {
      "symbol-placement": "point", "text-field": ["get", "label"], "text-font": pickFont(),
      "text-size": 12, "text-offset": [0, -1], "text-allow-overlap": true, "text-ignore-placement": true,
      "text-rotation-alignment": "map", "text-rotate": ["get", "rotate"], "text-max-width": 60,
    },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.6 },
  });
  map.addLayer({
    id: "arrow-ends", type: "circle", source: "arrows", filter: ["==", ["get", "part"], "end"],
    paint: { "circle-radius": 7, "circle-color": "#fff", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 3 },
  });
  // The head, over the handle at the tip; its size on screen is kept at every zoom (headLength).
  map.addLayer({ id: "arrows-head-halo", type: "line", source: "arrows", filter: ["==", ["get", "part"], "head"],
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 7 } });
  map.addLayer({ id: "arrows-head", type: "line", source: "arrows", filter: ["==", ["get", "part"], "head"],
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3.5 } });
  // The heads are drawn again for the new zoom.
  map.on("zoomend", () => { map.getSource("arrows").setData(arrowsFC()); drawLive(); });

  map.addSource("routes", { type: "geojson", data: emptyFC() });
  const rpart = (...parts) => ["match", ["get", "part"], parts, true, false];
  map.addLayer({ id: "routes-halo", type: "line", source: "routes", filter: rpart("line", "dashed"),
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 6, "line-opacity": 0.6 } });
  map.addLayer({ id: "routes-line", type: "line", source: "routes", filter: rpart("line"),
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3.5 } });
  map.addLayer({ id: "routes-dashed", type: "line", source: "routes", filter: rpart("dashed"),
    layout: { "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "routes-way", type: "symbol", source: "routes", filter: rpart("way"),
    layout: { "symbol-placement": "line", "symbol-spacing": 70, "icon-image": CHEVRON, "icon-allow-overlap": true, "icon-ignore-placement": true,
      "icon-rotation-alignment": "map" },
    paint: { "icon-color": ["get", "color"], "icon-halo-color": "#fff", "icon-halo-width": 1.5 } });
  map.addLayer({ id: "routes-vertex", type: "circle", source: "routes", filter: rpart("vertex"),
    paint: { "circle-radius": 5, "circle-color": "#fff", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 2.5 } });
  // Edit Route: a small dot halfway along each leg; drag it for a new point there.
  map.addLayer({ id: "routes-mid", type: "circle", source: "routes", filter: rpart("mid"),
    paint: { "circle-radius": 3.5, "circle-color": "#fff", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 1.5 } });
  map.addLayer({ id: "routes-label", type: "symbol", source: "routes", filter: rpart("label"),
    layout: { "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 13, "text-offset": [0, -1.1], "text-allow-overlap": true },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.5 } });

  map.addSource("markers", { type: "geojson", data: markersFC() });
  map.addLayer({
    id: "markers-circle", type: "circle", source: "markers",
    paint: { "circle-radius": 8, "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 },
  });
  map.addLayer({
    id: "markers-label", type: "symbol", source: "markers",
    layout: {
      "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 13,
      "text-offset": [0.9, 0], "text-anchor": "left", "text-allow-overlap": true,
    },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });

  // What the devices share (shared.js): areas, lines and routes, markers; the devices on top.
  map.addSource("live", { type: "geojson", data: emptyFC() });
  const part = (...parts) => ["match", ["get", "part"], parts, true, false];
  map.addLayer({ id: "live-area", type: "fill", source: "live", filter: part("area"), paint: { "fill-color": ["get", "color"], "fill-opacity": 0.12 } });
  map.addLayer({ id: "live-halo", type: "line", source: "live", filter: part("line", "head", "dashed", "area"),
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 6, "line-opacity": 0.6 } });
  map.addLayer({ id: "live-line", type: "line", source: "live", filter: part("line", "head", "area"),
    layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3 } });
  map.addLayer({ id: "live-dashed", type: "line", source: "live", filter: part("dashed"),
    layout: { "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "live-vertex", type: "circle", source: "live", filter: part("vertex"),
    paint: { "circle-radius": 4, "circle-color": "#fff", "circle-stroke-color": ["get", "color"], "circle-stroke-width": 2 } });
  map.addLayer({ id: "live-point", type: "circle", source: "live", filter: part("point"),
    paint: { "circle-radius": 8, "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
  map.addLayer({ id: "live-label", type: "symbol", source: "live", filter: part("point", "label"),
    layout: { "text-field": ["get", "label"], "text-font": pickFont(), "text-size": 13, "text-allow-overlap": true,
      "text-anchor": ["match", ["get", "part"], "point", "left", "center"],
      "text-offset": ["match", ["get", "part"], "point", ["literal", [0.9, 0]], ["literal", [0, -1]]] },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.5 } });
  map.addSource("people", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "people-symbol", type: "symbol", source: "people",
    layout: { "icon-image": ["get", "icon"], "icon-allow-overlap": true, "icon-ignore-placement": true },
    paint: { "icon-opacity": ["get", "opacity"] } });
  map.addLayer({ id: "people-name", type: "symbol", source: "people",
    layout: { "text-field": ["get", "name"], "text-font": pickFont(), "text-size": 14, "text-offset": [1.2, 0], "text-anchor": "left",
      "text-allow-overlap": true, "text-ignore-placement": true },
    paint: { "text-color": "#004e63", "text-halo-color": "#fff", "text-halo-width": 2, "text-opacity": ["get", "opacity"] } });

  // This browser's own location, placed by hand: like the others, in the server's color, draggable.
  map.addSource("me", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "me-symbol", type: "symbol", source: "me",
    layout: { "icon-image": "live-me", "icon-allow-overlap": true, "icon-ignore-placement": true } });
  map.addLayer({ id: "me-name", type: "symbol", source: "me",
    layout: { "text-field": ["get", "name"], "text-font": pickFont(), "text-size": 14, "text-offset": [1.2, 0], "text-anchor": "left",
      "text-allow-overlap": true, "text-ignore-placement": true },
    paint: { "text-color": "#33461a", "text-halo-color": "#fff", "text-halo-width": 2 } });

  map.addSource("temp", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "temp-fill", type: "fill", source: "temp", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#333", "fill-opacity": 0.08 } });
  map.addLayer({ id: "temp-line", type: "line", source: "temp", paint: { "line-color": "#333", "line-width": 2, "line-dasharray": [2, 2] } });
  map.addLayer({ id: "temp-points", type: "circle", source: "temp", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 4, "circle-color": "#fff", "circle-stroke-color": "#333", "circle-stroke-width": 2 } });

  footprintCenter = visibleCenter();
  updateFootprint();
  drawGrid();
  renderLists();
  drawRoutes();
  showSheet();
  drawLive();
  drawMe();
  terrainReady.then((t) => t.addLayers());

  map.on("mousedown", onDown); map.on("touchstart", onDown);
  map.on("mousemove", onMove); map.on("touchmove", onMove);
  map.on("mouseup", onUp); map.on("touchend", onUp);
});


// Magnetic model: load a newer one from the server if it has one; warn before it runs out.
initWmmNotice($("wmm-notice"));

// Terrain analysis (viewshed, radio): only when the tile server has elevation tiles.
let terrain = null;
const terrainReady = initTerrain({
  map, tilesBase: TILES, state, saveSettings, root: $("terrain"),
  onLayout: () => updateFootprint(),
}).then((t) => { terrain = t; renderLayerPanel(); renderTerrainSwitch(); return t; });

map.on("click", (e) => {
  if (justDragged) { justDragged = false; return; }
  if (onPin(e) || (terrain && terrain.onClick(e))) return;
  if (mode === "marker") { addMarker(e.lngLat); return; }
  if (mode === "me") { placeMe(e.lngLat); return; }
  if (mode === "arrow-start") { pendingArrow = { a: { lng: e.lngLat.lng, lat: e.lngLat.lat } }; setMode("arrow-end"); return; }
  if (mode === "arrow-end") { addArrow(pendingArrow.a, { lng: e.lngLat.lng, lat: e.lngLat.lat }); pendingArrow = null; setMode(null); return; }
  if (mode === "route") { routeClick({ lng: e.lngLat.lng, lat: e.lngLat.lat }); return; }
  if (mode === "route-edit") { editClick(e); return; }
  if (mode && shapeClick(e)) return;
  // One item under the tap: its row; several on top of each other: a list to choose from.
  const hits = itemsAt(e.point);
  if (hits.length === 1) { showItem(hits[0].kind, hits[0].id); return; }
  if (hits.length > 1) { chooseItem(e.lngLat, hits); return; }
  if (liveClick(e)) return;
  // On the Print tab, a tap outside the sheet brings the sheet there.
  if (tab !== "print") return;
  const inside = map.queryRenderedFeatures(e.point, { layers: ["footprint-fill"] }).length > 0;
  if (!inside) { footprintCenter = { lng: e.lngLat.lng, lat: e.lngLat.lat }; updateFootprint(); }
});

/* ---------- dragging: markers, arrow ends, the sheet ---------- */
let dragging = null, justDragged = false;
// Terrain points are draggable DOM markers; their presses must not also drag the sheet.
const onPin = (e) => !!(e.originalEvent && e.originalEvent.target && e.originalEvent.target.closest && e.originalEvent.target.closest(".terrain-pin"));
function onDown(e) {
  if (mode === "route-edit" && editing) {
    if (editing.tool) return;            // adding or removing: taps, not drags
    const hit = editHit(e.point);
    if (hit) { dragging = { kind: hit.kind === "point" ? "edit-move" : "edit-insert", index: hit.index, ll: null }; return begin(e); }
    return;
  }
  if (mode || (terrain && terrain.picking) || onPin(e)) return;
  const pt = e.point;
  // The item chosen last (in the list or from items on top of each other) moves first.
  if (selected) {
    const mine = (layer) => map.queryRenderedFeatures(pt, { layers: [layer] }).filter((f) => f.properties.id === selected.id);
    const h = selected.kind === "marker" ? mine("markers-circle") : selected.kind === "arrow" ? mine("arrow-ends") : [];
    if (h.length) {
      const p = h[0].properties;
      dragging = selected.kind === "marker" ? { kind: "marker", id: p.id } : selected.kind === "arrow" ? { kind: "arrow-end", id: p.id, end: p.end } : { kind: "route-point", id: p.id, index: p.index };
      return begin(e);
    }
  }
  let hit = map.getLayer("me-symbol") ? map.queryRenderedFeatures(pt, { layers: ["me-symbol"] }) : [];
  if (hit.length) { dragging = { kind: "me" }; return begin(e); }
  hit = map.queryRenderedFeatures(pt, { layers: ["markers-circle"] });
  if (hit.length) { dragging = { kind: "marker", id: hit[0].properties.id }; return begin(e); }
  hit = map.queryRenderedFeatures(pt, { layers: ["arrow-ends"] });
  if (hit.length) { dragging = { kind: "arrow-end", id: hit[0].properties.id, end: hit[0].properties.end }; return begin(e); }
  hit = tab === "print" ? map.queryRenderedFeatures(pt, { layers: ["footprint-fill"] }) : [];
  if (hit.length) {
    const c = currentCenter();
    footprintCenter = { lng: c.lng, lat: c.lat };
    dragging = { kind: "sheet", startPt: pt, startCenterPx: map.project([c.lng, c.lat]) };
    return begin(e);
  }
}
function begin(e) { e.preventDefault(); map.getCanvas().style.cursor = "grabbing"; }
function onMove(e) {
  if (mode === "arrow-end" && pendingArrow) {
    map.getSource("temp").setData(lineFC([pendingArrow.a, { lng: e.lngLat.lng, lat: e.lngLat.lat }]));
    return;
  }
  if (pendingShape) { previewShape({ lng: e.lngLat.lng, lat: e.lngLat.lat }); return; }
  if (pendingRoute) { previewRoute({ lng: e.lngLat.lng, lat: e.lngLat.lat }); return; }
  if (!dragging) return;
  if (dragging.kind === "edit-move" || dragging.kind === "edit-insert") { dragging.ll = { lng: e.lngLat.lng, lat: e.lngLat.lat }; editPreview(dragging); return; }
  if (dragging.kind === "me") {
    state.myLocation = { lng: e.lngLat.lng, lat: e.lngLat.lat };
    drawMe();
  } else if (dragging.kind === "marker") {
    const m = markers.find((x) => x.id === dragging.id);
    if (m) { m.lng = e.lngLat.lng; m.lat = e.lngLat.lat; map.getSource("markers").setData(markersFC()); }
  } else if (dragging.kind === "arrow-end") {
    const a = arrows.find((x) => x.id === dragging.id);
    if (a) { a[dragging.end] = { lng: e.lngLat.lng, lat: e.lngLat.lat }; map.getSource("arrows").setData(arrowsFC()); }
  } else if (dragging.kind === "route-point") {
    const r = routes.find((x) => x.id === dragging.id);
    if (r && r.points[dragging.index]) { r.points[dragging.index] = { lng: e.lngLat.lng, lat: e.lngLat.lat }; dragging.moved = true; drawRoutes(); }
  } else if (dragging.kind === "sheet") {
    const dx = e.point.x - dragging.startPt.x, dy = e.point.y - dragging.startPt.y;
    const ll = map.unproject([dragging.startCenterPx.x + dx, dragging.startCenterPx.y + dy]);
    footprintCenter = { lng: ll.lng, lat: ll.lat };
    updateFootprint();
  }
}
function onUp() {
  if (!dragging) return;
  const d = dragging; dragging = null; justDragged = true;
  map.getCanvas().style.cursor = "";
  setTimeout(() => (justDragged = false), 300);
  if (d.kind === "me") placeMe(state.myLocation);
  if (d.kind === "marker") { saveMarkers(); select("marker", d.id); }
  if (d.kind === "arrow-end") { saveArrows(); select("arrow", d.id); }
  if (d.kind === "edit-move" || d.kind === "edit-insert") { editDrop(d); return; }
  if (d.kind === "route-point") { saveRoutes(); select("route", d.id); }
}

/* ---------- helpers ---------- */
function pickFont() {
  const layers = map.getStyle().layers || [];
  for (const l of layers) {
    const f = l.layout && l.layout["text-font"];
    if (Array.isArray(f) && typeof f[0] === "string") return f;
  }
  return ["Open Sans Regular"];
}
function emptyFC() { return { type: "FeatureCollection", features: [] }; }

/** The MGRS grid for the view, when MGRS Grid Lines is on. */
function drawGrid() {
  const src = map.getSource("grid");
  if (!src) return;
  const on = state.layers.grid !== false;
  try {
    for (const id of ["grid-line", "grid-label", "grid-name"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  } catch { /* a new map style is still loading */ }
  const b = map.getBounds();
  src.setData(on ? gridFC([[b.getWest(), b.getSouth()], [b.getEast(), b.getNorth()]], map.getZoom()) : emptyFC());
}
function lineFC(pts) {
  return { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "LineString", coordinates: pts.map((p) => [p.lng, p.lat]) }, properties: {} }] };
}
function currentCenter() {
  if (footprintCenter) return footprintCenter;
  const c = map.getCenter();
  return { lng: c.lng, lat: c.lat };
}
function effectiveDpi() {
  // Phones run out of memory on the biggest sheets; keep them at 150 dpi there.
  if (window.innerWidth <= 640 && state.paper === "archc") return Math.min(state.dpi, 150);
  return state.dpi;
}
function currentAtlas() {
  const n = +state.atlas || 1;
  const lay = layout(state.paper, state.orientation, state.scale, { binder: state.binder && n > 1, terrain: !!(terrain && terrain.printData()) });
  return { ...atlasCells(currentCenter(), n, lay, state.scale, effectiveDpi()), lay };
}
function footprintPolygon() {
  const a = currentAtlas();
  const [[w, s], [e, n]] = a.bounds;
  return { poly: [[w, s], [e, s], [e, n], [w, n], [w, s]], bounds: a.bounds, zoom: a.zoom, lay: a.lay, atlas: a };
}
function updateFootprint() {
  if (!map.getSource("footprint")) return;
  const { lay, atlas } = footprintPolygon();
  const feats = atlas.cells.map((c) => {
    const [[w, s], [e, n]] = c.bounds;
    return { type: "Feature", geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] }, properties: {} };
  });
  if (atlas.n > 1) {
    for (const c of atlas.cells) feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [c.center.lng, c.center.lat] }, properties: { name: c.name } });
  }
  map.getSource("footprint").setData({ type: "FeatureCollection", features: feats });
  const fp = footprintMeters(lay, state.scale);
  const c = currentCenter();
  const cellTxt = `${(fp.w / 1000).toFixed(1)} × ${(fp.h / 1000).toFixed(1)} km`;
  $("info-footprint").textContent = atlas.n > 1
    ? `${atlas.n * atlas.n} pages of ${cellTxt}`
    : cellTxt;
  $("info-centre").textContent = toMGRS(c.lat, c.lng, 4);
  $("info-style").textContent = (styles.find(([id]) => id === printStyleId()) || [, printStyleId()])[1];
  $("info-grid").textContent = state.scale <= 12000 ? "MGRS 1 km + 100 m ticks" : state.scale >= 100000 ? "MGRS 1 km, 10 km bold" : "MGRS 1 km";
  const pages = atlas.n > 1 ? atlas.n * atlas.n + 1 : 1;
  const phone = window.innerWidth <= 640 || /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
  const warn = $("atlas-warning");
  if (atlas.n > 1 && phone) {
    warn.textContent = `Atlases are built in the browser. On a phone or tablet this is slow and can run out of memory, so use a computer.${pages > 30 ? " " + pages + " pages take several minutes." : ""}`;
  } else if (pages > 30) {
    warn.textContent = `${pages} pages take several minutes and make a large file. Standard (200 dpi) quality takes about half the time.`;
  } else {
    warn.textContent = "";
  }
}

function arrowInfo(a) { return arrowInfoFor(a, currentCenter()); }
function arrowLabel(a) {
  const i = arrowInfo(a);
  return `${a.label ? a.label + "  " : ""}${fmtDistance(i.distance).split(" (")[0]}  ${fmtBearing(i.trueBearing)}T ${fmtBearing(i.magBearing)}M`;
}

/* ---------- data: markers ---------- */
// Shared markers, arrows, routes and shapes (shared: true) are in the same lists, but the server
// keeps them, not this browser; the Shared Markers switch hides them on screen and in print.
const ownOnly = (list) => list.filter((x) => !x.shared);
const groupShown = (it) => !(groups.find((g) => g.name === (it.group || "")) || {}).hidden;
const shown = (list) => (state.layers.shared === false ? ownOnly(list) : list).filter(groupShown);
/** The group new items go into (the Add menu's To), as an item's properties. */
const addTo = () => (state.addGroup && groups.some((g) => g.name === state.addGroup) ? { group: state.addGroup } : {});
function markersFC() {
  return { type: "FeatureCollection", features: shown(markers).map((m) => ({ type: "Feature", geometry: { type: "Point", coordinates: [m.lng, m.lat] }, properties: { id: m.id, label: m.label, color: m.color } })) };
}
function saveMarkers() {
  localStorage.setItem(LS.markers, JSON.stringify(ownOnly(markers)));
  syncSoon();
  if (map.getSource("markers")) map.getSource("markers").setData(markersFC());
  if (routes.some((r) => r.avoid)) drawRoutes();   // a route may avoid what changed
  renderLists();
}
function addMarker(ll) {
  const m = { id: uid(), lng: ll.lng, lat: ll.lat, label: `P${markers.length + 1}`, color: state.color, shared: sharingOn(), ...addTo() };
  markers.push(m);
  setMode(null);
  saveMarkers();
  select("marker", m.id, true);
}

/* ---------- data: arrows ---------- */
/** An arrow head's length in metres at a latitude: 18 px on screen at the map's zoom. */
const headLength = (lat) => 18 * metresPerPixel(lat, map.getZoom());
function arrowsFC() {
  const feats = [];
  for (const a of shown(arrows)) {
    const props = { id: a.id, color: a.color, label: arrowLabel(a) };
    feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[a.a.lng, a.a.lat], [a.b.lng, a.b.lat]] }, properties: { ...props, part: "line" } });
    feats.push({ type: "Feature", geometry: arrowHead(a.a, a.b, headLength(a.b.lat)), properties: { ...props, part: "head", label: "" } });
    // One label at the midpoint, rotated to the on-screen angle of the shaft and kept upright.
    const pa = project(a.a.lng, a.a.lat, 0), pb = project(a.b.lng, a.b.lat, 0);
    let rot = (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI;
    if (rot > 90 || rot < -90) rot += 180;
    const mid = unproject((pa.x + pb.x) / 2, (pa.y + pb.y) / 2, 0);
    feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [mid.lng, mid.lat] }, properties: { ...props, part: "label", rotate: rot } });
    feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [a.a.lng, a.a.lat] }, properties: { ...props, part: "end", end: "a" } });
    feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [a.b.lng, a.b.lat] }, properties: { ...props, part: "end", end: "b" } });
  }
  return { type: "FeatureCollection", features: feats };
}
function saveArrows() {
  localStorage.setItem(LS.arrows, JSON.stringify(ownOnly(arrows)));
  syncSoon();
  if (map.getSource("arrows")) map.getSource("arrows").setData(arrowsFC());
  if (routes.some((r) => r.avoid)) drawRoutes();   // a route may avoid what changed
  renderLists();
}
function addArrow(a, b) {
  const ar = { id: uid(), a, b, label: `A${arrows.length + 1}`, color: state.color, shared: sharingOn(), ...addTo() };
  arrows.push(ar);
  map.getSource("temp").setData(emptyFC());
  saveArrows();
  select("arrow", ar.id, true);
}
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 5); }

/* ---------- data: routes ---------- */
// A route is its points (2 to 30, the most one mesh radio packet carries) and its Route Type, as
// in the app. Its legs follow roads or trails where the server's router finds a way (routing.js).
const router = createRouter(() => { drawRoutes(); refreshLists(); }, undefined, nearRoads, avoidAreas);
/** The areas a route avoids when its Avoid Areas names this group (routing.js routeAsk): the
 *  group's circles (and markers, 100 m around them) as circles, its rectangles and polygons as
 *  areas, its arrows as lines that cut the roads they cross. Its routes are not obstacles. */
function avoidAreas(group) {
  const of = (list) => list.filter((it) => it.group === group);
  const ring = (s) => { const r = shapeRing(s); const [f, l] = [r[0], r[r.length - 1]]; return f[0] === l[0] && f[1] === l[1] ? r.slice(0, -1) : r; };
  return {
    nogos: [...of(markers).map((m) => [m.lng, m.lat, 100]), ...of(shapes).filter((s) => s.type === "circle").map((s) => [s.center.lng, s.center.lat, s.radius])],
    polygons: of(shapes).filter((s) => s.type !== "circle").map(ring),
    polylines: of(arrows).map((a) => [[a.a.lng, a.a.lat], [a.b.lng, a.b.lat]]),
  };
}
/** While legs are being worked out: a note at the bottom of the map, so that it is clear that
 *  something is happening (a long leg can take minutes). */
function routeStatus() {
  const n = router.busy();
  $("route-status").hidden = !n;
  $("route-status-text").textContent = `Working out route${n === 1 ? "" : "s"}: ${n} leg${n === 1 ? "" : "s"} to go. A long leg can take a few minutes.`;
}
/** The lines of the roads near a point that a Route Type can use, from the map tiles loaded now
 *  (the base map's transportation layer): where a leg that ends off the road goes to the road. */
function nearRoads(p, routing) {
  const classes = ROAD_CLASSES[routing];
  const layer = (map.getStyle().layers || []).find((l) => l["source-layer"] === "transportation");
  if (!classes || !layer) return [];
  const feats = map.querySourceFeatures(layer.source, { sourceLayer: "transportation", filter: ["in", ["get", "class"], ["literal", classes]] });
  // Only lines near the point: about 15 km around it.
  const dLat = 0.14, dLng = 0.14 / Math.max(0.1, Math.cos((p.lat * Math.PI) / 180));
  const near = (c) => Math.abs(c[0] - p.lng) < dLng && Math.abs(c[1] - p.lat) < dLat;
  const lines = [];
  for (const f of feats) {
    const g = f.geometry;
    const parts = g.type === "LineString" ? [g.coordinates] : g.type === "MultiLineString" ? g.coordinates : [];
    for (const line of parts) if (line.some(near)) lines.push(line);
  }
  return lines;
}
function drawRoutes() {
  const list = shown(routes);
  router.want(list);
  routeStatus();
  const fc = routesFC(list, router);
  const r = editing && routes.find((x) => x.id === editing.id);
  if (r) {
    // A dot halfway along each leg as it is drawn (along the roads once worked out).
    const base = { id: r.id, color: r.color };
    routeLegs(r, router).forEach((leg, i) => {
      const line = [...(leg.head ? [leg.head[0]] : []), ...leg.line, ...(leg.tail ? [leg.tail[1]] : [])];
      fc.features.push({ type: "Feature", geometry: { type: "Point", coordinates: halfway(line) }, properties: { ...base, part: "mid", index: i + 1 } });
    });
  }
  try { if (map.getSource("routes")) map.getSource("routes").setData(fc); } catch { /* a new map style is still loading */ }
}

/* ---------- Edit Route ---------- */
// Drag a point to move it, or the dot halfway along a leg for a new point there: a dashed line
// follows the pointer, and the legs are worked out only when it is let go. Add Points at Start or
// at End, then tap the map; Remove Points, then tap the points. Undo takes back the last change,
// Done keeps the route, Cancel puts it back as it was.
let editing = null;          // { id, tool: null | "start" | "end" | "remove", before: points, history: [points] }
const copyPoints = (pts) => pts.map((p) => ({ lng: p.lng, lat: p.lat }));
/** The point [lon, lat] halfway along a line of [lon, lat] points (by ground length). */
function halfway(line) {
  const d = (a, b) => Math.hypot((b[0] - a[0]) * Math.cos((a[1] * Math.PI) / 180), b[1] - a[1]);
  const total = line.reduce((t, c, i) => (i ? t + d(line[i - 1], c) : 0), 0);
  let left = total / 2;
  for (let i = 1; i < line.length; i++) {
    const seg = d(line[i - 1], line[i]);
    if (seg >= left && seg > 0) { const t = left / seg; return [line[i - 1][0] + t * (line[i][0] - line[i - 1][0]), line[i - 1][1] + t * (line[i][1] - line[i - 1][1])]; }
    left -= seg;
  }
  return line[line.length - 1];
}
function editRoute(id) {
  const r = routes.find((x) => x.id === id);
  if (!r) return;
  setMode(null);
  editing = { id, tool: null, before: copyPoints(r.points), history: [] };
  setMode("route-edit");
  renderLists();
  const b = r.points.reduce((x, p) => [Math.min(x[0], p.lng), Math.min(x[1], p.lat), Math.max(x[2], p.lng), Math.max(x[3], p.lat)], [180, 90, -180, -90]);
  if (!map.getBounds().contains([b[0], b[1]]) || !map.getBounds().contains([b[2], b[3]])) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: panelPadding(), maxZoom: 15 });
  drawRoutes();
}
const editedRoute = () => (editing ? routes.find((x) => x.id === editing.id) : null);
/** The edited route's point or dot under a screen point: { kind: "point" | "mid", index }, or null. */
function editHit(pt) {
  const r = editedRoute();
  if (!r) return null;
  const box = [[pt.x - 7, pt.y - 7], [pt.x + 7, pt.y + 7]];
  const mine = (layer) => map.queryRenderedFeatures(box, { layers: [layer] }).filter((f) => f.properties.id === r.id);
  const point = mine("routes-vertex")[0], mid = mine("routes-mid")[0];
  return point ? { kind: "point", index: point.properties.index } : mid ? { kind: "mid", index: mid.properties.index } : null;
}
/** While a point or a dot is dragged: dashed lines from the points before and after it. */
function editPreview(d) {
  const r = editedRoute();
  if (!r || !d.ll) return;
  const before = r.points[d.index - 1], after = d.kind === "edit-move" ? r.points[d.index + 1] : r.points[d.index];
  map.getSource("temp").setData({ type: "FeatureCollection", features: [
    ...[before, after].filter(Boolean).map((p) => lineFC([p, d.ll]).features[0]),
    { type: "Feature", geometry: { type: "Point", coordinates: [d.ll.lng, d.ll.lat] }, properties: {} },
  ] });
}
/** A dragged point or dot let go: the change is made, and its legs worked out. */
function editDrop(d) {
  map.getSource("temp").setData(emptyFC());
  const r = editedRoute();
  if (!r || !d.ll) return;                       // only pressed, not moved: nothing changes
  if (d.kind === "edit-insert" && r.points.length >= MAX_ROUTE_POINTS) { editChanged(); return; }
  editing.history.push(copyPoints(r.points));
  if (d.kind === "edit-move") r.points[d.index] = d.ll; else r.points.splice(d.index, 0, d.ll);
  editChanged();
}
function editChanged() { saveRoutes(); setMode("route-edit"); drawRoutes(); }
function editTool(tool) {
  if (!editing) return;
  editing.tool = editing.tool === tool ? null : tool;
  setMode("route-edit");
}
function editUndo() {
  const r = editedRoute();
  if (!r || !editing.history.length) return;
  r.points = editing.history.pop();
  editChanged();
}
function editEnd(keep) {
  const r = editedRoute();
  const before = editing && editing.before;
  editing = null;                                // first: the lists then show Edit Route again
  if (r && !keep) r.points = before;
  setMode(null);
  saveRoutes();
}
/** A tap while a route is edited: with Add Points, a point at the start or end; with Remove
 *  Points, the point tapped goes (a route keeps two). Otherwise a tap does nothing. */
function editClick(e) {
  const r = editedRoute();
  if (!r) { editEnd(true); return; }
  const tool = editing.tool;
  if (tool === "remove") {
    const hit = editHit(e.point);
    if (!hit || hit.kind !== "point" || r.points.length <= 2) return;
    editing.history.push(copyPoints(r.points));
    r.points.splice(hit.index, 1);
    editChanged();
  } else if ((tool === "start" || tool === "end") && r.points.length < MAX_ROUTE_POINTS) {
    editing.history.push(copyPoints(r.points));
    const p = { lng: e.lngLat.lng, lat: e.lngLat.lat };
    if (tool === "start") r.points.unshift(p); else r.points.push(p);
    editChanged();
  }
}
function saveRoutes() {
  localStorage.setItem(LS.routes, JSON.stringify(ownOnly(routes)));
  syncSoon();
  drawRoutes();
  renderLists();
}
const routeType = (key) => ROUTE_TYPES_LIST.find(([k]) => k === key) || ROUTE_TYPES_LIST[0];
function addRoute(points) {
  const [key, , , mph] = routeType(state.routeType || "walking");
  const r = { id: uid(), points, routing: key, speedMph: mph, label: `R${routes.length + 1}`, color: state.color, shared: sharingOn(), ...addTo() };
  routes.push(r);
  pendingRoute = null;
  setMode(null);
  saveRoutes();
  select("route", r.id, true);
}
/** A map tap while a route's points are tapped: a new point, or (on the last one) the end. */
function routeClick(ll) {
  pendingRoute = pendingRoute || [];
  const last = pendingRoute[pendingRoute.length - 1];
  if (last && pendingRoute.length >= 2 && near(ll, last)) { finishRoute(); return; }
  pendingRoute.push(ll);
  if (pendingRoute.length >= MAX_ROUTE_POINTS) { finishRoute(); return; }
  setMode("route");
  previewRoute(ll);
}
function finishRoute() {
  if (pendingRoute && pendingRoute.length >= 2) addRoute(pendingRoute);
}
function previewRoute(ll) {
  const pts = [...pendingRoute, ll];
  map.getSource("temp").setData({ type: "FeatureCollection", features: [
    ...lineFC(pts).features,
    ...pendingRoute.map((p) => ({ type: "Feature", geometry: { type: "Point", coordinates: [p.lng, p.lat] }, properties: {} })),
  ] });
}
/** "Walking · 2.4 mi · 1 h 12 min", how many legs are straight for want of a way, and how many
 *  end off the road (dashed from the road to the point). */
function routeDetail(r) {
  const st = routeStats(r, router);
  const working = st.pendingLegs ? ` · <span class="working"><i class="spinner"></i>working out ${st.pendingLegs} leg${st.pendingLegs === 1 ? "" : "s"}</span>` : "";
  const legs = (st.straightLegs ? ` · ${st.straightLegs} straight leg${st.straightLegs === 1 ? "" : "s"}: no route found` : "") + working;
  const off = st.offRoad ? ` · ${st.offRoad} leg${st.offRoad === 1 ? " ends" : "s end"} off road (dashed)` : "";
  const much = st.avoidTooMuch ? ` · ${escapeHtml(r.avoid)} has too many points to avoid all of them` : "";
  return `${routeType(r.routing)[1]} · ${fmtLength(st.meters, state.units)} · ${durationText(st.seconds)}${legs}${off}${much}`;
}
/** Route Type and Speed of a route, as in the app. A new type comes with its own speed. */
function routeEditor(r) {
  const row = document.createElement("div");
  row.className = "route-row";
  row.innerHTML = `<span>Route Type</span><select aria-label="Route Type"></select><span>Speed</span><select aria-label="Speed"></select>`;
  const [type, speed] = row.querySelectorAll("select");
  for (const [key, name] of ROUTE_TYPES_LIST) type.add(new Option(name, key));
  type.value = routeType(r.routing)[0];
  const metric = state.units === "metric";
  for (const mph of SPEEDS) speed.add(new Option(metric ? `${Math.round(mph * 1.609344)} km/h` : `${mph} mph`, mph));
  speed.value = SPEEDS.includes(r.speedMph) ? r.speedMph : 2;
  type.onchange = () => {
    r.routing = type.value; r.speedMph = routeType(type.value)[3]; state.routeType = type.value; saveSettings();
    setRouteOptions(r, cleanOptions(r.routing, r.options));
    persist("route", true);
  };
  speed.onchange = () => { r.speedMph = +speed.value; persist("route", true); };
  return row;
}
/** A route's options as kept: none at all when there are none (the item stays as it was). */
function setRouteOptions(r, options) { if (options.length) r.options = options; else delete r.options; }
const optionsOpen = new Set();      // the routes whose Route Options are open in the list
/** Route Options of a route along roads or trails (not Straight Line): its Route Type's switches,
 *  Trail Difficulty (Walking), the group whose shapes it avoids, and which Way. Folded; the
 *  heading says what is set. */
function routeOptionsEditor(r) {
  if (!routeType(r.routing)[2]) return null;
  const keys = cleanOptions(r.routing, r.options);
  const own = ROUTE_OPTIONS[r.routing] || [];
  const alt = Number.isInteger(r.alternative) && r.alternative > 0 && r.alternative < 4 ? r.alternative : 0;
  const on = [...own.filter(([k]) => keys.includes(k)).map(([, name]) => name),
    ...DIFFICULTY.filter(([k]) => k && keys.includes(k)).map(([, name]) => `${name} Trails`),
    ...(r.avoid ? [`Avoids ${r.avoid}`] : []), ...(alt ? [ALTERNATIVES[alt]] : [])];
  const box = document.createElement("details");
  box.className = "route-options";
  box.open = optionsOpen.has(r.id);
  box.ontoggle = () => { if (box.open) optionsOpen.add(r.id); else optionsOpen.delete(r.id); };
  box.innerHTML = `<summary>Route Options <span class="hint">${on.length ? escapeHtml(on.join(", ")) : "none"}</span></summary>`;
  const set = (key, value) => {
    const next = new Set(keys);
    if (value) next.add(key); else next.delete(key);
    setRouteOptions(r, cleanOptions(r.routing, [...next]));
    persist("route", true);
  };
  for (const [key, name] of own) {
    const l = document.createElement("label");
    l.className = "check";
    l.innerHTML = `<input type="checkbox"${keys.includes(key) ? " checked" : ""}><span>${name}</span>`;
    l.querySelector("input").onchange = (e) => set(key, e.target.checked);
    box.appendChild(l);
  }
  const select = (label, options, value, onchange) => {
    const l = document.createElement("label");
    l.className = "option-row";
    l.innerHTML = `<span>${label}</span><select></select>`;
    const sel = l.querySelector("select");
    for (const [v, text] of options) sel.add(new Option(text, v));
    sel.value = value;
    sel.onchange = () => onchange(sel.value);
    box.appendChild(l);
  };
  if (r.routing === "walking") {
    select("Trail Difficulty", DIFFICULTY.map(([k, name]) => [k, name]), DIFFICULTY.find(([k]) => k && keys.includes(k))?.[0] || "", (v) => {
      setRouteOptions(r, cleanOptions(r.routing, [...keys.filter((k) => k !== "easy" && k !== "hard"), ...(v ? [v] : [])]));
      persist("route", true);
    });
  }
  // Any group but the route's own can hold the areas to avoid ("Danger Areas").
  const names = groups.filter((g) => g.name).map((g) => g.name);
  if (r.avoid && !names.includes(r.avoid)) names.push(r.avoid);
  select("Avoid Areas In", [["", "No Group"], ...names.map((n) => [n, n])], r.avoid || "", (v) => {
    if (v) r.avoid = v; else delete r.avoid;
    persist("route", true);
  });
  select("Way", ALTERNATIVES.map((name, i) => [String(i), name]), String(alt), (v) => {
    if (+v) r.alternative = +v; else delete r.alternative;
    persist("route", true);
  });
  return box;
}

/* ---------- data: shapes ---------- */
function shapesFC() {
  const feats = [];
  for (const s of shown(shapes)) {
    const measure = state.shapeMeasure !== false;
    feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [shapeRing(s)] }, properties: { id: s.id, color: s.color } });
    const c = shapeCenter(s);
    feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [c.lng, c.lat] },
      properties: { id: s.id, label: [s.label, measure ? shapeInsideText(s, state.units) : ""].filter(Boolean).join("\n") } });
    if (measure) {
      for (const [leg] of shapeLegs(s)) {
        feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[leg.a.lng, leg.a.lat], [leg.b.lng, leg.b.lat]] },
          properties: { id: s.id, leg: fmtLength(leg.metres, state.units) } });
      }
    }
  }
  return { type: "FeatureCollection", features: feats };
}
function saveShapes() {
  localStorage.setItem(LS.shapes, JSON.stringify(ownOnly(shapes)));
  syncSoon();
  if (map.getSource("shapes")) map.getSource("shapes").setData(shapesFC());
  if (routes.some((r) => r.avoid)) drawRoutes();   // a route may avoid what changed
  renderLists();
}
function addShape(s) {
  const n = shapes.filter((x) => x.type === s.type).length + 1;
  const sh = { id: uid(), label: `${{ circle: "C", rect: "R", polygon: "P" }[s.type]}${n}`, color: state.color, ...addTo(), ...s };
  sh.shared = sharingOn() && shareableShape(shapeRing(sh));
  if (sh.type === "circle") sh.unit = defaultUnit(sh.radius, state.units);
  shapes.push(sh);
  pendingShape = null;
  setMode(null);
  saveShapes();
  select("shape", sh.id, true);
}
const near = (a, b) => { const p = map.project([a.lng, a.lat]), q = map.project([b.lng, b.lat]); return Math.hypot(p.x - q.x, p.y - q.y) < 12; };
/** Handle a map click while drawing a shape. Returns true when the click was used. */
function shapeClick(e) {
  const ll = { lng: e.lngLat.lng, lat: e.lngLat.lat };
  if (mode === "circle-center") { pendingShape = { type: "circle", center: ll }; setMode("circle-edge"); return true; }
  if (mode === "circle-edge") {
    const r = inverse(pendingShape.center.lat, pendingShape.center.lng, ll.lat, ll.lng).distance;
    if (r > 0) addShape({ type: "circle", center: pendingShape.center, radius: r });
    return true;
  }
  if (mode === "rect-start") { pendingShape = { type: "rect", points: [ll] }; setMode("rect-end"); return true; }
  if (mode === "rect-end") { addShape({ type: "rect", points: [pendingShape.points[0], ll] }); return true; }
  if (mode === "polygon") {
    pendingShape = pendingShape || { type: "polygon", points: [] };
    const pts = pendingShape.points;
    if (pts.length >= 3 && near(ll, pts[0])) { finishPolygon(); return true; }
    pts.push(ll);
    setMode("polygon");
    previewShape(ll);
    return true;
  }
  return false;
}
function finishPolygon() {
  if (pendingShape && pendingShape.points.length >= 3) addShape({ type: "polygon", points: pendingShape.points });
}
/** The shape being drawn, following the pointer, in the temp source. */
function previewShape(ll) {
  const s = pendingShape;
  let shape = null;
  if (s.type === "circle") shape = { type: "circle", center: s.center, radius: inverse(s.center.lat, s.center.lng, ll.lat, ll.lng).distance };
  else if (s.type === "rect") shape = { type: "rect", points: [s.points[0], ll] };
  const feats = [];
  if (shape && (shape.type !== "circle" || shape.radius > 0)) {
    feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [shapeRing(shape)] }, properties: {} });
    $("mode-hint").textContent = shape.type === "circle"
      ? `Radius ${fmtLength(shape.radius, state.units)}. Tap to set it. You can type an exact radius later.`
      : `${shapeSummary(shape, state.units)}. Tap the opposite corner.`;
  }
  if (s.type === "polygon") {
    const pts = [...s.points, ll].map((p) => [p.lng, p.lat]);
    feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: pts }, properties: {} });
    for (const p of s.points) feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [p.lng, p.lat] }, properties: {} });
  }
  map.getSource("temp").setData({ type: "FeatureCollection", features: feats });
}

/* ---------- lists ---------- */
function select(kind, id, focus = false) {
  selected = { kind, id };
  renderLists();
  if (focus) {
    const el = document.querySelector(`[data-id="${id}"] input[type=text]`);
    if (el) { el.focus(); el.select(); }
  }
}
/** The items under a point on the map, the top one first: [{ kind, id }], each once. */
const ITEM_LAYERS = [["markers-circle", "marker"], ["arrow-ends", "arrow"], ["arrows-head", "arrow"], ["arrows-line", "arrow"],
  ["routes-vertex", "route"], ["routes-label", "route"], ["routes-line", "route"], ["routes-dashed", "route"], ["shapes-label", "shape"], ["shapes-line", "shape"]];
function itemsAt(point) {
  const box = [[point.x - 4, point.y - 4], [point.x + 4, point.y + 4]];
  const kindOf = Object.fromEntries(ITEM_LAYERS);
  const layers = ITEM_LAYERS.map(([l]) => l).filter((l) => map.getLayer(l));
  const out = [];
  for (const f of map.queryRenderedFeatures(box, { layers })) {
    const kind = kindOf[f.layer.id], id = f.properties.id;
    if (id && !out.some((x) => x.id === id) && listOf(kind).some((x) => x.id === id)) out.push({ kind, id });
  }
  return out;
}
/** An item tapped on the map: its row in the Markers tab, its group opened. */
function showItem(kind, id) {
  const it = listOf(kind).find((x) => x.id === id);
  if (!it) return;
  if (livePopup) livePopup.remove();
  const g = groupState(it.group || "");
  if (g.closed) { g.closed = false; saveGroups(); }
  if (tab !== "markers") showTab("markers");
  document.body.classList.remove("panel-collapsed");
  select(kind, id);
  const row = document.querySelector(`#group-list [data-id="${id}"]`);
  if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
/** Several items on top of each other: which one? */
function chooseItem(lngLat, hits) {
  const KIND_NAMES = { marker: "Marker", arrow: "Arrow", route: "Route" };
  const rows = hits.map(({ kind, id }, i) => {
    const it = listOf(kind).find((x) => x.id === id);
    const what = kind === "shape" ? SHAPE_NAMES[it.type] : KIND_NAMES[kind];
    return `<button type="button" class="choice" data-i="${i}"><i style="background:${it.color}"></i>${escapeHtml(it.label || what)} <small>${what}${it.group ? ` · ${escapeHtml(it.group)}` : ""}</small></button>`;
  });
  popup(lngLat, `<b>Which one?</b>${rows.join("")}`);
  for (const b of livePopup.getElement().querySelectorAll("button.choice")) {
    b.onclick = () => { const h = hits[+b.dataset.i]; showItem(h.kind, h.id); };
  }
}
/** The groups and their items: every group an item names is in the list. */
function renderLists() {
  renderLists.pending = false;
  for (const kind of KINDS) for (const it of listOf(kind)) if (it.group) groupState(it.group);
  const box = $("group-list");
  box.replaceChildren();
  const all = KINDS.flatMap((kind) => listOf(kind).map((it) => [kind, it]));
  if (!all.length && !groups.some((g) => g.name)) {
    box.innerHTML = `<div class="hint">Nothing on the map yet. Press Add to put a marker, arrow, route or shape on it. New Group keeps items together for a task, such as "Water Sources".</div>`;
  }
  for (const name of ["", ...groups.filter((g) => g.name).map((g) => g.name)]) {
    const items = all.filter(([, it]) => (it.group || "") === name);
    if (name || items.length) box.appendChild(groupSection(name, items));
  }
  renderAddGroup();
}
function groupState(name) {
  let g = groups.find((x) => x.name === name);
  if (!g) { g = { name, hidden: false, closed: false }; groups.push(g); saveGroups(); }
  return g;
}
function saveGroups() { localStorage.setItem(LS.groups, JSON.stringify(groups)); }
/** A group in the list: its name and how many items, show or hide, export, rename, delete; then its items. */
function groupSection(name, items) {
  const g = groupState(name);
  const sec = document.createElement("div");
  sec.className = "group" + (g.hidden ? " off" : "");
  const head = document.createElement("div");
  head.className = "group-head";
  const label = name || "Ungrouped";
  head.innerHTML = `<button class="twisty" type="button" aria-expanded="${!g.closed}" title="${g.closed ? "Open" : "Close"} ${escapeHtml(label)}">${g.closed ? "▸" : "▾"}</button>
    <span class="group-name">${escapeHtml(label)} <span class="hint">${items.length}</span></span>`;
  const show = toggle("", !g.hidden, (on) => { g.hidden = !on; saveGroups(); sharedChanged(); }, `Show or hide ${label} on the map and in print.`);
  show.querySelector("input").ariaLabel = `Show ${label}`;
  head.appendChild(show);
  const button = (text, title, onclick, cls = "icon") => {
    const b = Object.assign(document.createElement("button"), { type: "button", className: cls, title, textContent: text, onclick });
    b.ariaLabel = title;
    head.appendChild(b);
  };
  // Ungrouped names no group in its file (an import would make a group called "Ungrouped").
  button("⤓", `Export ${label}`, () => exportItems(items, name || null, label));
  if (name) {
    button("✎", `Rename ${label}`, () => renameGroup(name));
    button("✕", `Delete ${label}`, () => deleteGroup(name, items), "icon danger");
  }
  head.querySelector(".twisty").onclick = () => { g.closed = !g.closed; saveGroups(); renderLists(); };
  sec.appendChild(head);
  if (!g.closed) {
    for (const [kind, it] of items) sec.appendChild(itemRow(kind, it));
    if (!items.length) sec.insertAdjacentHTML("beforeend", `<div class="hint group-empty">Empty. Choose ${escapeHtml(label)} under To, then Add.</div>`);
  }
  return sec;
}
/** The To menu beside Add: the group new items go into. */
function renderAddGroup() {
  const sel = $("add-group");
  sel.replaceChildren(new Option("Ungrouped", ""), ...groups.filter((g) => g.name).map((g) => new Option(g.name, g.name)));
  sel.value = addTo().group || "";
}
$("add-group").onchange = (e) => { state.addGroup = e.target.value; saveSettings(); };
const MAX_GROUP = 30;
const cleanGroup = (s) => String(s || "").replace(/\s+/g, " ").trim().slice(0, MAX_GROUP);
$("new-group").onclick = () => {
  const name = cleanGroup(prompt("Name of the new group, for example Water Sources:"));
  if (!name) return;
  const g = groupState(name);
  g.hidden = false; g.closed = false;
  saveGroups();
  state.addGroup = name; saveSettings();     // what is added next goes into it
  renderLists();
};
function renameGroup(name) {
  const to = cleanGroup(prompt(`New name for ${name}:`, name));
  if (!to || to === name) return;
  if (groups.some((g) => g.name === to)) { alert(`There is already a group named ${to}.`); return; }
  groupState(name).name = to;
  saveGroups();
  if (state.addGroup === name) { state.addGroup = to; saveSettings(); }
  for (const kind of KINDS) {
    const moved = listOf(kind).filter((it) => it.group === name);
    for (const it of moved) it.group = to;
    if (moved.length) persist(kind);
  }
  renderLists();
}
function deleteGroup(name, items) {
  const shared = items.filter(([, it]) => it.shared).length;
  if (items.length && !confirm(`Delete ${name} and the ${items.length} item${items.length === 1 ? "" : "s"} in it?` +
    (shared ? `\n\n${shared} of them ${shared === 1 ? "is" : "are"} shared: removed from all devices.` : ""))) return;
  groups = groups.filter((g) => g.name !== name);
  saveGroups();
  for (const kind of KINDS) {
    const kept = listOf(kind).filter((it) => it.group !== name);
    if (kept.length !== listOf(kind).length) { setList(kind, kept); persist(kind); }
  }
  if (selected && items.some(([, it]) => it.id === selected.id)) selected = null;
  renderLists();
}
/** What the list says under an item's name. */
function itemDetail(kind, it) {
  if (kind === "marker") return toMGRS(it.lat, it.lng, 4);
  if (kind === "arrow") {
    const i = arrowInfo(it);
    return `${fmtDistance(i.distance)} · ${fmtBearing(i.trueBearing)} T · ${fmtBearing(i.gridBearing)} G · ${fmtBearing(i.magBearing)} M`;
  }
  if (kind === "route") return routeDetail(it);
  return `${SHAPE_NAMES[it.type]} · ${shapeSummary(it, state.units)}`;
}
/** The lists again, but not while a name is being typed in them: after it. */
function refreshLists() {
  const typing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#tab-markers input, #tab-markers select");
  if (typing) renderLists.pending = true; else renderLists();
}
/** Radius field and unit menu for a circle. The radius is kept in metres; the menu offers
 *  the units of the system chosen in Units (ft/mi or m/km). */
function radiusEditor(s) {
  const row = document.createElement("div");
  row.className = "radius-row";
  row.innerHTML = `<span>Radius</span><input type="number" min="0" step="any" inputmode="decimal" aria-label="Radius"><select aria-label="Radius unit"></select>`;
  const [input, sel] = row.querySelectorAll("input,select");
  const units = UNITS[state.units];
  if (!units.some(([n]) => n === s.unit)) s.unit = defaultUnit(s.radius, state.units);
  for (const [n] of units) sel.add(new Option(n, n));
  sel.value = s.unit;
  const show = () => { input.value = +(s.radius / unitMetres(s.unit)).toFixed(s.unit === "ft" || s.unit === "m" ? 0 : 3); };
  show();
  input.onchange = () => {
    const v = parseFloat(input.value);
    if (!(v > 0)) { show(); return; }
    s.radius = v * unitMetres(s.unit);
    persist("shape", true);
  };
  sel.onchange = () => { s.unit = sel.value; show(); persist("shape"); };
  return row;
}
/** An item's row: its kind and color, name, Show on Map and Delete; what it measures; its group
 *  and, for some kinds, more to set. */
function itemRow(kind, it) {
  const row = document.createElement("div");
  row.className = "item" + (selected && selected.kind === kind && selected.id === it.id ? " selected" : "");
  row.dataset.id = it.id;
  // Shared with the devices, or can be (a shape whose outline is too detailed for the server cannot).
  const shareable = it.shared || (sharingOn() && (kind !== "shape" || shareableShape(shapeRing(it))));
  const what = kind === "shape" ? SHAPE_NAMES[it.type] : { marker: "Marker", arrow: "Arrow", route: "Route" }[kind];
  row.innerHTML = `
    <div class="item-row">
      <button class="dot${light(it.color) ? " light" : ""}" title="${what}: Color" style="background:${it.color}">${icon(kind === "shape" ? it.type : kind)}</button>
      <input type="text" value="${escapeHtml(it.label)}" placeholder="Name" maxlength="30" aria-label="${what} Name">
      <button class="icon" title="Show on Map">◎</button>
      <button class="icon danger" title="Delete">✕</button>
    </div>
    <div class="item-detail">${itemDetail(kind, it)}${it.shared ? ` · ${escapeHtml(sharedBy(it))}` : ""}</div>
    ${shareable ? `<label class="check share"><input type="checkbox"${it.shared ? " checked" : ""}><span>Share with All Devices</span></label>` : ""}`;
  const [dot, text, go, del] = row.querySelectorAll(".item-row button, .item-row input");
  const share = row.querySelector(".share input");
  if (share) share.onchange = () => {
    if (!share.checked && !confirm("Stop sharing?\n\nIt is removed from all devices and kept in this browser.")) { share.checked = true; return; }
    it.shared = share.checked;
    persist(kind, true);
  };
  dot.onclick = (e) => { e.stopPropagation(); paletteOpenFor = paletteOpenFor === it.id ? null : it.id; renderLists(); };
  text.oninput = () => { it.label = text.value; persist(kind); };
  text.onfocus = () => { selected = { kind, id: it.id }; };
  go.onclick = () => { const c = kind === "marker" ? it : kind === "arrow" ? it.a : kind === "route" ? it.points[0] : shapeCenter(it); map.flyTo({ center: [c.lng, c.lat] }); };
  del.onclick = () => {
    if (it.shared && !confirm("Delete Shared Marker?\n\nIt is removed from all devices.")) return;
    setList(kind, listOf(kind).filter((x) => x.id !== it.id));
    if (selected && selected.id === it.id) selected = null;
    persist(kind, true);
  };
  if (kind === "shape" && it.type === "circle") row.appendChild(radiusEditor(it));
  if (kind === "route") {
    row.appendChild(routeEditor(it));
    const o = routeOptionsEditor(it);
    if (o) row.appendChild(o);
    const edit = Object.assign(document.createElement("button"), { type: "button", className: "secondary compact edit-route", textContent: editing && editing.id === it.id ? "Editing…" : "Edit Route" });
    edit.disabled = !!(editing && editing.id === it.id);
    edit.onclick = () => editRoute(it.id);
    row.appendChild(edit);
  }
  if (groups.some((g) => g.name)) row.appendChild(groupEditor(kind, it));
  if (paletteOpenFor === it.id) {
    row.appendChild(swatchRow(it.color, (hex) => { it.color = hex; paletteOpenFor = null; persist(kind, true); }));
  }
  return row;
}
/** The group an item is in, to move it to another. */
function groupEditor(kind, it) {
  const row = document.createElement("label");
  row.className = "group-row";
  row.innerHTML = `<span>Group</span><select></select>`;
  const sel = row.querySelector("select");
  sel.add(new Option("Ungrouped", ""));
  for (const g of groups) if (g.name) sel.add(new Option(g.name, g.name));
  sel.value = it.group || "";
  sel.onchange = () => {
    if (sel.value) it.group = sel.value; else delete it.group;
    groupState(sel.value).hidden = false; saveGroups();
    persist(kind, true);
  };
  return row;
}
/** True for a light color, which needs a dark icon on it. */
function light(hex) {
  const n = parseInt(String(hex).slice(1), 16);
  return ((n >> 16) & 255) * 0.299 + ((n >> 8) & 255) * 0.587 + (n & 255) * 0.114 > 160;
}
function persist(kind, rerender = false) {
  if (kind === "marker") { localStorage.setItem(LS.markers, JSON.stringify(ownOnly(markers))); map.getSource("markers").setData(markersFC()); syncSoon(); }
  else if (kind === "arrow") { localStorage.setItem(LS.arrows, JSON.stringify(ownOnly(arrows))); map.getSource("arrows").setData(arrowsFC()); syncSoon(); }
  else if (kind === "route") { localStorage.setItem(LS.routes, JSON.stringify(ownOnly(routes))); drawRoutes(); syncSoon(); }
  else { localStorage.setItem(LS.shapes, JSON.stringify(ownOnly(shapes))); map.getSource("shapes").setData(shapesFC()); syncSoon(); }
  if (kind !== "route" && routes.some((r) => r.avoid)) drawRoutes();   // a route may avoid what changed
  if (rerender) renderLists();
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------- modes and buttons ---------- */
const SHAPE_MODES = { circle: ["circle-center", "circle-edge"], rect: ["rect-start", "rect-end"], polygon: ["polygon"] };
function setMode(m) {
  mode = m;
  if (m !== "arrow-end") pendingArrow = null;
  if (m !== "route") pendingRoute = null;
  if (!m || !Object.values(SHAPE_MODES).flat().includes(m)) pendingShape = null;
  if (m !== "route-edit" && editing) { editing = null; drawRoutes(); renderLists(); }   // another tool: the edit is kept
  if (map.getSource("temp") && !pendingArrow && !pendingShape && !pendingRoute) map.getSource("temp").setData(emptyFC());
  $("add-me").classList.toggle("active", m === "me");
  map.getCanvas().style.cursor = m ? "crosshair" : "";
  // Double-click zoom would swallow the taps that place corners.
  if (m) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
  const corners = pendingShape && pendingShape.points ? pendingShape.points.length : 0;
  const stops = pendingRoute ? pendingRoute.length : 0;
  $("mode-hint").textContent =
    m === "marker" ? "Tap the map to place the marker." :
    m === "me" ? "Tap the map where you are. Your location is shared with all devices." :
    m === "arrow-start" ? "Tap the map at the start of the arrow." :
    m === "arrow-end" ? "Tap the map at the end of the arrow." :
    m === "route" ? (stops < 2 ? `Tap the points of the route in order. Points so far: ${stops}.` : `${stops} points (at most ${MAX_ROUTE_POINTS}; for more, start a second route at the last point). To finish the route, tap the last point again, press Enter or press Finish Route.`) :
    m === "circle-center" ? "Tap the center of the circle." :
    m === "circle-edge" ? "Tap the edge to set the radius. You can type an exact radius later." :
    m === "rect-start" ? "Tap one corner of the rectangle." :
    m === "rect-end" ? "Tap the opposite corner." :
    m === "polygon" ? (corners < 3 ? `Tap the corners of the polygon. Corners so far: ${corners}.` : `${corners} corners. To finish the shape, tap the first corner, press Enter or press Finish Shape.`) :
    m === "route-edit" ? editHint() : "";
  $("finish-shape").textContent = m === "route" ? "Finish Route" : m === "route-edit" ? "Done" : "Finish Shape";
  $("finish-shape").hidden = !((m === "polygon" && corners >= 3) || (m === "route" && stops >= 2) || m === "route-edit");
  const er = m === "route-edit" ? editedRoute() : null;
  for (const [id, tool] of [["route-add-start", "start"], ["route-add-end", "end"], ["route-remove", "remove"]]) {
    $(id).hidden = !er;
    $(id).classList.toggle("active", !!er && editing.tool === tool);
  }
  $("route-undo").hidden = !er;
  $("route-undo").disabled = !er || !editing.history.length;
  // While a route is edited the map pans as usual; crosshairs only while adding points.
  if (m === "route-edit") map.getCanvas().style.cursor = editing && (editing.tool === "start" || editing.tool === "end") ? "crosshair" : "";
  $("mode-bar").hidden = !m;
}
function editHint() {
  const r = editedRoute();
  if (!r) return "";
  const n = r.points.length;
  const full = n >= MAX_ROUTE_POINTS ? " The route is full." : "";
  const tool = editing.tool;
  return tool === "start" ? `Tap the map to add points before the start of ${r.label || "the route"}. ${n} of ${MAX_ROUTE_POINTS} points.${full}` :
    tool === "end" ? `Tap the map to add points after the end of ${r.label || "the route"}. ${n} of ${MAX_ROUTE_POINTS} points.${full}` :
    tool === "remove" ? `Tap the points to remove (a route keeps two). ${n} of ${MAX_ROUTE_POINTS} points.` :
    `Editing ${r.label || "the route"}: drag a point to move it, or drag the small dot on a leg to add a point there. ${n} of ${MAX_ROUTE_POINTS} points.${full}`;
}
/** An entry of the Add menu: its tool, with the map in view (a phone's panel folds away). */
function startTool(m) {
  openAddMenu(false);
  setMode(m);
  if (window.innerWidth <= 640) document.body.classList.add("panel-collapsed");
}
$("add-marker").onclick = () => startTool("marker");
$("mode-cancel").onclick = () => (mode === "route-edit" ? editEnd(false) : setMode(null));
$("route-add-start").onclick = () => editTool("start");
$("route-add-end").onclick = () => editTool("end");
$("route-remove").onclick = () => editTool("remove");
$("route-undo").onclick = editUndo;
$("add-me").onclick = () => {
  if (mode === "me") { setMode(null); return; }
  if (!cleanText($("display-name").value, MAX_NAME)) { askName(); return; }
  setMode("me");
};
$("add-arrow").onclick = () => startTool("arrow-start");
$("add-route").onclick = () => startTool("route");
for (const [type, modes] of Object.entries(SHAPE_MODES)) $(`add-${type}`).onclick = () => startTool(modes[0]);
$("finish-shape").onclick = () => (mode === "route" ? finishRoute() : mode === "route-edit" ? editEnd(true) : finishPolygon());
// Only what is kept in this browser: shared items stay (each has its own Delete).
// Everything in every group; the warning says how many are shared (those go from all devices too).
$("clear-all").onclick = () => {
  const all = KINDS.flatMap((kind) => listOf(kind));
  if (!all.length) { fileHint("Nothing to clear: there are no markers, arrows, routes or shapes."); return; }
  const shared = all.filter((x) => x.shared).length;
  if (!confirm(`Delete all ${all.length} item${all.length === 1 ? "" : "s"} (markers, arrows, routes and shapes) in every group?` +
    (shared ? `\n\n${shared === all.length ? (shared === 1 ? "It is" : "All of them are") : `${shared} of them ${shared === 1 ? "is" : "are"}`} shared: removed from all devices too.` : ""))) return;
  for (const kind of KINDS) setList(kind, []);
  selected = null;
  saveMarkers(); saveArrows(); saveRoutes(); saveShapes();
};
// Save to a file, and load one: GeoJSON (src/drawings.js).
// Every item carries its group; a group's file also names the group, so that it comes back as one.
$("save-drawings").onclick = () => exportItems(KINDS.flatMap((kind) => listOf(kind).map((it) => [kind, it])));
/** Saves [kind, item] pairs to a file: one group's (group: its name) or all of them; title names
 *  the file and the messages. */
function exportItems(items, group = null, title = group) {
  const of = (kind) => items.filter(([k]) => k === kind).map(([, it]) => it);
  const [m, a, r, s] = KINDS.map(of);
  if (!items.length) { fileHint(title ? `Nothing to export: ${title} is empty.` : "Nothing to export. Add a marker, arrow, route or shape first."); return; }
  const data = toGeoJSON(m, a, s, { title: state.title || "", saved: new Date().toISOString(), ...(group ? { group } : {}) }, r);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/geo+json" }));
  const link = Object.assign(document.createElement("a"), { href: url, download: fileName(title || state.title) });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  fileHint(`Exported ${countText(m.length, a.length, s.length, r.length)} to ${link.download}.`);
}
$("load-drawings").onclick = () => $("load-file").click();
$("load-file").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";                 // the same file can be chosen again
  if (!file) return;
  let got;
  try {
    if (file.size > 20 * 1024 * 1024) throw new Error("This file is larger than 20 MB and cannot be imported.");
    got = fromGeoJSON(await file.text(), state.color);
  } catch (err) { fileHint(err.message, true); return; }
  const total = got.markers.length + got.arrows.length + got.shapes.length + got.routes.length;
  if (!total) { fileHint(`Nothing to import from ${file.name}.${got.skipped ? ` It has ${got.skipped} item${got.skipped === 1 ? "" : "s"} that this page cannot draw.` : ""}`, true); return; }
  // A file from other software is a group of its own, named after the file; a file of this page's
  // keeps its items' groups. The groups that get items are shown.
  const own = (it) => (got.own ? it : { ...it, group: it.group || cleanGroup(file.name.replace(/\.[^.]*$/, "").replace(/[-_ ]?\d{4}-\d{2}-\d{2}$/, "").replace(/[-_]+/g, " ")) });
  for (const it of [...got.markers, ...got.arrows, ...got.routes, ...got.shapes]) Object.assign(it, own(it));
  for (const it of [...got.markers, ...got.arrows, ...got.routes, ...got.shapes]) { if (!it.group) delete it.group; groupState(it.group || "").hidden = false; }
  saveGroups();
  for (const m of got.markers) markers.push({ ...m, id: uid() });
  for (const a of got.arrows) arrows.push({ ...a, id: uid() });
  for (const r of got.routes) routes.push({ ...r, id: uid() });
  for (const s of got.shapes) {
    const sh = { ...s, id: uid() };
    if (sh.type === "circle" && !sh.unit) sh.unit = defaultUnit(sh.radius, state.units);
    if (sh.unit === undefined) delete sh.unit;
    shapes.push(sh);
  }
  saveMarkers(); saveArrows(); saveRoutes(); saveShapes();
  // Show what was loaded.
  const pts = [...got.markers, ...got.arrows.flatMap((a) => [a.a, a.b]), ...got.routes.flatMap((r) => r.points),
    ...got.shapes.flatMap((s) => shapeRing(s).map(([lng, lat]) => ({ lng, lat })))];
  // A loop, not Math.min(...list): a detailed outline can have more points than a call takes.
  const b = pts.reduce((r, p) => [Math.min(r[0], p.lng), Math.min(r[1], p.lat), Math.max(r[2], p.lng), Math.max(r[3], p.lat)],
    [Infinity, Infinity, -Infinity, -Infinity]);
  map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 80, maxZoom: 15, duration: 0 });
  fileHint(`Imported ${countText(got.markers.length, got.arrows.length, got.shapes.length, got.routes.length)} from ${file.name}` +
    (got.skipped ? `. Skipped ${got.skipped} item${got.skipped === 1 ? "" : "s"}: only points, lines of up to ${MAX_ROUTE_POINTS} points and areas are supported.` : "."));
};
function countText(m, a, s, r = 0) {
  const part = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
  return [m && part(m, "marker"), a && part(a, "arrow"), r && part(r, "route"), s && part(s, "shape")].filter(Boolean).join(", ");
}
const FILE_HINT = $("file-hint").textContent;
function fileHint(text, error = false) {
  const el = $("file-hint");
  el.textContent = text;
  el.classList.toggle("error", error);
  clearTimeout(fileHint.timer);
  fileHint.timer = setTimeout(() => { el.textContent = FILE_HINT; el.classList.remove("error"); }, 12000);
}
$("units").onchange = (e) => { state.units = e.target.value; saveSettings(); saveShapes(); renderLists(); if (terrain) terrain.refresh(); };
$("shape-measure").onchange = (e) => { state.shapeMeasure = e.target.checked; saveSettings(); saveShapes(); };
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
  if (e.key === "Escape") { if (mode === "route-edit") editEnd(false); else setMode(null); openAddMenu(false); if (terrain) terrain.cancelPick(); }
  if (e.key === "Enter" && (mode === "polygon" || mode === "route" || mode === "route-edit") && !typing) $("finish-shape").onclick();
  if (mode === "route-edit" && !typing && e.key === "z" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); editUndo(); }
});

// New-item colour swatches
const newColorBox = $("new-color");
function renderNewColor() {
  newColorBox.innerHTML = "";
  newColorBox.appendChild(swatchRow(state.color, (hex) => { state.color = hex; saveSettings(); renderNewColor(); }));
}
renderNewColor();

scaleSel.onchange = () => { state.scale = +scaleSel.value; saveSettings(); updateFootprint(); };
// Switch the map style in place: the sheet outline, markers and arrows are carried over.
const OWN_SOURCES = ["grid", "footprint", "shapes", "arrows", "routes", "markers", "live", "people", "me", "temp", ...TERRAIN_SOURCES];
function redraw(diff = true) {
  map.setStyle(drawnStyle(), {
    diff,
    transformStyle: (prev, next) => !prev ? next : {
      ...next,
      sources: { ...next.sources, ...Object.fromEntries(OWN_SOURCES.filter((k) => prev.sources[k]).map((k) => [k, prev.sources[k]])) },
      layers: [...next.layers, ...prev.layers.filter((l) => OWN_SOURCES.includes(l.source))],
    },
  });
}
styleSel.onchange = async () => {
  STYLE = state.style = styleSel.value; saveSettings();
  styleUrl = styleUrlFor(STYLE);
  rawStyle = await fetchStyle(styleUrl);
  redraw(false);
  renderLayerPanel();
  updateFootprint();
};

/* ---------- layer switches ---------- */
function setLayers(patch) { Object.assign(state.layers, patch); saveSettings(); redraw(); renderLayerPanel(); if (terrain) terrain.refresh(); }
function segmented(label, key, options) {
  const row = document.createElement("div");
  row.className = "seg-row";
  row.innerHTML = `<span>${label}</span><div class="seg"></div>`;
  for (const [value, text] of options) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = text;
    b.className = state.layers[key] === value ? "on" : "";
    b.setAttribute("aria-pressed", state.layers[key] === value);
    b.onclick = () => setLayers({ [key]: value });
    row.lastChild.appendChild(b);
  }
  return row;
}
function toggle(text, checked, onchange, title) {
  const l = document.createElement("label");
  l.className = "switch";
  if (title) l.title = title;
  l.innerHTML = `<span>${text}</span><input type="checkbox" role="switch">`;
  const box = l.querySelector("input");
  box.checked = checked;
  box.onchange = () => onchange(box.checked);
  return l;
}
function renderLayerPanel() {
  const box = $("layer-controls");
  box.innerHTML = "";
  const have = groupsOf(rawStyle);
  box.appendChild(segmented("Ink", "ink", [["colour", "Color"], ["bw", "B&W"]]));
  if (have.has("hillshade")) box.appendChild(segmented("Hillshade", "hillshade", [["off", "Off"], ["soft", "Soft"], ["sharp", "Sharp"]]));
  if (have.has("contours")) box.appendChild(segmented("Contours", "contours", [["off", "Off"], ["index", "Index"], ["all", "All"]]));
  const off = new Set(state.layers.off);
  const sections = SECTIONS.map(([title, groups]) => [title, groups.filter(([g]) => have.has(g))]);
  const other = [...have].filter((g) => !OWN_CONTROLS.has(g) && !SECTIONS.some(([, gs]) => gs.some(([x]) => x === g)));
  if (other.length) sections.push(["Other", other.map((g) => [g, g.replace(/-/g, " ")])]);
  for (const [title, groups] of sections) {
    if (!groups.length) continue;
    const h = document.createElement("h3"); h.textContent = title; box.appendChild(h);
    for (const [g, text] of groups) {
      box.appendChild(toggle(text, !off.has(g), (on) => {
        const s = new Set(state.layers.off); on ? s.delete(g) : s.add(g);
        setLayers({ off: [...s] });
      }));
    }
  }
  const h = document.createElement("h3"); h.textContent = "Grid"; box.appendChild(h);
  box.appendChild(toggle("MGRS Grid Lines", state.layers.grid !== false, (on) => { state.layers.grid = on; saveSettings(); drawGrid(); }, "On screen and in print. The grid numbers at the edges always print."));
  const hidden = state.layers.off.filter((g) => have.has(g)).length;
  $("layers-summary").textContent = hidden ? `${hidden} hidden` : "";
}
$("layers-reset").onclick = () => { state.layers = { ...LAYER_DEFAULTS }; saveSettings(); redraw(); renderLayerPanel(); renderLiveSwitches(); renderTerrainSwitch(); drawLive(); drawGrid(); if (terrain) terrain.refresh(); };
/** The Analysis Overlay switch, at the top of the Terrain Analysis view. */
function renderTerrainSwitch() {
  if (!terrain || !terrain.available) return;
  $("terrain-overlay-switch").replaceChildren(toggle("Analysis Overlay", state.layers.terrain !== false, (on) => {
    state.layers.terrain = on; saveSettings(); terrain.refresh(); updateFootprint();
  }, "The viewshed or radio coverage overlay and its points, on screen and in print."));
}
renderLayerPanel();
paperSel.onchange = () => { state.paper = paperSel.value; saveSettings(); updateFootprint(); };
$("orientation").onchange = (e) => { state.orientation = e.target.value; saveSettings(); updateFootprint(); };
atlasSel.onchange = () => { state.atlas = +atlasSel.value; saveSettings(); updateFootprint(); };
$("binder").onchange = (e) => { state.binder = e.target.checked; saveSettings(); updateFootprint(); };
$("dpi").onchange = (e) => { state.dpi = +e.target.value; saveSettings(); updateFootprint(); };
$("title").oninput = (e) => { state.title = e.target.value; saveSettings(); };
$("list-page").onchange = (e) => { state.listPage = e.target.checked; saveSettings(); };
$("bring").onclick = () => { footprintCenter = visibleCenter(); updateFootprint(); };
$("fit").onclick = () => { const { bounds } = footprintPolygon(); map.fitBounds(bounds, { padding: panelPadding(), duration: 400 }); };
$("panel-toggle").onclick = () => document.body.classList.toggle("panel-collapsed");

// The Terrain tab is there only when the server has elevation data.
const terrainTab = document.querySelector('.tabs [data-tab="terrain"]');
terrainReady.then((t) => { terrainTab.hidden = !t.available; });

/* ---------- the tabs ---------- */
const TABS = [...document.querySelectorAll(".tabs [data-tab]")];
function showTab(name) {
  const was = tab;
  tab = name;
  // The page opens again on the tab it was on; on Terrain only once that tab is there.
  if (name !== "terrain") { state.tab = name; saveSettings(); }
  for (const b of TABS) b.ariaSelected = String(b.dataset.tab === name);
  for (const key of ["layers", "markers", "print", "terrain"]) $(`tab-${key}`).hidden = key !== name;
  document.body.classList.remove("panel-collapsed");
  showSheet(was !== "print" && name === "print");
  if (name === "terrain" && terrain) terrain.onShow();
}
for (const b of TABS) b.onclick = () => showTab(b.dataset.tab);

/** The sheet's outline: on the map only on the Print tab. Opening the tab with the sheet out of
 *  view brings it to the middle of what can be seen. */
function showSheet(opening = false) {
  if (!map.getLayer("footprint-fill")) return;
  const on = tab === "print";
  for (const id of ["footprint-fill", "footprint-line", "footprint-label"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  if (opening && footprintCenter && !map.getBounds().contains([footprintCenter.lng, footprintCenter.lat])) {
    footprintCenter = visibleCenter();
    updateFootprint();
  }
}

/* ---------- what the devices share ---------- */
const people = new Map();            // uid -> name, from locations and messages
let live = null;
let livePopup = null;
const SHARED_LAYERS = ["live-area", "live-halo", "live-line", "live-dashed", "live-vertex", "live-point", "live-label"];
const PEOPLE_LAYERS = ["people-symbol", "people-name"];

// The devices' symbols, made again whenever a style change has dropped them.
map.on("styleimagemissing", (e) => {
  const fill = PERSON_COLORS[e.id];
  const ratio = Math.max(1, Math.round(window.devicePixelRatio || 1));
  if (fill && !map.hasImage(e.id)) map.addImage(e.id, personImage(fill, ratio), { pixelRatio: ratio });
  if (e.id === CHEVRON && !map.hasImage(e.id)) map.addImage(e.id, chevronImage(ratio), { pixelRatio: ratio, sdf: true });
});

/** The live layers as they are now; also every few seconds, so that locations age between answers.
 *  Shared markers, arrows, routes and areas are in the map's own lists; this layer has only what
 *  the lists cannot hold. */
function drawLive() {
  if (!map.getSource("live")) return;
  const st = live && live.status === "on" ? live.state : null;
  const info = (live && live.info) || {};
  try {
    map.getSource("live").setData(st ? sharedFC([...st.markers.values()].filter((f) => !itemFromFeature(f)), headLength) : emptyFC());
    const others = st ? st.positions.filter((p) => p.uid !== live.uid) : [];
    map.getSource("people").setData(st ? peopleFC(others, +info.interval || 5, +info.stale || 120, (Date.now() - st.at) / 1000) : emptyFC());
    for (const id of SHARED_LAYERS) map.setLayoutProperty(id, "visibility", state.layers.shared !== false ? "visible" : "none");
    for (const id of PEOPLE_LAYERS) map.setLayoutProperty(id, "visibility", state.layers.people !== false ? "visible" : "none");
  } catch { /* a new map style is still loading: drawn again in a few seconds */ }
}
setInterval(drawLive, 5000);

function renderLiveSwitches() {
  const box = $("live-switches");
  box.replaceChildren(
    toggle("Device Locations", state.layers.people !== false, (on) => { state.layers.people = on; saveSettings(); drawLive(); }, "The devices sharing their location now. On screen only."),
    toggle("Shared Markers", state.layers.shared !== false, (on) => {
      state.layers.shared = on; saveSettings(); drawLive(); sharedChanged();
    }, "Markers, arrows, routes and shapes shared with all devices, on screen and in print."),
  );
  liveSwitchesUsable();
}
/** The Shared switches do nothing while sharing is off on the server: grayed out until it is on. */
function liveSwitchesUsable() {
  for (const box of $("live-switches").querySelectorAll("input")) box.disabled = !sharingOn();
}

const when = (iso) => {
  const t = new Date(iso);
  if (isNaN(t)) return "";
  return t.toDateString() === new Date().toDateString() ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : t.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
};
const nameOf = (uid) => (live && uid === live.uid ? "You" : people.get(uid) || "Unknown");
/** "Shared · NAME · time": who changed a shared item last, and when. */
const sharedBy = (it) => ["Shared", it.by && nameOf(it.by), it.updated && when(it.updated)].filter(Boolean).join(" · ");

/** A route, an area or a device under the pointer: its details in a popup. True when there was one. */
function liveClick(e) {
  const hits = map.queryRenderedFeatures(e.point, { layers: ["me-symbol", ...PEOPLE_LAYERS, ...SHARED_LAYERS].filter((id) => map.getLayer(id)) });
  if (!hits.length) return false;
  const h = hits[0];
  if (h.layer.id === "me-symbol") {
    const m = state.myLocation;
    popup([m.lng, m.lat], `<b>${escapeHtml(cleanText($("display-name").value, MAX_NAME))}</b><span>${toMGRS(m.lat, m.lng, 5)}</span><span>My Location, placed by hand. Drag it to move it.</span>`);
  } else if (PEOPLE_LAYERS.includes(h.layer.id)) showPerson(h.properties.uid);
  else showShared(h.properties.id);
  return true;
}
function popup(lngLat, html) {
  if (livePopup) livePopup.remove();
  livePopup = new maplibregl.Popup({ maxWidth: "260px" }).setLngLat(lngLat).setHTML(`<div class="live-popup">${html}</div>`).addTo(map);
}
function showPerson(uid) {
  const st = live && live.state;
  const p = st && st.positions.find((x) => x.uid === uid);
  if (!p) return;
  const age = (+p.age || 0) + (Date.now() - st.at) / 1000;
  const metric = state.units === "metric";
  const lines = [`<b>${escapeHtml(p.name)}</b>`, `<span>${toMGRS(p.lat, p.lon, 5)}</span>`, `<span>Location: ${ageText(age)}</span>`];
  if (Number.isFinite(p.speed) && p.speed > 0.5) lines.push(`<span>Speed: ${metric ? Math.round(p.speed * 3.6) + " km/h" : Math.round(p.speed * 2.23694) + " mph"}${Number.isFinite(p.course) ? " · " + fmtBearing(p.course) + " T" : ""}</span>`);
  if (Number.isFinite(p.acc)) lines.push(`<span>Accuracy: ±${metric ? Math.round(p.acc) + " m" : Math.round(p.acc * 3.28084) + " ft"}</span>`);
  popup([p.lon, p.lat], lines.join(""));
}
function sharedDetail(f, d) {
  const p = f.properties || {};
  return escapeHtml([kindText(d), sharedBy({ by: p.by, updated: p.updated })].join(" · "));
}
function showShared(id) {
  const f = live && live.state && live.state.markers.get(String(id));
  const d = f && describe(f);
  if (!d) return;
  const [lon, lat] = d.points[0];
  const lines = [`<b>${escapeHtml(d.label || "Unnamed")}</b>`, `<span>${sharedDetail(f, d)}</span>`, `<span>${toMGRS(lat, lon, 5)}</span>`];
  if (d.kind === "route") {
    let m = 0;
    for (let i = 1; i < d.points.length; i++) m += inverse(d.points[i - 1][1], d.points[i - 1][0], d.points[i][1], d.points[i][0]).distance;
    lines.push(`<span>${d.points.length} points · ${fmtLength(m, state.units)}${d.routing !== "straight" ? " in straight legs" : ""}</span>`);
  }
  popup([lon, lat], lines.join(""));
}

/* ---------- shared items: in the map's lists, kept in step with the server ---------- */
// A change to a shared marker, arrow, route or shape (added, moved, renamed, recolored, shared,
// deleted, no longer shared) is sent shortly after it is made; what the server sends is taken in unless a change
// of ours to that item is still on its way, or the server's answers do not have it yet.
const sentAs = new Map();   // id -> itemKey as the server has it (or as it was last sent)
const holds = new Map();    // id -> the rev the server's state must reach before its copy counts again (Infinity: on its way)
const sharingOn = () => !!(live && live.status === "on");
let syncTimer = null, syncing = false, syncAgain = false;
function syncSoon(ms = 700) { clearTimeout(syncTimer); syncTimer = setTimeout(syncOut, ms); }

/** PUT (feature) or DELETE (null) of one shared item. true, or the status code (0: no answer). */
async function shareRequest(id, feature) {
  holds.set(id, Infinity);
  const opts = feature ? { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(feature) } : { method: "DELETE" };
  let code = 0, body = null;
  try {
    const r = await fetch(`${BASE}markers/${encodeURIComponent(id)}`, opts);
    code = r.status;
    try { body = await r.json(); } catch { /* no JSON */ }
  } catch { /* no answer */ }
  if (code === 200 && body && Number.isFinite(body.rev)) { holds.set(id, body.rev); return true; }
  holds.delete(id);
  if (body && body.error) console.warn("shared marker:", code, body.error);
  return code;
}

async function syncOut() {
  if (!sharingOn()) return;
  if (syncing) { syncAgain = true; return; }
  syncing = true;
  let retry = false;
  try {
    const mine = new Map();
    for (const kind of KINDS) for (const it of listOf(kind)) if (it.shared) mine.set(it.id, [kind, it]);
    for (const [id, [kind, it]] of mine) {
      const key = itemKey(it);
      if (sentAs.get(id) === key) continue;
      sentAs.set(id, key);
      const f = featureFromItem(it, kind, live.uid, new Date(), kind === "shape" ? shapeRing(it) : null);
      it.by = live.uid; it.updated = f.properties.updated;
      const r = await shareRequest(id, f);
      if (r === true) continue;
      sentAs.delete(id);
      if (r === 400 || r === 413 || r === 507) {
        // The server will not keep it: it stays in this browser, not shared.
        it.shared = false;
        persist(kind, true);
        fileHint(r === 507 ? "The server cannot store more shared markers. The item is kept in this browser." : "The server did not accept this item. It is kept in this browser.", true);
      } else retry = true;
    }
    for (const id of [...sentAs.keys()]) {
      if (mine.has(id)) continue;
      const key = sentAs.get(id);
      sentAs.delete(id);
      const r = await shareRequest(id, null);
      if (r !== true) { sentAs.set(id, key); retry = true; }
    }
  } finally {
    syncing = false;
  }
  if (syncAgain) { syncAgain = false; syncSoon(); } else if (retry) syncSoon(5000);
}

/** What the server holds now, into the lists. */
function syncIn(st) {
  let changed = false;
  const there = new Set();
  for (const f of st.markers.values()) {
    const got = itemFromFeature(f);
    if (!got) continue;
    const id = got.item.id;
    there.add(id);
    const hold = holds.get(id);
    if (hold !== undefined) { if (hold === Infinity || st.rev < hold) continue; holds.delete(id); }
    const list = listOf(got.kind);
    const i = list.findIndex((x) => x.id === id);
    if (i >= 0 && !list[i].shared) continue;          // this browser's own copy, no longer shared
    if (i >= 0 && itemKey(list[i]) !== sentAs.get(id)) continue;   // a change of ours not sent yet
    const key = itemKey(got.item);
    sentAs.set(id, key);
    if (i >= 0 && itemKey(list[i]) === key && list[i].updated === got.item.updated) continue;
    // Changed in place: a list row being typed in keeps the same item.
    if (i >= 0) { for (const k of ["lng", "lat", "a", "b", "type", "center", "radius", "points", "group", "options", "avoid", "alternative"]) delete list[i][k]; Object.assign(list[i], got.item); } else list.push(got.item);
    changed = true;
  }
  // Removed on the server: gone here too (one not sent yet, or on its way, stays).
  for (const kind of KINDS) {
    const list = listOf(kind);
    const keepIt = (x) => !x.shared || there.has(x.id) || !sentAs.has(x.id) || holds.has(x.id);
    const kept = list.filter(keepIt);
    if (kept.length === list.length) continue;
    for (const x of list) if (!keepIt(x)) sentAs.delete(x.id);
    setList(kind, kept);
    changed = true;
  }
  if (changed) sharedChanged();
}
/** Sharing ended (switched off on the server): the shared items are the server's, so they go. */
function dropShared() {
  if (!KINDS.some((kind) => listOf(kind).some((x) => x.shared))) return;
  for (const kind of KINDS) setList(kind, ownOnly(listOf(kind)));
  sentAs.clear(); holds.clear();
  sharedChanged();
}
function sharedChanged() {
  if (map.getSource("markers")) {
    map.getSource("markers").setData(markersFC()); map.getSource("arrows").setData(arrowsFC()); map.getSource("shapes").setData(shapesFC());
  }
  drawRoutes();
  refreshLists();
}
$("tab-markers").addEventListener("focusout", () => setTimeout(() => { if (renderLists.pending && !document.activeElement.closest("#tab-markers input, #tab-markers select")) renderLists(); }, 0));

/* ---------- messages ---------- */
let openConversation = null;          // the partner shown, or null
let picking = false;                  // choosing whom a new message is for
let shownCount = 0;                   // messages in the open conversation when it was last drawn
function renderMessages() {
  const on = live && live.status === "on" && live.info && live.info.messages;
  // A server from before messages has locations and markers only.
  $("messages-status").textContent = !live || on ? "" : live.status === "on" ? "This server has no messages. Update it to send and receive messages." : live.text;
  const convs = live ? conversations(live.messages.list, live.uid, nameOf) : [];
  const unread = convs.reduce((n, c) => n + c.unread, 0);
  markUnread(unread);
  document.title = `${unread ? `(${unread}) ` : ""}${view === "messages" ? "Messages" : "Map"}`;
  if (view !== "messages") return;
  $("conversations-view").hidden = !!openConversation || picking;
  $("pick-view").hidden = !picking;
  $("conversation").hidden = !openConversation;
  if (picking) {
    const st = live && live.state;
    const list = st ? st.positions.filter((p) => p.uid !== live.uid).sort((a, b) => a.name.localeCompare(b.name)) : [];
    $("pick-list").replaceChildren(...(list.length ? list.map((p) => {
      const b = document.createElement("button");
      b.className = "conv-row";
      b.innerHTML = `<b>${escapeHtml(p.name)}</b><span></span><small>Location: ${ageText((+p.age || 0) + (Date.now() - st.at) / 1000)}</small>`;
      b.onclick = () => { picking = false; openConversation = p.uid; shownCount = -1; renderMessages(); $("message-text").focus(); };
      return b;
    }) : [Object.assign(document.createElement("div"), { className: "hint", textContent: "No active devices." })]));
    return;
  }
  if (!openConversation) {
    $("conversations").replaceChildren(...convs.map((c) => {
      const b = document.createElement("button");
      b.className = "conv-row";
      b.innerHTML = `<b>${escapeHtml(c.name || "Unknown")}</b>${c.unread ? `<span class="badge">${c.unread}</span>` : "<span></span>"}
        <small>${c.last ? escapeHtml(`${c.last.mine ? "You" : c.last.name}: ${c.last.text}`) : "No messages."}</small>`;
      b.onclick = () => { openConversation = c.partner; shownCount = -1; renderMessages(); };
      return b;
    }));
    return;
  }
  const c = convs.find((x) => x.partner === openConversation) || { partner: openConversation, name: nameOf(openConversation) };
  $("conversation-title").textContent = c.name;
  const list = live.messages.list.filter((m) => partnerOf(m, live.uid) === openConversation);
  $("conversation-list").replaceChildren(...(list.length ? list.map((m) => {
    const d = document.createElement("div");
    d.className = "msg" + (m.mine ? " mine" : "");
    d.innerHTML = `<small>${escapeHtml(m.mine ? "You" : m.name)} · ${when(m.time)}</small>${escapeHtml(m.text)}<small>${statusText(m)}</small>`;
    return d;
  }) : [Object.assign(document.createElement("div"), { className: "hint", textContent: "No messages." })]));
  // A new message, or the conversation just opened: the newest is in view.
  if (list.length !== shownCount) { shownCount = list.length; $("messages-page").scrollTop = $("messages-page").scrollHeight; }
  live.markRead(openConversation);
}
/** The unread count on the header's Messages link (the header is drawn again now and then). */
function markUnread(n) {
  const a = document.querySelector('#fm-bar a[data-page="messages"]');
  if (!a) return;
  let b = a.querySelector(".badge");
  if (!b) { b = Object.assign(document.createElement("span"), { className: "badge" }); a.append(b); }
  b.hidden = !n;
  b.textContent = String(n);
}
window.addEventListener("fm-nav", () => renderMessages());

/** Map or Messages, switched in place: the header marks it, the address keeps it. */
function showView(name) {
  if (name === view && $("messages-page").hidden === (name !== "messages")) return;
  view = name;
  setMode(null);
  document.body.classList.toggle("view-messages", name === "messages");
  $("messages-page").hidden = name !== "messages";
  const bar = $("fm-bar");
  bar.dataset.page = name;
  if (window.fmNavRedraw) window.fmNavRedraw();
  history.replaceState(null, "", `${location.pathname}${name === "messages" ? "?messages" : ""}${location.hash}`);
  if (name === "map") map.resize();
  shownCount = -1;
  renderMessages();
}
// The header's Map and Messages links switch the view here, without loading the page again.
$("fm-bar").addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("a[data-page]");
  if (!a || (a.dataset.page !== "map" && a.dataset.page !== "messages") || e.ctrlKey || e.metaKey || e.shiftKey) return;
  e.preventDefault();
  showView(a.dataset.page);
});

$("conversation-back").onclick = () => { openConversation = null; renderMessages(); };
$("pick-back").onclick = () => { picking = false; renderMessages(); };
$("new-message").onclick = () => { picking = true; renderMessages(); };
$("display-name").value = state.displayName || "";
$("display-name").onchange = () => {
  state.displayName = cleanText($("display-name").value, MAX_NAME);
  $("display-name").value = state.displayName;
  saveSettings();
  if (state.displayName) $("name-hint").textContent = "";
  // The location placed by hand goes on under the new name; without a name it stops.
  if (state.myLocation) { if (state.displayName) placeMe(state.myLocation); else stopMe(); }
};
/** Asks for the Display Name: messages and My Location need it. */
function askName() {
  showView("messages");
  $("name-hint").textContent = "Enter a Display Name first.";
  $("display-name").focus();
}

/* ---------- My Location, placed by hand ---------- */
function placeMe(ll) {
  const name = cleanText($("display-name").value, MAX_NAME);
  if (!name) { askName(); return; }
  state.myLocation = { lng: ll.lng, lat: ll.lat };
  saveSettings();
  if (live) live.setLocation({ lat: ll.lat, lon: ll.lng }, name);
  if (mode === "me") setMode(null);
  drawMe();
}
function stopMe() {
  state.myLocation = null;
  saveSettings();
  if (live) live.setLocation(null);
  drawMe();
}
function drawMe() {
  const m = sharingOn() && state.myLocation;
  if (map.getSource("me")) {
    try {
      map.getSource("me").setData(m ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [m.lng, m.lat] },
        properties: { name: cleanText($("display-name").value, MAX_NAME) } }] } : emptyFC());
    } catch { /* a new map style is still loading */ }
  }
  $("my-location").hidden = !sharingOn();
  $("my-location-text").textContent = m ? `My Location: ${toMGRS(m.lat, m.lng, 5)}` : "My Location: Not Shared. Set it with Set My Location in the map's Layers tab.";
  $("my-location-stop").hidden = !m;
}
$("my-location-stop").onclick = stopMe;
function sendMessage() {
  const box = $("message-text");
  if (!live || !openConversation || !cleanText(box.value)) return;
  const name = cleanText($("display-name").value, MAX_NAME);
  if (!name) { askName(); return; }
  state.displayName = name; saveSettings();
  if (live.send(openConversation, box.value, name)) box.value = "";
  box.focus();
}
$("message-send").onclick = sendMessage;
$("message-text").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendMessage(); } });

/** Everything that shows what the server holds, after each answer. */
function onLive(l) {
  live = l;
  const st = l.status === "on" ? l.state : null;
  if (st) for (const p of st.positions) people.set(p.uid, p.name);
  for (const m of l.messages.list) if (!m.mine) people.set(m.from, m.name);
  // No sharing service on this server: none of it is shown.
  const there = l.status !== "absent" && l.status !== "starting";
  $("live-layers").hidden = !there;
  $("live-status").textContent = l.status === "on" ? "" : l.text;
  liveSwitchesUsable();
  if (st) { syncIn(st); syncSoon(); }
  else if (l.status === "disabled" || l.status === "absent") dropShared();
  // Sharing came or went: the lists offer Share with All Devices only while it is on.
  if (sharingOn() !== onLive.sharing) {
    onLive.sharing = sharingOn();
    sharedChanged();
    $("add-me").hidden = !onLive.sharing;
    if (!onLive.sharing && mode === "me") setMode(null);
    drawMe();
  }
  drawLive();
  renderMessages();
}
renderLiveSwitches();
showTab(tab);
if (view === "messages") { view = "map"; showView("messages"); }
$("add-me").hidden = true;            // until the server says location sharing is enabled
live = startLive(onLive);
if (state.myLocation && cleanText(state.displayName, MAX_NAME)) live.setLocation({ lat: state.myLocation.lat, lon: state.myLocation.lng }, state.displayName);

/* ---------- Tile URLs for other apps ---------- */
// A style that starts with a background fills every pixel, so its tiles can be JPEG: about a
// third of the size of PNG. A style without one is an overlay and needs PNG, which can be
// see-through. (The same addresses as on TileServer GL's own front page.)
async function fillTileUrls() {
  const get = async (url) => { try { const r = await fetch(url, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch { return null; } };
  // styles.json, not rendered.json: TileServer GL's rendered.json has no style ids. The print
  // scale bands are left out: they are for printing only.
  const [styles, data] = await Promise.all([get(`${TILES}/styles.json`), get(`${TILES}/data.json`)]);
  const rows = await Promise.all((styles || []).filter((t) => t.id && !BAND.test(t.id)).map(async (t) => {
    const st = await get(styleUrlFor(t.id));
    const jpeg = !!(st && st.layers && st.layers[0] && st.layers[0].type === "background");
    return [t.name || t.id, t.id, `${TILES}/styles/${t.id}/{z}/{x}/{y}.${jpeg ? "jpg" : "png"}`];
  }));
  rows.sort((a, b) => a[0].localeCompare(b[0], "en", { numeric: true }));
  $("tile-list").replaceChildren(...rows.map(([name, id, url]) => {
    const d = document.createElement("div");
    d.className = "addr";
    d.innerHTML = `<b>${escapeHtml(name)}</b><div class="copy"><input type="text" readonly aria-label="Tile URL for ${escapeHtml(name)}"><button type="button">Copy</button></div>
      <p class="more"><a href="${TILES}/styles/${id}.json">TileJSON</a> · <a href="${TILES}/styles/${id}/wmts.xml">WMTS</a> · <a href="${TILES}/styles/${id}/style.json">Style</a></p>`;
    const [input, button] = d.querySelectorAll("input,button");
    input.value = url;
    input.onfocus = () => input.select();
    button.onclick = () => {
      // Not navigator.clipboard: that needs https, which a server on a local network rarely has.
      input.select();
      const done = document.execCommand("copy");
      button.textContent = done ? "Copied" : "Press Ctrl+C";
      setTimeout(() => (button.textContent = "Copy"), 2500);
    };
    return d;
  }));
  if (!rows.length) $("tile-list").innerHTML = `<p>No Map Styles</p>`;
  $("data-block").hidden = !(data && data.length);
  $("data-list").replaceChildren(...(data || []).flatMap((t) => {
    const span = Object.assign(document.createElement("span"), { textContent: t.name && t.name !== t.id ? `${t.id} (${t.name})` : t.id });
    const a = (text, href) => Object.assign(document.createElement("a"), { textContent: text, href });
    return [span, a("TileJSON", `${TILES}/data/${t.id}.json`), a("Inspect", `${TILES}/data/${t.id}/`)];
  }));
}
$("tile-urls").onclick = () => { fillTileUrls(); $("addresses").showModal(); };
$("addresses-close").onclick = () => $("addresses").close();

/* ---------- PDF ---------- */
let aborter = null;
async function makePdf(kind) {
  const btns = [$("print"), $("download")];
  btns.forEach((b) => (b.disabled = true));
  $("cancel").hidden = false;
  const status = $("status");
  status.textContent = "Starting…";
  aborter = new AbortController();
  const t0 = Date.now();
  try {
    const c = currentCenter();
    const { pdf, pages } = await renderSheet({
      styleUrl: await printStyle(), center: c, scale: state.scale, paper: state.paper, orientation: state.orientation,
      dpi: effectiveDpi(), title: state.title.trim(), markers: shown(markers), arrows: shown(arrows), shapes: shown(shapes), units: state.units, shapeMeasure: state.shapeMeasure !== false, listPage: state.listPage, config,
      atlas: +state.atlas || 1, binder: state.binder, gridLines: state.layers.grid !== false, ink: state.layers.ink, signal: aborter.signal,
      terrain: terrain && terrain.printData(),
      onStatus: (t) => (status.textContent = t),
    });
    const base = (state.title.trim() || toMGRS(c.lat, c.lng, 3)).replace(/[^\w-]+/g, "_");
    const name = `${base}_1-${state.scale}${state.atlas > 1 ? `_atlas${state.atlas}x${state.atlas}` : ""}.pdf`;
    if (kind === "download") {
      pdf.save(name);
    } else {
      pdf.autoPrint();
      const url = pdf.output("bloburl");
      const w = window.open(url, "_blank");
      if (!w) { status.textContent = "The browser blocked the pop-up. Use Download PDF instead."; return; }
    }
    status.textContent = `Done: ${pages} page${pages === 1 ? "" : "s"} in ${Math.round((Date.now() - t0) / 1000)} s.`;
  } catch (e) {
    console.error(e);
    status.textContent = e.message === "Cancelled" ? "Canceled." : "Failed: " + (e.message || e);
  } finally {
    btns.forEach((b) => (b.disabled = false));
    $("cancel").hidden = true;
    aborter = null;
  }
}
$("cancel").onclick = () => { if (aborter) { aborter.abort(); $("status").textContent = "Canceling after this page…"; } };
$("print").onclick = () => makePdf("print");
$("download").onclick = () => makePdf("download");
