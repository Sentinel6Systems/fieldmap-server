// Unit tests for reading what the devices share (src/live.js, src/shared.js). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeState, mergeMessages, conversations, partnerOf, ageLook, ageText, newUid, cleanText, newMessageId, statusText } from "../src/live.js";
import { describe, sharedFC, peopleFC, kindText, itemFromFeature, featureFromItem, itemKey } from "../src/shared.js";

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
  const route = { type: "Feature", id: "route-0001", geometry: { type: "LineString", coordinates: [[10, 45], [10.1, 45], [10.2, 45]] }, properties: { fieldmap: { kind: "route" } } };
  assert.equal(itemFromFeature(route), null, "routes stay with the live layer");
});
