import * as maplibregl from "maplibre-gl";
// MapLibre 6 loads its worker from a separate file; tell it where (same folder as app.js).
maplibregl.setWorkerUrl(new URL("maplibre-gl-worker.js", document.baseURI).href);
import { renderSheet, atlasCells } from "./render.js";
import { layout, footprintMeters, SCALES, PAPERS, ATLAS_GRIDS } from "./layout.js";
import { zoomForScale, viewportBounds, inverse, destination, fmtDistance, fmtBearing, project, unproject } from "./geo.js";
import { toMGRS } from "./utm.js";
import { arrowInfo as arrowInfoFor } from "./features.js";
import { PALETTE, swatchRow } from "./palette.js";

const qs = new URLSearchParams(location.search);
const STYLE = qs.get("style") || "topo-print";
// Tile server base. Tried in order: ?tiles=<url>, then /maps on this host (the Pi behind
// Internet-in-a-Box), then this host's root (the docker-compose stack, where nginx fronts
// TileServer GL), then port 8080 on this host.
async function findTiles() {
  const candidates = qs.get("tiles") ? [qs.get("tiles")] : [`${location.origin}/maps`, location.origin, `${location.protocol}//${location.hostname}:8080`];
  for (const base of candidates) {
    const b = base.replace(/\/$/, "");
    try { const r = await fetch(`${b}/styles/${STYLE}/style.json`, { cache: "no-store" }); if (r.ok) return b; } catch {}
  }
  return candidates[0].replace(/\/$/, "");
}
const TILES = await findTiles();
const styleUrl = `${TILES}/styles/${STYLE}/style.json`;

const LS = {
  markers: "fieldmap.print.markers",
  arrows: "fieldmap.print.arrows",
  settings: "fieldmap.print.settings",
};

const $ = (id) => document.getElementById(id);
const state = Object.assign(
  { scale: 24000, paper: "letter", orientation: "landscape", dpi: 300, title: "", listPage: false, color: PALETTE[0][0], atlas: 1, binder: true },
  JSON.parse(localStorage.getItem(LS.settings) || "{}")
);
let markers = JSON.parse(localStorage.getItem(LS.markers) || "[]");
let arrows = JSON.parse(localStorage.getItem(LS.arrows) || "[]");
let config = {};
let mode = null;            // null | "marker" | "arrow-start" | "arrow-end"
let pendingArrow = null;    // {a:{lng,lat}} while placing the end
let selected = null;        // {kind:"marker"|"arrow", id}
let paletteOpenFor = null;  // id of the row whose swatches are open

fetch("config.json").then((r) => (r.ok ? r.json() : {})).then((c) => (config = c)).catch(() => {});

/* ---------- controls ---------- */
const scaleSel = $("scale");
for (const s of SCALES) scaleSel.add(new Option(`1:${s.toLocaleString("en-US")}`, s));
scaleSel.value = state.scale;
const paperSel = $("paper");
for (const [k, p] of Object.entries(PAPERS)) paperSel.add(new Option(p.label, k));
paperSel.value = state.paper;
$("orientation").value = state.orientation;
const atlasSel = $("atlas");
for (const g of ATLAS_GRIDS) atlasSel.add(new Option(g === 1 ? "Single sheet" : `Atlas ${g} × ${g} (${g * g} pages + index)`, g));
atlasSel.value = state.atlas || 1;
$("binder").checked = state.binder !== false;
$("dpi").value = state.dpi;
$("title").value = state.title;
$("list-page").checked = !!state.listPage;

function saveSettings() { localStorage.setItem(LS.settings, JSON.stringify(state)); }

/* ---------- map ---------- */
const start = location.hash.match(/^#(\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)/);
const map = new maplibregl.Map({
  container: "map",
  style: styleUrl,
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
  map.addLayer({ id: "temp-line", type: "line", source: "temp", paint: { "line-color": "#333", "line-width": 2, "line-dasharray": [2, 2] } });

  footprintCenter = visibleCenter();
  updateFootprint();
  renderLists();

  map.on("mousedown", onDown); map.on("touchstart", onDown);
  map.on("mousemove", onMove); map.on("touchmove", onMove);
  map.on("mouseup", onUp); map.on("touchend", onUp);
});


map.on("click", (e) => {
  if (justDragged) { justDragged = false; return; }
  if (mode === "marker") { addMarker(e.lngLat); return; }
  if (mode === "arrow-start") { pendingArrow = { a: { lng: e.lngLat.lng, lat: e.lngLat.lat } }; setMode("arrow-end"); return; }
  if (mode === "arrow-end") { addArrow(pendingArrow.a, { lng: e.lngLat.lng, lat: e.lngLat.lat }); pendingArrow = null; setMode(null); return; }
  const hitM = map.queryRenderedFeatures(e.point, { layers: ["markers-circle"] });
  if (hitM.length) { select("marker", hitM[0].properties.id); return; }
  const hitA = map.queryRenderedFeatures(e.point, { layers: ["arrow-ends", "arrows-line"] });
  if (hitA.length) { select("arrow", hitA[0].properties.id); return; }
  const inside = map.queryRenderedFeatures(e.point, { layers: ["footprint-fill"] }).length > 0;
  if (!inside) { footprintCenter = { lng: e.lngLat.lng, lat: e.lngLat.lat }; updateFootprint(); }
});

/* ---------- dragging: markers, arrow ends, the sheet ---------- */
let dragging = null, justDragged = false;
function onDown(e) {
  if (mode) return;
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
  const lay = layout(state.paper, state.orientation, state.scale, { binder: state.binder && n > 1 });
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
    warn.textContent = `An atlas renders every page in this browser. On a phone it is slow and may run out of memory: use a laptop for atlases${pages > 30 ? ", and expect several minutes for " + pages + " pages" : ""}.`;
  } else if (pages > 30) {
    warn.textContent = `${pages} pages: expect several minutes and a large file. 200 dpi halves the work.`;
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
        <button class="icon" title="Go to">◎</button>
        <button class="icon danger" title="Delete">✕</button>
      </div>
      <div class="item-detail">${detail(it)}</div>`;
    const [dot, text, go, del] = row.querySelectorAll("button,input");
    dot.onclick = (e) => { e.stopPropagation(); paletteOpenFor = paletteOpenFor === it.id ? null : it.id; renderLists(); };
    text.oninput = () => { it.label = text.value; persist(kind); };
    text.onfocus = () => { selected = { kind, id: it.id }; };
    go.onclick = () => { const c = kind === "marker" ? it : it.a; map.flyTo({ center: [c.lng, c.lat] }); };
    del.onclick = () => {
      if (kind === "marker") markers = markers.filter((x) => x.id !== it.id); else arrows = arrows.filter((x) => x.id !== it.id);
      if (selected && selected.id === it.id) selected = null;
      persist(kind, true);
    };
    if (paletteOpenFor === it.id) {
      row.appendChild(swatchRow(it.color, (hex) => { it.color = hex; paletteOpenFor = null; persist(kind, true); }));
    }
    list.appendChild(row);
  }
}
function persist(kind, rerender = false) {
  if (kind === "marker") { localStorage.setItem(LS.markers, JSON.stringify(markers)); map.getSource("markers").setData(markersFC()); }
  else { localStorage.setItem(LS.arrows, JSON.stringify(arrows)); map.getSource("arrows").setData(arrowsFC()); }
  if (rerender) renderLists();
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------- modes and buttons ---------- */
function setMode(m) {
  mode = m;
  if (m !== "arrow-end") { pendingArrow = null; if (map.getSource("temp")) map.getSource("temp").setData(emptyFC()); }
  $("add-marker").classList.toggle("active", m === "marker");
  $("add-arrow").classList.toggle("active", m === "arrow-start" || m === "arrow-end");
  map.getCanvas().style.cursor = m ? "crosshair" : "";
  $("mode-hint").textContent =
    m === "marker" ? "Tap the map to place the marker." :
    m === "arrow-start" ? "Tap the map at the arrow's start." :
    m === "arrow-end" ? "Now tap where the arrow ends." : "";
}
$("add-marker").onclick = () => setMode(mode === "marker" ? null : "marker");
$("add-arrow").onclick = () => setMode(mode ? null : "arrow-start");
$("clear-all").onclick = () => {
  if ((markers.length || arrows.length) && confirm("Remove all markers and arrows?")) { markers = []; arrows = []; saveMarkers(); saveArrows(); }
};
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setMode(null); });

// New-item colour swatches
const newColorBox = $("new-color");
function renderNewColor() {
  newColorBox.innerHTML = "";
  newColorBox.appendChild(swatchRow(state.color, (hex) => { state.color = hex; saveSettings(); renderNewColor(); }));
}
renderNewColor();

scaleSel.onchange = () => { state.scale = +scaleSel.value; saveSettings(); updateFootprint(); };
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
      styleUrl, center: c, scale: state.scale, paper: state.paper, orientation: state.orientation,
      dpi: effectiveDpi(), title: state.title.trim(), markers, arrows, listPage: state.listPage, config,
      atlas: +state.atlas || 1, binder: state.binder, signal: aborter.signal,
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
      if (!w) { status.textContent = "Pop-up blocked. Use Download instead."; return; }
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
$("cancel").onclick = () => { if (aborter) { aborter.abort(); $("status").textContent = "Cancelling after this page…"; } };
$("print").onclick = () => makePdf("print");
$("download").onclick = () => makePdf("download");
