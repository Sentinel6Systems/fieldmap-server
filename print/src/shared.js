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
export function sharedFC(features) {
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
      const inv = inverse(lat1, lon1, lat2, lon2);
      const head = Math.min(Math.max(inv.distance * 0.08, 15), 400);
      const back = inv.finalBearing + 180;
      const l1 = destination(lat2, lon2, back - 25, head), l2 = destination(lat2, lon2, back + 25, head);
      add({ type: "LineString", coordinates: pts }, { ...base, part: "line" });
      add({ type: "LineString", coordinates: [[l1.lng, l1.lat], [lon2, lat2], [l2.lng, l2.lat]] }, { ...base, part: "head" });
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
export const PERSON_COLORS = { "live-person": "#00b4d8", "live-person-old": "#8a8f8a" };
