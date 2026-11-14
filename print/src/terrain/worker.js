// Web worker for the terrain analysis: fetches and decodes elevation tiles from the tile
// server (cached for the session), builds the local grid and runs viewshed, radio
// coverage or a profile. Messages in: { cmd: "run", id, job, src } and { cmd: "cancel", id }.
// Messages out: { type: "progress", id, frac, text }, { type: "result", id, result },
// { type: "error", id, message } and { type: "cancelled", id }.
import {
  decodeTile, chooseZoom, tilesForBounds, tileSampler, gridSpec, buildGrid, viewshed, radioArea,
  profilePoints, pixelMetres, toMercator, TILE,
} from "./core.js";

const MAX_TILES = 160;               // decoded tiles kept (256 KB each)
const cache = new Map();             // "z/x/y" -> Promise<Float32Array | null>
const cancelled = new Set();
let canvas = null;

class Cancelled extends Error {}

function tileUrl(src, z, x, y) {
  return src.tiles[0].replace("{z}", z).replace("{x}", x).replace("{y}", y);
}

async function decode(blob, encoding) {
  const bmp = await createImageBitmap(blob, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
  if (!canvas) canvas = new OffscreenCanvas(TILE, TILE);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.clearRect(0, 0, TILE, TILE);
  ctx.drawImage(bmp, 0, 0);
  bmp.close();
  return decodeTile(ctx.getImageData(0, 0, TILE, TILE).data, encoding);
}

function getTile(src, z, x, y) {
  const key = `${z}/${x}/${y}`;
  if (cache.has(key)) {
    const p = cache.get(key);
    cache.delete(key); cache.set(key, p);   // most recently used last
    return p;
  }
  const p = fetch(tileUrl(src, z, x, y))
    .then(async (r) => (r.ok && r.status !== 204 ? decode(await r.blob(), src.encoding) : null))
    .catch(() => null);
  cache.set(key, p);
  while (cache.size > MAX_TILES) cache.delete(cache.keys().next().value);
  return p;
}

/** Fetch tiles six at a time; returns a sync lookup tile(x, y). */
async function loadTiles(src, z, list, progress) {
  const got = new Map();
  let next = 0, done = 0;
  const worker = async () => {
    while (next < list.length) {
      const [x, y] = list[next++];
      got.set(`${x}/${y}`, await getTile(src, z, x, y));
      await progress(++done / list.length);
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, list.length) }, worker));
  return (x, y) => got.get(`${x}/${y}`) || null;
}

async function run(id, job, src) {
  let lastYield = 0;
  // Every 50 ms: report progress and let a cancel message in (setTimeout is slow to come
  // back, so it is not awaited more often than that).
  const step = (lo, hi, text) => async (f) => {
    const now = Date.now();
    if (now - lastYield < 50) return;
    lastYield = now;
    postMessage({ type: "progress", id, frac: lo + (hi - lo) * f, text });
    await new Promise((r) => setTimeout(r, 0));
    if (cancelled.has(id)) throw new Cancelled();
  };

  if (job.kind === "profile") {
    const lat = (job.a.lat + job.b.lat) / 2;
    const res = Math.max(job.res || 0, pixelMetres(lat, src.maxzoom));
    const { pts, step: dStep } = profilePoints(job.a, job.b, res, job.distance);
    const z = chooseZoom(lat, res, src.minzoom, src.maxzoom);
    const s0 = tileSampler(z, () => null), seen = new Map();
    for (const p of pts) {
      const tx = Math.floor((s0.fx(p.lng) + 0.5) / TILE), ty = Math.floor((s0.fy(p.lat) + 0.5) / TILE);
      for (const [dx, dy] of [[0, 0], [1, 0], [0, 1], [1, 1], [-1, 0], [0, -1], [-1, -1], [1, -1], [-1, 1]]) seen.set(`${tx + dx}/${ty + dy}`, [tx + dx, ty + dy]);
    }
    const tile = await loadTiles(src, z, [...seen.values()], step(0, 0.9, "Loading elevation tiles"));
    const sampler = tileSampler(z, tile);
    const heights = Float64Array.from(pts, (p) => sampler.height(p.lng, p.lat));
    return { kind: "profile", heights, step: dStep, pts, zoom: z };
  }

  const spec = gridSpec(job.center, job.radius, job.res);
  const z = chooseZoom(job.center.lat, job.res, src.minzoom, src.maxzoom);
  const tile = await loadTiles(src, z, tilesForBounds(spec.bounds, z), step(0, 0.35, "Loading elevation tiles"));
  const h = await buildGrid(spec, tileSampler(z, tile), step(0.35, 0.45, "Building the height grid"));
  const c = spec.c, n = spec.n, res = spec.res;
  const ground = h[c * n + c];
  if (job.kind === "viewshed") {
    const vis = await viewshed(spec, h, job.params, step(0.45, 0.9, "Working out what is visible"));
    const cellAt = (x, y) => {
      const i = Math.round(x / res) + c, j = c - Math.round(y / res);
      return i < 0 || j < 0 || i >= n || j >= n ? 0 : vis[j * n + i];
    };
    await step(0.9, 1, "Drawing")(0);
    const r = toMercator(spec, job.radius, cellAt);
    return { kind: "viewshed", ...r, ground, zoom: z, res, stats: count(vis) };
  }
  const rad = await radioArea(spec, h, job.params, step(0.45, 0.9, "Tracing radio paths"));
  await step(0.9, 1, "Drawing")(0);
  const idx = toMercator(spec, job.radius, (x, y) => {
    const d = Math.hypot(x, y), s = Math.round(d / res) - 1;
    if (s >= rad.nSamp) return -1;
    if (s < 0) return -2;
    let r = Math.round((Math.atan2(x, y) / (2 * Math.PI)) * rad.nRad);
    r = ((r % rad.nRad) + rad.nRad) % rad.nRad;
    return r * rad.nSamp + s;
  }, Int32Array);
  const cls = new Uint8Array(idx.data.length), loss = new Float32Array(idx.data.length).fill(NaN);
  for (let i = 0; i < idx.data.length; i++) {
    const k = idx.data[i];
    if (k === -2) { cls[i] = 1; loss[i] = 0; } else if (k >= 0) { cls[i] = rad.cls[k]; loss[i] = rad.loss[k]; }
  }
  return { kind: "radio", width: idx.width, height: idx.height, bounds: idx.bounds, data: cls, loss, ground, zoom: z, res, stats: count(rad.cls) };
}

function count(arr) {
  const c = [0, 0, 0, 0, 0];
  for (const v of arr) c[v]++;
  return c;
}

onmessage = async (e) => {
  const m = e.data;
  if (m.cmd === "cancel") { cancelled.add(m.id); return; }
  if (m.cmd !== "run") return;
  const t0 = Date.now();
  try {
    const result = await run(m.id, m.job, m.src);
    result.ms = Date.now() - t0;
    const transfer = [result.data, result.loss, result.heights].filter(Boolean).map((a) => a.buffer);
    postMessage({ type: "result", id: m.id, result }, transfer);
  } catch (err) {
    if (err instanceof Cancelled) postMessage({ type: "cancelled", id: m.id });
    else postMessage({ type: "error", id: m.id, message: String(err && err.message || err) });
  } finally {
    cancelled.delete(m.id);
  }
};
