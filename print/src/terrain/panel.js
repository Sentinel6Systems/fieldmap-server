// The Terrain Analysis view of the panel (its own tab): viewshed and radio coverage from a point (area), or
// a profile between two points (line). Owns its map markers and overlay, runs the worker
// and keeps the last result for printing. Hidden when the tile server has no elevation.
import * as maplibregl from "maplibre-gl";
import { findElevation, TerrainEngine } from "./client.js";
import { pixelMetres, profileAnalysis } from "./core.js";
import {
  FT, LIMITS, VIEW_LIMITS, HF_NOTE, legend, overlayCanvas, drawSwatch, fmtH, fmtD, inputsLine, dataLine,
  profileReadouts, drawProfile,
} from "./draw.js";
import { inverse, destination, fmtBearing } from "../geo.js";
import { toMGRS, zoneFor } from "../utm.js";
import { declinationInfo } from "../declination.js";

export const TERRAIN_SOURCES = ["terrain-overlay", "terrain-lines"];

export const DEFAULTS = {
  mode: "off", kind: "area", point: null, a: null, b: null,
  obsH: 6 * FT, tgtH: 0, radius: 10000, kVis: 7 / 6, kRadio: 4 / 3,
  freq: 462.6, txH: 5 * FT, rxH: 5 * FT,
  // A 5 W handheld with its stock antenna (a little below 0 dBi), and a typical FM
  // receiver: 12 dB SINAD at about 0.35 µV.
  budgetOn: false, powerW: 5, txGain: -2, rxGain: -2, cableLoss: 0, sens: -116,
  printProfile: false,
};

const FREQS = [["MURS 151 MHz", 151.82], ["2 m Amateur 146 MHz", 146], ["GMRS / FRS 462 MHz", 462.6], ["70 cm Amateur 440 MHz", 440], ["915 MHz ISM (LoRa, Meshtastic)", 915]];
const OBSERVER = [["Standing", 6 * FT]];
const TARGETS = [["Ground", 0], ["Person", 6 * FT], ["Building", 30 * FT]];
const ANTENNAS = [["Handheld", 5 * FT], ["Vehicle Roof", 6 * FT], ["Mast", 30 * FT]];
const MAX_RADIUS = 25000;
const EMPTY_IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

/** Set up the Terrain Analysis view. Resolves to a controller (available: false when there is
 *  no elevation tile set). */
export async function initTerrain({ map, tilesBase, state, saveSettings, root, onLayout }) {
  const src = await findElevation(tilesBase);
  const off = { available: false, picking: false, sources: [], printData: () => null, onClick: () => false, refresh() {}, cancelPick() {}, addLayers() {} };
  if (!src) return off;
  root.hidden = false;
  const t = state.terrain = { ...DEFAULTS, ...(state.terrain || {}) };
  const engine = new TerrainEngine(new URL("terrain-worker.js", document.baseURI).href, src);
  const body = root.querySelector("#terrain-controls");
  let picking = null;              // "point" | "a" | "b" while waiting for a tap
  let area = null;                 // { result, t } of the last area run
  let line = null;                 // { heights, step, pts, prof, t } of the last line run
  let status = "", timer = 0;
  const markers = {};

  const units = () => state.units;
  const save = () => saveSettings();
  const visible = () => t.mode !== "off" && state.layers.terrain !== false;

  /* ---------------- map layers and markers ---------------- */
  function addLayers() {
    if (!map.getSource("terrain-overlay")) {
      map.addSource("terrain-overlay", { type: "image", url: EMPTY_IMG, coordinates: [[0, 0.001], [0.001, 0.001], [0.001, 0], [0, 0]] });
      map.addLayer({ id: "terrain-overlay", type: "raster", source: "terrain-overlay", paint: { "raster-fade-duration": 0, "raster-resampling": "nearest" } }, "footprint-fill");
    }
    if (!map.getSource("terrain-lines")) {
      map.addSource("terrain-lines", { type: "geojson", data: fc([]) });
      map.addLayer({ id: "terrain-lines-halo", type: "line", source: "terrain-lines", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#fff", "line-width": 4, "line-opacity": 0.7 } }, "footprint-fill");
      map.addLayer({ id: "terrain-lines", type: "line", source: "terrain-lines", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#222", "line-width": 1.6, "line-dasharray": [3, 2] } }, "footprint-fill");
      map.addLayer({ id: "terrain-worst", type: "circle", source: "terrain-lines", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 6, "circle-color": "#c0392b", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    }
    drawMap();
  }
  const fc = (features) => ({ type: "FeatureCollection", features });
  const lineF = (coords) => ({ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} });

  function ring(center, radius) {
    const pts = [];
    for (let i = 0; i <= 120; i++) { const p = destination(center.lat, center.lng, i * 3, radius); pts.push([p.lng, p.lat]); }
    return pts;
  }

  function marker(key, label, cls) {
    if (!markers[key]) {
      const el = document.createElement("div");
      el.className = `terrain-pin ${cls}`;
      el.textContent = label;
      el.title = "Drag to move";
      const m = new maplibregl.Marker({ element: el, draggable: true, anchor: "center" });
      m.on("dragstart", () => { engine.cancel(); setStatus("Moving the point…"); });
      m.on("dragend", () => { const ll = m.getLngLat(); t[key] = { lng: ll.lng, lat: ll.lat }; save(); render(); schedule(0); });
      markers[key] = m;
    }
    markers[key].getElement().textContent = label;
    return markers[key];
  }

  function drawMap() {
    if (!map.getSource("terrain-lines")) return;
    const show = visible();
    const wanted = !show ? [] : t.kind === "area" ? (t.point ? ["point"] : []) : ["a", "b"].filter((k) => t[k]);
    for (const k of Object.keys(markers)) if (!wanted.includes(k)) markers[k].remove();
    for (const k of wanted) {
      const label = k === "point" ? (t.mode === "radio" ? "Tx" : "Eye") : k.toUpperCase();
      marker(k, label, k === "point" ? "terrain-pin-point" : "terrain-pin-ab").setLngLat([t[k].lng, t[k].lat]).addTo(map);
    }
    const feats = [];
    if (show && t.kind === "area" && t.point) feats.push(lineF(ring(t.point, t.radius)));
    if (show && t.kind === "line" && t.a && t.b) {
      feats.push(lineF([[t.a.lng, t.a.lat], [t.b.lng, t.b.lat]]));
      const w = line && line.prof && line.prof.worstIndex > 0 && line.pts[line.prof.worstIndex];
      if (w && sameLine()) feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [w.lng, w.lat] }, properties: {} });
    }
    map.getSource("terrain-lines").setData(fc(feats));
    const img = map.getSource("terrain-overlay");
    if (show && t.kind === "area" && area) {
      const [w, s, e, n] = area.result.bounds;
      overlayCanvas(area.result, t, state.layers.ink === "bw").toBlob((blob) => {
        const url = URL.createObjectURL(blob);
        img.updateImage({ url, coordinates: [[w, n], [e, n], [e, s], [w, s]] });
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      });
    } else img.updateImage({ url: EMPTY_IMG, coordinates: [[0, 0.001], [0.001, 0.001], [0.001, 0], [0, 0]] });
  }
  const sameLine = () => line && line.a === `${t.a.lng},${t.a.lat}` && line.b === `${t.b.lng},${t.b.lat}`;

  /* ---------------- running jobs ---------------- */
  function setStatus(text) { status = text; const el = body.querySelector(".terrain-status"); if (el) el.textContent = text; }

  /** Re-run after input changes (debounced). Any running job is cancelled. */
  function schedule(delay = 300) {
    clearTimeout(timer);
    engine.cancel();
    timer = setTimeout(compute, delay);
  }

  async function compute() {
    if (t.mode === "off") { area = null; line = null; drawMap(); return; }
    const progress = (f, text) => setStatus(`${text}… ${Math.round(f * 100)} %`);
    try {
      if (t.kind === "area") {
        if (!t.point) { area = null; drawMap(); setStatus("Press Place, then tap the map."); return; }
        const dataRes = pixelMetres(t.point.lat, src.maxzoom);
        // Radio paths cost radius³: past 12 km the grid coarsens to radius / 400.
        const res = t.mode === "radio" ? Math.max(dataRes, t.radius / 400) : dataRes;
        const params = t.mode === "viewshed"
          ? { obsH: t.obsH, tgtH: t.tgtH, k: t.kVis, radius: t.radius }
          : { txH: t.txH, rxH: t.rxH, fMHz: t.freq, k: t.kRadio, radius: t.radius };
        const result = await engine.run({ kind: t.mode, center: t.point, radius: t.radius, res, params }, progress);
        area = { result, t: { ...t } };
        setStatus(`Done in ${(result.ms / 1000).toFixed(1)} s · grid ${Math.round(res)} m`);
      } else {
        if (!t.a || !t.b) { line = null; drawMap(); renderProfile(); setStatus("Place A and B on the map."); return; }
        const distance = inverse(t.a.lat, t.a.lng, t.b.lat, t.b.lng).distance;
        if (distance < 1) { setStatus("A and B are at the same place."); return; }
        const r = await engine.run({ kind: "profile", a: t.a, b: t.b, distance }, progress);
        line = { ...r, a: `${t.a.lng},${t.a.lat}`, b: `${t.b.lng},${t.b.lat}` };
        analyse();
        setStatus(r.heights.some(Number.isNaN) ? "Part of the line has no elevation data." : `Done in ${(r.ms / 1000).toFixed(1)} s`);
      }
      drawMap();
      onLayout();
    } catch (e) {
      if (!e.cancelled) setStatus(`Failed: ${e.message}`);
    }
  }

  /** Profile analysis from the last heights with the current heights, k and frequency. */
  function analyse() {
    if (!line) return;
    const z = Array.from(line.heights, (v) => (Number.isNaN(v) ? 0 : v));
    line.prof = t.mode === "viewshed"
      ? profileAnalysis(z, line.step, { txH: t.obsH, rxH: t.tgtH, k: t.kVis })
      : profileAnalysis(z, line.step, { txH: t.txH, rxH: t.rxH, k: t.kRadio, fMHz: t.freq });
    line.t = { ...t };
    renderProfile();
  }

  /* ---------------- panel ---------------- */
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on")) el[k] = v; else if (k === "text") el.textContent = v; else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid) el.append(kid);
    return el;
  };

  function seg(label, value, options, onpick) {
    return h("div", { class: "seg-row" }, h("span", { text: label }), h("div", { class: "seg" },
      options.map(([v, text]) => h("button", { type: "button", class: v === value ? "on" : "", "aria-pressed": String(v === value), text, onclick: () => onpick(v) }))));
  }

  /** Height in the panel's unit with presets. key: field of t holding metres. */
  function heightRow(label, key, presets, opts = {}) {
    const metric = units() === "metric", f = metric ? 1 : FT;
    const sel = h("select", { "aria-label": `${label} preset` });
    for (const [name, m] of presets) sel.add(new Option(`${name} (${fmtH(m, units())})`, String(m)));
    sel.add(new Option("Custom", "custom"));
    const match = presets.find(([, m]) => Math.abs(m - t[key]) < 0.01);
    sel.value = match ? String(match[1]) : "custom";
    const num = h("input", { type: "number", min: "0", max: metric ? "300" : "1000", step: "any", inputmode: "decimal", "aria-label": `${label} in ${metric ? "meters" : "feet"}` });
    num.value = +(t[key] / f).toFixed(metric ? 1 : 0);
    sel.onchange = () => { if (sel.value !== "custom") { t[key] = +sel.value; num.value = +(t[key] / f).toFixed(metric ? 1 : 0); changed(opts.cheap); } else num.focus(); };
    num.onchange = () => {
      const v = parseFloat(num.value);
      if (!(v >= 0)) { num.value = +(t[key] / f).toFixed(1); return; }
      t[key] = v * f;
      const m2 = presets.find(([, m]) => Math.abs(m - t[key]) < 0.01);
      sel.value = m2 ? String(m2[1]) : "custom";
      changed(opts.cheap);
    };
    return h("div", { class: "terrain-row" }, h("span", { text: label }), sel, h("span", { class: "terrain-num" }, num, h("span", { text: metric ? "m" : "ft" })));
  }

  function numberRow(label, key, unit, { min, max, step = "any", cheap = false } = {}) {
    const num = h("input", { type: "number", min, max, step, inputmode: "decimal", "aria-label": label });
    num.value = t[key];
    num.onchange = () => {
      const v = parseFloat(num.value);
      if (!Number.isFinite(v) || (min != null && v < +min) || (max != null && v > +max)) { num.value = t[key]; return; }
      t[key] = v; changed(cheap);
    };
    return h("label", { class: "terrain-field" }, h("span", { text: label }), h("span", { class: "terrain-num" }, num, h("span", { text: unit })));
  }

  /** Something changed: save; recolour only (cheap) or run again. */
  function changed(cheap) {
    save();
    if (cheap === "recolour") { drawMap(); renderLegend(); onLayout(); return; }
    if (cheap === "analyse" && t.kind === "line" && sameLine()) { analyse(); drawMap(); onLayout(); return; }
    schedule();
    renderLegend();
  }

  function pointRow(key, label) {
    const p = t[key];
    const txt = p ? `${toMGRS(p.lat, p.lng, 5)} · ${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}` : "not placed";
    const btn = h("button", { type: "button", class: `secondary compact${picking === key ? " active" : ""}`, text: picking === key ? "Tap the map…" : p ? "Move" : "Place",
      onclick: () => { picking = picking === key ? null : key; map.getCanvas().style.cursor = picking ? "crosshair" : ""; render(); } });
    return h("div", { class: "terrain-point" }, h("span", { class: "terrain-point-label", text: label }), h("span", { class: "terrain-point-pos", text: txt }), btn);
  }

  function render() {
    body.innerHTML = "";
    const metric = units() === "metric";
    body.append(seg("Analysis", t.mode, [["off", "Off"], ["viewshed", "Viewshed"], ["radio", "Radio Coverage"]], (v) => { t.mode = v; area = null; save(); render(); schedule(0); }));
    if ($summary()) $summary().textContent = t.mode === "off" ? "" : t.mode === "viewshed" ? "Viewshed" : "Radio Coverage";
    if (t.mode === "off") {
      body.append(h("div", { class: "hint", text: "Viewshed shows what is visible from a point. Radio Coverage shows where a VHF or UHF radio can likely reach, or checks a single link." }));
      drawMap(); onLayout();
      return;
    }
    body.append(seg("Mode", t.kind, [["area", "Area Coverage"], ["line", "Point-to-Point"]], (v) => { t.kind = v; save(); render(); schedule(0); }));
    const radio = t.mode === "radio";
    if (t.kind === "area") body.append(pointRow("point", radio ? "Transmitter" : "Observer"));
    else body.append(pointRow("a", radio ? "A (Transmitter)" : "A (Observer)"), pointRow("b", radio ? "B (Receiver)" : "B (Target)"));
    body.append(h("div", { class: "hint", text: "Press Place or Move, then tap the map. Drag a point to move it." }));

    if (!radio) {
      body.append(heightRow("Observer Height (AGL)", "obsH", OBSERVER, { cheap: "analyse" }));
      body.append(heightRow(t.kind === "line" ? "Target Height at B (AGL)" : "Target Height (AGL)", "tgtH", TARGETS, { cheap: "analyse" }));
    } else {
      const sel = h("select", { "aria-label": "Frequency preset" });
      for (const [name, f] of FREQS) sel.add(new Option(name, String(f)));
      sel.add(new Option("Custom", "custom"));
      const fm = FREQS.find(([, f]) => Math.abs(f - t.freq) < 1e-6);
      sel.value = fm ? String(fm[1]) : "custom";
      const num = h("input", { type: "number", min: "30", max: "3000", step: "any", inputmode: "decimal", "aria-label": "Frequency in MHz" });
      num.value = t.freq;
      sel.onchange = () => { if (sel.value !== "custom") { t.freq = +sel.value; num.value = t.freq; changed("analyse"); } else num.focus(); };
      num.onchange = () => {
        const v = parseFloat(num.value);
        if (!(v >= 30 && v <= 3000)) { num.value = t.freq; setStatus("Enter a frequency from 30 to 3000 MHz."); return; }
        t.freq = v; const m = FREQS.find(([, f]) => Math.abs(f - v) < 1e-6); sel.value = m ? String(m[1]) : "custom"; changed("analyse");
      };
      body.append(h("div", { class: "terrain-row" }, h("span", { text: "Frequency" }), sel, h("span", { class: "terrain-num" }, num, h("span", { text: "MHz" }))));
      body.append(heightRow(t.kind === "line" ? "Antenna Height A" : "Transmitter Antenna Height", "txH", ANTENNAS, { cheap: "analyse" }));
      body.append(heightRow(t.kind === "line" ? "Antenna Height B" : "Receiver Antenna Height", "rxH", ANTENNAS, { cheap: "analyse" }));
    }

    if (t.kind === "area") {
      const f = metric ? 1000 : 1609.344;
      const range = h("input", { type: "range", min: String(1000 / f), max: String(MAX_RADIUS / f), step: metric ? "0.5" : "0.25", "aria-label": "Radius" });
      range.value = String(t.radius / f);
      const out = h("span", { class: "terrain-radius", text: fmtD(t.radius, units()) });
      range.oninput = () => { out.textContent = fmtD(+range.value * f, units()); };
      range.onchange = () => { t.radius = Math.min(MAX_RADIUS, Math.max(1000, +range.value * f)); changed(); render(); };
      body.append(h("div", { class: "terrain-row terrain-range" }, h("span", { text: "Radius" }), range, out));
    }

    if (radio) {
      const det = h("details", { class: "terrain-more" }, h("summary", { text: "Link Budget" }));
      det.open = !!t.budgetOn;
      const cb = h("input", { type: "checkbox" }); cb.checked = !!t.budgetOn;
      cb.onchange = () => { t.budgetOn = cb.checked; changed(t.kind === "line" ? "analyse" : "recolour"); render(); };
      det.append(h("label", { class: "check", title: "Link margin is the received level minus the receiver sensitivity." }, cb, h("span", { text: "Color by Link Margin" })));
      const cheap = t.kind === "line" ? "analyse" : "recolour";
      det.append(numberRow("Transmit Power", "powerW", "W", { min: "0.01", max: "1000", cheap }), numberRow("Transmit Antenna Gain", "txGain", "dBi", { min: "-20", max: "30", cheap }),
        numberRow("Receive Antenna Gain", "rxGain", "dBi", { min: "-20", max: "30", cheap }), numberRow("Cable Loss", "cableLoss", "dB", { min: "0", max: "30", cheap }),
        numberRow("Receiver Sensitivity", "sens", "dBm", { min: "-150", max: "-50", cheap }));
      det.append(h("div", { class: "hint", text: "The defaults are a 5 W handheld radio with its stock antenna. The receiver is usable down to −116 dBm (about 0.35 µV)." }));
      body.append(det);
    }
    const adv = h("details", { class: "terrain-more" }, h("summary", { text: "Advanced" }));
    adv.append(numberRow(radio ? "Earth Radius Factor k (Radio)" : "Earth Radius Factor k (Visual)", radio ? "kRadio" : "kVis", "", { min: "0.5", max: "100", cheap: "analyse" }));
    adv.append(h("div", { class: "hint", text: "k adjusts the earth curvature for refraction (earth radius = k × 6,371 km). Typical values are 7/6 (1.167) for sight and 4/3 (1.333) for radio; some weather lowers k." }));
    body.append(adv);

    body.append(h("div", { class: "terrain-status status", text: status }));
    body.append(h("div", { class: "terrain-legend" }));
    body.append(h("div", { class: "terrain-profile" }));
    if (t.kind === "line") {
      const cb = h("input", { type: "checkbox" }); cb.checked = !!t.printProfile;
      cb.onchange = () => { t.printProfile = cb.checked; save(); onLayout(); };
      body.append(h("label", { class: "check" }, cb, h("span", { text: "Include Profile Page" })));
    }
    body.append(h("div", { class: "hint terrain-limits", text: radio ? `${LIMITS} ${HF_NOTE}` : VIEW_LIMITS }));
    body.append(h("div", { class: "hint", text: `${dataLine(src)}. ${src.attribution}` }));
    renderLegend();
    renderProfile();
    drawMap();
    onLayout();
  }
  const $summary = () => document.getElementById("terrain-summary");

  function renderLegend() {
    const box = body.querySelector(".terrain-legend");
    if (!box) return;
    box.innerHTML = "";
    if (t.mode === "off" || t.kind !== "area") return;
    for (const item of legend(t)) {
      const c = document.createElement("canvas");
      c.width = 36; c.height = 20;
      drawSwatch(c.getContext("2d"), 1, 1, 34, 18, item.style, state.layers.ink === "bw", 8);
      box.append(h("div", { class: "terrain-legend-item" }, item.style ? c : h("span", { class: "terrain-legend-blank" }), h("span", { text: item.label })));
    }
  }

  function bearingTexts() {
    const inv = inverse(t.a.lat, t.a.lng, t.b.lat, t.b.lng);
    const decl = declinationInfo(t.a.lat, t.a.lng, zoneFor(t.a.lat, t.a.lng)).declination;
    return { trueTxt: fmtBearing(inv.initialBearing), magTxt: fmtBearing(inv.initialBearing - decl) };
  }

  function renderProfile() {
    const box = body.querySelector(".terrain-profile");
    if (!box) return;
    box.innerHTML = "";
    if (t.mode === "off" || t.kind !== "line" || !line || !line.prof || !sameLine() || root.closest("[hidden]")) return;
    const w = Math.max(240, body.clientWidth || 360), hh = Math.round(w * 0.55), dpr = window.devicePixelRatio || 1;
    const c = h("canvas", { class: "terrain-chart" });
    c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); c.style.width = "100%";
    const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
    drawProfile(g, line.prof, { x: 0, y: 0, w: c.width, h: c.height }, { units: units(), fontPx: 10 * dpr, bw: state.layers.ink === "bw", mode: t.mode, k: t.mode === "viewshed" ? t.kVis : t.kRadio });
    box.append(c);
    const dl = h("dl", { class: "info terrain-readouts" });
    for (const [k, v] of profileReadouts(line.prof, t, { units: units(), ...bearingTexts() })) dl.append(h("dt", { text: k }), h("dd", { text: v }));
    box.append(dl);
  }

  /* ---------------- public ---------------- */
  // The chart is drawn at the panel's width, which is only known while the view is open (onShow).
  let resizeTimer = 0;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderProfile, 200); });
  window.__terrain = { t, get area() { return area; }, get line() { return line; } };   // for debugging in the console
  render();
  if (t.mode !== "off") schedule(0);

  return {
    available: true,
    src,
    sources: TERRAIN_SOURCES,
    addLayers,
    /** Ink, layer switch or units changed. */
    refresh() { render(); },
    /** The view was opened: the profile chart can be drawn at its width now. */
    onShow() { renderProfile(); },
    get picking() { return !!picking; },
    cancelPick() { if (picking) { picking = null; map.getCanvas().style.cursor = ""; render(); } },
    /** A map tap while placing a point: returns true when it was used. */
    onClick(e) {
      if (!picking) return false;
      t[picking] = { lng: e.lngLat.lng, lat: e.lngLat.lat };
      picking = null; map.getCanvas().style.cursor = "";
      save(); render(); schedule(0);
      return true;
    },
    /** What the printed sheets need, or null when nothing is to be printed. */
    printData() {
      if (!visible()) return null;
      const where = t.kind === "area" ? (t.point ? toMGRS(t.point.lat, t.point.lng, 5) : "") : t.a && t.b ? `from A ${toMGRS(t.a.lat, t.a.lng, 5)} to B ${toMGRS(t.b.lat, t.b.lng, 5)}` : "";
      if (t.kind === "area") {
        if (!area || !t.point) return null;
        return { t: { ...area.t, budgetOn: t.budgetOn, powerW: t.powerW, txGain: t.txGain, rxGain: t.rxGain, cableLoss: t.cableLoss, sens: t.sens }, area: area.result, src, where, inputs: inputsLine(area.t, units(), where), data: dataLine(src), legend: legend(t) };
      }
      if (!line || !line.prof || !sameLine()) return null;
      return {
        t: { ...t }, line: { prof: line.prof, pts: line.pts }, src, where, inputs: inputsLine(t, units(), where), data: dataLine(src), legend: [],
        readouts: profileReadouts(line.prof, t, { units: units(), ...bearingTexts() }), profilePage: !!t.printProfile,
      };
    },
  };
}
