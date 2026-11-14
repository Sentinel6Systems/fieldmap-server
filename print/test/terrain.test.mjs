// Unit tests for the terrain engine (src/terrain/core.js). Run: npm test (Node 22 or later).
// Synthetic terrain where the answer is known, and the radio formulas against worked
// examples from ITU-R P.525 and P.526.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  gridSpec, viewshed, radioArea, fspl, fresnelRadius, fresnelNu, knifeEdgeLoss, profileAnalysis,
  decodeTile, chooseZoom, pixelMetres, pathClass, pathAnalysis, EARTH_R,
} from "../src/terrain/core.js";

const FLAT = 1e9;   // k so large that the earth is flat
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg}: ${a} vs ${b} (±${tol})`);

/** A grid spec with heights from f(x, y) in metres east / north of the centre. */
function terrain(radius, res, f) {
  const spec = gridSpec({ lat: 40, lng: -100 }, radius, res);
  const { n, c } = spec, h = new Float32Array(n * n);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) h[j * n + i] = f((i - c) * res, (c - j) * res);
  return { spec, h, cell: (x, y) => (c - Math.round(y / res)) * n + c + Math.round(x / res) };
}

test("viewshed: from the top of a cone everything is visible", async () => {
  const t = terrain(3000, 30, (x, y) => 500 - 0.1 * Math.hypot(x, y));
  const v = await viewshed(t.spec, t.h, { obsH: 2, tgtH: 0, k: FLAT, radius: 3000 });
  let inside = 0, seen = 0;
  for (const c of v) if (c) { inside++; if (c === 2) seen++; }
  assert.equal(seen, inside);
});

test("viewshed: from the bottom of a conical pit everything is visible", async () => {
  const t = terrain(3000, 30, (x, y) => 0.1 * Math.hypot(x, y));
  const v = await viewshed(t.spec, t.h, { obsH: 2, tgtH: 0, k: FLAT, radius: 3000 });
  assert.ok(v.every((c) => c !== 1));
});

test("viewshed: the floor of a pit is hidden, the ground beyond it is visible", async () => {
  // A 20 m deep pit with vertical walls, 1.7 to 2.3 km east, 600 m wide north to south.
  const pit = (x, y) => x > 1700 && x < 2300 && Math.abs(y) < 300;
  const t = terrain(4000, 20, (x, y) => (pit(x, y) ? -20 : 0));
  const v = await viewshed(t.spec, t.h, { obsH: 2, tgtH: 0, k: FLAT, radius: 4000 });
  for (let x = 1760; x <= 2240; x += 20) assert.equal(v[t.cell(x, 0)], 1, `pit floor at ${x} m hidden`);
  for (let x = 100; x <= 1680; x += 20) assert.equal(v[t.cell(x, 0)], 2, `ground at ${x} m visible`);
  for (let x = 2320; x <= 3980; x += 20) assert.equal(v[t.cell(x, 0)], 2, `ground at ${x} m visible`);
});

test("viewshed: behind a single ridge the ground is hidden and a tall target is visible up to the expected range", async () => {
  // Ridge 50 m high, 60 m thick, 970 to 1030 m east. Eye 2 m: a 100 m target at distance d
  // is visible while (100 - 2) / d >= (50 - 2) / 970 (the ridge's near edge), i.e. d <= 1980 m.
  const t = terrain(4000, 10, (x) => (x >= 970 && x <= 1030 ? 50 : 0));
  const v = await viewshed(t.spec, t.h, { obsH: 2, tgtH: 100, k: FLAT, radius: 4000 });
  for (let x = 1040; x <= 1970; x += 10) assert.equal(v[t.cell(x, 0)], 3, `target at ${x} m visible, ground hidden`);
  for (let x = 1990; x <= 3990; x += 10) assert.equal(v[t.cell(x, 0)], 1, `nothing visible at ${x} m`);
  for (let x = 10; x <= 960; x += 10) assert.equal(v[t.cell(x, 0)], 2, `ground in front of the ridge at ${x} m`);
});

test("viewshed: curvature and refraction hide flat ground beyond the radio horizon", async () => {
  // Eye 10 m over flat ground, k = 4/3: horizon at sqrt(2 k R h) = 13.03 km.
  const k = 4 / 3, horizon = Math.sqrt(2 * k * EARTH_R * 10);
  const t = terrain(15000, 100, () => 0);
  const v = await viewshed(t.spec, t.h, { obsH: 10, tgtH: 0, k, radius: 15000 });
  for (let x = 100; x <= horizon - 150; x += 100) assert.equal(v[t.cell(x, 0)], 2, `${x} m visible`);
  for (let x = Math.ceil((horizon + 150) / 100) * 100; x <= 15000; x += 100) assert.equal(v[t.cell(x, 0)], 1, `${x} m hidden`);
});

test("free-space loss matches ITU-R P.525 (eq. 6: 32.4 + 20 log f + 20 log d)", () => {
  near(fspl(1000, 1000), 92.45, 0.01, "1 km at 1 GHz");
  near(fspl(10000, 146), 32.45 + 20 * Math.log10(146) + 20, 1e-9, "10 km at 146 MHz");
  // From the definition 20 log(4 pi d / lambda): 5 km at 462 MHz.
  near(fspl(5000, 462), 20 * Math.log10((4 * Math.PI * 5000 * 462e6) / 299792458), 0.01, "5 km at 462 MHz");
});

test("first Fresnel zone radius matches ITU-R P.526 eq. 2 (550 sqrt(d1 d2 / ((d1 + d2) f)), km and MHz)", () => {
  // The 550 of eq. 2 is a rounded sqrt(c) (547.5), so the two agree to about 0.5 %.
  near(fresnelRadius(5000, 5000, 150), 550 * Math.sqrt(25 / (10 * 150)), 0.4, "mid-path, 10 km, 150 MHz");
  near(fresnelRadius(1000, 9000, 915), 550 * Math.sqrt(9 / (10 * 915)), 0.1, "1 km from one end, 915 MHz");
  near(fresnelRadius(5000, 5000, 150), 70.7, 0.1, "worked value");
});

test("knife-edge loss J(nu) matches ITU-R P.526 eq. 31 and Fig. 9", () => {
  near(knifeEdgeLoss(-0.78), 0, 0.01, "nu = -0.78");
  near(knifeEdgeLoss(-1), 0, 0, "below -0.78");
  near(knifeEdgeLoss(0), 6.0, 0.05, "grazing: 6 dB");
  near(knifeEdgeLoss(1), 13.9, 0.05, "nu = 1");
  near(knifeEdgeLoss(2), 19.0, 0.1, "nu = 2");
  near(knifeEdgeLoss(2.4), 20.5, 0.1, "nu = 2.4 (about 20 dB)");
  // nu from geometry (eq. 26): obstacle 10 m above the line, 1 km from each end, 300 MHz.
  near(fresnelNu(10, 1000, 1000, 299.792458), 10 * Math.sqrt((2 * 2000) / (1 * 1000 * 1000)), 1e-9, "nu geometry");
});

test("point-to-point: a ridge mid-path, worked by hand", () => {
  // 10 km path over flat ground at 0 m with a 30 m ridge at 5 km. Antennas 10 m, 150 MHz, k = 4/3.
  // Bulge at mid-path: 5000 * 5000 / (2 * 4/3 * 6371000) = 1.472 m; ridge 31.47 m effective,
  // 21.47 m above the 10 m line. F1 = sqrt(lambda * 5000 * 5000 / 10000) = 70.68 m,
  // nu = sqrt(2) * 21.47 / 70.68 = 0.4296, J = 9.71 dB. Clearance = -30.4 % of F1.
  const step = 100, z = new Float64Array(101);
  z[50] = 30;
  const p = profileAnalysis(z, step, { txH: 10, rxH: 10, k: 4 / 3, fMHz: 150 });
  assert.equal(p.distance, 10000);
  assert.equal(p.worstIndex, 50);
  assert.equal(p.los, false);
  near(p.series[50].terrain, 31.472, 0.001, "ridge with bulge");
  near(p.radio.fresnel, 70.68, 0.01, "F1 at the ridge");
  near(p.radio.nu, 0.4296, 0.0005, "nu");
  near(p.radio.diffraction, 9.71, 0.01, "J(nu)");
  near(p.radio.clearancePct, -30.38, 0.05, "clearance");
  near(p.radio.fspl, 32.45 + 20 * Math.log10(150) + 20, 1e-9, "free-space loss");
  assert.equal(p.radio.cls, 3);
});

test("path classes: clear, marginal, diffraction, blocked", () => {
  const step = 100, z = new Float64Array(101);
  const cls = (zz, h) => pathClass(pathAnalysis(zz, step, 100, h, h, 150, 4 / 3));
  assert.equal(cls(z, 80), 1, "high antennas over flat ground");
  const bump = z.slice(); bump[50] = 15;
  assert.equal(cls(bump, 20), 2, "line of sight, zone partly blocked");
  const ridge = z.slice(); ridge[50] = 40;
  assert.equal(cls(ridge, 10), 3, "one ridge");
  const two = z.slice(); two[30] = 40; two[70] = 40;
  assert.equal(cls(two, 10), 4, "two ridges");
  const wall = z.slice(); wall[50] = 300;
  assert.equal(cls(wall, 10), 4, "more than 20 dB");
});

test("radio area: flat ground, high antennas: clear everywhere, loss is free-space", async () => {
  const t = terrain(5000, 50, () => 100);
  const r = await radioArea(t.spec, t.h, { txH: 60, rxH: 60, fMHz: 462, k: 4 / 3, radius: 5000 });
  assert.ok(r.nRad >= 2 * Math.PI * 100, "radials at most one cell apart at the edge");
  assert.ok(r.cls.every((c) => c === 1));
  near(r.loss[r.nSamp - 1], fspl(5000, 462), 1e-3, "loss at the edge");
});

test("tile decoding: Mapbox terrain-RGB and Terrarium, no-data pixels", () => {
  const px = new Uint8ClampedArray([1, 134, 160, 255, 0, 0, 0, 255, 128, 0, 128, 255, 9, 9, 9, 0]);
  const m = decodeTile(px, "mapbox", 2);
  near(m[0], 0, 1e-6, "mapbox 0 m");
  assert.ok(Number.isNaN(m[1]) && Number.isNaN(m[3]), "black and transparent are no data");
  const t = decodeTile(px, "terrarium", 2);
  near(t[2], 0.5, 1e-9, "terrarium 0.5 m");
});

test("zoom choice: the coarsest zoom no coarser than the requested resolution", () => {
  near(pixelMetres(0, 12), 38.22, 0.01, "z12 at the equator");
  assert.equal(chooseZoom(40, 30, 8, 12), 12);
  assert.equal(chooseZoom(40, 60, 8, 12), 11);
  assert.equal(chooseZoom(40, 10, 8, 12), 12);
});
