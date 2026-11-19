// Unit tests for saving and loading markers, arrows and shapes (src/drawings.js). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { toGeoJSON, fromGeoJSON, fileName } from "../src/drawings.js";

const markers = [{ id: "m1", lng: -105.27, lat: 40.01, label: "Camp", color: "#d7263d" }];
const arrows = [{ id: "a1", a: { lng: -105.3, lat: 40 }, b: { lng: -105.2, lat: 40.05 }, label: "", color: "#2d8cf0" }];
const shapes = [
  { id: "c1", type: "circle", center: { lng: -105.25, lat: 40.02 }, radius: 1609.344, unit: "mi", label: "C1", color: "#1d7a46" },
  { id: "r1", type: "rect", points: [{ lng: -105.4, lat: 39.9 }, { lng: -105.35, lat: 39.95 }], label: "R1", color: "#ff9900" },
  { id: "p1", type: "polygon", points: [{ lng: -105.1, lat: 40 }, { lng: -105.05, lat: 40.02 }, { lng: -105.08, lat: 40.06 }], label: "Zone", color: "#663399" },
];
const strip = (list) => list.map(({ id, ...rest }) => rest);

test("what is saved loads back the same", () => {
  const text = JSON.stringify(toGeoJSON(markers, arrows, shapes, { title: "Test" }));
  const got = fromGeoJSON(text, "#000000");
  assert.deepEqual(got.markers, strip(markers));
  assert.deepEqual(got.arrows, strip(arrows));
  assert.deepEqual(got.shapes, strip(shapes));
  assert.equal(got.skipped, 0);
});

test("the file is plain GeoJSON that other software can read", () => {
  const fc = toGeoJSON(markers, arrows, shapes);
  assert.equal(fc.type, "FeatureCollection");
  assert.deepEqual(fc.features.map((f) => f.geometry.type), ["Point", "LineString", "Polygon", "Polygon", "Polygon"]);
  const ring = fc.features[2].geometry.coordinates[0];
  assert.ok(ring.length > 90, "a circle is traced as a ring");
  assert.deepEqual(ring[0], ring[ring.length - 1], "rings are closed");
  assert.equal(fc.features[0].properties.name, "Camp");
});

test("files from other software: points, two-point lines, polygons, multi geometries", () => {
  const fc = { type: "FeatureCollection", features: [
    { type: "Feature", geometry: { type: "Point", coordinates: [10, 50] }, properties: { name: "Hut", "marker-color": "#00FF00" } },
    { type: "Feature", geometry: { type: "LineString", coordinates: [[10, 50], [11, 51]] }, properties: {} },
    { type: "Feature", geometry: { type: "LineString", coordinates: [[10, 50], [11, 51], [12, 52]] }, properties: {} },
    { type: "Feature", geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] }, properties: { title: "Field" } },
    { type: "Feature", geometry: { type: "MultiPoint", coordinates: [[1, 1], [2, 2]] }, properties: {} },
    { type: "Feature", geometry: null, properties: {} },
  ] };
  const got = fromGeoJSON(fc, "#123456");
  assert.equal(got.markers.length, 3);
  assert.deepEqual(got.markers[0], { lng: 10, lat: 50, label: "Hut", color: "#00ff00" });
  assert.equal(got.markers[1].color, "#123456");
  assert.equal(got.arrows.length, 1);
  assert.equal(got.shapes.length, 1);
  assert.equal(got.shapes[0].points.length, 3, "the closing point is dropped");
  assert.equal(got.shapes[0].label, "Field");
  assert.equal(got.skipped, 2, "the three-point line and the empty feature");
});

test("bad files give a plain message, bad values are not loaded", () => {
  assert.throws(() => fromGeoJSON("not json", "#000000"), /not a GeoJSON file/);
  assert.throws(() => fromGeoJSON('{"hello": 1}', "#000000"), /no map features/);
  const got = fromGeoJSON({ type: "FeatureCollection", features: [
    { type: "Feature", geometry: { type: "Point", coordinates: [10, 95] }, properties: {} },
    { type: "Feature", geometry: { type: "Point", coordinates: ["a", 1] }, properties: {} },
    { type: "Feature", geometry: { type: "Point", coordinates: [190, 10] }, properties: { name: "x".repeat(80), color: "red" } },
  ] }, "#000000");
  assert.equal(got.markers.length, 1);
  assert.equal(got.markers[0].lng, -170);
  assert.equal(got.markers[0].label.length, 30);
  assert.equal(got.markers[0].color, "#000000");
  assert.equal(got.skipped, 2);
});

test("file name from the title and the date", () => {
  const d = new Date("2026-11-15T12:00:00Z");
  assert.equal(fileName("Ridge walk: day 2", d), "Ridge-walk-day-2-2026-11-15.geojson");
  assert.equal(fileName("", d), "map-drawings-2026-11-15.geojson");
});

test("an outline with many sides gets no side lengths, a hand-drawn polygon does", async () => {
  const { shapeLegs, MAX_LEGS } = await import("../src/shapes.js");
  const ring = (n) => Array.from({ length: n }, (_, i) => ({ lng: Math.cos((i / n) * 2 * Math.PI) * 0.01, lat: 40 + Math.sin((i / n) * 2 * Math.PI) * 0.01 }));
  assert.equal(shapeLegs({ type: "polygon", points: ring(5) }).length, 5);
  assert.equal(shapeLegs({ type: "polygon", points: ring(MAX_LEGS + 1) }).length, 0);
});
