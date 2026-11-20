import * as maplibregl from "maplibre-gl";
// MapLibre 6 loads its worker from a separate file; tell it where (same folder as app.js).
maplibregl.setWorkerUrl(new URL("maplibre-gl-worker.js", document.baseURI).href);
import { renderSheet, atlasCells } from "./render.js";
import { layout, footprintMeters, SCALES, PAPERS, ATLAS_GRIDS } from "./layout.js";
import { zoomForScale, viewportBounds, inverse, destination, fmtDistance, fmtBearing, project, unproject } from "./geo.js";
import { toMGRS } from "./utm.js";
import { arrowInfo as arrowInfoFor } from "./features.js";
import { PALETTE, swatchRow } from "./palette.js";
import { toGeoJSON, fromGeoJSON, fileName } from "./drawings.js";
import { UNITS, defaultUnit, unitMetres, shapeRing, shapeCenter, shapeSummary, shapeLegs, shapeInsideText, fmtLength, SHAPE_NAMES } from "./shapes.js";
import { SECTIONS, OWN_CONTROLS, DEFAULTS as LAYER_DEFAULTS, applyLayers, groupsOf } from "./layers.js";
import { initTerrain, TERRAIN_SOURCES } from "./terrain/panel.js";
import { initWmmNotice } from "./wmm.js";
import { startLive, conversations, partnerOf, ageText } from "./live.js";
import { sharedFC, peopleFC, describe, kindText, personImage, PERSON_COLORS } from "./shared.js";

// The map at /map/: what the devices share (live.js, shared.js), drawing, terrain analysis, and
// printing (the Print tab: the sheet on the map, render.js). Opened with ?print (the print
// tool's old address), it starts on the Print tab.
const qs = new URLSearchParams(location.search);

const LS = {
  markers: "fieldmap.print.markers",
  arrows: "fieldmap.print.arrows",
  shapes: "fieldmap.print.shapes",
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
// Tile server base. Tried in order: ?tiles=<url>, then /maps on this host (the Pi behind
// Internet-in-a-Box), then this host's root (the docker-compose stack, where nginx fronts
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
let config = {};
// null | "marker" | "arrow-start" | "arrow-end" | "circle-center" | "circle-edge" | "rect-start" | "rect-end" | "polygon"
let mode = null;
let pendingArrow = null;    // {a:{lng,lat}} while placing the end
let pendingShape = null;    // {type, center | points} while a shape is being drawn
let selected = null;        // {kind:"marker"|"arrow"|"shape", id}
let paletteOpenFor = null;  // id of the row whose swatches are open
// The panel's tab: layers | markers | messages | print. The sheet is on the map only on the Print tab.
let tab = qs.has("print") ? "print" : ["layers", "markers", "messages", "print"].includes(state.tab) ? state.tab : "layers";

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
map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-left");
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

/* ---------- the tool bar: what can be added to the map ---------- */
const ICONS = {
  marker: '<circle cx="12" cy="12" r="5.5" fill="currentColor"/>',
  arrow: '<path d="M5 19 19 5M10 5h9v9" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  circle: '<circle cx="12" cy="12" r="7.5" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  rect: '<rect x="4.5" y="6.5" width="15" height="11" fill="none" stroke="currentColor" stroke-width="2.2"/>',
  polygon: '<path d="M12 4 20 10 17 19H7L4 10Z" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>',
};
const TOOLS = [["marker", "Add Marker"], ["arrow", "Add Arrow"], ["circle", "Circle"], ["rect", "Rectangle"], ["polygon", "Polygon"]];
map.addControl({
  onAdd() {
    const box = document.createElement("div");
    box.className = "maplibregl-ctrl maplibregl-ctrl-group toolbar";
    for (const [key, title] of TOOLS) {
      const b = document.createElement("button");
      b.type = "button"; b.id = `add-${key}`; b.title = title; b.ariaLabel = title;
      b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[key]}</svg>`;
      box.appendChild(b);
    }
    return box;
  },
  onRemove() {},
}, "top-left");

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
    id: "arrows-line", type: "line", source: "arrows", filter: ["match", ["get", "part"], ["line", "head"], true, false],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 3 },
  });
  map.addLayer({
    id: "arrows-halo", type: "line", source: "arrows", filter: ["match", ["get", "part"], ["line", "head"], true, false],
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

  map.addSource("temp", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "temp-fill", type: "fill", source: "temp", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#333", "fill-opacity": 0.08 } });
  map.addLayer({ id: "temp-line", type: "line", source: "temp", paint: { "line-color": "#333", "line-width": 2, "line-dasharray": [2, 2] } });
  map.addLayer({ id: "temp-points", type: "circle", source: "temp", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 4, "circle-color": "#fff", "circle-stroke-color": "#333", "circle-stroke-width": 2 } });

  footprintCenter = visibleCenter();
  updateFootprint();
  renderLists();
  showSheet();
  drawLive();
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
}).then((t) => { terrain = t; renderLayerPanel(); return t; });

map.on("click", (e) => {
  if (justDragged) { justDragged = false; return; }
  if (onPin(e) || (terrain && terrain.onClick(e))) return;
  if (mode === "marker") { addMarker(e.lngLat); return; }
  if (mode === "arrow-start") { pendingArrow = { a: { lng: e.lngLat.lng, lat: e.lngLat.lat } }; setMode("arrow-end"); return; }
  if (mode === "arrow-end") { addArrow(pendingArrow.a, { lng: e.lngLat.lng, lat: e.lngLat.lat }); pendingArrow = null; setMode(null); return; }
  if (mode && shapeClick(e)) return;
  const hitM = map.queryRenderedFeatures(e.point, { layers: ["markers-circle"] });
  if (hitM.length) { select("marker", hitM[0].properties.id); return; }
  const hitA = map.queryRenderedFeatures(e.point, { layers: ["arrow-ends", "arrows-line"] });
  if (hitA.length) { select("arrow", hitA[0].properties.id); return; }
  const hitS = map.queryRenderedFeatures(e.point, { layers: ["shapes-line", "shapes-label"] });
  if (hitS.length) { select("shape", hitS[0].properties.id); return; }
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
  if (mode || (terrain && terrain.picking) || onPin(e)) return;
  const pt = e.point;
  let hit = map.queryRenderedFeatures(pt, { layers: ["markers-circle"] });
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
  if (!dragging) return;
  if (dragging.kind === "marker") {
    const m = markers.find((x) => x.id === dragging.id);
    if (m) { m.lng = e.lngLat.lng; m.lat = e.lngLat.lat; map.getSource("markers").setData(markersFC()); }
  } else if (dragging.kind === "arrow-end") {
    const a = arrows.find((x) => x.id === dragging.id);
    if (a) { a[dragging.end] = { lng: e.lngLat.lng, lat: e.lngLat.lat }; map.getSource("arrows").setData(arrowsFC()); }
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
  if (d.kind === "marker") { saveMarkers(); select("marker", d.id); }
  if (d.kind === "arrow-end") { saveArrows(); select("arrow", d.id); }
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
function markersFC() {
  return { type: "FeatureCollection", features: markers.map((m) => ({ type: "Feature", geometry: { type: "Point", coordinates: [m.lng, m.lat] }, properties: { id: m.id, label: m.label, color: m.color } })) };
}
function saveMarkers() {
  localStorage.setItem(LS.markers, JSON.stringify(markers));
  if (map.getSource("markers")) map.getSource("markers").setData(markersFC());
  renderLists();
}
function addMarker(ll) {
  const m = { id: uid(), lng: ll.lng, lat: ll.lat, label: `P${markers.length + 1}`, color: state.color };
  markers.push(m);
  setMode(null);
  saveMarkers();
  select("marker", m.id, true);
}

/* ---------- data: arrows ---------- */
function arrowsFC() {
  const feats = [];
  for (const a of arrows) {
    const inv = inverse(a.a.lat, a.a.lng, a.b.lat, a.b.lng);
    const head = Math.min(Math.max(inv.distance * 0.08, 15), 400);
    const back = inv.finalBearing + 180;
    const l1 = destination(a.b.lat, a.b.lng, back - 25, head), l2 = destination(a.b.lat, a.b.lng, back + 25, head);
    const props = { id: a.id, color: a.color, label: arrowLabel(a) };
    feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[a.a.lng, a.a.lat], [a.b.lng, a.b.lat]] }, properties: { ...props, part: "line" } });
    feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[l1.lng, l1.lat], [a.b.lng, a.b.lat], [l2.lng, l2.lat]] }, properties: { ...props, part: "head", label: "" } });
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
  localStorage.setItem(LS.arrows, JSON.stringify(arrows));
  if (map.getSource("arrows")) map.getSource("arrows").setData(arrowsFC());
  renderLists();
}
function addArrow(a, b) {
  const ar = { id: uid(), a, b, label: `A${arrows.length + 1}`, color: state.color };
  arrows.push(ar);
  map.getSource("temp").setData(emptyFC());
  saveArrows();
  select("arrow", ar.id, true);
}
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 5); }

/* ---------- data: shapes ---------- */
function shapesFC() {
  const feats = [];
  for (const s of shapes) {
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
  localStorage.setItem(LS.shapes, JSON.stringify(shapes));
  if (map.getSource("shapes")) map.getSource("shapes").setData(shapesFC());
  renderLists();
}
function addShape(s) {
  const n = shapes.filter((x) => x.type === s.type).length + 1;
  const sh = { id: uid(), label: `${{ circle: "C", rect: "R", polygon: "P" }[s.type]}${n}`, color: state.color, ...s };
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
function renderLists() {
  renderList($("marker-list"), markers, "marker", "No markers yet.", (m) => `${toMGRS(m.lat, m.lng, 4)}`);
  renderList($("arrow-list"), arrows, "arrow", "No arrows yet.", (a) => {
    const i = arrowInfo(a);
    return `${fmtDistance(i.distance)} · ${fmtBearing(i.trueBearing)} T · ${fmtBearing(i.gridBearing)} G · ${fmtBearing(i.magBearing)} M`;
  });
  renderList($("shape-list"), shapes, "shape", "No shapes yet.", (s) => `${SHAPE_NAMES[s.type]} · ${shapeSummary(s, state.units)}`);
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
function renderList(list, items, kind, emptyText, detail) {
  list.innerHTML = "";
  if (!items.length) { list.innerHTML = `<div class="hint">${emptyText}</div>`; return; }
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "item" + (selected && selected.kind === kind && selected.id === it.id ? " selected" : "");
    row.dataset.id = it.id;
    row.innerHTML = `
      <div class="item-row">
        <button class="dot" title="Color" style="background:${it.color}"></button>
        <input type="text" value="${escapeHtml(it.label)}" placeholder="Name" maxlength="30">
        <button class="icon" title="Show on Map">◎</button>
        <button class="icon danger" title="Remove">✕</button>
      </div>
      <div class="item-detail">${detail(it)}</div>`;
    const [dot, text, go, del] = row.querySelectorAll("button,input");
    dot.onclick = (e) => { e.stopPropagation(); paletteOpenFor = paletteOpenFor === it.id ? null : it.id; renderLists(); };
    text.oninput = () => { it.label = text.value; persist(kind); };
    text.onfocus = () => { selected = { kind, id: it.id }; };
    go.onclick = () => { const c = kind === "marker" ? it : kind === "arrow" ? it.a : shapeCenter(it); map.flyTo({ center: [c.lng, c.lat] }); };
    del.onclick = () => {
      if (kind === "marker") markers = markers.filter((x) => x.id !== it.id);
      else if (kind === "arrow") arrows = arrows.filter((x) => x.id !== it.id);
      else shapes = shapes.filter((x) => x.id !== it.id);
      if (selected && selected.id === it.id) selected = null;
      persist(kind, true);
    };
    if (kind === "shape" && it.type === "circle") row.appendChild(radiusEditor(it));
    if (paletteOpenFor === it.id) {
      row.appendChild(swatchRow(it.color, (hex) => { it.color = hex; paletteOpenFor = null; persist(kind, true); }));
    }
    list.appendChild(row);
  }
}
function persist(kind, rerender = false) {
  if (kind === "marker") { localStorage.setItem(LS.markers, JSON.stringify(markers)); map.getSource("markers").setData(markersFC()); }
  else if (kind === "arrow") { localStorage.setItem(LS.arrows, JSON.stringify(arrows)); map.getSource("arrows").setData(arrowsFC()); }
  else { localStorage.setItem(LS.shapes, JSON.stringify(shapes)); map.getSource("shapes").setData(shapesFC()); }
  if (rerender) renderLists();
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------- modes and buttons ---------- */
const SHAPE_MODES = { circle: ["circle-center", "circle-edge"], rect: ["rect-start", "rect-end"], polygon: ["polygon"] };
function setMode(m) {
  mode = m;
  if (m !== "arrow-end") pendingArrow = null;
  if (!m || !Object.values(SHAPE_MODES).flat().includes(m)) pendingShape = null;
  if (map.getSource("temp") && !pendingArrow && !pendingShape) map.getSource("temp").setData(emptyFC());
  $("add-marker").classList.toggle("active", m === "marker");
  $("add-arrow").classList.toggle("active", m === "arrow-start" || m === "arrow-end");
  for (const [type, modes] of Object.entries(SHAPE_MODES)) $(`add-${type}`).classList.toggle("active", modes.includes(m));
  map.getCanvas().style.cursor = m ? "crosshair" : "";
  // Double-click zoom would swallow the taps that place corners.
  if (m) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
  const corners = pendingShape && pendingShape.points ? pendingShape.points.length : 0;
  $("mode-hint").textContent =
    m === "marker" ? "Tap the map to place the marker." :
    m === "arrow-start" ? "Tap the map at the start of the arrow." :
    m === "arrow-end" ? "Tap the map at the end of the arrow." :
    m === "circle-center" ? "Tap the center of the circle." :
    m === "circle-edge" ? "Tap the edge to set the radius. You can type an exact radius later." :
    m === "rect-start" ? "Tap one corner of the rectangle." :
    m === "rect-end" ? "Tap the opposite corner." :
    m === "polygon" ? (corners < 3 ? `Tap the corners of the polygon. Corners so far: ${corners}.` : `${corners} corners. To finish the shape, tap the first corner, press Enter or press Finish Shape.`) : "";
  $("finish-shape").hidden = !(m === "polygon" && corners >= 3);
  $("mode-bar").hidden = !m;
}
$("add-marker").onclick = () => setMode(mode === "marker" ? null : "marker");
$("add-arrow").onclick = () => setMode(mode ? null : "arrow-start");
for (const [type, modes] of Object.entries(SHAPE_MODES)) $(`add-${type}`).onclick = () => setMode(modes.includes(mode) ? null : modes[0]);
$("finish-shape").onclick = finishPolygon;
$("clear-all").onclick = () => {
  if ((markers.length || arrows.length || shapes.length) && confirm("Clear all markers, arrows and shapes?")) {
    markers = []; arrows = []; shapes = []; saveMarkers(); saveArrows(); saveShapes();
  }
};
// Save to a file, and load one: GeoJSON (src/drawings.js).
$("save-drawings").onclick = () => {
  if (!markers.length && !arrows.length && !shapes.length) { fileHint("Nothing to export. Add a marker, arrow or shape first."); return; }
  const data = toGeoJSON(markers, arrows, shapes, { title: state.title || "", saved: new Date().toISOString() });
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/geo+json" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: fileName(state.title) });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  fileHint(`Exported ${countText(markers.length, arrows.length, shapes.length)} to ${a.download}.`);
};
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
  const total = got.markers.length + got.arrows.length + got.shapes.length;
  if (!total) { fileHint(`Nothing to import from ${file.name}.${got.skipped ? ` It has ${got.skipped} item${got.skipped === 1 ? "" : "s"} that this page cannot draw.` : ""}`, true); return; }
  for (const m of got.markers) markers.push({ ...m, id: uid() });
  for (const a of got.arrows) arrows.push({ ...a, id: uid() });
  for (const s of got.shapes) {
    const sh = { ...s, id: uid() };
    if (sh.type === "circle" && !sh.unit) sh.unit = defaultUnit(sh.radius, state.units);
    if (sh.unit === undefined) delete sh.unit;
    shapes.push(sh);
  }
  saveMarkers(); saveArrows(); saveShapes();
  // Show what was loaded.
  const pts = [...got.markers, ...got.arrows.flatMap((a) => [a.a, a.b]),
    ...got.shapes.flatMap((s) => shapeRing(s).map(([lng, lat]) => ({ lng, lat })))];
  // A loop, not Math.min(...list): a detailed outline can have more points than a call takes.
  const b = pts.reduce((r, p) => [Math.min(r[0], p.lng), Math.min(r[1], p.lat), Math.max(r[2], p.lng), Math.max(r[3], p.lat)],
    [Infinity, Infinity, -Infinity, -Infinity]);
  map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 80, maxZoom: 15, duration: 0 });
  fileHint(`Imported ${countText(got.markers.length, got.arrows.length, got.shapes.length)} from ${file.name}` +
    (got.skipped ? `. Skipped ${got.skipped} item${got.skipped === 1 ? "" : "s"}: only points, two-point lines and areas are supported.` : "."));
};
function countText(m, a, s) {
  const part = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
  return [m && part(m, "marker"), a && part(a, "arrow"), s && part(s, "shape")].filter(Boolean).join(", ");
}
const FILE_HINT = $("file-hint").textContent;
function fileHint(text, error = false) {
  const el = $("file-hint");
  el.textContent = text;
  el.classList.toggle("error", error);
  clearTimeout(fileHint.timer);
  fileHint.timer = setTimeout(() => { el.textContent = FILE_HINT; el.classList.remove("error"); }, 12000);
}
$("units").onchange = (e) => { state.units = e.target.value; saveSettings(); saveShapes(); if (terrain) terrain.refresh(); };
$("shape-measure").onchange = (e) => { state.shapeMeasure = e.target.checked; saveSettings(); saveShapes(); };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { setMode(null); if (terrain) terrain.cancelPick(); }
  if (e.key === "Enter" && mode === "polygon" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) finishPolygon();
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
const OWN_SOURCES = ["footprint", "shapes", "arrows", "markers", "live", "people", "temp", ...TERRAIN_SOURCES];
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
  box.appendChild(toggle("MGRS Grid Lines", state.layers.grid !== false, (on) => { state.layers.grid = on; saveSettings(); }, "The grid numbers at the edges always print."));
  if (terrain && terrain.available) {
    const ht = document.createElement("h3"); ht.textContent = "Terrain Analysis"; box.appendChild(ht);
    box.appendChild(toggle("Analysis Overlay", state.layers.terrain !== false, (on) => {
      state.layers.terrain = on; saveSettings(); terrain.refresh(); updateFootprint();
    }, "The viewshed or radio coverage overlay and its points."));
  }
  const hidden = state.layers.off.filter((g) => have.has(g)).length;
  $("layers-summary").textContent = hidden ? `${hidden} hidden` : "";
}
$("layers-reset").onclick = () => { state.layers = { ...LAYER_DEFAULTS }; saveSettings(); redraw(); renderLayerPanel(); renderLiveSwitches(); drawLive(); };
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

/* ---------- the tabs ---------- */
const TABS = [...document.querySelectorAll(".tabs [data-tab]")];
function showTab(name) {
  if (name === "messages" && TABS.find((b) => b.dataset.tab === "messages").hidden) name = "layers";
  const was = tab;
  tab = name;
  state.tab = name; saveSettings();
  for (const b of TABS) b.ariaSelected = String(b.dataset.tab === name);
  for (const key of ["layers", "markers", "messages", "print"]) $(`tab-${key}`).hidden = key !== name;
  document.body.classList.remove("panel-collapsed");
  showSheet(was !== "print" && name === "print");
  if (name === "messages") renderMessages();
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
});

/** The live layers as they are now; also every few seconds, so that locations age between answers. */
function drawLive() {
  if (!map.getSource("live")) return;
  const st = live && live.status === "on" ? live.state : null;
  const info = (live && live.info) || {};
  try {
    map.getSource("live").setData(st ? sharedFC([...st.markers.values()]) : emptyFC());
    map.getSource("people").setData(st ? peopleFC(st.positions, +info.interval || 5, +info.stale || 120, (Date.now() - st.at) / 1000) : emptyFC());
    for (const id of SHARED_LAYERS) map.setLayoutProperty(id, "visibility", state.layers.shared !== false ? "visible" : "none");
    for (const id of PEOPLE_LAYERS) map.setLayoutProperty(id, "visibility", state.layers.people !== false ? "visible" : "none");
  } catch { /* a new map style is still loading: drawn again in a few seconds */ }
}
setInterval(drawLive, 5000);

function renderLiveSwitches() {
  const box = $("live-switches");
  box.replaceChildren(
    toggle("Device Locations", state.layers.people !== false, (on) => { state.layers.people = on; saveSettings(); drawLive(); }, "The devices sharing their location now. On screen only."),
    toggle("Shared Markers", state.layers.shared !== false, (on) => { state.layers.shared = on; saveSettings(); drawLive(); }, "Markers, arrows and routes shared by the devices. On screen only."),
  );
}

const when = (iso) => {
  const t = new Date(iso);
  if (isNaN(t)) return "";
  return t.toDateString() === new Date().toDateString() ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : t.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
};
const nameOf = (uid) => people.get(uid) || "Unknown";

/** A shared item or a device under the pointer: its details in a popup. True when there was one. */
function liveClick(e) {
  const hits = map.queryRenderedFeatures(e.point, { layers: [...PEOPLE_LAYERS, ...SHARED_LAYERS].filter((id) => map.getLayer(id)) });
  if (!hits.length) return false;
  const h = hits[0];
  if (PEOPLE_LAYERS.includes(h.layer.id)) showPerson(h.properties.uid);
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
  const by = p.by ? ` · ${escapeHtml(nameOf(p.by))}` : "";
  return `${kindText(d)}${by}${p.updated ? " · " + when(p.updated) : ""}`;
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

/** The Shared Markers list on the Markers tab, newest first. */
function renderShared() {
  const st = live && live.status === "on" ? live.state : null;
  $("shared-section").hidden = !st;
  if (!st) return;
  const items = [...st.markers.values()].map((f) => [f, describe(f)]).filter(([, d]) => d)
    .sort(([a], [b]) => String((b.properties || {}).updated || "").localeCompare(String((a.properties || {}).updated || "")));
  $("shared-count").textContent = items.length ? String(items.length) : "";
  const list = $("shared-list");
  if (!items.length) { list.innerHTML = `<div class="hint">No shared markers.</div>`; return; }
  list.replaceChildren(...items.map(([f, d]) => {
    const row = document.createElement("div");
    row.className = "shared-row";
    row.innerHTML = `<span class="shared-dot${d.kind === "marker" ? "" : " line"}" style="background:${d.color}"></span>
      <span class="what">${escapeHtml(d.label || "Unnamed")}<small>${sharedDetail(f, d)}</small></span>
      <button class="icon" title="Show on Map">◎</button>`;
    row.querySelector("button").onclick = () => {
      const [lon, lat] = d.points[0];
      map.flyTo({ center: [lon, lat] });
      showShared(f.id);
    };
    return row;
  }));
}

/* ---------- messages ---------- */
let openConversation = null;          // the partner shown, or null for the list
function renderMessages() {
  const on = live && live.status === "on" && live.info && live.info.messages;
  $("messages-status").textContent = !live || on ? "" : live.text;
  const convs = live ? conversations(live.messages.list, live.uid) : [];
  const unread = convs.reduce((n, c) => n + c.unread, 0);
  $("unread").hidden = !unread;
  $("unread").textContent = String(unread);
  document.title = unread ? `(${unread}) Map` : "Map";
  if (tab !== "messages") return;
  $("conversations").hidden = !!openConversation;
  $("conversation").hidden = !openConversation;
  if (!openConversation) {
    $("conversations").replaceChildren(...convs.map((c) => {
      const b = document.createElement("button");
      b.className = "conv-row";
      b.innerHTML = `<b>${escapeHtml(c.partner === "*" ? c.name : c.name || "Unknown")}</b>${c.unread ? `<span class="badge">${c.unread}</span>` : "<span></span>"}
        <small>${c.last ? escapeHtml(`${c.last.mine ? "You" : c.last.name}: ${c.last.text}`) : "No messages."}</small>`;
      b.onclick = () => { openConversation = c.partner; renderMessages(); };
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
    d.innerHTML = `<small>${escapeHtml(m.mine ? "You" : m.name)} · ${when(m.time)}</small>${escapeHtml(m.text)}<small>${m.mine ? "" : "Received (Server)"}</small>`;
    return d;
  }) : [Object.assign(document.createElement("div"), { className: "hint", textContent: "No messages." })]));
  live.markRead(openConversation);
}
$("conversation-back").onclick = () => { openConversation = null; renderMessages(); };

/** Everything that shows what the server holds, after each answer. */
function onLive(l) {
  live = l;
  const st = l.status === "on" ? l.state : null;
  if (st) for (const p of st.positions) people.set(p.uid, p.name);
  for (const m of l.messages.list) if (!m.mine) people.set(m.from, m.name);
  // No sharing service on this server: none of it is shown.
  const there = l.status !== "absent" && l.status !== "starting";
  $("live-layers").hidden = !there;
  // A server from before messages has locations and markers only.
  const messaging = there && !(l.status === "on" && l.info && !l.info.messages);
  TABS.find((b) => b.dataset.tab === "messages").hidden = !messaging;
  if (!messaging && tab === "messages") showTab("layers");
  $("live-status").textContent = l.status === "on" ? "" : l.text;
  drawLive();
  renderShared();
  renderMessages();
}
renderLiveSwitches();
showTab(tab);
startLive(onLive);

/* ---------- Tile URLs for other apps ---------- */
// A style that starts with a background fills every pixel, so its tiles can be JPEG: about a
// third of the size of PNG. A style without one is an overlay and needs PNG, which can be
// see-through. (The same addresses as on TileServer GL's own front page.)
async function fillTileUrls() {
  const get = async (url) => { try { const r = await fetch(url, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch { return null; } };
  const [rendered, data] = await Promise.all([get(`${TILES}/rendered.json`), get(`${TILES}/data.json`)]);
  const rows = await Promise.all((rendered || []).map(async (t) => {
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
      dpi: effectiveDpi(), title: state.title.trim(), markers, arrows, shapes, units: state.units, shapeMeasure: state.shapeMeasure !== false, listPage: state.listPage, config,
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
