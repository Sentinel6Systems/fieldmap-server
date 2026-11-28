// The shared markers and the device locations (from live.js) as map features, and the words
// for them. A shared item is a GeoJSON Feature as the app and the server keep it
// (fieldmap-android data/Drawings.kt, tools/share-server.py):
//   Point                                     a marker
//   LineString of two points                  an arrow, from its start to its head
//   LineString with fieldmap.kind "route"     a route through fieldmap.waypoints (2-30 points)
//   Polygon                                   an area (a shape of the map; not shared yet)
// Each device draws a route's legs along roads or trails with its own routing data; the map
// has none, so it draws them straight, dashed unless the route is a Straight Line one.
import { inverse, destination } from "./geo.js";
import { ageLook } from "./live.js";

const FALLBACK = "#d7263d";
const colour = (c) => (typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c) ? c.toLowerCase() : null);
const ROUTE_TYPES = { driving: "Driving", walking: "Walking", straight: "Straight Line" };

/** What a shared feature is: { kind, label, color, points: [[lon, lat], ...], routing, speedMph }, or null. */
export function describe(f) {
  const g = f && f.geometry, p = (f && f.properties) || {};
  const fm = p.fieldmap && typeof p.fieldmap === "object" ? p.fieldmap : {};
  const color = colour(fm.color) || colour(p["marker-color"]) || colour(p.stroke) || colour(p.fill) || FALLBACK;
  const label = String((typeof fm.label === "string" ? fm.label : p.name) || "").slice(0, 30);
  const ok = (c) => Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1]);
  if (!g) return null;
  if (g.type === "Point" && ok(g.coordinates)) return { kind: "marker", label, color, points: [g.coordinates.slice(0, 2)] };
  if (g.type === "LineString" && Array.isArray(g.coordinates) && g.coordinates.every(ok) && g.coordinates.length >= 2) {
    if (fm.kind === "route") {
      const way = Array.isArray(fm.waypoints) && fm.waypoints.length >= 2 && fm.waypoints.every(ok) ? fm.waypoints : g.coordinates;
      return { kind: "route", label, color, points: way.map((c) => c.slice(0, 2)), routing: ROUTE_TYPES[fm.routing] ? fm.routing : "straight", speedMph: fm.speedMph };
    }
    if (g.coordinates.length === 2) return { kind: "arrow", label, color, points: g.coordinates.map((c) => c.slice(0, 2)) };
    return { kind: "route", label, color, points: g.coordinates.map((c) => c.slice(0, 2)), routing: "straight" };
  }
  if (g.type === "Polygon" && Array.isArray(g.coordinates) && Array.isArray(g.coordinates[0]) && g.coordinates[0].every(ok) && g.coordinates[0].length >= 4) {
    return { kind: "area", label, color, points: g.coordinates[0].map((c) => c.slice(0, 2)) };
  }
  return null;
}

/** "Marker", "Arrow", "Route · Driving", "Area". */
export function kindText(d) {
  return d.kind === "route" ? `Route · ${ROUTE_TYPES[d.routing]}` : { marker: "Marker", arrow: "Arrow", area: "Area" }[d.kind];
}

/** The shared items as features to draw. part: point, line, dashed, head, vertex, area, label. */
/** An arrow's head: the two lines back from its tip b, `length` metres long (the caller makes that
 *  a fixed size on screen for the zoom), at most 40% of the arrow. */
export function arrowHead(a, b, length) {
  const inv = inverse(a.lat, a.lng, b.lat, b.lng);
  const len = Math.min(length, inv.distance * 0.4);
  const back = inv.finalBearing + 180;
  const l1 = destination(b.lat, b.lng, back - 28, len), l2 = destination(b.lat, b.lng, back + 28, len);
  return { type: "LineString", coordinates: [[l1.lng, l1.lat], [b.lng, b.lat], [l2.lng, l2.lat]] };
}

/** headLength(lat): how long an arrow's head is in metres there, for the zoom it is drawn at. */
export function sharedFC(features, headLength = () => 400) {
  const out = [];
  const add = (geometry, props) => out.push({ type: "Feature", geometry, properties: props });
  for (const f of features) {
    const d = describe(f);
    if (!d) continue;
    const base = { id: String(f.id), color: d.color };
    const pts = d.points;
    if (d.kind === "marker") {
      add({ type: "Point", coordinates: pts[0] }, { ...base, part: "point", label: d.label });
    } else if (d.kind === "arrow") {
      const [[lon1, lat1], [lon2, lat2]] = pts;
      add({ type: "LineString", coordinates: pts }, { ...base, part: "line" });
      add(arrowHead({ lng: lon1, lat: lat1 }, { lng: lon2, lat: lat2 }, headLength(lat2)), { ...base, part: "head" });
      add({ type: "Point", coordinates: [(lon1 + lon2) / 2, (lat1 + lat2) / 2] }, { ...base, part: "label", label: d.label });
    } else if (d.kind === "route") {
      add({ type: "LineString", coordinates: pts }, { ...base, part: d.routing === "straight" ? "line" : "dashed" });
      for (const c of pts) add({ type: "Point", coordinates: c }, { ...base, part: "vertex" });
      add({ type: "Point", coordinates: pts[0] }, { ...base, part: "label", label: d.label });
    } else {
      add({ type: "Polygon", coordinates: [pts] }, { ...base, part: "area" });
      const c = pts.slice(0, -1).reduce((s, p) => [s[0] + p[0], s[1] + p[1]], [0, 0]).map((v) => v / (pts.length - 1));
      add({ type: "Point", coordinates: c }, { ...base, part: "label", label: d.label });
    }
  }
  return { type: "FeatureCollection", features: out };
}

/** The device locations as features: a diamond each, live or gray and fading as in the app.
 *  `later`: seconds since the server gave the ages. Gone ones are left out. */
export function peopleFC(positions, interval, stale, later = 0) {
  const features = [];
  for (const p of positions || []) {
    if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon)) continue;
    const age = (+p.age || 0) + later;
    const look = ageLook(age, interval, stale);
    if (!look.opacity) continue;
    features.push({ type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
      properties: { uid: p.uid, name: p.name, icon: look.fresh ? "live-person" : "live-person-old", opacity: Math.round(look.opacity * 100) / 100 } });
  }
  return { type: "FeatureCollection", features };
}

/** The symbol of a device: a diamond, white-edged, in the app's colors (live / old). */
export function personImage(fill, ratio = 1) {
  const size = Math.round(30 * ratio), c = size / 2;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d");
  const diamond = (r, colour) => {
    g.fillStyle = colour;
    g.beginPath(); g.moveTo(c, c - r); g.lineTo(c + r, c); g.lineTo(c, c + r); g.lineTo(c - r, c); g.closePath(); g.fill();
  };
  diamond(c, "rgba(17,17,17,.55)");
  diamond(c - 2 * ratio, "#ffffff");
  diamond(c - 6 * ratio, fill);
  return g.getImageData(0, 0, size, size);
}
/** The chevron along a route that shows its direction of travel, as in the app: drawn white, to be
 *  colored as a signed-distance image (icon-color, with a white halo). */
export function chevronImage(ratio = 1) {
  const size = Math.round(14 * ratio), m = 3 * ratio;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const g = canvas.getContext("2d");
  g.strokeStyle = "#fff"; g.lineWidth = 3 * ratio; g.lineCap = "round"; g.lineJoin = "round";
  g.beginPath(); g.moveTo(m, m); g.lineTo(size - m, size / 2); g.lineTo(m, size - m); g.stroke();
  return g.getImageData(0, 0, size, size);
}
export const CHEVRON = "route-chevron";
// Live, gone quiet, and this browser's own location (placed by hand).
export const PERSON_COLORS = { "live-person": "#00b4d8", "live-person-old": "#8a8f8a", "live-me": "#7a9a2e" };

/* ---------- this map's items <-> shared features ---------- */
// A shared marker, arrow, route or shape is kept in the map's own lists (main.js) with
// shared: true, so that it is drawn, moved, renamed, colored and printed like any other; these
// turn one into the other. The form is the app's (Drawings.feature with sharing): the server
// keeps "by" (the id of whoever changed it last) and "updated". A shape is its outline (a
// Polygon, for software that knows nothing else) with what makes it again exactly in
// "fieldmap": a circle's center and radius, a rectangle's two corners.

/** Route Types as the app has them: its key, the router's profile, and the speed a new route of
 *  that type is timed at (Drawings.kt RouteType). */
export const ROUTE_TYPES_LIST = [["driving", "Driving", "car-vario", 25], ["walking", "Walking", "hiking-mountain", 2], ["straight", "Straight Line", null, 2]];
/** The speeds a route can be timed at, mph (the app's Routes.SPEEDS). */
export const SPEEDS = [2, 4, 10, 15, 25, 45, 60];
export const MAX_ROUTE_POINTS = 30;
export const MAX_AREA_POINTS = 200;
const ll = (c) => ({ lng: c[0], lat: c[1] });
const pos = (p) => [p.lng, p.lat];
const r6 = (v) => Math.round(v * 1e6) / 1e6;

/** A shared feature as an item of the map's lists, or null:
 *  { kind: "marker", item: { id, lng, lat } }, { kind: "arrow", item: { id, a, b } },
 *  { kind: "route", item: { id, points, routing, speedMph } },
 *  { kind: "shape", item: { id, type, center, radius } | { id, type, points } }; with group when it has one. */
export function itemFromFeature(f) {
  const d = describe(f);
  if (!d) return null;
  const p = f.properties || {};
  const fm = p.fieldmap && typeof p.fieldmap === "object" ? p.fieldmap : {};
  const common = { id: String(f.id), label: d.label, color: d.color, shared: true, by: typeof p.by === "string" ? p.by : "", updated: typeof p.updated === "string" ? p.updated : "" };
  // The group it is kept in (the web map's groups; the app keeps none yet).
  const group = typeof fm.group === "string" ? fm.group.replace(/\s+/g, " ").trim().slice(0, 30) : "";
  if (group) common.group = group;
  const [first, second] = d.points;
  if (d.kind === "marker") return { kind: "marker", item: { ...common, lng: first[0], lat: first[1] } };
  if (d.kind === "arrow") return { kind: "arrow", item: { ...common, a: ll(first), b: ll(second) } };
  if (d.kind === "route") {
    const mph = SPEEDS.includes(fm.speedMph) ? fm.speedMph : 2;
    return { kind: "route", item: { ...common, points: d.points.slice(0, MAX_ROUTE_POINTS).map(ll), routing: d.routing, speedMph: mph } };
  }
  const ok = (c) => Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1]);
  if (fm.kind === "circle" && ok(fm.center) && Number.isFinite(fm.radius) && fm.radius > 0) {
    return { kind: "shape", item: { ...common, type: "circle", center: ll(fm.center), radius: fm.radius } };
  }
  if (fm.kind === "rect" && Array.isArray(fm.points) && fm.points.length === 2 && fm.points.every(ok)) {
    return { kind: "shape", item: { ...common, type: "rect", points: fm.points.map(ll) } };
  }
  return { kind: "shape", item: { ...common, type: "polygon", points: d.points.slice(0, -1).map(ll) } };
}

/** An item of the map's lists as the feature PUT to /mapshare/markers/<id>.
 *  kind: marker | arrow | route | shape; ring: the shape's outline (shapes.js shapeRing). */
export function featureFromItem(it, kind, by, now = new Date(), ring = null) {
  const label = String(it.label || "").slice(0, 30);
  const fieldmap = { kind: kind === "shape" ? it.type : kind, label, color: it.color, id: it.id, shared: true };
  if (it.group) fieldmap.group = it.group;
  let geometry;
  if (kind === "marker") geometry = { type: "Point", coordinates: [it.lng, it.lat] };
  else if (kind === "arrow") geometry = { type: "LineString", coordinates: [pos(it.a), pos(it.b)] };
  else if (kind === "route") {
    geometry = { type: "LineString", coordinates: it.points.map(pos) };
    Object.assign(fieldmap, { waypoints: it.points.map(pos), routing: it.routing, speedMph: it.speedMph });
  } else {
    geometry = { type: "Polygon", coordinates: [ring.map(([x, y]) => [r6(x), r6(y)])] };
    if (it.type === "circle") Object.assign(fieldmap, { center: pos(it.center), radius: it.radius });
    if (it.type === "rect") fieldmap.points = it.points.map(pos);
  }
  const properties = { name: label, stroke: it.color, "marker-color": it.color };
  if (kind === "shape") properties.fill = it.color;
  return { type: "Feature", id: it.id, geometry, properties: {
    ...properties, fieldmap, by, updated: now.toISOString().replace(/\.\d+Z$/, "Z"),
  } };
}

/** What of an item the server holds: a change to any of it is sent. */
export const itemKey = (it) => JSON.stringify([it.lng, it.lat, it.a, it.b, it.label, it.color, it.type, it.center, it.radius, it.points, it.routing, it.speedMph, it.group]);

/** Whether the server can keep a shape: its outline has at most MAX_AREA_POINTS points. */
export const shareableShape = (ring) => ring.length <= MAX_AREA_POINTS;
