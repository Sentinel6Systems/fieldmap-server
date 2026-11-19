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

// Print styles, in menu order. Only the ones the tile server actually has are offered.
// ?style=<id> forces any style on the server for this visit.
const PRINT_STYLES = [["topo-print", "Standard"], ["topo-usgs", "USGS quadrangle"]];
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
    serverStyles = new Set((await r.json()).map((s) => s.id));
    return PRINT_STYLES.filter(([id]) => serverStyles.has(id));
  } catch { return [PRINT_STYLES[0]]; }
}
// Print scale bands with their own style (<style>-12k, -24k, -50k, -100k): line weights
// fixed for the scale, so a sheet looks the same at any latitude. Used when the server has one.
const band = (scale) => (scale <= 16000 ? "12k" : scale <= 36000 ? "24k" : scale <= 75000 ? "50k" : "100k");
async function printStyle() {
  const id = `${STYLE}-${band(state.scale)}`;
  if (serverStyles.has(id)) {
    try { return applyLayers(await fetchStyle(styleUrlFor(id)), state.layers); } catch (e) { console.warn("print style:", e); }
  }
  return drawnStyle();
}
const styles = await availableStyles();
let STYLE = qs.get("style") || (styles.some(([id]) => id === state.style) ? state.style : PRINT_STYLES[0][0]);
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

fetch("config.json").then((r) => (r.ok ? r.json() : {})).then((c) => (config = c)).catch(() => {});

/* ---------- controls ---------- */
const scaleSel = $("scale");
for (const s of SCALES) scaleSel.add(new Option(`1:${s.toLocaleString("en-US")}`, s));
scaleSel.value = state.scale;
const styleSel = $("style");
for (const [id, label] of styles) styleSel.add(new Option(label, id));
if (!styles.some(([id]) => id === STYLE)) styleSel.add(new Option(STYLE, STYLE));
styleSel.value = STYLE;
styleSel.parentElement.hidden = styleSel.options.length < 2;
const paperSel = $("paper");
for (const [k, p] of Object.entries(PAPERS)) paperSel.add(new Option(p.label, k));
paperSel.value = state.paper;
$("orientation").value = state.orientation;
const atlasSel = $("atlas");
for (const g of ATLAS_GRIDS) atlasSel.add(new Option(g === 1 ? "Single sheet" : `Atlas ${g} × ${g} (${g * g} pages + index)`, g));
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

  map.addSource("temp", { type: "geojson", data: emptyFC() });
  map.addLayer({ id: "temp-fill", type: "fill", source: "temp", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#333", "fill-opacity": 0.08 } });
  map.addLayer({ id: "temp-line", type: "line", source: "temp", paint: { "line-color": "#333", "line-width": 2, "line-dasharray": [2, 2] } });
  map.addLayer({ id: "temp-points", type: "circle", source: "temp", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 4, "circle-color": "#fff", "circle-stroke-color": "#333", "circle-stroke-width": 2 } });

  footprintCenter = visibleCenter();
  updateFootprint();
  renderLists();
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
  hit = map.queryRenderedFeatures(pt, { layers: ["footprint-fill"] });
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
  $("info-grid").textContent = state.scale <= 12000 ? "MGRS 1 km + 100 m ticks" : state.scale >= 100000 ? "MGRS 1 km, 10 km bold" : "MGRS 1 km";
  const pages = atlas.n > 1 ? atlas.n * atlas.n + 1 : 1;
  const phone = window.innerWidth <= 640 || /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
  const warn = $("atlas-warning");
  if (atlas.n > 1 && phone) {
    warn.textContent = `The browser makes every page of an atlas. On a phone or tablet, this is slow and the browser can run out of memory. Use a laptop for atlases.${pages > 30 ? " " + pages + " pages take several minutes." : ""}`;
  } else if (pages > 30) {
    warn.textContent = `${pages} pages take several minutes and make a large file. The Standard quality of 200 dpi cuts the work by half.`;
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
        <button class="dot" title="Colour" style="background:${it.color}"></button>
        <input type="text" value="${escapeHtml(it.label)}" placeholder="Name" maxlength="30">
        <button class="icon" title="Show on the map">◎</button>
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
    m === "circle-center" ? "Tap the centre of the circle." :
    m === "circle-edge" ? "Tap the edge to set the radius. You can type an exact radius later." :
    m === "rect-start" ? "Tap one corner of the rectangle." :
    m === "rect-end" ? "Tap the opposite corner." :
    m === "polygon" ? (corners < 3 ? `Tap the corners of the polygon. Corners so far: ${corners}.` : `${corners} corners. To finish the shape, tap the first corner, press Enter or press Finish shape.`) : "";
  $("finish-shape").hidden = !(m === "polygon" && corners >= 3);
}
$("add-marker").onclick = () => setMode(mode === "marker" ? null : "marker");
$("add-arrow").onclick = () => setMode(mode ? null : "arrow-start");
for (const [type, modes] of Object.entries(SHAPE_MODES)) $(`add-${type}`).onclick = () => setMode(modes.includes(mode) ? null : modes[0]);
$("finish-shape").onclick = finishPolygon;
$("clear-all").onclick = () => {
  if ((markers.length || arrows.length || shapes.length) && confirm("Remove all markers, arrows and shapes?")) {
    markers = []; arrows = []; shapes = []; saveMarkers(); saveArrows(); saveShapes();
  }
};
// Save to a file, and load one: GeoJSON (src/drawings.js).
$("save-drawings").onclick = () => {
  if (!markers.length && !arrows.length && !shapes.length) { fileHint("There is nothing to save. Add a marker, arrow or shape first."); return; }
  const data = toGeoJSON(markers, arrows, shapes, { title: state.title || "", saved: new Date().toISOString() });
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: "application/geo+json" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: fileName(state.title) });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  fileHint(`Saved ${countText(markers.length, arrows.length, shapes.length)} as ${a.download}.`);
};
$("load-drawings").onclick = () => $("load-file").click();
$("load-file").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";                 // the same file can be chosen again
  if (!file) return;
  let got;
  try {
    if (file.size > 20 * 1024 * 1024) throw new Error("This file is more than 20 MB. It is too large for a file of markers, arrows and shapes.");
    got = fromGeoJSON(await file.text(), state.color);
  } catch (err) { fileHint(err.message, true); return; }
  const total = got.markers.length + got.arrows.length + got.shapes.length;
  if (!total) { fileHint(`The page cannot load anything from ${file.name}.${got.skipped ? ` The file has ${got.skipped} item${got.skipped === 1 ? "" : "s"} that this page cannot draw.` : ""}`, true); return; }
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
  fileHint(`Loaded ${countText(got.markers.length, got.arrows.length, got.shapes.length)} from ${file.name}` +
    (got.skipped ? `. The page did not load ${got.skipped} item${got.skipped === 1 ? "" : "s"}. It draws only points, two-point lines and areas.` : "."));
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
const OWN_SOURCES = ["footprint", "shapes", "arrows", "markers", "temp", ...TERRAIN_SOURCES];
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
function toggle(text, checked, onchange) {
  const l = document.createElement("label");
  l.className = "switch";
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
  box.appendChild(segmented("Ink", "ink", [["colour", "Colour"], ["bw", "B&W"]]));
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
  box.appendChild(toggle("MGRS grid lines. The numbers at the edges stay.", state.layers.grid !== false, (on) => { state.layers.grid = on; saveSettings(); }));
  if (terrain && terrain.available) {
    const ht = document.createElement("h3"); ht.textContent = "Terrain analysis"; box.appendChild(ht);
    box.appendChild(toggle("Viewshed or radio overlay and its points", state.layers.terrain !== false, (on) => {
      state.layers.terrain = on; saveSettings(); terrain.refresh(); updateFootprint();
    }));
  }
  const hidden = state.layers.off.filter((g) => have.has(g)).length;
  $("layers-summary").textContent = hidden ? `${hidden} hidden` : "";
}
$("layers-reset").onclick = () => { state.layers = { ...LAYER_DEFAULTS }; saveSettings(); redraw(); renderLayerPanel(); };
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
      if (!w) { status.textContent = "The browser blocked the pop-up. Press Download PDF instead."; return; }
    }
    status.textContent = `Done: ${pages} page${pages === 1 ? "" : "s"} in ${Math.round((Date.now() - t0) / 1000)} s.`;
  } catch (e) {
    console.error(e);
    status.textContent = e.message === "Cancelled" ? "Cancelled." : "Failed: " + (e.message || e);
  } finally {
    btns.forEach((b) => (b.disabled = false));
    $("cancel").hidden = true;
    aborter = null;
  }
}
$("cancel").onclick = () => { if (aborter) { aborter.abort(); $("status").textContent = "The print will stop after this page…"; } };
$("print").onclick = () => makePdf("print");
$("download").onclick = () => makePdf("download");
