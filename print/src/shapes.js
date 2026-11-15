// Drawn shapes: circles (centre + radius in metres), rectangles (two opposite corners,
// edges along meridians and parallels) and polygons (a list of points). Geometry and the
// lengths and areas shown with them, in the units the panel is set to.
import { destination, inverse } from "./geo.js";

const R = 6371008.8, D2R = Math.PI / 180;

/** Units per system for the circle radius, smallest first: [name, metres per unit]. */
export const UNITS = {
  imperial: [["ft", 0.3048], ["mi", 1609.344]],
  metric: [["m", 1], ["km", 1000]],
};

/** The unit a radius reads best in: ft below 0.2 mi, m below 1 km. */
export function defaultUnit(metres, system) {
  return system === "metric" ? (metres < 1000 ? "m" : "km") : (metres < 0.2 * 1609.344 ? "ft" : "mi");
}
export function unitMetres(unit) {
  for (const list of Object.values(UNITS)) for (const [n, m] of list) if (n === unit) return m;
  return 1;
}

/** Closed outline [[lng, lat], ...] of a shape. Circles are traced as 96-point geodesic rings. */
export function shapeRing(s) {
  if (s.type === "circle") {
    const ring = [];
    for (let i = 0; i <= 96; i++) {
      const p = destination(s.center.lat, s.center.lng, (i * 360) / 96, s.radius);
      ring.push([p.lng, p.lat]);
    }
    return ring;
  }
  if (s.type === "rect") {
    const [a, b] = s.points, w = Math.min(a.lng, b.lng), e = Math.max(a.lng, b.lng);
    const so = Math.min(a.lat, b.lat), n = Math.max(a.lat, b.lat);
    return [[w, so], [e, so], [e, n], [w, n], [w, so]];
  }
  const ring = s.points.map((p) => [p.lng, p.lat]);
  return ring.length ? [...ring, ring[0]] : ring;
}

/** Area of a closed ring on the sphere, m². */
export function ringArea(ring) {
  let sum = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    const [l1, p1] = ring[i], [l2, p2] = ring[i + 1];
    sum += (l2 - l1) * D2R * (2 + Math.sin(p1 * D2R) + Math.sin(p2 * D2R));
  }
  return Math.abs((sum * R * R) / 2);
}

/** Where the shape's label goes: the circle centre, otherwise the mean of the corners. */
export function shapeCenter(s) {
  if (s.type === "circle") return s.center;
  const pts = s.type === "rect" ? shapeRing(s).slice(0, 4).map(([lng, lat]) => ({ lng, lat })) : s.points;
  return { lng: pts.reduce((t, p) => t + p.lng, 0) / pts.length, lat: pts.reduce((t, p) => t + p.lat, 0) / pts.length };
}

export function fmtLength(m, system) {
  if (system === "metric") return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`;
  const mi = m / 1609.344;
  return mi < 0.2 ? `${Math.round(m / 0.3048).toLocaleString("en-US")} ft` : `${mi.toFixed(2)} mi`;
}

export function fmtArea(m2, system) {
  if (system === "metric") return m2 < 1e6 ? `${(m2 / 1e4).toFixed(1)} ha` : `${(m2 / 1e6).toFixed(2)} km²`;
  const acres = m2 / 4046.8564224;
  return acres < 640 ? `${acres.toFixed(1)} acres` : `${(m2 / 2589988.110336).toFixed(2)} sq mi`;
}

/** One-line description: radius or sides, and area. */
export function shapeSummary(s, system) {
  if (s.type === "circle") return `radius ${fmtLength(s.radius, system)} · ${fmtArea(Math.PI * s.radius * s.radius, system)}`;
  const area = fmtArea(ringArea(shapeRing(s)), system);
  if (s.type === "rect") {
    const [a, b] = s.points;
    const w = inverse(a.lat, a.lng, a.lat, b.lng).distance, h = inverse(a.lat, a.lng, b.lat, a.lng).distance;
    return `${fmtLength(w, system)} × ${fmtLength(h, system)} · ${area}`;
  }
  return area;
}

/** Polygons with more sides than this (an outline loaded from other map software) get no side
 *  lengths, only their area: thousands of labels would cover the map. */
export const MAX_LEGS = 24;

/** The legs that get their own length label: every side of a polygon; a rectangle's width and
 *  height once each, as a list of choices (south, else north; east, else west) so a printed page
 *  can use the side that is on it; none for a circle (its radius goes with the area).
 *  Returns [[{ a, b, metres }, ...choices], ...]. */
export function shapeLegs(s) {
  if (s.type === "circle" || (s.type === "polygon" && s.points.length > MAX_LEGS)) return [];
  const ring = shapeRing(s).map(([lng, lat]) => ({ lng, lat }));
  const legs = [];
  for (let i = 0; i < ring.length - 1; i++) legs.push({ a: ring[i], b: ring[i + 1], metres: inverse(ring[i].lat, ring[i].lng, ring[i + 1].lat, ring[i + 1].lng).distance });
  // rect ring: SW-SE (south), SE-NE (east), NE-NW (north), NW-SW (west)
  return s.type === "rect" ? [[legs[0], legs[2]], [legs[1], legs[3]]] : legs.map((l) => [l]);
}

/** What goes inside the shape next to its legs: radius and area for a circle, area otherwise. */
export function shapeInsideText(s, system) {
  return s.type === "circle" ? shapeSummary(s, system) : fmtArea(ringArea(shapeRing(s)), system);
}

export const SHAPE_NAMES = { circle: "Circle", rect: "Rectangle", polygon: "Polygon" };
