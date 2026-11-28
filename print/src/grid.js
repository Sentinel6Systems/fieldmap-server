// The MGRS grid on the screen map (main.js): the lines for what is in view, as GeoJSON. Printed
// sheets draw their own grid (render.js). The spacing follows the zoom: 100 m, 1 km, 10 km or
// 100 km lines, and the grid zone lines (6 degrees by 8) when zoomed out further.
import { fromUTM, toUTM, squareId, bandFor } from "./utm.js";

const STEPS = [100, 1000, 10000, 100000];
const MIN_PX = 70;          // closest two lines may be on screen
const SEGS = 24;            // points along each line, so that it bends with the projection

/** Metres per screen pixel at a latitude and MapLibre zoom (512 px tiles). */
export function metresPerPixel(lat, zoom) {
  return (40075016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * 2 ** zoom);
}

/** The line spacing (metres) for a zoom, or 0 for the grid zone lines only. */
export function gridStep(lat, zoom) {
  const mpp = metresPerPixel(lat, zoom);
  return STEPS.find((s) => s / mpp >= MIN_PX) || 0;
}

/** The digits a line is labelled with: its place within the 100 km square, at its spacing. */
export function lineLabel(v, step) {
  const inSquare = ((v % 100000) + 100000) % 100000;
  if (step >= 100000) return "";        // the square letters say it
  if (step >= 1000) return String(Math.floor(inSquare / 1000)).padStart(2, "0");
  return String(Math.floor(inSquare / 100)).padStart(3, "0");
}

const line = (coords, props) => ({ type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: props });
const point = (coords, props) => ({ type: "Feature", geometry: { type: "Point", coordinates: coords }, properties: props });

/** Splits a run of [lon, lat] points into the parts inside west..east, cut at the edges. */
function clipLon(pts, west, east) {
  const out = [];
  let run = [];
  const inside = (p) => p[0] >= west && p[0] <= east;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i], q = pts[i - 1];
    if (q && inside(p) !== inside(q)) {
      const edge = inside(p) ? (q[0] < west ? west : east) : (p[0] < west ? west : east);
      const t = (edge - q[0]) / (p[0] - q[0]);
      run.push([edge, q[1] + t * (p[1] - q[1])]);
      if (!inside(p)) { if (run.length > 1) out.push(run); run = []; continue; }
    }
    if (inside(p)) run.push(p);
  }
  if (run.length > 1) out.push(run);
  return out;
}

/** The grid zone lines (and their names) for a view; used when the 100 km lines would crowd. */
function zoneLines([[west, south], [east, north]]) {
  const f = [];
  const s = Math.max(-80, south), n = Math.min(84, north);
  if (s >= n) return f;
  for (let lon = Math.ceil((west + 180) / 6) * 6 - 180; lon <= east; lon += 6) {
    f.push(line([[lon, s], [lon, n]], { kind: "zone" }));
  }
  for (let lat = Math.ceil((s + 80) / 8) * 8 - 80; lat <= n; lat += 8) {
    f.push(line([[Math.max(west, -180), lat], [Math.min(east, 180), lat]], { kind: "zone" }));
  }
  // The zone names, in the middle of each cell that is in view.
  for (let lon = Math.floor((west + 180) / 6) * 6 - 180; lon < east; lon += 6) {
    for (let lat = Math.floor((s + 80) / 8) * 8 - 80; lat < n; lat += 8) {
      const cx = lon + 3, cy = Math.min(lat + 4, 82);
      if (cx < -180 || cx > 180) continue;
      const zone = ((Math.floor((cx + 180) / 6) % 60) + 60) % 60 + 1;
      f.push(point([cx, cy], { kind: "name", label: `${zone}${bandFor(cy)}` }));
    }
  }
  return f;
}

/**
 * The grid for the map view: bounds [[west, south], [east, north]] in degrees, the zoom, and the
 * latitude the spacing is worked out at (the middle of the view). Each UTM zone in view gets its
 * own lines, cut at the zone's edges; 100 km lines carry the square letters in each square.
 */
export function gridFC(bounds, zoom) {
  const [[west, south], [east, north]] = bounds;
  const s = Math.max(-80, south), n = Math.min(84, north);
  const midLat = (s + n) / 2;
  const step = gridStep(midLat, zoom);
  if (!step || s >= n) return { type: "FeatureCollection", features: zoneLines(bounds) };
  const features = [];
  const firstZone = Math.floor((Math.max(west, -180) + 180) / 6) + 1;
  const lastZone = Math.min(60, Math.floor((Math.min(east, 179.999) + 180) / 6) + 1);
  for (let zone = firstZone; zone <= lastZone; zone++) {
    const zw = Math.max(west, (zone - 1) * 6 - 180), ze = Math.min(east, zone * 6 - 180);
    if (zw >= ze) continue;
    // The UTM extent of this zone's part of the view (northings below the equator negative, so
    // that one zone runs on across it).
    const edge = [];
    for (let i = 0; i <= 8; i++) {
      const lon = zw + ((ze - zw) * i) / 8, lat = s + ((n - s) * i) / 8;
      edge.push([lon, s], [lon, n], [zw, lat], [ze, lat]);
    }
    const us = edge.map(([lon, lat]) => {
      const u = toUTM(lat, lon, zone);
      return [u.easting, lat < 0 ? u.northing - 10000000 : u.northing];
    });
    const minE = Math.min(...us.map((u) => u[0])), maxE = Math.max(...us.map((u) => u[0]));
    const minN = Math.min(...us.map((u) => u[1])), maxN = Math.max(...us.map((u) => u[1]));
    const at = (e, nn) => { const ll = fromUTM(zone, "N", e, nn); return [ll.lon, ll.lat]; };
    const kind = (v) => (v % 100000 === 0 ? "major" : "minor");
    for (let e = Math.ceil(minE / step) * step; e <= maxE; e += step) {
      const pts = [];
      for (let i = 0; i <= SEGS; i++) pts.push(at(e, minN + ((maxN - minN) * i) / SEGS));
      for (const part of clipLon(pts, zw, ze)) features.push(line(part, { kind: kind(e), label: lineLabel(e, step) }));
    }
    for (let nn = Math.ceil(minN / step) * step; nn <= maxN; nn += step) {
      const pts = [];
      for (let i = 0; i <= SEGS; i++) pts.push(at(minE + ((maxE - minE) * i) / SEGS, nn));
      for (const part of clipLon(pts, zw, ze)) features.push(line(part, { kind: kind(nn), label: lineLabel(nn, step) }));
    }
    // The 100 km square letters, where the squares are large enough on screen to hold them.
    if (step >= 10000) {
      for (let e = Math.floor(minE / 100000) * 100000; e < maxE; e += 100000) {
        for (let nn = Math.floor(minN / 100000) * 100000; nn < maxN; nn += 100000) {
          const c = at(e + 50000, nn + 50000);
          if (c[0] < zw || c[0] > ze || c[1] < s || c[1] > n || e < 100000 || e >= 900000) continue;
          features.push(point(c, { kind: "name", label: `${zone}${bandFor(c[1])} ${squareId(zone, e, nn < 0 ? nn + 10000000 : nn)}` }));
        }
      }
    }
  }
  return { type: "FeatureCollection", features };
}
