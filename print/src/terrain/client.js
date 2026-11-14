// Main-thread side of the terrain worker: finds the elevation tile set on the tile server
// and runs one job at a time (starting a new one cancels the one before).

/** The elevation source on the tile server, or null when it is missing or an empty
 *  placeholder. Tile URLs are built from `tilesBase` (not taken from the TileJSON), so
 *  they work behind a path prefix such as /maps. */
export async function findElevation(tilesBase) {
  try {
    const r = await fetch(`${tilesBase}/data/elevation.json`, { cache: "no-store" });
    if (!r.ok) return null;
    const tj = await r.json();
    if (!tj || tj.name === "empty placeholder" || !/^(png|webp)$/.test(tj.format || "")) return null;
    return {
      tiles: [`${tilesBase}/data/elevation/{z}/{x}/{y}.${tj.format}`],
      encoding: tj.encoding === "terrarium" ? "terrarium" : "mapbox",
      minzoom: +tj.minzoom || 0, maxzoom: +tj.maxzoom || 12,
      bounds: tj.bounds, attribution: String(tj.attribution || "").replace(/<[^>]*>/g, ""),
      source: tj["fieldmap:source"] || "", sourceDate: tj["fieldmap:source_date"] || "", built: tj["fieldmap:built"] || "",
    };
  } catch { return null; }
}

export class TerrainEngine {
  constructor(workerUrl, src) {
    this.src = src;
    this.worker = new Worker(workerUrl);
    this.pending = null;
    this.seq = 0;
    this.worker.onmessage = (e) => {
      const m = e.data, p = this.pending;
      if (!p || m.id !== p.id) return;
      if (m.type === "progress") { p.onProgress && p.onProgress(m.frac, m.text); return; }
      this.pending = null;
      if (m.type === "result") p.resolve(m.result);
      else if (m.type === "cancelled") p.reject(Object.assign(new Error("Cancelled"), { cancelled: true }));
      else p.reject(new Error(m.message));
    };
  }
  /** Run a job; resolves with the worker's result. A running job is cancelled first. */
  run(job, onProgress) {
    this.cancel();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending = { id, resolve, reject, onProgress };
      this.worker.postMessage({ cmd: "run", id, job, src: this.src });
    });
  }
  cancel() {
    const p = this.pending;
    if (!p) return;
    this.worker.postMessage({ cmd: "cancel", id: p.id });
    this.pending = null;
    p.reject(Object.assign(new Error("Cancelled"), { cancelled: true }));
  }
  get busy() { return !!this.pending; }
}
