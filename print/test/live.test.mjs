// Unit tests for reading what the devices share (src/live.js, src/shared.js). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeState, mergeMessages, conversations, partnerOf, ageLook, ageText, newUid, cleanText, newMessageId, statusText } from "../src/live.js";
import { describe, sharedFC, peopleFC, kindText, itemFromFeature, featureFromItem, itemKey } from "../src/shared.js";
import { shapeRing } from "../src/shapes.js";
import { createRouter, readLeg, routeLegs, routeStats, routesFC, durationText, legKey, nearestRoadPoints, ends, routeAsk, routingVersion } from "../src/routing.js";

const ME = "web-0011223344556677";
const point = (id, lon, lat, extra = {}) => ({ type: "Feature", id, geometry: { type: "Point", coordinates: [lon, lat] },
  properties: { name: id, fieldmap: { kind: "marker", label: id, color: "#2a9d3f" }, by: "test-phone-0001", updated: "2026-01-31T12:00:00Z", ...extra } });

test("a full state replaces the markers; a later one adds, changes and removes", () => {
  let s = mergeState(null, { rev: 10, full: true, positions: [{ uid: "test-phone-0001" }], markers: [point("marker-0001", 10, 45), point("marker-0002", 11, 45)], removed: [] });
  assert.deepEqual([...s.markers.keys()], ["marker-0001", "marker-0002"]);
  s = mergeState(s, { rev: 12, full: false, positions: [], markers: [point("marker-0003", 12, 45), { ...point("marker-0001", 10.5, 45) }], removed: ["marker-0002"] });
  assert.equal(s.rev, 12);
  assert.deepEqual([...s.markers.keys()].sort(), ["marker-0001", "marker-0003"]);
  assert.deepEqual(s.markers.get("marker-0001").geometry.coordinates, [10.5, 45]);
  assert.deepEqual(s.positions, []);
  s = mergeState(s, { rev: 20, full: true, positions: [], markers: [point("marker-0009", 1, 1)], removed: [] });
  assert.deepEqual([...s.markers.keys()], ["marker-0009"], "a full answer forgets the rest");
});

test("a browser id is one the server takes", () => {
  const uid = newUid((n) => new Uint8Array(n).fill(171));
  assert.equal(uid, "web-abababababababab");
  assert.match(newUid(), /^[A-Za-z0-9-]{8,64}$/);
});

test("messages: new ones kept once, the received ones listed for the delivery note", () => {
  const answer = { seq: 7, messages: [
    { seq: 5, id: "msg-00000001", from: "test-phone-0001", name: "Alpha", to: "*", text: "hello all", time: "2026-01-31T12:00:00Z" },
    { seq: 6, id: "msg-00000002", from: "test-phone-0002", name: "Bravo", to: ME, text: "hi", time: "2026-01-31T12:01:00Z" },
    { seq: 7, id: "msg-00000003", from: "test-phone-0002", name: "Bravo", to: "test-phone-0001", text: "not ours", time: "2026-01-31T12:02:00Z" },
  ], delivered: [], groupDelivered: {} };
  let got = mergeMessages({ list: [], seq: null }, answer, ME);
  assert.equal(got.seq, 7);
  assert.deepEqual(got.received, ["msg-00000001", "msg-00000002"]);
  assert.deepEqual(got.list.map((m) => m.id), ["msg-00000001", "msg-00000002"]);
  assert.ok(got.list.every((m) => !m.read && !m.mine));
  got = mergeMessages(got, answer, ME);
  assert.equal(got.list.length, 2, "the same message twice is kept once");
});

test("messages: our own sent ones learn they were delivered", () => {
  const mine = { id: "msg-00000010", from: ME, name: "Desk", to: "test-phone-0001", text: "x", time: "2026-01-31T12:00:00Z", mine: true };
  const group = { id: "msg-00000011", from: ME, name: "Desk", to: "*", text: "y", time: "2026-01-31T12:00:01Z", mine: true };
  const got = mergeMessages({ list: [mine, group], seq: 3 }, { seq: 3, messages: [], delivered: ["msg-00000010"], groupDelivered: { "msg-00000011": ["test-phone-0001", "test-phone-0002"] } }, ME);
  assert.equal(got.list[0].status, "delivered");
  assert.deepEqual(got.list[1].deliveredTo, ["test-phone-0001", "test-phone-0002"]);
});

test("conversations: the group first, then people, newest first, with unread counts", () => {
  const list = [
    { id: "a", from: "test-phone-0001", name: "Alpha", to: "*", text: "1", time: "2026-01-31T12:00:00Z", read: false },
    { id: "b", from: "test-phone-0002", name: "Bravo", to: ME, text: "2", time: "2026-01-31T12:01:00Z", read: false },
    { id: "c", from: "test-phone-0001", name: "Alpha", to: ME, text: "3", time: "2026-01-31T12:02:00Z", read: true },
    { id: "d", from: ME, name: "Desk", to: "test-phone-0002", text: "4", time: "2026-01-31T12:03:00Z", mine: true },
  ];
  const c = conversations(list, ME);
  assert.deepEqual(c.map((x) => [x.partner, x.name, x.unread]), [["*", "Group (Private)", 1], ["test-phone-0002", "Bravo", 1], ["test-phone-0001", "Alpha", 0]]);
  assert.equal(partnerOf(list[3], ME), "test-phone-0002");
  assert.deepEqual(conversations([], ME).map((x) => x.partner), ["*"]);
});

test("locations age as in the app", () => {
  assert.deepEqual(ageLook(10, 5, 120), { fresh: true, opacity: 1 });
  const old = ageLook(30, 5, 120);
  assert.equal(old.fresh, false);
  assert.ok(Math.abs(old.opacity - 0.85) < 1e-9);
  assert.ok(Math.abs(ageLook(119, 5, 120).opacity - 0.405) < 0.01);
  assert.equal(ageLook(120, 5, 120).opacity, 0);
  assert.deepEqual([ageText(2), ageText(40), ageText(600), ageText(7200)], ["now", "40 s ago", "10 min ago", "2 h ago"]);
});

test("shared items: marker, arrow, route and area, as the app and the server keep them", () => {
  const arrow = { type: "Feature", id: "arrow-0001", geometry: { type: "LineString", coordinates: [[10, 45], [10.01, 45.01]] }, properties: { fieldmap: { kind: "arrow", label: "A1", color: "#1f5fa8" } } };
  const route = { type: "Feature", id: "route-0001", geometry: { type: "LineString", coordinates: [[10, 45], [10.1, 45], [10.1, 45.1]] },
    properties: { fieldmap: { kind: "route", label: "R1", color: "#6f2dbd", routing: "driving", speedMph: 25, waypoints: [[10, 45], [10.1, 45], [10.1, 45.1]] } } };
  const area = { type: "Feature", id: "area-0001", geometry: { type: "Polygon", coordinates: [[[10, 45], [10.1, 45], [10.1, 45.1], [10, 45]]] }, properties: { name: "Zone" } };
  assert.deepEqual(describe(point("marker-0001", 10, 45)), { kind: "marker", label: "marker-0001", color: "#2a9d3f", points: [[10, 45]] });
  assert.equal(describe(arrow).kind, "arrow");
  assert.equal(kindText(describe(route)), "Route · Driving");
  assert.equal(describe(area).label, "Zone");
  assert.equal(describe({ type: "Feature", geometry: { type: "Point", coordinates: ["x", 1] }, properties: {} }), null);

  const parts = (f) => sharedFC([f]).features.map((x) => x.properties.part);
  assert.deepEqual(parts(point("marker-0001", 10, 45)), ["point"]);
  assert.deepEqual(parts(arrow), ["line", "head", "label"]);
  assert.deepEqual(parts(route), ["dashed", "vertex", "vertex", "vertex", "label"], "a road route is drawn with straight dashed legs");
  assert.deepEqual(parts(area), ["area", "label"]);
  const straight = { ...route, properties: { fieldmap: { ...route.properties.fieldmap, routing: "straight" } } };
  assert.equal(parts(straight)[0], "line");
});

test("device locations: live, then gray, then gone", () => {
  const fc = peopleFC([
    { uid: "test-phone-0001", name: "Alpha", lat: 45, lon: 10, age: 3 },
    { uid: "test-phone-0002", name: "Bravo", lat: 45, lon: 10.1, age: 60 },
    { uid: "test-phone-0003", name: "Charlie", lat: 45, lon: 10.2, age: 118 },
  ], 5, 120, 5);
  assert.deepEqual(fc.features.map((f) => [f.properties.name, f.properties.icon]), [["Alpha", "live-person"], ["Bravo", "live-person-old"]]);
});

test("sending: text cleaned as the server takes it, ids as the app makes them, the status words", () => {
  assert.equal(cleanText("  two\nlines\t here  "), "two lines here");
  assert.equal(cleanText("x".repeat(200)).length, 140);
  assert.equal(cleanText("abc\u202edef"), "abcdef", "no marks that turn the text around");
  assert.equal(cleanText("a very long display name here", 24), "a very long display name");
  const id = newMessageId(1700000000000, () => 0.5);
  assert.match(id, /^[A-Za-z0-9-]{8,64}$/);
  assert.equal(statusText({ mine: false }), "Received (Server)");
  assert.equal(statusText({ mine: true, to: "*", status: "waiting" }), "Waiting");
  assert.equal(statusText({ mine: true, to: "test-phone-0001", status: "sent" }), "Sent (Server)");
  assert.equal(statusText({ mine: true, to: "test-phone-0001", status: "delivered" }), "Delivered");
  assert.equal(statusText({ mine: true, to: "*", status: "sent", deliveredTo: ["test-phone-0001", "test-phone-0002"] }), "Sent (Server) · Delivered to 2");
  assert.equal(statusText({ mine: true, to: "*", status: "failed" }), "Not Sent");
});

test("a conversation started here is named after the person", () => {
  const list = [{ id: "e", from: ME, name: "Desk", to: "test-phone-0002", text: "hi", time: "2026-01-31T12:00:00Z", mine: true }];
  assert.equal(conversations(list, ME, (uid) => (uid === "test-phone-0002" ? "Bravo" : uid))[1].name, "Bravo");
});

test("a marker and an arrow go to the server and come back the same", () => {
  const marker = { id: "lq3k2x9abc", lng: 10.5, lat: 45.25, label: "P1", color: "#d7263d", shared: true };
  const arrow = { id: "lq3k2y0def", a: { lng: 10, lat: 45 }, b: { lng: 10.01, lat: 45.01 }, label: "A1", color: "#1f5fa8", shared: true };
  const fm = featureFromItem(marker, "marker", ME, new Date("2026-01-31T12:00:00.123Z"));
  assert.equal(fm.properties.updated, "2026-01-31T12:00:00Z");
  assert.equal(fm.properties.by, ME);
  assert.deepEqual(fm.properties.fieldmap, { kind: "marker", label: "P1", color: "#d7263d", id: "lq3k2x9abc", shared: true });
  const back = itemFromFeature(fm);
  assert.equal(back.kind, "marker");
  assert.equal(itemKey(back.item), itemKey(marker));
  const fa = featureFromItem(arrow, "arrow", ME);
  assert.deepEqual(fa.geometry, { type: "LineString", coordinates: [[10, 45], [10.01, 45.01]] });
  assert.equal(itemKey(itemFromFeature(fa).item), itemKey(arrow));
});

test("a route and the shapes go to the server and come back the same", () => {
  const route = { id: "lq3k2z1ghi", points: [{ lng: 10, lat: 45 }, { lng: 10.1, lat: 45 }, { lng: 10.1, lat: 45.1 }], routing: "walking", speedMph: 2, label: "R1", color: "#6f2dbd", shared: true };
  const fr = featureFromItem(route, "route", ME);
  assert.deepEqual(fr.geometry.coordinates, [[10, 45], [10.1, 45], [10.1, 45.1]]);
  assert.deepEqual(fr.properties.fieldmap.waypoints, fr.geometry.coordinates, "the app's form: the line is its points");
  assert.equal(fr.properties.fieldmap.routing, "walking");
  const back = itemFromFeature(fr);
  assert.equal(back.kind, "route");
  assert.equal(itemKey(back.item), itemKey(route));
  const shapes = [
    { id: "lq3k2z2jkl", type: "circle", center: { lng: 10, lat: 45 }, radius: 1234.5, label: "C1", color: "#d7263d", shared: true },
    { id: "lq3k2z3mno", type: "rect", points: [{ lng: 10, lat: 45 }, { lng: 10.2, lat: 45.1 }], label: "R1", color: "#d7263d", shared: true },
    { id: "lq3k2z4pqr", type: "polygon", points: [{ lng: 10, lat: 45 }, { lng: 10.2, lat: 45 }, { lng: 10.1, lat: 45.1 }], label: "P1", color: "#d7263d", shared: true },
  ];
  for (const s of shapes) {
    const f = featureFromItem(s, "shape", ME, new Date(), shapeRing(s));
    assert.equal(f.geometry.type, "Polygon");
    assert.equal(f.properties.fieldmap.kind, s.type);
    const ring = f.geometry.coordinates[0];
    assert.deepEqual(ring[0], ring[ring.length - 1], "a closed ring");
    assert.ok(JSON.stringify(f).length < 8192, "fits the server's body limit");
    const got = itemFromFeature(f);
    assert.equal(got.kind, "shape");
    assert.equal(itemKey(got.item), itemKey(s), s.type);
  }
  // An outline from elsewhere is a polygon.
  const plain = { type: "Feature", id: "area-0001", geometry: { type: "Polygon", coordinates: [[[10, 45], [10.1, 45], [10.1, 45.1], [10, 45]]] }, properties: { name: "Zone" } };
  assert.deepEqual(itemFromFeature(plain).item.points, [{ lng: 10, lat: 45 }, { lng: 10.1, lat: 45 }, { lng: 10.1, lat: 45.1 }]);
});

test("routes: legs along roads from the router, straight and dashed where it has none", async () => {
  const route = { id: "r1", color: "#6f2dbd", label: "R1", routing: "driving", speedMph: 25,
    points: [{ lng: 10, lat: 45 }, { lng: 10.01, lat: 45 }, { lng: 10.02, lat: 45 }] };
  // A tapped point has many more decimals than the server takes (12 characters a number).
  route.points[0] = { lng: 9.999999987654321, lat: 45.00000012345678 };
  const asked = [];
  const answer = { type: "FeatureCollection", features: [{ type: "Feature", properties: { "track-length": "1500" },
    geometry: { type: "LineString", coordinates: [[10, 45, 100], [10.005, 45.002, 101], [10.01, 45, 102]] } }] };
  let done;
  const finished = new Promise((r) => (done = r));
  const router = createRouter(() => { if (asked.length === 2) setTimeout(done, 0); }, async (url) => {
    asked.push(url);
    return asked.length === 1 ? { ok: true, json: async () => answer } : { ok: false, status: 404 };
  });
  assert.equal(routeLegs(route, router).filter((l) => l.routed).length, 0, "nothing known yet");
  router.want([route]);
  router.want([route]);
  await finished;
  assert.equal(asked.length, 2, "one request per leg, each leg asked once");
  assert.match(asked[0], /^\/routing\/route\?lonlats=10,45%7C10\.01,45&profile=car-vario&v=0&format=geojson$/);
  const SERVER = /^-?[0-9.]{1,12},-?[0-9.]{1,12}((\||%7[Cc])-?[0-9.]{1,12},-?[0-9.]{1,12}){1,29}$/;   // web/nginx.conf
  assert.match(new URL(asked[0], "http://x").searchParams.get("lonlats").replace("|", "%7C"), SERVER);
  const legs = routeLegs(route, router);
  assert.deepEqual(legs.map((l) => l.routed), [true, false]);
  assert.equal(legs[0].line.length, 3);
  assert.equal(legs[0].meters, 1500);
  const parts = routesFC([route], router).features.map((f) => f.properties.part);
  assert.deepEqual(parts, ["line", "way", "dashed", "way", "vertex", "vertex", "vertex", "label"]);
  const st = routeStats(route, router);
  assert.equal(st.straightLegs, 1);
  assert.ok(st.meters > 1500 + 700 && st.meters < 1500 + 900);
  // A Straight Line route asks for nothing and has no dashed legs.
  const line = { ...route, routing: "straight" };
  router.want([line]);
  assert.equal(asked.length, 2);
  assert.deepEqual(routesFC([line], router).features.filter((f) => f.properties.part === "dashed"), []);
  assert.equal(legKey({ profile: "car-vario", extra: "" }, { lng: 10.0000001, lat: 45 }, { lng: 10, lat: 45 }), "car-vario|10,45|10,45");
  assert.equal(readLeg({ features: [{ geometry: { type: "Point", coordinates: [1, 2] } }] }), null);
  assert.deepEqual([durationText(30), durationText(45 * 60), durationText(125 * 60)], ["1 min", "45 min", "2 h 05 min"]);
});

test("routes: a leg that ends off the road goes along the road to the nearest one, then straight", async () => {
  // A road along latitude 45 from lon 10 to 10.1; the end point is 1 km north of it, off the road.
  const road = [[10, 45], [10.05, 45], [10.1, 45]];
  const off = { lng: 10.08, lat: 45.009 };
  const near = nearestRoadPoints(off, [road, [[10.08, 45.2], [10.09, 45.2]]]);
  assert.equal(near.length, 2, "two places on the near road; the far road is past the limit");
  assert.ok(near.every((p) => Math.abs(p.lat - 45) < 1e-9));
  assert.ok(Math.abs(near[0].lng - 10.08) < 1e-9 && Math.abs(near[0].lat - 45) < 1e-9);
  assert.ok(Math.abs(near[0].meters - 995) < 10);
  // Spread: the places tried are far enough apart to be on other stretches of road.
  assert.equal(nearestRoadPoints(off, [road], 3, 5000).length, 1);

  const route = { id: "r2", color: "#d7263d", label: "R2", routing: "driving", speedMph: 25, points: [{ lng: 10, lat: 45 }, off] };
  const asked = [];
  let done;
  const finished = new Promise((r) => (done = r));
  const answer = (line) => ({ ok: true, json: async () => ({ type: "FeatureCollection", features: [{ type: "Feature", properties: { "track-length": "6400" }, geometry: { type: "LineString", coordinates: line } }] }) });
  const router = createRouter(() => setTimeout(done, 0), async (url) => {
    asked.push(url);
    // The router finds no way to the point off the road, a way to the road next to it.
    return url.includes("10.08,45.009") ? { ok: false, status: 404 } : answer([[10, 45], [10.05, 45], [10.08, 45]]);
  }, (p, routing) => (routing === "driving" ? [road] : []));
  router.want([route]);
  await finished;
  assert.equal(asked.length, 2, "the leg as tapped, then to the nearest road");
  assert.match(asked[1], /lonlats=10,45%7C10\.08,45&/);
  const [leg] = routeLegs(route, router);
  assert.deepEqual(leg.tail, [[10.08, 45], [10.08, 45.009]]);
  assert.equal(leg.head, null);
  assert.ok(Math.abs(leg.meters - 6400 - 995) < 10, "the road, and the straight part to the point");
  const fc = routesFC([route], router).features;
  assert.deepEqual(fc.map((f) => f.properties.part), ["line", "dashed", "way", "vertex", "vertex", "label"]);
  assert.deepEqual(fc[2].geometry.coordinates.at(-1), [10.08, 45.009], "the chevrons run on to the point off the road");
  assert.deepEqual([routeStats(route, router).offRoad, routeStats(route, router).straightLegs], [1, 0]);
});

test("routes: a line the router ends on the road short of its point gets a dashed piece to it", () => {
  const a = { lng: 10, lat: 45 }, b = { lng: 10.08, lat: 45.009 };
  const line = [[10, 45], [10.05, 45], [10.08, 45]];          // the router stopped at the road, 1 km short of b
  const got = ends({ line, meters: 6400 }, a, b);
  assert.equal(got.head, null, "the start is on the line");
  assert.deepEqual(got.tail, [[10.08, 45], [10.08, 45.009]]);
  assert.equal(got.offRoad, true);
  const close = ends({ line: [[10, 45], [10.0805, 45.009]], meters: 1 }, a, b);
  assert.deepEqual([!!close.tail, close.offRoad], [true, false], "40 m short: a dashed piece, not off the road");
  assert.equal(ends({ line: [[10, 45], [10.08001, 45.009]], meters: 1 }, a, b).tail, null, "a meter short: none");
  assert.equal(ends({ line: [[10, 45], [10.08, 45.2]], meters: 1 }, a, { lng: 10.08, lat: 45.4 }), null, "more than 15 km off: no way");
});

test("a shared item carries its group both ways, and a new group is a change to send", () => {
  const it = { id: "marker-0009", lng: 10, lat: 45, label: "W1", color: "#2a9d3f", group: "Water Sources" };
  const f = featureFromItem(it, "marker", ME);
  assert.equal(f.properties.fieldmap.group, "Water Sources");
  assert.equal(itemFromFeature(f).item.group, "Water Sources");
  assert.notEqual(itemKey(it), itemKey({ ...it, group: "Food Sources" }));
  delete f.properties.fieldmap.group;
  assert.equal("group" in itemFromFeature(f).item, false);
});

test("a leg still being worked out counts as working, not as a leg with no way", async () => {
  let answer;
  const router = createRouter(() => {}, () => new Promise((r) => { answer = r; }));
  const route = { id: "r", points: [{ lng: 10, lat: 45 }, { lng: 10.01, lat: 45.01 }], routing: "walking", speedMph: 2 };
  router.want([route]);
  assert.equal(router.busy(), 1);
  let st = routeStats(route, router);
  assert.equal(st.pendingLegs, 1);
  assert.equal(st.straightLegs, 0);
  await new Promise((r) => setTimeout(r, 0));      // the request goes out after the data's date
  answer({ ok: false });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(router.busy(), 0);
  st = routeStats(route, router);
  assert.equal(st.pendingLegs, 0);
  assert.equal(st.straightLegs, 1);
});

test("route options, avoid areas and the way go into the router's request, and into its key", async () => {
  const asked = [];
  const router = createRouter(() => {}, async (u) => { asked.push(u); return { ok: false }; }, null,
    (g) => (g === "Danger" ? { nogos: [[10.5, 45.5, 250]], polygons: [[[10, 45], [10.1, 45], [10.1, 45.1]]], polylines: [[[10, 45], [10.2, 45.2]]] } : null));
  const route = { id: "r", points: [{ lng: 10, lat: 45 }, { lng: 10.01, lat: 45.01 }], routing: "driving", speedMph: 25,
    options: ["fuel-saver", "avoid-highways", "avoid-steps"], avoid: "Danger", alternative: 2 };
  const ask = routeAsk(route, router ? (g) => (g === "Danger" ? { nogos: [[10.5, 45.5, 250]] } : null) : null);
  assert.match(ask.extra, /profile:avoid_motorways=1/);
  assert.match(ask.extra, /profile:vmax=70/);
  assert.doesNotMatch(ask.extra, /allow_steps/, "a Walking option on a Driving route is not sent");
  router.want([route]);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(asked[0], /alternativeidx=2/);
  assert.match(asked[0], /nogos=10\.5,45\.5,250/);
  assert.match(asked[0], /polygons=10,45,10\.1,45,10\.1,45\.1/);
  assert.match(asked[0], /polylines=10,45,10\.2,45\.2/);
  assert.notEqual(legKey(routeAsk(route), route.points[0], route.points[1]), legKey(routeAsk({ ...route, alternative: 0 }), route.points[0], route.points[1]));
  assert.equal(routeAsk({ ...route, routing: "straight" }), null);
  const walk = routeAsk({ ...route, routing: "walking", options: ["avoid-steps", "hard", "easy"] });
  assert.match(walk.extra, /profile:allow_steps=0/);
  assert.match(walk.extra, /profile:SAC_scale_limit=1/, "easy wins over hard");
  assert.match(routeAsk({ ...route, routing: "walking", options: ["any-road"] }).extra, /profile:shortest_way=1/);
  assert.match(routeAsk({ ...route, routing: "driving", options: ["any-road"] }).extra, /^$|^&nogos|^&alternativeidx/, "not a Driving option");
});

test("a shared route keeps its options, avoid group and way; unknown ones are dropped", () => {
  const it = { id: "route-0001", points: [{ lng: 10, lat: 45 }, { lng: 10.1, lat: 45.1 }], routing: "walking", speedMph: 2, label: "R1",
    color: "#d32f2f", options: ["hard", "avoid-wet"], avoid: "Danger Areas", alternative: 1 };
  const f = featureFromItem(it, "route", ME);
  assert.equal(f.properties.fieldmap.options, "avoid-wet hard");
  const back = itemFromFeature(f).item;
  assert.deepEqual(back.options, ["avoid-wet", "hard"]);
  assert.equal(back.avoid, "Danger Areas");
  assert.equal(back.alternative, 1);
  f.properties.fieldmap.options = "avoid-highways bogus";
  f.properties.fieldmap.alternative = 9;
  const odd = itemFromFeature(f).item;
  assert.equal(odd.options, undefined);
  assert.equal(odd.alternative, undefined);
});

test("each leg request carries the routing data's date, for the server's route cache", async () => {
  assert.equal(await routingVersion(async () => ({ ok: true, json: async () => ({ made: "2026-11-28T05:30:00Z" }) })), "20261128053000");
  assert.equal(await routingVersion(async () => ({ ok: false })), "0");
  assert.equal(await routingVersion(async () => { throw new Error("offline"); }), "0");
  const asked = [];
  const router = createRouter(() => {}, async (u) => { asked.push(u); return { ok: false }; }, null, () => null, async () => "20261128053000");
  router.want([{ id: "r", points: [{ lng: 10, lat: 45 }, { lng: 10.01, lat: 45.01 }], routing: "walking", speedMph: 2 }]);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(asked[0], /&v=20261128053000&format=geojson$/);
});
