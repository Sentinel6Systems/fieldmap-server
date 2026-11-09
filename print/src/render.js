import * as maplibregl from "maplibre-gl";
import { jsPDF } from "jspdf";
import { zoomForScale, project, unproject, viewportBounds, fmtDistance, fmtBearing, EARTH_CIRC } from "./geo.js";
import { layout, ATLAS_OVERLAP_IN } from "./layout.js";
import { toUTM, fromUTM, zoneFor, bandFor, squareId, toMGRS } from "./utm.js";
import { declinationInfo, fmtAngle } from "./declination.js";
import { arrowInfo } from "./features.js";
import { colorName } from "./palette.js";

const BLUE = "#1f5fa8";
const INK = "#111";
const FAM = "Helvetica, Arial, sans-serif";
const TILE_CSS = 1000;   // offscreen map tile size in CSS px (×pixelRatio on canvas)
const TILE_PAD = 120;    // extra CSS px rendered around each tile and cropped away

/* =====================================================================
 * Atlas layout: a grid of identical pages around a centre, in Mercator
 * at the zoom of the centre latitude. Rows A.. from the north, columns
 * 1.. from the west. n = 1 is a single sheet.
 * ===================================================================*/
export function atlasCells(center, n, lay, scale, dpi) {
  const pixelRatio = dpi / 100;
  const zoom = zoomForScale(scale, center.lat, dpi, pixelRatio);
  const cellW = lay.map.w * 100, cellH = lay.map.h * 100;          // CSS px
  const ov = ATLAS_OVERLAP_IN * 100;
  const stepX = cellW - ov, stepY = cellH - ov;
  const c = project(center.lng, center.lat, zoom);
  const totalW = stepX * (n - 1) + cellW, totalH = stepY * (n - 1) + cellH;
  const cells = [];
  for (let r = 0; r < n; r++) {
    for (let col = 0; col < n; col++) {
      const cx = c.x - totalW / 2 + cellW / 2 + col * stepX;
      const cy = c.y - totalH / 2 + cellH / 2 + r * stepY;
      const cc = unproject(cx, cy, zoom);
      const sw = unproject(cx - cellW / 2, cy + cellH / 2, zoom), ne = unproject(cx + cellW / 2, cy - cellH / 2, zoom);
      cells.push({
        row: r, col, name: `${String.fromCharCode(65 + r)}${col + 1}`,
        center: { lng: cc.lng, lat: cc.lat },
        bounds: [[sw.lng, sw.lat], [ne.lng, ne.lat]],
        index: r * n + col,
      });
    }
  }
  const sw = unproject(c.x - totalW / 2, c.y + totalH / 2, zoom), ne = unproject(c.x + totalW / 2, c.y - totalH / 2, zoom);
  return { cells, zoom, bounds: [[sw.lng, sw.lat], [ne.lng, ne.lat]], n, cellW, cellH };
}

export function cellFor(cells, lng, lat) {
  return cells.find((c) => lng >= c.bounds[0][0] && lng <= c.bounds[1][0] && lat >= c.bounds[0][1] && lat <= c.bounds[1][1]);
}

/* =====================================================================
 * Entry point. opts:
 *  styleUrl, center {lng,lat}, scale, paper, orientation, dpi, title,
 *  markers, arrows, listPage, config, atlas (1|2|3|4|6|8), binder (bool),
 *  onStatus(text), signal (AbortSignal)
 * Resolves to { pdf, pages }.
 * ===================================================================*/
export async function renderSheet(opts) {
  const status = opts.onStatus || (() => {});
  const n = opts.atlas || 1;
  const binder = !!opts.binder && n > 1;
  const lay = layout(opts.paper, opts.orientation, opts.scale, { binder });
  const atlas = atlasCells(opts.center, n, lay, opts.scale, opts.dpi);
  const pdf = new jsPDF({
    unit: "in", format: [lay.page.w, lay.page.h],
    orientation: lay.page.w > lay.page.h ? "landscape" : "portrait", compress: true,
  });
  pdf.setProperties({ title: opts.title || "Map", creator: "Emergency Box print tool" });
  let first = true;
  const addPage = () => { if (!first) pdf.addPage([lay.page.w, lay.page.h], lay.page.w > lay.page.h ? "landscape" : "portrait"); first = false; };
  const check = () => { if (opts.signal && opts.signal.aborted) throw new Error("Cancelled"); };

  const wantList = !!opts.listPage && ((opts.markers && opts.markers.length) || (opts.arrows && opts.arrows.length));
  const extra = (n > 1 ? 2 : 0) + (wantList ? 1 : 0);        // cover + index (+ list)
  const total = atlas.cells.length + extra;
  const firstCellPage = 1 + extra;                            // single sheet: 1; atlas: after cover, index (and list)

  let gridInfo;
  if (n > 1) {
    status("Rendering the index page…");
    const index = await renderIndexPage(lay, opts, atlas, status, total);
    gridInfo = index.gridInfo;
    check();
    status("Rendering the cover…");
    addPage();
    await renderCoverPage(pdf, lay, opts, atlas, gridInfo, index.features, total, status);
    check();
    addPage();
    pdf.addImage(index.img, "JPEG", 0, 0, lay.page.w, lay.page.h, undefined, "FAST");
    if (wantList) {
      status("Adding the list of markers and arrows…");
      addPage();
      addListPage(pdf, lay, opts, gridInfo, atlas, total);
    }
  }
  for (const cell of atlas.cells) {
    check();
    const pageNo = firstCellPage + cell.index;
    const label = n > 1 ? `${cell.name} (page ${pageNo} of ${total})` : "";
    const sub = (t) => status(n > 1 ? `${cell.name}: ${t}` : t);
    addPage();
    const info = await renderMapPage(pdf, lay, { ...opts, center: cell.center, zoom: atlas.zoom }, atlas, cell, sub, pageNo, total);
    if (!gridInfo) gridInfo = info;
    check();
    if (n > 1) status(`Done ${label}`);
  }
  if (n === 1 && wantList) {
    status("Adding the list page…");
    addPage();
    addListPage(pdf, lay, opts, gridInfo, atlas, total);
  }
  return { pdf, pages: total };
}

/* =====================================================================
 * One map page (single sheet or atlas cell)
 * ===================================================================*/
async function renderMapPage(pdf, lay, opts, atlas, cell, status, pageNo, total) {
  const dpi = opts.dpi, pixelRatio = dpi / 100;
  const cssW = Math.round(lay.map.w * 100), cssH = Math.round(lay.map.h * 100);
  const zoom = opts.zoom;
  const center = opts.center;

  status("Rendering map…");
  const mapCanvas = await renderMapCanvas(opts.styleUrl, center, zoom, cssW, cssH, pixelRatio, status);

  const W = Math.round(lay.page.w * dpi), H = Math.round(lay.page.h * dpi);
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const px = (i) => i * dpi;
  const mapRect = { x: px(lay.map.x), y: px(lay.map.y), w: px(lay.map.w), h: px(lay.map.h) };
  ctx.drawImage(mapCanvas, mapRect.x, mapRect.y, mapRect.w, mapRect.h);

  // Buildings: tiles below z14 only carry large buildings, so between z13 and z14
  // (1:24k, 1:25k) draw the building layer again from the z14 tiles.
  if (zoom >= 13 && zoom < 14) {
    status("Adding buildings from the detailed tiles…");
    const style = await loadStyle(opts.styleUrl);
    const layers = (style.layers || []).filter((l) => l["source-layer"] === "building" && l.type === "fill");
    if (layers.length) {
      const sources = {}; for (const l of layers) sources[l.source] = style.sources[l.source];
      const overlay = { version: 8, sources, layers: layers.map((l) => ({ ...l, minzoom: 13 })) };
      try {
        const oc = await renderMapCanvas(overlay, center, zoom + 1, cssW * 2, cssH * 2, pixelRatio / 2, () => {});
        ctx.drawImage(oc, mapRect.x, mapRect.y, mapRect.w, mapRect.h);
      } catch (e) { console.warn("building overlay failed:", e); }
    }
  }

  const toPage = pageProjector(center, zoom, cssW, cssH, pixelRatio, mapRect);
  const bounds = viewportBounds(center.lng, center.lat, zoom, cssW, cssH);

  status("Drawing grid and margins…");
  const tabs = atlas.n > 1 ? neighbourTabs(lay, dpi, atlas, cell) : [];
  const gridInfo = drawGrid(ctx, bounds, toPage, mapRect, { ...opts, center }, dpi, { avoid: tabs.map((t) => t.rect) });
  drawArrows(ctx, toPage, mapRect, opts.arrows || [], dpi, opts.center);
  drawMarkers(ctx, toPage, mapRect, opts.markers || [], dpi);
  ctx.strokeStyle = INK; ctx.lineWidth = dpi / 100;
  ctx.strokeRect(mapRect.x, mapRect.y, mapRect.w, mapRect.h);

  const pageTitle = atlas.n > 1 ? `${opts.title || "Atlas"} — ${cell.name}` : (opts.title || `${gridInfo.gzd} ${gridInfo.square}`);
  drawBlock(ctx, lay, dpi, { ...opts, center, title: pageTitle }, gridInfo, {
    scaleNote: atlas.n > 1 ? "nominal" : "",
    pageNote: atlas.n > 1 ? `Page ${pageNo} of ${total} · atlas ${atlas.n} × ${atlas.n}` : "",
  });
  if (tabs.length) drawNeighbourTabs(ctx, dpi, tabs);

  status("Encoding page…");
  pdf.addImage(canvas.toDataURL("image/jpeg", 0.9), "JPEG", 0, 0, lay.page.w, lay.page.h, undefined, "FAST");
  canvas.width = canvas.height = 0;
  return gridInfo;
}

/* =====================================================================
 * Atlas index page: the whole grid on one page at a smaller scale.
 * ===================================================================*/
async function renderIndexPage(lay, opts, atlas, status, total) {
  const dpi = opts.dpi, pixelRatio = dpi / 100;
  const cssW = Math.round(lay.map.w * 100), cssH = Math.round(lay.map.h * 100);
  // Zoom that fits the whole atlas with ~8% margin.
  const totalW = (atlas.cellW - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellW;
  const totalH = (atlas.cellH - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellH;
  const fit = Math.min(cssW / totalW, cssH / totalH) * 0.92;
  const zoom = atlas.zoom + Math.log2(fit);
  const center = opts.center;
  const indexScale = Math.round(opts.scale / fit / 1000) * 1000;

  const gather = featureGatherer(await loadStyle(opts.styleUrl), atlas.bounds);
  const mapCanvas = await renderMapCanvas(opts.styleUrl, center, zoom, cssW, cssH, pixelRatio, status, gather.collect);
  const W = Math.round(lay.page.w * dpi), H = Math.round(lay.page.h * dpi);
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const px = (i) => i * dpi;
  const mapRect = { x: px(lay.map.x), y: px(lay.map.y), w: px(lay.map.w), h: px(lay.map.h) };
  ctx.drawImage(mapCanvas, mapRect.x, mapRect.y, mapRect.w, mapRect.h);
  const toPage = pageProjector(center, zoom, cssW, cssH, pixelRatio, mapRect);
  const bounds = viewportBounds(center.lng, center.lat, zoom, cssW, cssH);
  const gridInfo = drawGrid(ctx, bounds, toPage, mapRect, { ...opts, scale: indexScale, center }, dpi, { kmStep: pickGridStep(indexScale) });

  // Page outlines and names
  ctx.save();
  ctx.beginPath(); ctx.rect(mapRect.x, mapRect.y, mapRect.w, mapRect.h); ctx.clip();
  ctx.lineWidth = dpi / 70; ctx.strokeStyle = "#c0392b";
  ctx.font = `bold ${Math.round(dpi * 0.22)}px ${FAM}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (const c of atlas.cells) {
    const a = toPage(c.bounds[0][0], c.bounds[1][1]), b = toPage(c.bounds[1][0], c.bounds[0][1]);
    ctx.fillStyle = "rgba(192,57,43,0.06)"; ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
    ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
    const cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
    ctx.lineWidth = dpi / 30; ctx.strokeStyle = "#fff"; ctx.strokeText(c.name, cx, cy);
    ctx.fillStyle = "#c0392b"; ctx.fillText(c.name, cx, cy);
    ctx.lineWidth = dpi / 70; ctx.strokeStyle = "#c0392b";
  }
  ctx.restore();
  drawArrows(ctx, toPage, mapRect, opts.arrows || [], dpi, opts.center);
  drawMarkers(ctx, toPage, mapRect, opts.markers || [], dpi);
  ctx.strokeStyle = INK; ctx.lineWidth = dpi / 100;
  ctx.strokeRect(mapRect.x, mapRect.y, mapRect.w, mapRect.h);

  const cellKm = { w: (lay.map.w * 0.0254 * opts.scale) / 1000, h: (lay.map.h * 0.0254 * opts.scale) / 1000 };
  drawBlock(ctx, lay, dpi, { ...opts, scale: indexScale, title: `${opts.title || "Atlas"} — Index` }, gridInfo, {
    scaleNote: "approx.",
    pageNote: `Page 2 of ${total} · ${atlas.n} × ${atlas.n} pages at 1:${opts.scale.toLocaleString("en-US")}, each ${cellKm.w.toFixed(1)} × ${cellKm.h.toFixed(1)} km, 1 cm overlap`,
  });
  const img = canvas.toDataURL("image/jpeg", 0.9);
  canvas.width = canvas.height = 0;
  return { img, gridInfo, features: gather.result(), indexScale };
}

/* =====================================================================
 * Gather named features inside the atlas from the vector tiles while the
 * index page renders: places, peaks, lakes, rivers. All offline.
 * ===================================================================*/
function featureGatherer(style, bounds) {
  const [[w, sth], [e, nth]] = bounds;
  const inside = (lng, lat) => lng >= w && lng <= e && lat >= sth && lat <= nth;
  const sourceId = Object.keys(style.sources || {}).find((k) => style.sources[k].type === "vector") || "openmaptiles";
  const places = new Map(), peaks = new Map(), lakes = new Map(), rivers = new Map();
  const nameOf = (p) => p["name:en"] || p.name || p["name:latin"];
  const anyInside = (geom) => {
    const coords = geom.type === "Point" ? [geom.coordinates] : geom.type === "LineString" ? geom.coordinates : geom.type === "MultiLineString" ? geom.coordinates.flat() : geom.type === "Polygon" ? geom.coordinates[0] : geom.type === "MultiPolygon" ? geom.coordinates.flat(2) : [];
    return coords.some((c) => inside(c[0], c[1]));
  };
  const collect = (map) => {
    const q = (layer) => { try { return map.querySourceFeatures(sourceId, { sourceLayer: layer }); } catch { return []; } };
    for (const f of q("place")) {
      const nm = nameOf(f.properties); if (!nm || !anyInside(f.geometry)) continue;
      const cls = f.properties.class;
      if (!["city", "town", "village", "hamlet", "suburb", "neighbourhood", "island"].includes(cls)) continue;
      if (!places.has(nm)) places.set(nm, { name: nm, cls, rank: f.properties.rank || 99 });
    }
    for (const f of q("mountain_peak")) {
      const nm = nameOf(f.properties); if (!nm || !anyInside(f.geometry)) continue;
      const ft = f.properties.ele_ft || (f.properties.ele ? Math.round(f.properties.ele * 3.28084) : null);
      if (!peaks.has(nm)) peaks.set(nm, { name: nm, ft });
    }
    for (const f of q("water_name")) {
      const nm = nameOf(f.properties); if (!nm || !anyInside(f.geometry)) continue;
      if (!lakes.has(nm)) lakes.set(nm, { name: nm, cls: f.properties.class });
    }
    for (const f of q("waterway")) {
      const nm = nameOf(f.properties); if (!nm || !anyInside(f.geometry)) continue;
      if (!rivers.has(nm)) rivers.set(nm, { name: nm, cls: f.properties.class });
    }
  };
  const order = { city: 0, town: 1, village: 2, suburb: 3, hamlet: 4, neighbourhood: 5, island: 6 };
  const result = () => ({
    places: [...places.values()].sort((a, b) => (order[a.cls] - order[b.cls]) || (a.rank - b.rank) || a.name.localeCompare(b.name)),
    peaks: [...peaks.values()].sort((a, b) => (b.ft || 0) - (a.ft || 0)),
    lakes: [...lakes.values()].sort((a, b) => a.name.localeCompare(b.name)),
    rivers: [...rivers.values()].filter((r) => r.cls === "river" || r.cls === "canal").concat([...rivers.values()].filter((r) => r.cls === "stream")).slice(0, 40),
  });
  return { collect, result };
}

/* =====================================================================
 * Cover page
 * ===================================================================*/
async function renderCoverPage(pdf, lay, opts, atlas, g, feats, total, status) {
  const dpi = opts.dpi, pixelRatio = dpi / 100;
  const W = Math.round(lay.page.w * dpi), H = Math.round(lay.page.h * dpi);
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, W, H);
  const px = (i) => i * dpi;
  const M = lay.map.x;                       // same left margin as the map pages (binder-aware)
  const R = lay.page.w - (lay.page.w - lay.map.x - lay.map.w);
  const colW = R - M;
  const portrait = lay.page.h >= lay.page.w;

  // Facts
  const [[w, sth], [e, nth]] = atlas.bounds;
  const widthKm = ((atlas.cellW - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellW) / 100 * 0.0254 * opts.scale / 1000;
  const heightKm = ((atlas.cellH - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellH) / 100 * 0.0254 * opts.scale / 1000;
  const cellKm = { w: (lay.map.w * 0.0254 * opts.scale) / 1000, h: (lay.map.h * 0.0254 * opts.scale) / 1000 };
  const decl = declinationInfo(opts.center.lat, opts.center.lng, g.zone);
  const squares = new Set();
  for (const [lng, lat] of [[w, sth], [e, sth], [e, nth], [w, nth], [opts.center.lng, opts.center.lat]]) {
    const u = toUTM(lat, lng, g.zone); squares.add(squareId(g.zone, u.easting, u.northing));
  }
  const cfg = opts.config || {};
  const last = atlas.cells[atlas.cells.length - 1].name;
  const dms = (v, pos, neg) => `${Math.abs(v).toFixed(4)}° ${v >= 0 ? pos : neg}`;
  const facts = [
    ["Scale", `1:${opts.scale.toLocaleString("en-US")} (nominal across the atlas)`],
    ["Pages", `${atlas.cells.length} map pages A1–${last}, plus cover, index${total - atlas.cells.length > 2 ? " and list" : ""} · ${total} pages`],
    ["Sheet", `${lay.page.w} × ${lay.page.h} in, each map ${cellKm.w.toFixed(1)} × ${cellKm.h.toFixed(1)} km, 1 cm overlap`],
    ["Coverage", `${widthKm.toFixed(1)} × ${heightKm.toFixed(1)} km · about ${Math.round(widthKm * heightKm).toLocaleString("en-US")} km² (${Math.round(widthKm * heightKm * 0.3861).toLocaleString("en-US")} sq mi)`],
    ["Bounds", `${dms(nth, "N", "S")} to ${dms(sth, "N", "S")} · ${dms(w, "E", "W")} to ${dms(e, "E", "W")}`],
    ["Centre", `${g.centerMGRS} · ${opts.center.lat.toFixed(5)}, ${opts.center.lng.toFixed(5)}`],
    ["Grid", `MGRS / USNG, UTM zone ${g.zone}${g.band}, 100 km square${squares.size > 1 ? "s" : ""} ${[...squares].join(", ")} · 1 km lines · WGS 84`],
    ["Declination", `${fmtAngle(decl.declination)} at centre (${decl.asOf.getUTCFullYear()}), moving ${Math.abs(decl.annualChange).toFixed(2)}°/yr ${decl.annualChange < 0 ? "west" : "east"} · G-M angle ${fmtAngle(decl.gmAngle)} · ${decl.model}`],
    ["Contours", `${cfg.contourMinor || 40} ${cfg.contourUnit || "ft"}, index ${cfg.contourMajor || 200} ${cfg.contourUnit || "ft"} · terrain ${cfg.terrainSource || "Copernicus GLO-30"}`],
    ["Map data", `© OpenStreetMap contributors${cfg.dataDate ? ` · ${cfg.dataDate}` : ""} · printed ${new Date().toISOString().slice(0, 10)} from the Emergency Box, offline`],
  ];

  // Locator map (one small render; the atlas occupies about a third of it)
  status("Rendering the locator map…");
  const locIn = portrait ? Math.min(3.3, colW * 0.46) : Math.min(3.6, colW * 0.36);
  const locCss = Math.round(locIn * 100);
  const totalW = (atlas.cellW - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellW;
  const totalH = (atlas.cellH - ATLAS_OVERLAP_IN * 100) * (atlas.n - 1) + atlas.cellH;
  const locZoom = atlas.zoom + Math.log2((locCss * 0.36) / Math.max(totalW, totalH));
  let locCanvas = null;
  try { locCanvas = await renderMapCanvas(opts.styleUrl, opts.center, locZoom, locCss, locCss, pixelRatio, () => {}); } catch (e) { console.warn("locator failed:", e); }

  // ---- draw ----
  let y = px(0.55);
  ctx.fillStyle = "#1b1f24"; ctx.fillRect(px(M), y, px(colW), px(0.06));          // top rule
  y += px(0.5);
  ctx.fillStyle = "#555"; ctx.font = `${px(0.13)}px ${FAM}`; ctx.textBaseline = "alphabetic";
  ctx.fillText("TOPOGRAPHIC ATLAS", px(M), y); y += px(0.42);
  const title = opts.title || `${g.gzd} ${g.square}`;
  ctx.fillStyle = INK; fitText(ctx, title, px(colW), `bold ${px(0.46)}px ${FAM}`, 0.5); ctx.fillText(title, px(M), y); y += px(0.3);
  ctx.fillStyle = "#333"; ctx.font = `${px(0.15)}px ${FAM}`;
  const sub = feats.places.length ? `${feats.places.slice(0, 3).map((p) => p.name).join(" · ")}${feats.places.length > 3 ? " and surroundings" : ""}` : `${g.centerMGRS}`;
  ctx.fillText(sub, px(M), y); y += px(0.25);
  ctx.fillStyle = "#bbb"; ctx.fillRect(px(M), y, px(colW), px(0.012)); y += px(0.3);

  // Locator at right, facts at left
  const locX = px(R) - px(locIn), locY = y;
  if (locCanvas) {
    ctx.drawImage(locCanvas, locX, locY, px(locIn), px(locIn));
    const c0 = project(opts.center.lng, opts.center.lat, locZoom);
    const toLoc = (lng, lat) => { const p = project(lng, lat, locZoom); return { x: locX + (p.x - c0.x + locCss / 2) * pixelRatio, y: locY + (p.y - c0.y + locCss / 2) * pixelRatio }; };
    const a = toLoc(w, nth), b = toLoc(e, sth);
    ctx.fillStyle = "rgba(192,57,43,0.12)"; ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
    ctx.strokeStyle = "#c0392b"; ctx.lineWidth = dpi / 60; ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
    ctx.strokeStyle = INK; ctx.lineWidth = dpi / 120; ctx.strokeRect(locX, locY, px(locIn), px(locIn));
    ctx.fillStyle = "#555"; ctx.font = `${px(0.08)}px ${FAM}`;
    const locScale = Math.round((opts.scale * Math.pow(2, atlas.zoom - locZoom)) / 1000) * 1000;
    ctx.fillText(`Atlas area outlined in red · about 1:${locScale.toLocaleString("en-US")}`, locX, locY + px(locIn) + px(0.14));
  }

  const factsW = px(colW) - px(locIn) - px(0.35);
  ctx.textBaseline = "alphabetic";
  for (const [k, v] of facts) {
    ctx.font = `bold ${px(0.085)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText(k.toUpperCase(), px(M), y);
    ctx.font = `${px(0.1)}px ${FAM}`; ctx.fillStyle = INK;
    y = wrapText(ctx, v, px(M) + px(0.95), y, factsW - px(0.95), px(0.14)) + px(0.07);
  }
  y = Math.max(y, locY + px(locIn) + px(0.45));

  // Places inside the atlas
  ctx.fillStyle = "#bbb"; ctx.fillRect(px(M), y, px(colW), px(0.012)); y += px(0.3);
  const section = (heading, items) => {
    if (!items.length) return;
    ctx.font = `bold ${px(0.085)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText(heading.toUpperCase(), px(M), y); y += px(0.16);
    ctx.font = `${px(0.1)}px ${FAM}`; ctx.fillStyle = INK;
    y = wrapText(ctx, items.join(" · "), px(M), y, px(colW), px(0.15)) + px(0.16);
  };
  const townish = feats.places.filter((p) => ["city", "town", "village"].includes(p.cls)).map((p) => p.name);
  const hamlets = feats.places.filter((p) => !["city", "town", "village"].includes(p.cls)).map((p) => p.name);
  section("Towns and villages", townish.slice(0, 30));
  section("Hamlets and localities", hamlets.slice(0, 40));
  section("Peaks", feats.peaks.slice(0, 14).map((p) => (p.ft ? `${p.name} ${p.ft.toLocaleString("en-US")} ft` : p.name)));
  section("Lakes and ponds", feats.lakes.slice(0, 24).map((l) => l.name));
  section("Rivers and streams", feats.rivers.slice(0, 24).map((r) => r.name));
  if (!feats.places.length && !feats.peaks.length && !feats.lakes.length) {
    ctx.font = `${px(0.1)}px ${FAM}`; ctx.fillStyle = "#777"; ctx.fillText("No named places found in the map data for this area.", px(M), y); y += px(0.2);
  }

  // How to use
  const howY = Math.max(y + px(0.15), px(lay.page.h) - px(1.55));
  if (howY < px(lay.page.h) - px(0.9)) {
    ctx.fillStyle = "#bbb"; ctx.fillRect(px(M), howY, px(colW), px(0.012));
    ctx.font = `bold ${px(0.085)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText("USING THIS ATLAS", px(M), howY + px(0.28));
    ctx.font = `${px(0.095)}px ${FAM}`; ctx.fillStyle = INK;
    wrapText(ctx, `The index page shows every map page in position. Pages are named by row (A at the north) and column (1 at the west); red tabs on each page name its neighbours. Blue numbers on the map edges are the MGRS 1 km grid: read right, then up. Compass bearings must be corrected by the G-M angle printed on every page. Scale is ${(2.54 / (opts.scale / 100000)).toFixed(1)} cm per km (${(63360 / opts.scale).toFixed(2)} in per mile).`, px(M), howY + px(0.46), px(colW), px(0.14));
  }
  ctx.fillStyle = "#1b1f24"; ctx.fillRect(px(M), px(lay.page.h) - px(0.35), px(colW), px(0.03));

  pdf.addImage(canvas.toDataURL("image/jpeg", 0.9), "JPEG", 0, 0, lay.page.w, lay.page.h, undefined, "FAST");
  canvas.width = canvas.height = 0;
}

function wrapText(ctx, text, x, y, maxW, lineH) {
  const words = String(text).split(/\s+/);
  let line = "";
  for (const wd of words) {
    const t = line ? line + " " + wd : wd;
    if (ctx.measureText(t).width > maxW && line) { ctx.fillText(line, x, y); y += lineH; line = wd; }
    else line = t;
  }
  if (line) { ctx.fillText(line, x, y); y += lineH; }
  return y;
}

function pickGridStep(scale) {
  if (scale <= 60000) return 1000;
  if (scale <= 250000) return 10000;
  return 100000;
}

/* =====================================================================
 * Map rendering: tiled, so very large pages never exceed canvas limits.
 * Returns a canvas of (cssW*pr) × (cssH*pr) pixels.
 * ===================================================================*/
async function renderMapCanvas(style, center, zoom, cssW, cssH, pixelRatio, status, collect) {
  const out = document.createElement("canvas");
  out.width = Math.round(cssW * pixelRatio); out.height = Math.round(cssH * pixelRatio);
  const octx = out.getContext("2d");
  const cols = Math.ceil(cssW / TILE_CSS), rows = Math.ceil(cssH / TILE_CSS);
  const tileW = Math.ceil(cssW / cols), tileH = Math.ceil(cssH / rows);
  const c0 = project(center.lng, center.lat, zoom);
  const originX = c0.x - cssW / 2, originY = c0.y - cssH / 2;   // world px of the page's top-left
  let k = 0;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      k++;
      if (rows * cols > 1) status(`Rendering map (part ${k} of ${rows * cols})…`);
      const x0 = c * tileW, y0 = r * tileH;
      const w = Math.min(tileW, cssW - x0), h = Math.min(tileH, cssH - y0);
      const padL = Math.min(TILE_PAD, x0), padT = Math.min(TILE_PAD, y0);
      const padR = Math.min(TILE_PAD, cssW - x0 - w), padB = Math.min(TILE_PAD, cssH - y0 - h);
      const rw = w + padL + padR, rh = h + padT + padB;
      const cc = unproject(originX + x0 - padL + rw / 2, originY + y0 - padT + rh / 2, zoom);
      const { map, container } = await renderMap(style, { lng: cc.lng, lat: cc.lat }, zoom, rw, rh, pixelRatio);
      try {
        if (collect) { try { collect(map); } catch (e) { console.warn("collect:", e); } }
        octx.drawImage(map.getCanvas(),
          Math.round(padL * pixelRatio), Math.round(padT * pixelRatio), Math.round(w * pixelRatio), Math.round(h * pixelRatio),
          Math.round(x0 * pixelRatio), Math.round(y0 * pixelRatio), Math.round(w * pixelRatio), Math.round(h * pixelRatio));
      } finally {
        map.remove(); container.remove();
      }
    }
  }
  return out;
}

function renderMap(style, center, zoom, cssW, cssH, pixelRatio) {
  return new Promise((resolve, reject) => {
    const container = document.createElement("div");
    Object.assign(container.style, { position: "fixed", left: "-20000px", top: "0", width: cssW + "px", height: cssH + "px" });
    document.body.appendChild(container);
    let map;
    try {
      map = new maplibregl.Map({
        container, style, center: [center.lng, center.lat], zoom, bearing: 0, pitch: 0,
        interactive: false, attributionControl: false, preserveDrawingBuffer: true,
        fadeDuration: 0, pixelRatio, maxZoom: 24,
      });
    } catch (e) { container.remove(); return reject(e); }
    const timer = setTimeout(() => { map.remove(); container.remove(); reject(new Error("Map render timed out")); }, 120000);
    map.on("error", (e) => console.warn("maplibre:", e && e.error && e.error.message));
    map.once("idle", () => { clearTimeout(timer); setTimeout(() => resolve({ map, container }), 150); });
  });
}

const styleCache = new Map();
async function loadStyle(url) {
  if (typeof url !== "string") return url;
  if (!styleCache.has(url)) styleCache.set(url, fetch(url).then((r) => r.json()));
  return styleCache.get(url);
}

/** lng/lat -> page pixel, for a page whose map area is centred on `center` at `zoom`. */
function pageProjector(center, zoom, cssW, cssH, pixelRatio, mapRect) {
  const c = project(center.lng, center.lat, zoom);
  return (lng, lat) => {
    const p = project(lng, lat, zoom);
    return { x: mapRect.x + (p.x - c.x + cssW / 2) * pixelRatio, y: mapRect.y + (p.y - c.y + cssH / 2) * pixelRatio };
  };
}

/* =====================================================================
 * MGRS grid
 * ===================================================================*/
function clipToRect(a, b, r) {
  let t0 = 0, t1 = 1;
  const dx = b.x - a.x, dy = b.y - a.y;
  const checks = [[-dx, a.x - r.x], [dx, r.x + r.w - a.x], [-dy, a.y - r.y], [dy, r.y + r.h - a.y]];
  for (const [p, q] of checks) {
    if (p === 0) { if (q < 0) return null; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return null; if (t > t0) t0 = t; }
    else { if (t < t0) return null; if (t < t1) t1 = t; }
  }
  return [{ x: a.x + t0 * dx, y: a.y + t0 * dy }, { x: a.x + t1 * dx, y: a.y + t1 * dy }];
}

function drawGrid(ctx, bounds, toPage, r, opts, dpi, gopts = {}) {
  const [[west, south], [east, north]] = bounds;
  const corners = [[west, south], [east, south], [east, north], [west, north]];
  const c = opts.center;
  const zone = zoneFor(c.lat, c.lng);
  const band = bandFor(c.lat);
  const straddles = corners.some(([lng, lat]) => zoneFor(lat, lng) !== zone);
  const us = corners.map(([lng, lat]) => toUTM(lat, lng, zone));
  const hemi = c.lat >= 0 ? "N" : "S";
  const minE = Math.min(...us.map((u) => u.easting)), maxE = Math.max(...us.map((u) => u.easting));
  const minN = Math.min(...us.map((u) => u.northing)), maxN = Math.max(...us.map((u) => u.northing));

  const step = gopts.kmStep || 1000;
  const tickStep = step === 1000 && opts.scale <= 12000 ? 100 : 0;
  const heavyEvery = step === 1000 && opts.scale >= 100000 ? 10000 : 0;
  const thin = dpi / 150, heavy = dpi / 60;
  const fontPx = Math.round(dpi * 0.115), smallPx = Math.round(dpi * 0.07);
  const segs = 24;
  const labels = [];

  const sample = (isEasting, v) => {
    const pts = [];
    const lo = isEasting ? minN : minE, hi = isEasting ? maxN : maxE;
    for (let i = 0; i <= segs; i++) {
      const t = lo + ((hi - lo) * i) / segs;
      const ll = isEasting ? fromUTM(zone, hemi, v, t) : fromUTM(zone, hemi, t, v);
      pts.push(toPage(ll.lon, ll.lat));
    }
    return pts;
  };
  const edgeHits = (pts) => {
    const hits = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const s = clipToRect(pts[i], pts[i + 1], r);
      if (!s) continue;
      for (const p of s) {
        const side = Math.abs(p.x - r.x) < 0.5 ? "L" : Math.abs(p.x - (r.x + r.w)) < 0.5 ? "R" : Math.abs(p.y - r.y) < 0.5 ? "T" : Math.abs(p.y - (r.y + r.h)) < 0.5 ? "B" : null;
        if (side) hits.push({ ...p, side });
      }
    }
    return hits;
  };

  ctx.save();
  ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
  ctx.strokeStyle = BLUE;
  const line = (isEasting, v) => {
    const pts = sample(isEasting, v);
    ctx.lineWidth = heavyEvery && v % heavyEvery === 0 ? heavy : thin;
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.stroke();
    for (const h of edgeHits(pts)) labels.push({ ...h, v, isEasting });
  };
  for (let e = Math.ceil(minE / step) * step; e <= maxE; e += step) line(true, e);
  for (let nn = Math.ceil(minN / step) * step; nn <= maxN; nn += step) line(false, nn);
  ctx.restore();

  if (tickStep) {
    ctx.save(); ctx.strokeStyle = BLUE; ctx.lineWidth = thin;
    const tick = dpi * 0.06;
    const tickLine = (isEasting, v) => {
      for (const q of edgeHits(sample(isEasting, v))) {
        if (q.side === "L") strokeSeg(ctx, q.x, q.y, q.x + tick, q.y);
        else if (q.side === "R") strokeSeg(ctx, q.x - tick, q.y, q.x, q.y);
        else if (q.side === "T") strokeSeg(ctx, q.x, q.y, q.x, q.y + tick);
        else strokeSeg(ctx, q.x, q.y - tick, q.x, q.y);
      }
    };
    for (let e = Math.ceil(minE / tickStep) * tickStep; e <= maxE; e += tickStep) if (e % step) tickLine(true, e);
    for (let nn = Math.ceil(minN / tickStep) * tickStep; nn <= maxN; nn += tickStep) if (nn % step) tickLine(false, nn);
    ctx.restore();
  }

  // Edge labels: principal digits in bold with the hundreds-of-km prefix small.
  // Labels that would sit under a neighbour tab are skipped (they repeat every km).
  const avoid = gopts.avoid || [];
  const blocked = (x, y) => avoid.some((a) => x >= a.x - dpi * 0.12 && x <= a.x + a.w + dpi * 0.12 && y >= a.y - dpi * 0.12 && y <= a.y + a.h + dpi * 0.12);
  ctx.save();
  ctx.fillStyle = BLUE; ctx.textBaseline = "middle";
  const pad = dpi * 0.04;
  const seen = new Set();
  const unit = step >= 100000 ? 100000 : step >= 10000 ? 10000 : 1000;
  for (const l of labels) {
    const key = `${l.side}${Math.round(l.isEasting ? l.x : l.y)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (blocked(l.x, l.y)) continue;
    const km = Math.round(l.v / unit);
    const principal = unit === 1000 ? String(km % 100).padStart(2, "0") : String(Math.round(l.v / 1000));
    const prefix = unit === 1000 ? String(Math.floor(km / 100)) : "";
    ctx.font = `bold ${fontPx}px ${FAM}`; const pw = ctx.measureText(principal).width;
    ctx.font = `${smallPx}px ${FAM}`; const sw = prefix ? ctx.measureText(prefix).width : 0;
    const total = pw + sw + (prefix ? dpi * 0.01 : 0);
    ctx.save();
    if (l.side === "T" || l.side === "B") {
      const y = l.side === "T" ? r.y - pad - fontPx * 0.5 : r.y + r.h + pad + fontPx * 0.5;
      let x = l.x - total / 2;
      if (prefix) { ctx.font = `${smallPx}px ${FAM}`; ctx.fillText(prefix, x, y + fontPx * 0.15); x += sw + dpi * 0.01; }
      ctx.font = `bold ${fontPx}px ${FAM}`; ctx.fillText(principal, x, y);
    } else {
      ctx.translate(l.side === "L" ? r.x - pad - fontPx * 0.5 : r.x + r.w + pad + fontPx * 0.5, l.y);
      ctx.rotate(l.side === "L" ? -Math.PI / 2 : Math.PI / 2);
      let x = -total / 2;
      if (prefix) { ctx.font = `${smallPx}px ${FAM}`; ctx.fillText(prefix, x, fontPx * 0.15); x += sw + dpi * 0.01; }
      ctx.font = `bold ${fontPx}px ${FAM}`; ctx.fillText(principal, x, 0);
    }
    ctx.restore();
  }
  ctx.restore();

  const cu = toUTM(c.lat, c.lng, zone);
  return { zone, band, hemi, straddles, square: squareId(zone, cu.easting, cu.northing), gzd: `${zone}${band}`, centerMGRS: toMGRS(c.lat, c.lng, 4), stepKm: step / 1000 };
}

function strokeSeg(ctx, x0, y0, x1, y1) { ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); }

/* =====================================================================
 * Markers and arrows
 * ===================================================================*/
function drawMarkers(ctx, toPage, r, markers, dpi) {
  if (!markers.length) return;
  ctx.save();
  ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
  const R = dpi * 0.055, fontPx = Math.round(dpi * 0.11);
  ctx.font = `bold ${fontPx}px ${FAM}`; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
  for (const m of markers) {
    const p = toPage(m.lng, m.lat);
    if (p.x < r.x - R || p.x > r.x + r.w + R || p.y < r.y - R || p.y > r.y + r.h + R) continue;
    ctx.beginPath(); ctx.arc(p.x, p.y, R, 0, Math.PI * 2);
    ctx.fillStyle = m.color || "#d7263d"; ctx.fill();
    ctx.lineWidth = dpi / 100; ctx.strokeStyle = "#fff"; ctx.stroke();
    ctx.beginPath(); ctx.arc(p.x, p.y, R * 0.3, 0, Math.PI * 2); ctx.fillStyle = "#fff"; ctx.fill();
    if (m.label) {
      ctx.lineWidth = dpi / 40; ctx.strokeStyle = "#fff";
      ctx.strokeText(m.label, p.x + R * 1.5, p.y);
      ctx.fillStyle = INK; ctx.fillText(m.label, p.x + R * 1.5, p.y);
    }
  }
  ctx.restore();
}

function drawArrows(ctx, toPage, r, arrows, dpi, center) {
  if (!arrows.length) return;
  ctx.save();
  ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  const fontPx = Math.round(dpi * 0.09);
  for (const a of arrows) {
    const p1 = toPage(a.a.lng, a.a.lat), p2 = toPage(a.b.lng, a.b.lat);
    const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x);
    const len = Math.hypot(p2.x - p1.x, p2.y - p1.y);
    const head = Math.min(dpi * 0.14, len * 0.4), w = dpi / 60;
    for (const [col, lw] of [["#fff", w * 2.2], [a.color || "#d7263d", w]]) {
      ctx.strokeStyle = col; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.moveTo(p1.x, p1.y); ctx.lineTo(p2.x, p2.y); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(p2.x - head * Math.cos(ang - 0.45), p2.y - head * Math.sin(ang - 0.45));
      ctx.lineTo(p2.x, p2.y);
      ctx.lineTo(p2.x - head * Math.cos(ang + 0.45), p2.y - head * Math.sin(ang + 0.45));
      ctx.stroke();
    }
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(p1.x, p1.y, w * 1.6, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = a.color || "#d7263d"; ctx.lineWidth = w; ctx.stroke();
    const info = arrowInfo(a, center);
    const text = `${a.label ? a.label + "   " : ""}${fmtDistance(info.distance).split(" (")[0]}   ${fmtBearing(info.trueBearing)} T  ${fmtBearing(info.magBearing)} M`;
    ctx.save();
    ctx.translate((p1.x + p2.x) / 2, (p1.y + p2.y) / 2);
    let rot = ang; if (rot > Math.PI / 2 || rot < -Math.PI / 2) rot += Math.PI;
    ctx.rotate(rot);
    ctx.font = `bold ${fontPx}px ${FAM}`; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    ctx.lineWidth = dpi / 45; ctx.strokeStyle = "#fff";
    ctx.strokeText(text, 0, -w * 2.5); ctx.fillStyle = INK; ctx.fillText(text, 0, -w * 2.5);
    ctx.restore();
  }
  ctx.restore();
}

/* =====================================================================
 * Atlas neighbour tabs in the gutter
 * ===================================================================*/
function neighbourTabs(lay, dpi, atlas, cell) {
  const px = (i) => i * dpi;
  const n = atlas.n, r = { x: px(lay.map.x), y: px(lay.map.y), w: px(lay.map.w), h: px(lay.map.h) };
  const name = (row, col) => (row < 0 || col < 0 || row >= n || col >= n) ? null : `${String.fromCharCode(65 + row)}${col + 1}`;
  const g = px(lay.gutter) / 2;
  return [
    { t: name(cell.row - 1, cell.col), x: r.x + r.w * 0.5, y: r.y - g, rot: 0, arrow: "▲" },
    { t: name(cell.row + 1, cell.col), x: r.x + r.w * 0.5, y: r.y + r.h + g, rot: 0, arrow: "▼" },
    { t: name(cell.row, cell.col - 1), x: r.x - g, y: r.y + r.h * 0.5, rot: -Math.PI / 2, arrow: "▲" },
    { t: name(cell.row, cell.col + 1), x: r.x + r.w + g, y: r.y + r.h * 0.5, rot: Math.PI / 2, arrow: "▲" },
  ].map((tb) => {
    const label = tb.t ? `${tb.arrow} ${tb.t}` : "edge";
    const w = px(0.12) * label.length * 0.62 + px(0.16), h = px(0.19);
    const rw = tb.rot ? h : w, rh = tb.rot ? w : h;
    return { ...tb, label, w, h, rect: { x: tb.x - rw / 2, y: tb.y - rh / 2, w: rw, h: rh } };
  });
}

function drawNeighbourTabs(ctx, dpi, tabs) {
  const px = (i) => i * dpi;
  ctx.save();
  ctx.font = `bold ${px(0.11)}px ${FAM}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (const tb of tabs) {
    ctx.save(); ctx.translate(tb.x, tb.y); ctx.rotate(tb.rot);
    const w = ctx.measureText(tb.label).width + px(0.16), h = tb.h;
    ctx.fillStyle = tb.t ? "#c0392b" : "#999"; ctx.fillRect(-w / 2, -h / 2, w, h);
    ctx.fillStyle = "#fff"; ctx.fillText(tb.label, 0, px(0.005));
    ctx.restore();
  }
  ctx.restore();
}

/* =====================================================================
 * List page
 * ===================================================================*/
function addListPage(pdf, lay, opts, g, atlas, total) {
  const M = 0.5, W = lay.page.w - 2 * M;
  let y = M;
  const line = (txt, size = 9, style = "normal", color = [0, 0, 0]) => {
    pdf.setFont("helvetica", style); pdf.setFontSize(size); pdf.setTextColor(...color);
    pdf.text(txt, M, y); y += (size / 72) * 1.35;
  };
  const rule = () => { pdf.setDrawColor(180); pdf.setLineWidth(0.005); pdf.line(M, y - 0.12, M + W, y - 0.12); y += 0.06; };
  const more = (need) => { if (y + need > lay.page.h - M) { pdf.addPage([lay.page.w, lay.page.h], lay.page.w > lay.page.h ? "landscape" : "portrait"); y = M; } };
  const decl = declinationInfo(opts.center.lat, opts.center.lng, g.zone);
  const where = (lng, lat) => { if (atlas.n < 2) return ""; const c = cellFor(atlas.cells, lng, lat); return c ? `   Page ${c.name}` : "   (off the atlas)"; };

  pdf.setFont("helvetica", "bold"); pdf.setFontSize(14); pdf.setTextColor(0);
  pdf.text(`${opts.title || `${g.gzd} ${g.square}`} — points and arrows${atlas.n > 1 ? `   (page 3 of ${total})` : ""}`, M, y + 0.1); y += 0.3;
  line(`Sheet 1:${opts.scale.toLocaleString("en-US")} · MGRS ${g.gzd} ${g.square} · WGS 84 · Declination ${fmtAngle(decl.declination)} (${decl.asOf.getUTCFullYear()}) · G-M ${fmtAngle(decl.gmAngle)} · Printed ${new Date().toISOString().slice(0, 10)}`, 8, "normal", [80, 80, 80]);
  y += 0.1;
  const swatch = (hex, yy) => { const [r, gg, b] = hexRgb(hex); pdf.setFillColor(r, gg, b); pdf.setDrawColor(0); pdf.setLineWidth(0.005); pdf.rect(M, yy - 0.11, 0.14, 0.14, "FD"); };

  if (opts.markers && opts.markers.length) {
    more(0.6); line("MARKERS", 9, "bold", [60, 60, 60]); rule();
    for (const m of opts.markers) {
      more(0.45); swatch(m.color, y);
      pdf.setFont("helvetica", "bold"); pdf.setFontSize(10); pdf.setTextColor(0); pdf.text(m.label || "(unnamed)", M + 0.22, y);
      pdf.setFont("helvetica", "normal"); pdf.setFontSize(9);
      pdf.text(`${colorName(m.color)}   MGRS ${toMGRS(m.lat, m.lng, 5)}   ${m.lat.toFixed(5)}, ${m.lng.toFixed(5)}${where(m.lng, m.lat)}`, M + 1.6, y);
      y += 0.24;
    }
    y += 0.15;
  }
  if (opts.arrows && opts.arrows.length) {
    more(0.8); line("ARROWS", 9, "bold", [60, 60, 60]); rule();
    pdf.setFont("helvetica", "normal"); pdf.setFontSize(7.5); pdf.setTextColor(90);
    pdf.text("Bearings are from the start point: T = true, G = grid (this sheet's UTM zone), M = magnetic (compass).", M, y); y += 0.2;
    for (const a of opts.arrows) {
      more(0.6); const i = arrowInfo(a, opts.center); swatch(a.color, y);
      pdf.setFont("helvetica", "bold"); pdf.setFontSize(10); pdf.setTextColor(0); pdf.text(a.label || "(unnamed)", M + 0.22, y);
      pdf.setFont("helvetica", "normal"); pdf.setFontSize(9);
      pdf.text(`${colorName(a.color)}   Range ${fmtDistance(i.distance)}   Bearing ${fmtBearing(i.trueBearing)} T   ${fmtBearing(i.gridBearing)} G   ${fmtBearing(i.magBearing)} M${where(a.a.lng, a.a.lat)}`, M + 1.6, y);
      y += 0.17; pdf.setFontSize(8); pdf.setTextColor(60);
      pdf.text(`From  ${toMGRS(a.a.lat, a.a.lng, 5)}  (${a.a.lat.toFixed(5)}, ${a.a.lng.toFixed(5)})     To  ${toMGRS(a.b.lat, a.b.lng, 5)}  (${a.b.lat.toFixed(5)}, ${a.b.lng.toFixed(5)})${where(a.b.lng, a.b.lat)}`, M + 0.22, y);
      y += 0.26;
    }
  }
}

function hexRgb(hex) {
  const h = String(hex || "#000").replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/* =====================================================================
 * Bottom block: title, scale, grid, scale bars, declination
 * ===================================================================*/
function fitText(ctx, text, maxW, font, minScale = 0.6) {
  const m = font.match(/^(.*?)(\d+(?:\.\d+)?)px(.*)$/);
  let size = +m[2]; const min = size * minScale;
  for (;;) { ctx.font = `${m[1]}${size}px${m[3]}`; if (ctx.measureText(text).width <= maxW || size <= min) return size; size -= 1; }
}

function drawBlock(ctx, lay, dpi, opts, g, extra = {}) {
  const px = (i) => i * dpi;
  const bx = px(lay.block.x), by = px(lay.block.y), bw = px(lay.block.w);
  const cfg = opts.config || {};
  const unit = cfg.contourUnit || "ft";
  const scaleTxt = `1:${opts.scale.toLocaleString("en-US")}`;
  const declW = px(3.3), declX = bx + bw - declW;
  const barsW = px(1.7), barsX = declX - px(0.3) - barsW;
  const titleW = barsX - px(0.25) - bx;

  ctx.save();
  ctx.fillStyle = INK; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
  let y = by + px(0.2);
  const title = opts.title || `${g.gzd} ${g.square}`;
  fitText(ctx, title, titleW, `bold ${px(0.17)}px ${FAM}`); ctx.fillText(title, bx, y);

  y += px(0.21);
  ctx.font = `${px(0.075)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText("SCALE", bx, y);
  ctx.fillStyle = INK; ctx.font = `bold ${px(0.15)}px ${FAM}`;
  const sx = bx + px(0.4); ctx.fillText(scaleTxt, sx, y);
  let gx = sx + ctx.measureText(scaleTxt).width + px(0.08);
  if (extra.scaleNote) { ctx.font = `${px(0.07)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText(extra.scaleNote, gx, y); gx += ctx.measureText(extra.scaleNote).width + px(0.08); ctx.fillStyle = INK; }
  gx += px(0.17);
  ctx.font = `${px(0.075)}px ${FAM}`; ctx.fillStyle = "#555"; ctx.fillText("GRID", gx, y);
  ctx.fillStyle = INK;
  const gridTxt = `MGRS ${g.gzd} ${g.square} · ${g.stepKm || 1} km · WGS 84`;
  fitText(ctx, gridTxt, bx + titleW - (gx + px(0.33)), `bold ${px(0.11)}px ${FAM}`); ctx.fillText(gridTxt, gx + px(0.33), y);

  y += px(0.14);
  ctx.fillStyle = "#333";
  const lines = [
    `Centre ${g.centerMGRS}  ·  ${opts.center.lat.toFixed(5)}, ${opts.center.lng.toFixed(5)}${extra.pageNote ? "  ·  " + extra.pageNote : ""}`,
    `Contours ${cfg.contourMinor || 40} ${unit}, index ${cfg.contourMajor || 200} ${unit}  ·  Terrain ${cfg.terrainSource || "Copernicus GLO-30"}`,
    `© OpenStreetMap contributors${cfg.dataDate ? ` · ${cfg.dataDate}` : ""}  ·  Printed ${new Date().toISOString().slice(0, 10)}`,
  ];
  if (g.straddles) lines.push(`UTM zone ${g.zone} grid extended across the zone boundary.`);
  for (const l of lines) { fitText(ctx, l, titleW, `${px(0.075)}px ${FAM}`, 0.7); ctx.fillText(l, bx, y); y += px(0.105); }

  drawScaleBars(ctx, barsX, by + px(0.16), barsW, opts.scale, dpi);
  const decl = declinationInfo(opts.center.lat, opts.center.lng, g.zone);
  drawDeclination(ctx, declX, by + px(0.02), dpi, decl, lay.block.h);
  ctx.restore();
}

function niceBar(scale, maxInches, unitMeters) {
  const maxUnits = (maxInches * 0.0254 * scale) / unitMeters;
  const pow = Math.pow(10, Math.floor(Math.log10(maxUnits)));
  for (const m of [5, 2, 1]) if (m * pow <= maxUnits) return m * pow;
  return pow;
}

function drawScaleBars(ctx, x, y, w, scale, dpi) {
  const px = (i) => i * dpi;
  const units = [{ name: "km", m: 1000 }, { name: "mile", m: 1609.344 }];
  const h = px(0.07);
  ctx.font = `${px(0.075)}px ${FAM}`; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
  let yy = y;
  for (const u of units) {
    const len = niceBar(scale, w / dpi, u.m);
    const lenPx = ((len * u.m) / scale) * 39.3701 * dpi;
    for (let i = 0; i < 4; i++) { ctx.fillStyle = i % 2 ? "#fff" : INK; ctx.fillRect(x + (lenPx * i) / 4, yy, lenPx / 4, h); }
    ctx.strokeStyle = INK; ctx.lineWidth = dpi / 150; ctx.strokeRect(x, yy, lenPx, h);
    ctx.fillStyle = INK; ctx.fillText("0", x, yy - px(0.03));
    const lbl = `${len.toLocaleString("en-US")} ${u.name}${u.name === "mile" && len !== 1 ? "s" : ""}`;
    ctx.fillText(lbl, x + lenPx - ctx.measureText(lbl).width, yy - px(0.03));
    yy += h + px(0.26);
  }
}

function drawDeclination(ctx, x, y, dpi, d, blockHin) {
  const px = (i) => i * dpi;
  const H = px(blockHin - 0.08), cx = x + px(0.45), base = y + H;
  ctx.textAlign = "left";
  const arm = (deg, len, color, width, dash) => {
    const a = (deg * Math.PI) / 180;
    ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width; if (dash) ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(cx, base);
    const ex = cx + Math.sin(a) * len, ey = base - Math.cos(a) * len;
    ctx.lineTo(ex, ey); ctx.stroke(); ctx.restore();
    return { x: ex, y: ey, a };
  };
  const tn = arm(0, H, INK, dpi / 100);
  const gn = arm(d.convergence, H * 0.8, BLUE, dpi / 100, [dpi / 60, dpi / 60]);
  const mn = arm(d.declination, H * 0.92, INK, dpi / 70);
  ctx.save(); ctx.strokeStyle = "#666"; ctx.lineWidth = dpi / 200;
  const a0 = -Math.PI / 2, a1 = -Math.PI / 2 + (d.declination * Math.PI) / 180;
  ctx.beginPath(); ctx.arc(cx, base, H * 0.55, Math.min(a0, a1), Math.max(a0, a1)); ctx.stroke(); ctx.restore();
  ctx.fillStyle = INK; ctx.font = `${px(0.12)}px ${FAM}`; ctx.fillText("★", tn.x - px(0.055), tn.y - px(0.02));
  const ah = px(0.09);
  ctx.beginPath(); ctx.moveTo(mn.x, mn.y);
  ctx.lineTo(mn.x - Math.sin(mn.a + 0.35) * ah, mn.y + Math.cos(mn.a + 0.35) * ah);
  ctx.lineTo(mn.x - Math.sin(mn.a - 0.35) * ah, mn.y + Math.cos(mn.a - 0.35) * ah);
  ctx.closePath(); ctx.fill();
  ctx.font = `bold ${px(0.08)}px ${FAM}`;
  ctx.fillText("TN", tn.x + px(0.05), tn.y + px(0.12));
  ctx.fillText("MN", mn.x + (d.declination < 0 ? -px(0.26) : px(0.08)), mn.y + px(0.2));
  ctx.fillStyle = BLUE; ctx.fillText("GN", gn.x + px(0.06), gn.y + px(0.1));

  const tx = x + px(1.0); let ty = y + px(0.14);
  const row = (b, t) => {
    ctx.font = `bold ${px(0.08)}px ${FAM}`; ctx.fillStyle = INK; ctx.fillText(b, tx, ty);
    ctx.font = `${px(0.08)}px ${FAM}`; ctx.fillText(t, tx + px(0.28), ty); ty += px(0.11);
  };
  const dirWord = (deg) => (deg < 0 ? "west" : "east");
  row("TN", "true north (star)");
  row("GN", `grid north, ${fmtAngle(d.convergence, 2)} of TN`);
  row("MN", `magnetic north, ${fmtAngle(d.declination)} of TN (${d.asOf.getUTCFullYear()})`);
  row("G-M", `${fmtAngle(d.gmAngle)} — magnetic is ${dirWord(d.gmAngle)} of grid`);
  const ch = Math.abs(d.annualChange).toFixed(2);
  ctx.font = `${px(0.075)}px ${FAM}`; ctx.fillStyle = INK;
  ctx.fillText(`MN moves ${ch}° ${dirWord(d.annualChange).toUpperCase()} each year (declination ${Math.sign(d.annualChange) === Math.sign(d.declination) ? "increasing" : "decreasing"}).`, tx, ty); ty += px(0.1);
  ctx.font = `${px(0.065)}px ${FAM}`; ctx.fillStyle = "#555";
  ctx.fillText(`${d.model}${d.validThrough ? ` valid to ${d.validThrough}` : ""} · angles drawn true · local anomalies not modelled`, tx, ty);
}
