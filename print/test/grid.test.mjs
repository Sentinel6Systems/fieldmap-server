// Unit tests for the MGRS grid on the screen map (src/grid.js). Run: npm test.
import { test } from "node:test";
import assert from "node:assert/strict";
import { gridFC, gridStep, lineLabel } from "../src/grid.js";
import { toUTM } from "../src/utm.js";

const lines = (fc) => fc.features.filter((f) => f.geometry.type === "LineString");
const names = (fc) => fc.features.filter((f) => f.properties.kind === "name").map((f) => f.properties.label);

test("the spacing follows the zoom", () => {
  assert.equal(gridStep(40, 4), 0);
  assert.equal(gridStep(40, 8), 100000);
  assert.equal(gridStep(40, 10), 10000);
  assert.equal(gridStep(40, 13), 1000);
  assert.equal(gridStep(40, 16), 100);
});

test("line labels are the digits within the 100 km square", () => {
  assert.equal(lineLabel(4543000, 1000), "43");
  assert.equal(lineLabel(4540000, 10000), "4", "10 km lines: one digit");
  assert.equal(lineLabel(4500000, 10000), "0");
  assert.equal(lineLabel(4543200, 100), "432");
  assert.equal(lineLabel(4500000, 100000), "");
});

test("1 km lines fall on whole kilometres of the view's zone", () => {
  const fc = gridFC([[-75.2, 39.9], [-75.1, 40.0]], 13);
  assert.ok(lines(fc).length > 10);
  for (const f of lines(fc)) {
    for (const [lon, lat] of f.geometry.coordinates) {
      const u = toUTM(lat, lon, 18);
      const onLine = [u.easting, u.northing].some((v) => Math.abs(v - Math.round(v / 1000) * 1000) < 1);
      assert.ok(onLine, `${lon},${lat} is on no 1 km line`);
    }
  }
});

test("a view across a zone edge has each zone's lines cut at the edge", () => {
  const fc = gridFC([[-76, 39.5], [-74, 40.5]], 10);      // zone 18 only
  const both = gridFC([[-79, 39.5], [-77, 40.5]], 10);    // -78 is the edge of zones 17 and 18
  for (const f of lines(both)) {
    const lons = f.geometry.coordinates.map((c) => c[0]);
    assert.ok(Math.max(...lons) <= -78 + 1e-9 || Math.min(...lons) >= -78 - 1e-9, "a line crosses the zone edge");
  }
  assert.ok(names(both).some((n) => n.startsWith("17")) || names(both).some((n) => n.startsWith("18")));
  assert.ok(lines(fc).length > 0);
});

test("100 km squares carry their letters", () => {
  const fc = gridFC([[-77, 39], [-73, 42]], 8);
  assert.ok(names(fc).includes("18T WK") || names(fc).some((n) => /^18[ST] [A-Z]{2}$/.test(n)), names(fc).join(", "));
});

test("zoomed out, the grid zone lines and names", () => {
  const fc = gridFC([[-100, 30], [-60, 50]], 3);
  assert.ok(names(fc).includes("18T"));
  assert.ok(lines(fc).every((f) => f.properties.kind === "zone"));
});

test("below the equator one zone runs on across it", () => {
  const fc = gridFC([[-75.2, -0.05], [-75.1, 0.05]], 13);
  assert.ok(lines(fc).some((f) => f.geometry.coordinates.some((c) => c[1] < 0) && f.geometry.coordinates.some((c) => c[1] > 0)));
});

test("zoomed in, the lines' digits sit on the view's top and left edges, and the square is named", () => {
  const view = [[-75.2, 39.9], [-75.1, 40.0]];
  const fc = gridFC(view, 13);
  const edges = fc.features.filter((f) => f.properties.kind === "edge");
  assert.ok(edges.some((f) => f.properties.anchor === "top" && Math.abs(f.geometry.coordinates[1] - 40.0) < 1e-6));
  assert.ok(edges.some((f) => f.properties.anchor === "left" && Math.abs(f.geometry.coordinates[0] + 75.2) < 1e-6));
  assert.ok(edges.every((f) => /^\d\d$/.test(f.properties.label)));
  assert.ok(names(fc).some((n) => /^18S [A-Z]{2}$/.test(n)), names(fc).join(", "));
  // At 100 m lines too: the square stays named.
  assert.ok(names(gridFC([[-75.152, 39.95], [-75.148, 39.953]], 17)).length > 0);
});
