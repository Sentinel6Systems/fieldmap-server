// Compare the print tool's viewshed with gdal_viewshed on the same grid.
//   node test/compare-viewshed.mjs <dir written by tools/viewshed-compare.py> [elevation.mbtiles]
// Reports the share of cells inside the radius on which both agree (visible / not
// visible; with a target height, "target visible"). With an elevation.mbtiles (PNG
// tiles), it also builds the grid the way the print tool does, from the tiles, and
// compares that grid's heights and viewshed with the reference.
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { decode } from "fast-png";
import { gridSpec, viewshed, buildGrid, tileSampler, decodeTile, chooseZoom } from "../src/terrain/core.js";

const dir = process.argv[2];
const meta = JSON.parse(readFileSync(`${dir}/meta.json`, "utf8"));
const grid = new Float32Array(readFileSync(`${dir}/grid.f32`).buffer.slice(0));
const ref = new Uint8Array(readFileSync(`${dir}/gdal.u8`));
const spec = gridSpec({ lat: meta.lat, lng: meta.lon }, meta.radius, meta.res);
if (spec.n !== meta.n) throw new Error(`grid size ${spec.n} != ${meta.n}`);
const params = { obsH: meta.obsH, tgtH: meta.tgtH, k: meta.k, radius: meta.radius };

function agreement(vis, label) {
  let both = 0, total = 0, onlyUs = 0, onlyGdal = 0, seenUs = 0, seenGdal = 0;
  for (let i = 0; i < vis.length; i++) {
    if (!vis[i] || ref[i] === 1) continue;      // outside the circle, no data, or beyond gdal's radius
    const us = meta.tgtH > 0 ? vis[i] >= 2 : vis[i] === 2, them = ref[i] === 255;
    total++; seenUs += us; seenGdal += them;
    if (us === them) both++; else if (us) onlyUs++; else onlyGdal++;
  }
  console.log(`${label}: ${(100 * both / total).toFixed(2)} % of ${total} cells agree; visible ${(100 * seenUs / total).toFixed(1)} % here, ` +
    `${(100 * seenGdal / total).toFixed(1)} % gdal_viewshed; ${onlyUs} cells only here, ${onlyGdal} only gdal; ` +
    `visible area overlap (intersection / union) ${(100 * (seenUs - onlyUs) / (seenUs + onlyGdal)).toFixed(1)} %`);
}

agreement(await viewshed(spec, grid, params), "same grid");

const mbtiles = process.argv[3];
if (mbtiles) {
  const db = new DatabaseSync(mbtiles, { readOnly: true });
  const md = Object.fromEntries(db.prepare("SELECT name, value FROM metadata").all().map((r) => [r.name, r.value]));
  const z = chooseZoom(meta.lat, meta.res, +md.minzoom, +md.maxzoom);
  const q = db.prepare("SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?");
  const cache = new Map();
  const tile = (x, y) => {
    const key = `${x}/${y}`;
    if (!cache.has(key)) {
      const row = q.get(z, x, 2 ** z - 1 - y);
      let t = null;
      if (row) {
        const img = decode(row.tile_data);
        const rgba = new Uint8Array(256 * 256 * 4);
        for (let i = 0; i < 256 * 256; i++) for (let k = 0; k < 4; k++) rgba[i * 4 + k] = k < img.channels ? img.data[i * img.channels + k] : 255;
        t = decodeTile(rgba, md.encoding);
      }
      cache.set(key, t);
    }
    return cache.get(key);
  };
  const h = await buildGrid(spec, tileSampler(z, tile));
  let n = 0, sum = 0, sq = 0, max = 0;
  for (let i = 0; i < h.length; i++) {
    if (Number.isNaN(h[i]) || Number.isNaN(grid[i])) continue;
    const d = h[i] - grid[i]; n++; sum += d; sq += d * d; max = Math.max(max, Math.abs(d));
  }
  console.log(`tile grid (zoom ${z}) vs reference grid: mean ${(sum / n).toFixed(2)} m, RMS ${Math.sqrt(sq / n).toFixed(2)} m, max ${max.toFixed(1)} m over ${n} cells`);
  agreement(await viewshed(spec, h, params), "grid from the tiles");
}
