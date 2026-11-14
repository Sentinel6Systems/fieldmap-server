// Terrain analysis engine: elevation tiles -> a local grid in metres -> viewshed, radio
// coverage and point-to-point profiles. Pure functions with no DOM or network access, so
// the web worker (worker.js) and the Node tests (print/test/) run the same code.
//
// Heights are metres above sea level. Earth curvature with refraction uses an effective
// radius k × 6,371 km: a point at distance d sits d² / (2 k R) lower than on a flat earth.

export const EARTH_R = 6371000;
export const C = 299792458;
export const NODATA_BELOW = -9000;          // decoded heights below this are "no data"
const A = 6378137, E2 = 0.00669437999014;   // WGS 84
const D2R = Math.PI / 180;
export const TILE = 256;

/* ---------------------------------------------------------------- tiles */

/** RGBA pixels of one tile -> Float32Array of heights (NaN where there is no data). */
export function decodeTile(rgba, encoding, size = TILE) {
  const out = new Float32Array(size * size);
  for (let i = 0, p = 0; i < out.length; i++, p += 4) {
    const r = rgba[p], g = rgba[p + 1], b = rgba[p + 2];
    if (rgba[p + 3] === 0 || (r === 0 && g === 0 && b === 0)) { out[i] = NaN; continue; }
    const h = encoding === "terrarium" ? r * 256 + g + b / 256 - 32768 : -10000 + (r * 65536 + g * 256 + b) * 0.1;
    out[i] = h < NODATA_BELOW ? NaN : h;
  }
  return out;
}

/** Ground size of one tile pixel in metres at a latitude and zoom. */
export function pixelMetres(lat, z) {
  return (2 * Math.PI * A * Math.cos(lat * D2R)) / (TILE * 2 ** z);
}

/** The zoom whose pixels are no coarser than `res` metres, within [minzoom, maxzoom]. */
export function chooseZoom(lat, res, minzoom, maxzoom) {
  for (let z = minzoom; z <= maxzoom; z++) if (pixelMetres(lat, z) <= res * 1.05) return z;
  return maxzoom;
}

const mercY = (lat) => (1 - Math.asinh(Math.tan(lat * D2R)) / Math.PI) / 2;   // 0..1, north at 0
const mercLat = (y) => Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) / D2R;

/** Tiles [x, y] at zoom z that cover a lon/lat box. */
export function tilesForBounds([w, s, e, n], z) {
  const N = 2 ** z, out = [];
  const x0 = Math.floor(((w + 180) / 360) * N), x1 = Math.floor(((e + 180) / 360) * N);
  const y0 = Math.floor(mercY(n) * N), y1 = Math.floor(mercY(s) * N);
  for (let y = Math.max(0, y0); y <= Math.min(N - 1, y1); y++)
    for (let x = Math.max(0, x0); x <= Math.min(N - 1, x1); x++) out.push([x, y]);
  return out;
}

/** A height sampler over decoded tiles at zoom z. tile(x, y) returns a Float32Array or null.
 *  Bilinear between pixel centres; NaN when any of the four pixels is missing. */
export function tileSampler(z, tile) {
  const W = TILE * 2 ** z;
  let lastKey = -1, last = null;
  const px = (ix, iy) => {
    const tx = ix >> 8, ty = iy >> 8, key = ty * 1e7 + tx;
    if (key !== lastKey) { lastKey = key; last = tile(tx, ty); }
    return last ? last[(iy & 255) * TILE + (ix & 255)] : NaN;
  };
  const at = (fx, fy) => {
    const x0 = Math.floor(fx), y0 = Math.floor(fy), dx = fx - x0, dy = fy - y0;
    const a = px(x0, y0), b = px(x0 + 1, y0), c = px(x0, y0 + 1), d = px(x0 + 1, y0 + 1);
    return (a * (1 - dx) + b * dx) * (1 - dy) + (c * (1 - dx) + d * dx) * dy;
  };
  return {
    z,
    /** World pixel coordinates (pixel centres at .5) for a longitude / latitude. */
    fx: (lon) => ((lon + 180) / 360) * W - 0.5,
    fy: (lat) => mercY(lat) * W - 0.5,
    at,
    height: (lon, lat) => at(((lon + 180) / 360) * W - 0.5, mercY(lat) * W - 0.5),
  };
}

/* ---------------------------------------------------------------- local grid */

/** Radii of curvature (metres) of the WGS 84 ellipsoid at a latitude. */
function radii(lat) {
  const s = Math.sin(lat * D2R), w = 1 - E2 * s * s;
  return { M: (A * (1 - E2)) / (w * Math.sqrt(w)), N: A / Math.sqrt(w) };
}

/** A square grid of cells `res` metres apart, centred on a point, large enough for a
 *  circle of `radius` metres. Cell (i, j): i east from the west edge, j south from the
 *  north edge; the centre cell is (c, c). x = (i - c) res east, y = (c - j) res north. */
export function gridSpec(center, radius, res) {
  const c = Math.ceil(radius / res), n = 2 * c + 1;
  const { M, N } = radii(center.lat);
  const lat = (y) => center.lat + y / M / D2R;
  const lon = (x, la) => center.lng + x / (N * Math.cos(la * D2R)) / D2R;
  const toXY = (lng, la) => ({ x: (lng - center.lng) * D2R * N * Math.cos(la * D2R), y: (la - center.lat) * D2R * M });
  const half = c * res + res;
  const latN = lat(half), latS = lat(-half);
  const wide = Math.max(Math.abs(lon(half, latN) - center.lng), Math.abs(lon(half, latS) - center.lng));
  return { center, radius, res, c, n, lat, lon, toXY, bounds: [center.lng - wide, latS, center.lng + wide, latN] };
}

/** Fill a grid spec with heights from a tile sampler. Returns Float32Array n*n (row 0 north). */
export async function buildGrid(spec, sampler, tick = noTick) {
  const { n, c, res } = spec, h = new Float32Array(n * n);
  for (let j = 0; j < n; j++) {
    const la = spec.lat((c - j) * res), fy = sampler.fy(la);
    const lonAt = spec.lon(0, la), dLon = spec.lon(res, la) - lonAt;
    const fx0 = sampler.fx(lonAt - c * dLon), dfx = sampler.fx(lonAt + dLon) - sampler.fx(lonAt);
    for (let i = 0; i < n; i++) h[j * n + i] = sampler.at(fx0 + i * dfx, fy);
    if ((j & 31) === 31) await tick(j / n);
  }
  return h;
}
const noTick = async () => {};

/* ---------------------------------------------------------------- radio formulas */

/** Free-space basic transmission loss (ITU-R P.525): 32.45 + 20 log f(MHz) + 20 log d(km). */
export function fspl(dMetres, fMHz) {
  return 32.45 + 20 * Math.log10(fMHz) + 20 * Math.log10(dMetres / 1000);
}
/** Radius of the first Fresnel zone in metres at d1, d2 metres from the ends (ITU-R P.526). */
export function fresnelRadius(d1, d2, fMHz) {
  const lambda = C / (fMHz * 1e6);
  return Math.sqrt((lambda * d1 * d2) / (d1 + d2));
}
/** Diffraction parameter ν for an obstacle h metres above the direct line (ITU-R P.526 eq. 26). */
export function fresnelNu(h, d1, d2, fMHz) {
  const lambda = C / (fMHz * 1e6);
  return h * Math.sqrt((2 * (d1 + d2)) / (lambda * d1 * d2));
}
/** Single knife-edge diffraction loss J(ν) in dB (ITU-R P.526 eq. 31); 0 for ν ≤ -0.78. */
export function knifeEdgeLoss(nu) {
  if (nu <= -0.78) return 0;
  return 6.9 + 20 * Math.log10(Math.sqrt((nu - 0.1) ** 2 + 1) + nu - 0.1);
}
export const wattsToDbm = (w) => 10 * Math.log10(w * 1000);

/* ---------------------------------------------------------------- viewshed */

/** Viewshed over a grid by R2 ray casting: one ray from the observer to every cell on the
 *  edge of the grid; along a ray the height between two cells is interpolated and the
 *  steepest slope so far is the horizon. A cell is visible when any ray through it sees it.
 *  p: { obsH, tgtH, k, radius } (metres). Returns Uint8Array n*n:
 *  0 outside the radius or no data, 1 not visible, 2 ground visible,
 *  3 ground hidden but a target tgtH above it visible. */
export async function viewshed(spec, h, p, tick = noTick) {
  const { n, c, res } = spec;
  const out = new Uint8Array(n * n);
  const z0 = h[c * n + c];
  if (Number.isNaN(z0)) return out;
  const eye = z0 + p.obsH, kR2 = 2 * (p.k || 7 / 6) * EARTH_R, tgt = p.tgtH || 0;
  const rMax = Math.min(p.radius ?? Infinity, c * res), r2Max = (rMax / res) ** 2;
  // Effective heights (curvature and refraction applied) relative to the eye.
  const he = new Float32Array(n * n);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const d2 = ((i - c) ** 2 + (j - c) ** 2) * res * res;
    he[j * n + i] = h[j * n + i] - d2 / kR2 - eye;
  }
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) if ((i - c) ** 2 + (j - c) ** 2 <= r2Max && !Number.isNaN(h[j * n + i])) out[j * n + i] = 1;
  out[c * n + c] = 2;
  const ends = [];
  for (let k = 0; k < n; k++) ends.push([k, 0], [k, n - 1], [0, k], [n - 1, k]);
  let done = 0;
  for (const [ei, ej] of ends) {
    const dx = ei - c, dy = ej - c, steps = Math.max(Math.abs(dx), Math.abs(dy));
    if (!steps) continue;
    const xMajor = Math.abs(dx) >= Math.abs(dy);
    const sx = dx / steps, sy = dy / steps;
    let horizon = -Infinity;
    for (let t = 1; t <= steps; t++) {
      const fx = c + sx * t, fy = c + sy * t;
      // Interpolate across the minor axis between the two cells the ray passes.
      let hz, ci, cj;
      if (xMajor) {
        const y0 = Math.floor(fy), w = fy - y0, x = Math.round(fx);
        const a = he[y0 * n + x], b = w > 1e-9 ? he[(y0 + 1) * n + x] : a;
        hz = a + (b - a) * w; ci = x; cj = Math.round(fy);
      } else {
        const x0 = Math.floor(fx), w = fx - x0, y = Math.round(fy);
        const a = he[y * n + x0], b = w > 1e-9 ? he[y * n + x0 + 1] : a;
        hz = a + (b - a) * w; ci = Math.round(fx); cj = y;
      }
      if (Number.isNaN(hz)) continue;
      const d = t * Math.hypot(sx, sy);
      const slope = hz / d, k = cj * n + ci;
      if (out[k]) {                                         // inside the circle, with data
        if (slope >= horizon) out[k] = 2;
        else if (tgt > 0 && (hz + tgt) / d >= horizon && out[k] === 1) out[k] = 3;
      }
      if (slope > horizon) horizon = slope;
    }
    if ((++done & 255) === 0) await tick(done / ends.length);
  }
  return out;
}

/* ---------------------------------------------------------------- radio */

/** One terrain profile against a radio path. z: Float64Array of heights at d = i * step
 *  (i = 0 is the transmitter). Effective heights subtract d² / (2 k R) so the path is a
 *  straight line. Returns the worst point (largest ν) and how many separate stretches of
 *  terrain cut the line of sight. */
export function pathAnalysis(z, step, e, txH, rxH, fMHz, k) {
  const kR2 = 2 * k * EARTH_R, lambda = C / (fMHz * 1e6);
  const D = e * step, ht = z[0] + txH, hr = z[e] - (D * D) / kR2 + rxH;
  let best = -Infinity, bi = -1, runs = 0, inRun = false;
  for (let i = 1; i < e; i++) {
    const d1 = i * step, line = ht + ((hr - ht) * i) / e, ze = z[i] - (d1 * d1) / kR2;
    const cl = line - ze;
    const s = (-cl * Math.abs(cl)) / (d1 * (D - d1));
    if (s > best) { best = s; bi = i; }
    if (cl < 0) { if (!inRun) { runs++; inRun = true; } } else inRun = false;
  }
  if (bi < 0) return { nu: -Infinity, index: -1, clearance: Infinity, fresnel: 0, runs: 0 };
  const d1 = bi * step, d2 = D - d1;
  const line = ht + ((hr - ht) * bi) / e, cl = line - (z[bi] - (d1 * d1) / kR2);
  const F = Math.sqrt((lambda * d1 * d2) / D);
  return { nu: (-cl / F) * Math.SQRT2, index: bi, clearance: cl, fresnel: F, runs };
}

/** Path class from a path analysis: 1 clear (line of sight, ≥ 60 % of the first Fresnel
 *  zone clear), 2 marginal (line of sight, zone partly blocked), 3 diffraction (one
 *  obstruction, ≤ 20 dB), 4 blocked (more than 20 dB, or two or more obstructions). */
export function pathClass(a) {
  if (a.nu < 0) return a.clearance >= 0.6 * a.fresnel || a.index < 0 ? 1 : 2;
  if (a.runs >= 2 || knifeEdgeLoss(a.nu) > 20) return 4;
  return 3;
}

/** Radio coverage along radials from the grid centre: enough radials that neighbours are
 *  at most one cell apart at the edge, samples one cell apart along each. p: { txH, rxH,
 *  fMHz, k, radius }. Returns { nRad, nSamp, cls: Uint8Array, loss: Float32Array } indexed
 *  [radial * nSamp + sample]; sample s is at (s + 1) * res. loss = free-space + diffraction (dB). */
export async function radioArea(spec, h, p, tick = noTick) {
  const { n, c, res } = spec;
  const rCells = Math.min(Math.floor(p.radius / res), c);
  const nRad = Math.max(8, Math.ceil((2 * Math.PI * rCells * res) / res)), nSamp = rCells;
  const cls = new Uint8Array(nRad * nSamp), loss = new Float32Array(nRad * nSamp).fill(NaN);
  const z = new Float64Array(nSamp + 1);
  const at = (x, y) => {   // bilinear in grid cells
    let x0 = Math.floor(x), y0 = Math.floor(y), dx = x - x0, dy = y - y0;
    if (x0 >= n - 1) { x0 = n - 2; dx = 1; }
    if (y0 >= n - 1) { y0 = n - 2; dy = 1; }
    const i = y0 * n + x0;
    return (h[i] * (1 - dx) + h[i + 1] * dx) * (1 - dy) + (h[i + n] * (1 - dx) + h[i + n + 1] * dx) * dy;
  };
  z[0] = h[c * n + c];
  if (Number.isNaN(z[0])) return { nRad, nSamp, cls, loss };
  // Same test as pathAnalysis, inlined: per radial the effective heights are computed once,
  // and the worst point is the largest -cl|cl| / (i (e - i)), which orders points as ν does.
  const kR2 = 2 * p.k * EARTH_R, lambda = C / (p.fMHz * 1e6), ze = new Float64Array(nSamp + 1);
  const fsplAt = new Float64Array(nSamp + 1), inv = new Float64Array(nSamp + 1);
  for (let e = 1; e <= nSamp; e++) { fsplAt[e] = fspl(e * res, p.fMHz); inv[e] = 1 / e; }
  const ht = z[0] + p.txH;
  for (let r = 0; r < nRad; r++) {
    const th = (2 * Math.PI * r) / nRad, sx = Math.sin(th), sy = -Math.cos(th);   // grid rows run south
    let valid = nSamp;
    for (let s = 1; s <= nSamp; s++) {
      z[s] = at(c + sx * s, c + sy * s);
      if (Number.isNaN(z[s])) { valid = s - 1; break; }
      ze[s] = z[s] - (s * res) ** 2 / kR2;
    }
    for (let e = 1; e <= valid; e++) {
      const hr = ze[e] + p.rxH, slope = (hr - ht) / e;
      let best = -Infinity, bi = -1, runs = 0, inRun = false;
      for (let i = 1; i < e; i++) {
        const cl = ht + slope * i - ze[i];
        const sc = -cl * Math.abs(cl) * inv[i] * inv[e - i];
        if (sc > best) { best = sc; bi = i; }
        if (cl < 0) { if (!inRun) { runs++; inRun = true; } } else inRun = false;
      }
      const k = r * nSamp + e - 1;
      if (bi < 0) { cls[k] = 1; loss[k] = fsplAt[e]; continue; }
      const cl = ht + slope * bi - ze[bi], F = Math.sqrt((lambda * bi * (e - bi) * res) / e);
      const a = { nu: (-cl / F) * Math.SQRT2, index: bi, clearance: cl, fresnel: F, runs };
      cls[k] = pathClass(a);
      loss[k] = fsplAt[e] + knifeEdgeLoss(a.nu);
    }
    if ((r & 15) === 15) await tick(r / nRad);
  }
  return { nRad, nSamp, cls, loss };
}

/* ---------------------------------------------------------------- profile */

/** Heights along the great circle from a to b, `step` metres apart (at least 2 samples). */
export function profilePoints(a, b, step, distance) {
  const nSeg = Math.max(1, Math.ceil(distance / step));
  const φ1 = a.lat * D2R, λ1 = a.lng * D2R, φ2 = b.lat * D2R, λ2 = b.lng * D2R;
  const δ = 2 * Math.asin(Math.sqrt(Math.sin((φ2 - φ1) / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin((λ2 - λ1) / 2) ** 2));
  const pts = [];
  for (let i = 0; i <= nSeg; i++) {
    const f = i / nSeg;
    if (δ < 1e-12) { pts.push({ lng: a.lng, lat: a.lat }); continue; }
    const A1 = Math.sin((1 - f) * δ) / Math.sin(δ), B1 = Math.sin(f * δ) / Math.sin(δ);
    const x = A1 * Math.cos(φ1) * Math.cos(λ1) + B1 * Math.cos(φ2) * Math.cos(λ2);
    const y = A1 * Math.cos(φ1) * Math.sin(λ1) + B1 * Math.cos(φ2) * Math.sin(λ2);
    const zz = A1 * Math.sin(φ1) + B1 * Math.sin(φ2);
    pts.push({ lat: Math.atan2(zz, Math.hypot(x, y)) / D2R, lng: Math.atan2(y, x) / D2R });
  }
  return { pts, step: distance / nSeg };
}

/** Point-to-point analysis of a sampled profile. p: { txH, rxH, k, fMHz (optional) }.
 *  Returns the chart series (curvature applied as a bulge over the chord) and read-outs. */
export function profileAnalysis(z, step, p) {
  const e = z.length - 1, D = e * step, kR2 = 2 * p.k * EARTH_R;
  const series = [];
  for (let i = 0; i <= e; i++) {
    const d = i * step, bulge = (d * (D - d)) / kR2;
    series.push({ d, ground: z[i], terrain: z[i] + bulge });
  }
  const ht = z[0] + p.txH, hr = z[e] + p.rxH;
  // Line-of-sight check by angles (independent of frequency).
  let worst = -1, worstCl = Infinity;
  for (let i = 1; i < e; i++) {
    const line = ht + ((hr - ht) * i) / e, cl = line - series[i].terrain;
    if (cl < worstCl) { worstCl = cl; worst = i; }
  }
  const out = { distance: D, series, txTop: ht, rxTop: hr, worstIndex: worst, worstClearance: worstCl, los: worstCl > 0 || worst < 0 };
  if (p.fMHz) {
    const a = pathAnalysis(Float64Array.from(z), step, e, p.txH, p.rxH, p.fMHz, p.k);
    out.radio = {
      ...a,
      fspl: fspl(D, p.fMHz),
      diffraction: a.index < 0 ? 0 : knifeEdgeLoss(a.nu),
      clearancePct: a.index < 0 ? Infinity : (a.clearance / a.fresnel) * 100,
      cls: pathClass(a),
      fresnelAt: (d) => fresnelRadius(Math.max(d, 1e-6), Math.max(D - d, 1e-6), p.fMHz),
    };
    out.worstIndex = a.index < 0 ? worst : a.index;
  }
  return out;
}

/* ---------------------------------------------------------------- output raster */

/** Resample a result to a Web Mercator raster for display and print: pixel size about
 *  `res`, covering the circle. value(x, y) gets local metres and returns the pixel value
 *  (a byte by default, 0 = transparent). Returns { width, height, bounds: [w, s, e, n], data: Uint8Array }. */
export function toMercator(spec, radius, value, ArrayType = Uint8Array) {
  const z = Math.max(0, Math.round(Math.log2((2 * Math.PI * A * Math.cos(spec.center.lat * D2R)) / (TILE * spec.res))));
  const W = TILE * 2 ** z;
  const [w0, s0, e0, n0] = gridSpec(spec.center, radius, spec.res).bounds;
  const x0 = Math.floor(((w0 + 180) / 360) * W), x1 = Math.ceil(((e0 + 180) / 360) * W);
  const y0 = Math.floor(mercY(n0) * W), y1 = Math.ceil(mercY(s0) * W);
  const width = x1 - x0, height = y1 - y0, data = new ArrayType(width * height);
  for (let py = 0; py < height; py++) {
    const lat = mercLat((y0 + py + 0.5) / W);
    for (let px = 0; px < width; px++) {
      const lng = ((x0 + px + 0.5) / W) * 360 - 180;
      const { x, y } = spec.toXY(lng, lat);
      data[py * width + px] = value(x, y);
    }
  }
  return { width, height, data, bounds: [(x0 / W) * 360 - 180, mercLat(y1 / W), (x1 / W) * 360 - 180, mercLat(y0 / W)] };
}
