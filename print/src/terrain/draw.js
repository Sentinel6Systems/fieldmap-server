// Drawing for the terrain analysis, shared by the screen and the printed sheets: overlay
// colours and black-and-white patterns, the legend, the profile chart and the read-outs.
import { wattsToDbm } from "./core.js";

export const FT = 0.3048;
export const LIMITS = "Terrain only: no trees, buildings or clutter loss, no ground reflection or ducting, single-edge diffraction. " +
  "Real coverage is usually worse in forest and towns. A planning estimate, not a guarantee.";
export const VIEW_LIMITS = "Bare terrain from 30 m elevation data: trees and buildings are only partly in it. A planning estimate, not a guarantee.";
export const HF_NOTE = "VHF and UHF line of sight only: CB and HF (ground wave, skywave) are outside this model.";

/** Overlay styles. pattern: how black-and-white ink draws it. */
const STYLES = {
  ground: { rgba: [150, 20, 190, 115], pattern: "hatch" },
  target: { rgba: [255, 140, 0, 120], pattern: "dots" },
  clear: { rgba: [0, 160, 70, 125], pattern: "dense" },
  marginal: { rgba: [235, 200, 0, 130], pattern: "hatch" },
  diffraction: { rgba: [240, 110, 0, 125], pattern: "dots" },
};

/** Legend entries for the current settings: [{ key, label, style }]. Classes without a
 *  style (not visible, blocked, no link) are left uncoloured and listed without a swatch. */
export function legend(t) {
  if (t.mode === "viewshed") {
    const out = [{ key: "ground", label: "Ground visible", style: STYLES.ground }];
    if (t.tgtH > 0) out.push({ key: "target", label: "Target visible, ground hidden", style: STYLES.target });
    out.push({ key: "none", label: "Not visible: no colour" });
    return out;
  }
  if (t.budgetOn) {
    return [
      { key: "clear", label: "Comfortable: 20 dB or more to spare", style: STYLES.clear },
      { key: "marginal", label: "Workable: 10 to 20 dB", style: STYLES.marginal },
      { key: "diffraction", label: "Unlikely: 0 to 10 dB", style: STYLES.diffraction },
      { key: "none", label: "None: below the receiver's sensitivity, or two or more ridges in the way" },
    ];
  }
  return [
    { key: "clear", label: "Clear: line of sight, 60 % of the Fresnel zone clear", style: STYLES.clear },
    { key: "marginal", label: "Marginal: line of sight, Fresnel zone partly blocked", style: STYLES.marginal },
    { key: "diffraction", label: "Diffraction: one ridge in the way, up to 20 dB extra loss", style: STYLES.diffraction },
    { key: "none", label: "Blocked: more than 20 dB, or two or more ridges: no colour" },
  ];
}

/** Received level budget in dBm without the path loss: power + gains - cable loss. */
export function budgetBase(t) {
  return wattsToDbm(Math.max(1e-6, t.powerW)) + t.txGain + t.rxGain - t.cableLoss;
}

/** Style key per raster pixel. result: worker result (data = classes, loss = dB for radio). */
export function pixelKeys(result, t) {
  const n = result.data.length, keys = new Array(n).fill(null);
  if (result.kind === "viewshed") {
    for (let i = 0; i < n; i++) keys[i] = result.data[i] === 2 ? "ground" : result.data[i] === 3 && t.tgtH > 0 ? "target" : null;
    return keys;
  }
  const base = budgetBase(t);
  for (let i = 0; i < n; i++) {
    const c = result.data[i];
    if (!c) continue;
    if (t.budgetOn) {
      if (c === 5) continue;
      const margin = base - result.loss[i] - t.sens;
      keys[i] = margin >= 20 ? "clear" : margin >= 10 ? "marginal" : margin >= 0 ? "diffraction" : null;
    } else keys[i] = c === 1 ? "clear" : c === 2 ? "marginal" : c === 3 ? "diffraction" : null;
  }
  return keys;
}

/* ------------------------------------------------------------- patterns */

/** A repeating pattern tile for black-and-white ink, `s` pixels square. */
function patternTile(kind, s, lw) {
  const c = OffscreenCanvasOr(s, s), g = c.getContext("2d");
  g.strokeStyle = g.fillStyle = "#000";
  g.lineWidth = lw;
  if (kind === "dots") {
    g.beginPath(); g.arc(s / 2, s / 2, lw * 1.1, 0, 2 * Math.PI); g.fill();
  } else {
    // Diagonal lines (lower left to upper right), drawn three times so the tile joins up.
    for (const o of [-s, 0, s]) { g.beginPath(); g.moveTo(o, s); g.lineTo(o + s, 0); g.stroke(); }
    if (kind === "dense") for (const o of [-s, 0, s]) { g.beginPath(); g.moveTo(o, 0); g.lineTo(o + s, s); g.stroke(); }
  }
  return c;
}
function OffscreenCanvasOr(w, h) {
  if (typeof document !== "undefined") { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; }
  return new OffscreenCanvas(w, h);
}

/** Fill style for a style key: its colour, or a pattern for black-and-white ink.
 *  unit: pattern spacing in pixels. */
function fillFor(ctx, style, bw, unit) {
  if (!bw) { const [r, g, b, a] = style.rgba; return `rgba(${r},${g},${b},${(a / 255).toFixed(3)})`; }
  const s = Math.max(4, Math.round(style.pattern === "dots" ? unit * 0.9 : unit));
  return ctx.createPattern(patternTile(style.pattern, s, Math.max(1, unit / 6)), "repeat");
}

/** The overlay as a canvas the size of the result raster, for the map on screen. */
export function overlayCanvas(result, t, bw) {
  const { width, height } = result, keys = pixelKeys(result, t);
  const c = OffscreenCanvasOr(width, height), g = c.getContext("2d");
  const img = g.createImageData(width, height), d = img.data;
  // Black and white on screen: patterns in raster pixels (a few pixels apart).
  for (let i = 0; i < keys.length; i++) {
    const k = keys[i];
    if (!k) continue;
    const st = STYLES[k], p = i * 4;
    if (!bw) { d[p] = st.rgba[0]; d[p + 1] = st.rgba[1]; d[p + 2] = st.rgba[2]; d[p + 3] = st.rgba[3]; continue; }
    const x = i % width, y = (i / width) | 0;
    const on = st.pattern === "dots" ? x % 4 === 1 && y % 4 === 1 : st.pattern === "dense" ? (x + y) % 3 === 0 || (x - y + 3000) % 3 === 0 : (x + y) % 4 === 0;
    if (on) { d[p] = d[p + 1] = d[p + 2] = 20; d[p + 3] = 230; }
  }
  g.putImageData(img, 0, 0);
  return c;
}

/** Draw the overlay on a printed page. toPage(lng, lat) -> page pixels; clip: map area. */
export function drawOverlay(ctx, result, t, bw, toPage, clip, dpi) {
  const [w, s, e, n] = result.bounds;
  const tl = toPage(w, n), br = toPage(e, s);
  const x0 = Math.max(clip.x, Math.floor(tl.x)), y0 = Math.max(clip.y, Math.floor(tl.y));
  const x1 = Math.min(clip.x + clip.w, Math.ceil(br.x)), y1 = Math.min(clip.y + clip.h, Math.ceil(br.y));
  if (x1 <= x0 || y1 <= y0) return;
  const keys = pixelKeys(result, t);
  const tmp = OffscreenCanvasOr(x1 - x0, y1 - y0), g = tmp.getContext("2d");
  const mask = OffscreenCanvasOr(result.width, result.height), mg = mask.getContext("2d");
  for (const key of new Set(keys.filter(Boolean))) {
    const img = mg.createImageData(result.width, result.height);
    for (let i = 0; i < keys.length; i++) if (keys[i] === key) img.data[i * 4 + 3] = 255;
    mg.putImageData(img, 0, 0);
    g.globalCompositeOperation = "source-over";
    g.clearRect(0, 0, tmp.width, tmp.height);
    g.imageSmoothingEnabled = false;
    g.drawImage(mask, tl.x - x0, tl.y - y0, br.x - tl.x, br.y - tl.y);
    g.globalCompositeOperation = "source-in";
    g.fillStyle = fillFor(g, STYLES[key], bw, dpi * 0.045);
    g.fillRect(0, 0, tmp.width, tmp.height);
    ctx.drawImage(tmp, x0, y0);
  }
}

/** A legend swatch at (x, y), w × h pixels. */
export function drawSwatch(ctx, x, y, w, h, style, bw, unit) {
  ctx.save();
  ctx.fillStyle = "#fff"; ctx.fillRect(x, y, w, h);     // as on paper
  if (style) {
    ctx.fillStyle = fillFor(ctx, style, bw, unit);
    ctx.fillRect(x, y, w, h);
  }
  ctx.strokeStyle = "#444"; ctx.lineWidth = Math.max(1, unit / 8);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
}

/* ------------------------------------------------------------- units and text */

export function fmtH(m, units) {
  return units === "metric" ? `${+m.toFixed(m < 10 ? 1 : 0)} m` : `${Math.round(m / FT)} ft`;
}
export function fmtD(m, units) {
  if (units === "metric") return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
  const mi = m / 1609.344;
  return mi < 0.2 ? `${Math.round(m / FT)} ft` : `${mi.toFixed(mi < 10 ? 2 : 1)} mi`;
}
export const fmtK = (k) => (Math.abs(k - 7 / 6) < 1e-6 ? "7/6" : Math.abs(k - 4 / 3) < 1e-6 ? "4/3" : (+k.toFixed(3)).toString());
export const fmtDb = (v) => `${v >= 0 ? "" : "−"}${Math.abs(v).toFixed(1)} dB`;

/** One-line summary of the inputs, for the sheet collar and the panel. */
export function inputsLine(t, units, where) {
  const parts = [];
  if (t.mode === "viewshed") {
    parts.push(t.kind === "line" ? `Line of sight${where ? " " + where : ""}` : `Viewshed${where ? " from " + where : ""}`);
    parts.push(`observer ${fmtH(t.obsH, units)} above ground`, `target ${fmtH(t.tgtH, units)}`);
  } else {
    parts.push(t.kind === "line" ? `Radio path${where ? " " + where : ""}` : `Radio coverage${where ? " from " + where : ""}`);
    parts.push(`${+t.freq.toFixed(3)} MHz`, ...(t.kind === "line"
      ? [`antenna ${fmtH(t.txH, units)} at A`, `${fmtH(t.rxH, units)} at B`]
      : [`transmitter antenna ${fmtH(t.txH, units)}`, `receiver antenna ${fmtH(t.rxH, units)}`]));
  }
  if (t.kind !== "line") parts.push(`radius ${fmtD(t.radius, units)}`);
  parts.push(`k ${fmtK(t.mode === "viewshed" ? t.kVis : t.kRadio)}`);
  if (t.mode === "radio" && t.budgetOn) {
    parts.push(`budget ${+t.powerW.toFixed(2)} W, gains ${t.txGain} / ${t.rxGain} dBi, cable ${t.cableLoss} dB, sensitivity ${t.sens} dBm`);
  }
  return parts.join(" · ");
}

/** Elevation data line: source, date and when the tiles were built. */
export function dataLine(src) {
  if (!src) return "";
  return `Elevation ${src.source || "data"}${src.sourceDate ? ` (${src.sourceDate})` : ""}${src.built ? `, tiles built ${src.built}` : ""}`;
}

/* ------------------------------------------------------------- profile */

/** Read-outs for a profile: [[label, value], ...]. info: { bearing: {true, mag}, units }. */
export function profileReadouts(prof, t, info) {
  const u = info.units, out = [["Distance", fmtD(prof.distance, u)], ["Bearing A to B", `${info.trueTxt} true, ${info.magTxt} magnetic`]];
  if (t.mode === "viewshed") {
    out.push(["Line of sight", prof.los ? "Yes: B can be seen from A" : "No: terrain is in the way"]);
    if (prof.worstIndex > 0) out.push([prof.los ? "Closest clearance" : "Worst obstruction", `${fmtH(Math.abs(prof.worstClearance), u)} ${prof.los ? "below the sight line" : "above the sight line"}, ${fmtD(prof.series[prof.worstIndex].d, u)} from A`]);
    return out;
  }
  const r = prof.radio;
  out.push(["Path", ["", "Clear", "Marginal", "Diffraction", "Blocked (over 20 dB)", "Blocked (two or more ridges)"][r.cls]]);
  if (r.index > 0) out.push(["Worst point", `${fmtD(prof.series[r.index].d, u)} from A, clearance ${Math.round(r.clearancePct)} % of the first Fresnel zone`]);
  out.push(["Free-space loss", fmtDb(r.fspl)], ["Diffraction loss", fmtDb(r.diffraction)]);
  if (t.budgetOn) {
    const margin = budgetBase(t) - r.fspl - r.diffraction - t.sens;
    out.push(["Received level", `${(budgetBase(t) - r.fspl - r.diffraction).toFixed(1)} dBm`]);
    out.push(["Link margin", `${fmtDb(margin)}${r.cls === 5 ? " (optimistic: two or more ridges)" : ""}`]);
  }
  return out;
}

/** Profile chart in the rectangle (x, y, w, h). o: { units, fontPx, bw, mode } */
export function drawProfile(ctx, prof, rect, o) {
  const { x, y, w, h } = rect, u = o.units, fp = o.fontPx, radio = o.mode === "radio" && prof.radio;
  const hu = u === "metric" ? 1 : FT, du = u === "metric" ? 1000 : 1609.344;
  const s = prof.series, D = prof.distance;
  // Vertical range: terrain, antenna tops and (radio) the full Fresnel zone.
  let lo = Infinity, hi = -Infinity;
  const lineAt = (d) => prof.txTop + ((prof.rxTop - prof.txTop) * d) / D;
  for (const p of s) {
    if (!Number.isFinite(p.terrain)) continue;
    lo = Math.min(lo, p.terrain); hi = Math.max(hi, p.terrain);
    if (radio) { const f = radio.fresnelAt(p.d); lo = Math.min(lo, lineAt(p.d) - f); hi = Math.max(hi, lineAt(p.d) + f); }
  }
  lo = Math.min(lo, prof.txTop, prof.rxTop); hi = Math.max(hi, prof.txTop, prof.rxTop);
  if (!Number.isFinite(lo)) return;
  const pad = (hi - lo) * 0.08 + 2; lo -= pad; hi += pad;
  const L = x + fp * 4.2, R = x + w - fp * 0.8, T = y + fp * 0.8, B = y + h - fp * 2.6;
  const X = (d) => L + ((R - L) * d) / D, Y = (v) => B - ((B - T) * (v - lo)) / (hi - lo);
  ctx.save();
  ctx.font = `${fp}px Helvetica, Arial, sans-serif`; ctx.textBaseline = "middle";
  // Grid and axes
  const niceStep = (range, n) => { const r = range / n, p = 10 ** Math.floor(Math.log10(r)); return [1, 2, 5, 10].map((m) => m * p).find((v) => v >= r); };
  const vs = niceStep((hi - lo) / hu, 5);
  ctx.strokeStyle = "#ddd"; ctx.lineWidth = Math.max(1, fp / 12); ctx.fillStyle = "#333"; ctx.textAlign = "right";
  for (let v = Math.ceil(lo / hu / vs) * vs; v <= hi / hu; v += vs) {
    ctx.beginPath(); ctx.moveTo(L, Y(v * hu)); ctx.lineTo(R, Y(v * hu)); ctx.stroke();
    ctx.fillText(`${Math.round(v)}`, L - fp * 0.4, Y(v * hu));
  }
  const ds = niceStep(D / du, 6);
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let d = 0; d <= D / du + 1e-9; d += ds) {
    ctx.beginPath(); ctx.moveTo(X(d * du), T); ctx.lineTo(X(d * du), B); ctx.stroke();
    ctx.fillText(`${+d.toFixed(3)}`, X(d * du), B + fp * 0.3);
  }
  const caption = `Distance from A (${u === "metric" ? "km" : "mi"}) · height above sea level (${u === "metric" ? "m" : "ft"}) · earth bulge for k ${fmtK(o.k)}`;
  const cw = ctx.measureText(caption).width;
  if (cw > R - L) ctx.font = `${fp * Math.max(0.6, (R - L) / cw)}px Helvetica, Arial, sans-serif`;
  ctx.fillText(caption, (L + R) / 2, B + fp * 1.45);
  ctx.font = `${fp}px Helvetica, Arial, sans-serif`;
  // Terrain
  ctx.beginPath(); ctx.moveTo(X(0), B);
  for (const p of s) if (Number.isFinite(p.terrain)) ctx.lineTo(X(p.d), Y(p.terrain));
  ctx.lineTo(X(D), B); ctx.closePath();
  ctx.fillStyle = o.bw ? "#cfcfcf" : "#d8c3a0"; ctx.fill();
  ctx.strokeStyle = o.bw ? "#333" : "#7a5a2a"; ctx.lineWidth = Math.max(1, fp / 8); ctx.stroke();
  // Fresnel zone (radio): 100 % dashed, 60 % dotted
  if (radio) {
    for (const [frac, dash] of [[1, [fp * 0.5, fp * 0.35]], [0.6, [fp * 0.15, fp * 0.3]]]) {
      ctx.setLineDash(dash); ctx.strokeStyle = o.bw ? "#000" : "#1f5fa8"; ctx.lineWidth = Math.max(1, fp / 10);
      for (const sign of [1, -1]) {
        ctx.beginPath();
        for (let i = 0; i < s.length; i++) { const d = s[i].d; const v = lineAt(d) + sign * frac * radio.fresnelAt(d); i ? ctx.lineTo(X(d), Y(v)) : ctx.moveTo(X(d), Y(v)); }
        ctx.stroke();
      }
    }
    ctx.setLineDash([]);
  }
  // Masts and the path
  ctx.strokeStyle = "#000"; ctx.lineWidth = Math.max(1.5, fp / 6);
  for (const [d, top, g] of [[0, prof.txTop, s[0].ground], [D, prof.rxTop, s[s.length - 1].ground]]) { ctx.beginPath(); ctx.moveTo(X(d), Y(g)); ctx.lineTo(X(d), Y(top)); ctx.stroke(); }
  ctx.strokeStyle = o.bw ? "#000" : prof.los ? "#0a7d32" : "#c0392b"; ctx.lineWidth = Math.max(1.5, fp / 5);
  ctx.beginPath(); ctx.moveTo(X(0), Y(prof.txTop)); ctx.lineTo(X(D), Y(prof.rxTop)); ctx.stroke();
  // Worst point
  const wi = prof.worstIndex;
  if (wi > 0) {
    const p = s[wi], px = X(p.d), py = Y(p.terrain), r = fp * 0.45;
    ctx.strokeStyle = o.bw ? "#000" : "#c0392b"; ctx.lineWidth = Math.max(1.5, fp / 6);
    ctx.beginPath(); ctx.moveTo(px - r, py - r); ctx.lineTo(px + r, py + r); ctx.moveTo(px + r, py - r); ctx.lineTo(px - r, py + r); ctx.stroke();
    ctx.fillStyle = "#000"; ctx.textAlign = px > (L + R) / 2 ? "right" : "left"; ctx.textBaseline = "bottom";
    ctx.fillText(radio ? "worst point" : prof.los ? "closest point" : "obstruction", px + (px > (L + R) / 2 ? -r : r), py - r);
  }
  ctx.fillStyle = "#000"; ctx.textBaseline = "bottom"; ctx.font = `bold ${fp}px Helvetica, Arial, sans-serif`;
  ctx.textAlign = "left"; ctx.fillText("A", X(0) + fp * 0.3, Y(prof.txTop) - fp * 0.2);
  ctx.textAlign = "right"; ctx.fillText("B", X(D) - fp * 0.3, Y(prof.rxTop) - fp * 0.2);
  ctx.strokeStyle = "#555"; ctx.lineWidth = Math.max(1, fp / 10); ctx.strokeRect(L, T, R - L, B - T);
  ctx.restore();
}
