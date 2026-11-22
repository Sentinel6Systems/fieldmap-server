// Routes on the map: their legs along roads and trails, from the server's router (BRouter at
// /routing/route), asked one leg at a time as the app does, so that the map shows the same lines
// as the devices. A leg the router finds no way for (no routing data there, no road, no router
// on this server) is drawn straight and dashed, as the app does; a Straight Line route needs no
// router at all.
import { inverse } from "./geo.js";
import { ROUTE_TYPES_LIST } from "./shared.js";

export const ROUTER = "/routing/route";
const PROFILE = Object.fromEntries(ROUTE_TYPES_LIST.map(([key, , profile]) => [key, profile]));
const RETRY_MS = 60000;        // a leg with no answer is asked again after this
const AT_ONCE = 2;             // requests to the router at the same time
const r6 = (v) => Math.round(v * 1e6) / 1e6;

export const profileOf = (routing) => PROFILE[routing] || null;
export const legKey = (profile, a, b) => `${profile}|${r6(a.lng)},${r6(a.lat)}|${r6(b.lng)},${r6(b.lat)}`;
const straight = (a, b) => inverse(a.lat, a.lng, b.lat, b.lng).distance;

/** The router's answer for one leg: { line: [[lon, lat], ...], meters }, or null. */
export function readLeg(geojson) {
  const f = geojson && Array.isArray(geojson.features) ? geojson.features[0] : null;
  const c = f && f.geometry && f.geometry.type === "LineString" ? f.geometry.coordinates : null;
  if (!Array.isArray(c) || c.length < 2 || !c.every((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]))) return null;
  const meters = Number((f.properties || {})["track-length"]);
  return { line: c.map((p) => [p[0], p[1]]), meters: Number.isFinite(meters) ? meters : null };
}

/** Asks the router for the legs routes need and keeps the answers. onChange() after each one. */
export function createRouter(onChange, fetchFn = (u) => fetch(u)) {
  const legs = new Map();     // key -> { line, meters } | { failed: time } | { pending: true }
  const queue = [];
  let running = 0;
  async function ask(key, profile, a, b) {
    running++;
    let got = null;
    try {
      // Six decimals (about 10 cm): the server takes at most 12 characters a number.
      const at = (p) => `${r6(p.lng)},${r6(p.lat)}`;
      const r = await fetchFn(`${ROUTER}?lonlats=${at(a)}%7C${at(b)}&profile=${profile}&format=geojson`);
      if (r.ok) got = readLeg(await r.json());
    } catch { /* no answer: straight for now */ }
    legs.set(key, got || { failed: Date.now() });
    running--;
    pump();
    onChange();
  }
  function pump() {
    while (running < AT_ONCE && queue.length) ask(...queue.shift());
  }
  return {
    /** What the router gave for this leg: { line, meters }, or null (none yet, or none). */
    leg(profile, a, b) {
      const got = legs.get(legKey(profile, a, b));
      return got && got.line ? got : null;
    },
    /** True while this leg is still being asked for. */
    pending(profile, a, b) { const got = legs.get(legKey(profile, a, b)); return !!(got && got.pending); },
    /** Ask for the legs of these routes that are not known yet. */
    want(routes) {
      for (const route of routes) {
        const profile = profileOf(route.routing);
        if (!profile) continue;
        for (let i = 1; i < route.points.length; i++) {
          const a = route.points[i - 1], b = route.points[i], key = legKey(profile, a, b);
          const got = legs.get(key);
          if (got && !(got.failed && Date.now() - got.failed > RETRY_MS)) continue;
          legs.set(key, { pending: true });
          queue.push([key, profile, a, b]);
        }
      }
      pump();
    },
  };
}

/** A route's legs: [{ a, b, line, meters, routed }]; line is the router's when it has one. */
export function routeLegs(route, router) {
  const profile = profileOf(route.routing);
  const out = [];
  for (let i = 1; i < route.points.length; i++) {
    const a = route.points[i - 1], b = route.points[i];
    const got = profile && router ? router.leg(profile, a, b) : null;
    out.push({ a, b, line: got ? got.line : [[a.lng, a.lat], [b.lng, b.lat]], meters: got && got.meters !== null ? got.meters : straight(a, b), routed: !profile || !!got });
  }
  return out;
}

/** Length (meters) and time (seconds) of a route, and how many of its legs are straight for want
 *  of a way along roads or trails. */
export function routeStats(route, router) {
  const legs = routeLegs(route, router);
  const meters = legs.reduce((t, l) => t + l.meters, 0);
  const mph = route.speedMph || 2;
  return { meters, seconds: meters / (mph * 1609.344 / 3600), straightLegs: profileOf(route.routing) ? legs.filter((l) => !l.routed).length : 0 };
}

/** "45 min", "2 h 05 min". */
export function durationText(seconds) {
  const min = Math.max(1, Math.round(seconds / 60));
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

/** Routes as features to draw. part: line (along roads, or a Straight Line route), dashed (a leg
 *  with no way found, or still being asked for), vertex (a point, draggable: index), label. */
export function routesFC(routes, router) {
  const features = [];
  const add = (geometry, props) => features.push({ type: "Feature", geometry, properties: props });
  for (const route of routes) {
    const base = { id: route.id, color: route.color };
    for (const leg of routeLegs(route, router)) add({ type: "LineString", coordinates: leg.line }, { ...base, part: leg.routed ? "line" : "dashed" });
    route.points.forEach((p, index) => add({ type: "Point", coordinates: [p.lng, p.lat] }, { ...base, part: "vertex", index }));
    if (route.points.length) add({ type: "Point", coordinates: [route.points[0].lng, route.points[0].lat] }, { ...base, part: "label", label: route.label || "" });
  }
  return { type: "FeatureCollection", features };
}
