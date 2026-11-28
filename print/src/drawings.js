// Markers, arrows and shapes to and from a file, so that they can be kept and loaded again,
// in this browser or another one. The file is GeoJSON: other map software (QGIS, geojson.io,
// many TAK tools) reads it too, and files from them can be loaded here.
//
// Every feature carries "fieldmap" in its properties, with what the print tool needs to make
// the item exactly as it was: a circle is written as its traced outline, for other software,
// and as centre and radius in "fieldmap", for this tool. From other software:
//   Point                       -> marker
//   LineString of two points    -> arrow (first point to the second)
//   LineString of 3 to 30 points -> route with straight legs (the app's routes keep their
//                                  points and Route Type in "fieldmap", as written here)
//   Polygon                     -> polygon (the outer ring; holes are dropped)
// Multi* geometries are split into their parts. Anything else is skipped and counted.
//
// An item in a group carries the group's name in "fieldmap" (group); a file of one group also
// names it at the top ("fieldmap": { group }), for the items that do not.
import { shapeRing } from "./shapes.js";

export const FORMAT = "fieldmap-drawings";
const KINDS = ["marker", "arrow", "circle", "rect", "polygon", "route"];
const ROUTINGS = ["driving", "walking", "straight"];
const SPEEDS = [2, 4, 10, 15, 25, 45, 60];
const MAX_ROUTE_POINTS = 30;
const MAX_LABEL = 30;                      // as the name fields in the panel
const MAX_GROUP = 30;                      // as a group's name

/** A FeatureCollection of the items, ready for JSON.stringify. */
export function toGeoJSON(markers, arrows, shapes, extra = {}, routes = []) {
  const features = [];
  const props = (it, kind, more = {}) => ({ name: it.label || "", stroke: it.color, "marker-color": it.color,
    fieldmap: { kind, label: it.label || "", color: it.color, ...(it.group ? { group: it.group } : {}), ...more } });
  for (const m of markers) {
    features.push({ type: "Feature", geometry: { type: "Point", coordinates: [m.lng, m.lat] }, properties: props(m, "marker") });
  }
  for (const a of arrows) {
    features.push({ type: "Feature", geometry: { type: "LineString", coordinates: [[a.a.lng, a.a.lat], [a.b.lng, a.b.lat]] },
      properties: props(a, "arrow") });
  }
  for (const r of routes) {
    const pts = r.points.map((p) => [p.lng, p.lat]);
    features.push({ type: "Feature", geometry: { type: "LineString", coordinates: pts },
      properties: props(r, "route", { waypoints: pts, routing: r.routing, speedMph: r.speedMph }) });
  }
  for (const s of shapes) {
    const more = s.type === "circle" ? { center: s.center, radius: s.radius, unit: s.unit } : { points: s.points };
    features.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [shapeRing(s)] },
      properties: { ...props(s, s.type, more), fill: s.color } });
  }
  return { type: "FeatureCollection", fieldmap: { format: FORMAT, version: 1, ...extra }, features };
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
// Longitudes past ±180 (some software writes them for shapes across the 180° line) are
// brought back into range; the ones in range are kept exactly.
const lngLat = (c) => (Array.isArray(c) && isNum(c[0]) && isNum(c[1]) && Math.abs(c[1]) <= 90 && Math.abs(c[0]) <= 540
  ? { lng: Math.abs(c[0]) <= 180 ? c[0] : ((c[0] + 540) % 360) - 180, lat: c[1] } : null);
const point = (p) => (p && isNum(p.lng) && isNum(p.lat) ? lngLat([p.lng, p.lat]) : null);
const colour = (c, fallback) => (typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c) ? c.toLowerCase() : fallback);
const text = (...vals) => String(vals.find((v) => typeof v === "string" && v.trim()) || "").trim().slice(0, MAX_LABEL);

/** Parse the text of a file. Returns { markers, arrows, shapes, routes, skipped, own } with items that
 *  have no id yet (and a group, when the file gives one); own: the file is this page's. Throws an Error with a plain message when the file cannot be read at all.
 *  defaultColor: for items from other software that carry no colour. */
export function fromGeoJSON(textOrObject, defaultColor) {
  let data = textOrObject;
  if (typeof data === "string") {
    try { data = JSON.parse(data); } catch { throw new Error("This file is not a GeoJSON file. Choose a .geojson file that you saved on this page or in other map software."); }
  }
  const feats = data && data.type === "FeatureCollection" ? data.features
    : data && data.type === "Feature" ? [data]
    : data && data.type && data.coordinates ? [{ type: "Feature", geometry: data, properties: {} }] : null;
  if (!Array.isArray(feats)) throw new Error("This file is not GeoJSON. It has no map features.");
  const head = data && data.fieldmap && typeof data.fieldmap === "object" ? data.fieldmap : {};
  const out = { markers: [], arrows: [], shapes: [], routes: [], skipped: 0, own: head.format === FORMAT };
  const groupName = (v) => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, MAX_GROUP) : "");
  for (const f of feats) {
    const g = f && f.geometry, p = (f && f.properties) || {};
    const fm = p.fieldmap && typeof p.fieldmap === "object" && KINDS.includes(p.fieldmap.kind) ? p.fieldmap : null;
    const color = colour(fm && fm.color, colour(p["marker-color"] || p.stroke || p.fill || p.color, defaultColor));
    // This tool's own name as it was (it may be empty); else the name other software gave.
    const name = fm ? String(fm.label || "").slice(0, MAX_LABEL) : text(p.name, p.title, p.label, p.callsign);
    const group = groupName(fm && fm.group) || groupName(head.group);
    const inGroup = group ? { group } : {};
    if (!g) { out.skipped++; continue; }
    // This tool's own items first: exact circles and rectangles.
    if (fm && fm.kind === "circle") {
      const c = point(fm.center);
      if (c && isNum(fm.radius) && fm.radius > 0) {
        out.shapes.push({ type: "circle", center: c, radius: fm.radius, unit: typeof fm.unit === "string" ? fm.unit : undefined, label: name, color, ...inGroup });
        continue;
      }
    }
    if (fm && fm.kind === "rect" && Array.isArray(fm.points) && fm.points.length === 2) {
      const pts = fm.points.map(point);
      if (pts.every(Boolean)) { out.shapes.push({ type: "rect", points: pts, label: name, color, ...inGroup }); continue; }
    }
    const parts = g.type && g.type.startsWith("Multi") && Array.isArray(g.coordinates)
      ? g.coordinates.map((c) => ({ type: g.type.slice(5), coordinates: c })) : [g];
    for (const part of parts) {
      const c = part.coordinates;
      if (part.type === "Point") {
        const ll = lngLat(c);
        if (ll) out.markers.push({ ...ll, label: name, color, ...inGroup }); else out.skipped++;
      } else if (part.type === "LineString" && Array.isArray(c) && c.length >= 2 && ((fm && fm.kind === "route") || c.length > 2)) {
        // A route: the app's own (its points in "fieldmap"), or a line of a few points.
        const way = fm && fm.kind === "route" && Array.isArray(fm.waypoints) && fm.waypoints.length >= 2 ? fm.waypoints : c;
        const pts = way.map(lngLat);
        if (pts.length <= MAX_ROUTE_POINTS && pts.every(Boolean)) {
          const routing = fm && ROUTINGS.includes(fm.routing) ? fm.routing : "straight";
          out.routes.push({ points: pts, routing, speedMph: fm && SPEEDS.includes(fm.speedMph) ? fm.speedMph : 2, label: name, color, ...inGroup });
        } else out.skipped++;
      } else if (part.type === "LineString" && Array.isArray(c) && c.length === 2) {
        const a = lngLat(c[0]), b = lngLat(c[1]);
        if (a && b) out.arrows.push({ a, b, label: name, color, ...inGroup }); else out.skipped++;
      } else if (part.type === "Polygon" && Array.isArray(c) && Array.isArray(c[0])) {
        const ring = c[0].map(lngLat);
        if (ring.length > 1 && ring[0] && ring[ring.length - 1] && ring[0].lng === ring[ring.length - 1].lng && ring[0].lat === ring[ring.length - 1].lat) ring.pop();
        if (ring.length >= 3 && ring.every(Boolean)) out.shapes.push({ type: "polygon", points: ring, label: name, color, ...inGroup });
        else out.skipped++;
      } else {
        out.skipped++;
      }
    }
  }
  return out;
}

/** A file name from the map title and today's date: "<title>-YYYY-MM-DD.geojson". */
export function fileName(title, date = new Date()) {
  const base = String(title || "").normalize("NFKD").replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-").slice(0, 40) || "map-drawings";
  return `${base}-${date.toISOString().slice(0, 10)}.geojson`;
}
