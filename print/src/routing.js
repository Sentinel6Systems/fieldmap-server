// Routes on the map: their legs along roads and trails, from the server's router (BRouter at
// /routing/route), asked one leg at a time as the app does, so that the map shows the same lines
// as the devices. A leg the router finds no way for (no routing data there, no road, no router
// on this server) is drawn straight and dashed, as the app does; a Straight Line route needs no
// router at all.
//
// A point away from any road: the router itself starts or ends the line on the nearest road it can
// use (BRouter's dynamic range), so a line that ends more than END_GAP from its point gets the rest,
// from the road to the point, straight and dashed, as the app draws it; one that ends more than
// NEAR away counts as no way. When the router finds no way at all (a road it cannot reach), the
// map looks for the roads nearest that point (in its own tiles: nearRoads) and routes to the
// nearest one it can reach, dashed from there. The same for a start off the road.
import { inverse } from "./geo.js";
import { ROUTE_TYPES_LIST } from "./shared.js";

export const ROUTER = "/routing/route";
const PROFILE = Object.fromEntries(ROUTE_TYPES_LIST.map(([key, , profile]) => [key, profile]));
const RETRY_MS = 60000;        // a leg with no answer is asked again after this
const AT_ONCE = 2;             // requests to the router at the same time
const r6 = (v) => Math.round(v * 1e6) / 1e6;

export const profileOf = (routing) => PROFILE[routing] || null;
/** The road classes (OpenMapTiles' transportation layer) each Route Type can use. */
export const ROAD_CLASSES = {
  driving: ["motorway", "trunk", "primary", "secondary", "tertiary", "minor", "service"],
  walking: ["trunk", "primary", "secondary", "tertiary", "minor", "service", "track", "path"],
};
const NEAR = 15000;            // roads farther than this from a point are not tried (m)
const SPREAD = 500;            // the roads tried for one point are at least this far apart (m)
const TRIES = 3;               // roads tried at each end of a leg
const END_GAP = 100;           // a line ending farther than this from its point gets a dashed piece to it (m), as in the app

/** The points on these lines ([[lon, lat], ...] each) nearest p, nearest first: at most `count`,
 *  each at least `spread` m from the others, none farther than `most` m. [{ lng, lat, meters }] */
export function nearestRoadPoints(p, lines, count = TRIES, spread = SPREAD, most = NEAR) {
  const kx = Math.cos((p.lat * Math.PI) / 180) * 111320, ky = 110540;
  const xy = (c) => [(c[0] - p.lng) * kx, (c[1] - p.lat) * ky];
  const found = [];
  for (const line of lines) {
    for (let i = 1; i < line.length; i++) {
      const [x1, y1] = xy(line[i - 1]), [x2, y2] = xy(line[i]);
      const dx = x2 - x1, dy = y2 - y1, len = dx * dx + dy * dy;
      const t = len ? Math.max(0, Math.min(1, -(x1 * dx + y1 * dy) / len)) : 0;
      const x = x1 + t * dx, y = y1 + t * dy, meters = Math.hypot(x, y);
      if (meters <= most) found.push({ lng: p.lng + x / kx, lat: p.lat + y / ky, meters, x, y });
    }
  }
  found.sort((a, b) => a.meters - b.meters);
  const out = [];
  for (const f of found) {
    if (out.length >= count) break;
    if (out.every((o) => Math.hypot(o.x - f.x, o.y - f.y) >= spread)) out.push(f);
  }
  return out.map(({ lng, lat, meters }) => ({ lng, lat, meters }));
}
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

/** Asks for the legs routes need and keeps the answers. onChange() after each one.
 *  nearRoads(point, routing): the lines of the roads near a point that a Route Type can use. */
export function createRouter(onChange, fetchFn = (u) => fetch(u), nearRoads = null) {
  const legs = new Map();     // key -> { line, meters, head?, tail? } | { failed: time } | { pending: true }
  const queue = [];
  let running = 0;
  async function fetchLeg(profile, a, b) {
    try {
      // Six decimals (about 10 cm): the server takes at most 12 characters a number.
      const at = (p) => `${r6(p.lng)},${r6(p.lat)}`;
      const r = await fetchFn(`${ROUTER}?lonlats=${at(a)}%7C${at(b)}&profile=${profile}&format=geojson`);
      return r.ok ? readLeg(await r.json()) : null;
    } catch { return null; }   // no answer: straight for now
  }
  /** No way from a to b: along the roads to the nearest road the router reaches from the other
   *  end (b's first, then a's, then both), and straight from there. */
  async function viaRoads(profile, a, b, routing) {
    const near = (p) => { try { return nearestRoadPoints(p, nearRoads(p, routing) || []); } catch { return []; } };
    const nearB = near(b);
    for (const c of nearB) { const got = ends(await fetchLeg(profile, a, c), a, b); if (got) return got; }
    const nearA = near(a);
    for (const c of nearA) { const got = ends(await fetchLeg(profile, c, b), a, b); if (got) return got; }
    if (nearA.length && nearB.length) return ends(await fetchLeg(profile, nearA[0], nearB[0]), a, b);
    return null;
  }
  async function ask(key, profile, routing, a, b) {
    running++;
    let got = ends(await fetchLeg(profile, a, b), a, b);
    if (!got && nearRoads) got = await viaRoads(profile, a, b, routing);
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
          queue.push([key, profile, route.routing, a, b]);
        }
      }
      pump();
    },
  };
}

/** A line from the router for a leg from a to b, with a dashed piece from a to its start and from
 *  its end to b where it starts or ends more than END_GAP away; null (no way) when more than NEAR. */
export function ends(got, a, b) {
  if (!got) return null;
  const first = got.line[0], last = got.line[got.line.length - 1];
  const gapA = straight(a, { lng: first[0], lat: first[1] }), gapB = straight(b, { lng: last[0], lat: last[1] });
  if (gapA > NEAR || gapB > NEAR) return null;
  return { ...got, head: gapA > END_GAP ? [[a.lng, a.lat], first] : null, tail: gapB > END_GAP ? [last, [b.lng, b.lat]] : null };
}

/** A route's legs: [{ a, b, line, meters, routed, head, tail }]; line is the router's when it has
 *  one; head and tail the straight parts off the road at its ends, when it ends off the road. */
export function routeLegs(route, router) {
  const profile = profileOf(route.routing);
  const out = [];
  const ll = (c) => ({ lng: c[0], lat: c[1] });
  const length = (part) => (part ? straight(ll(part[0]), ll(part[1])) : 0);
  for (let i = 1; i < route.points.length; i++) {
    const a = route.points[i - 1], b = route.points[i];
    const got = profile && router ? router.leg(profile, a, b) : null;
    if (!got) { out.push({ a, b, line: [[a.lng, a.lat], [b.lng, b.lat]], meters: straight(a, b), routed: !profile, head: null, tail: null }); continue; }
    const head = got.head || null, tail = got.tail || null;
    let meters = got.meters;
    if (meters === null) meters = got.line.reduce((t, c, j) => (j ? t + straight(ll(got.line[j - 1]), ll(c)) : 0), 0);
    out.push({ a, b, line: got.line, meters: meters + length(head) + length(tail), routed: true, head, tail });
  }
  return out;
}

/** Length (meters) and time (seconds) of a route, how many of its legs are straight for want of a
 *  way along roads or trails, and how many end off the road. */
export function routeStats(route, router) {
  const legs = routeLegs(route, router);
  const meters = legs.reduce((t, l) => t + l.meters, 0);
  const mph = route.speedMph || 2;
  const routed = !!profileOf(route.routing);
  return { meters, seconds: meters / (mph * 1609.344 / 3600), straightLegs: routed ? legs.filter((l) => !l.routed).length : 0,
    offRoad: routed ? legs.filter((l) => l.head || l.tail).length : 0 };
}

/** "45 min", "2 h 05 min". */
export function durationText(seconds) {
  const min = Math.max(1, Math.round(seconds / 60));
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

/** Routes as features to draw. part: line (along roads, or a Straight Line route), dashed (a leg
 *  with no way found, or still being asked for, and the part of a leg off the road), vertex (a point, draggable: index), label. */
export function routesFC(routes, router) {
  const features = [];
  const add = (geometry, props) => features.push({ type: "Feature", geometry, properties: props });
  for (const route of routes) {
    const base = { id: route.id, color: route.color };
    for (const leg of routeLegs(route, router)) {
      if (leg.head) add({ type: "LineString", coordinates: leg.head }, { ...base, part: "dashed" });
      add({ type: "LineString", coordinates: leg.line }, { ...base, part: leg.routed ? "line" : "dashed" });
      if (leg.tail) add({ type: "LineString", coordinates: leg.tail }, { ...base, part: "dashed" });
    }
    route.points.forEach((p, index) => add({ type: "Point", coordinates: [p.lng, p.lat] }, { ...base, part: "vertex", index }));
    if (route.points.length) add({ type: "Point", coordinates: [route.points[0].lng, route.points[0].lat] }, { ...base, part: "label", label: route.label || "" });
  }
  return { type: "FeatureCollection", features };
}
