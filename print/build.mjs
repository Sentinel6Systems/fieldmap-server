import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";
mkdirSync("dist", { recursive: true });
await build({
  entryPoints: ["src/main.js"],
  bundle: true, minify: true, sourcemap: false, format: "esm",
  target: ["es2022"], outfile: "dist/app.js", logLevel: "info",
});
// The terrain analysis worker: a classic (non-module) worker, which every browser runs.
await build({
  entryPoints: ["src/terrain/worker.js"],
  bundle: true, minify: true, sourcemap: false, format: "iife",
  target: ["es2022"], outfile: "dist/terrain-worker.js", logLevel: "info",
});
cpSync("public", "dist", { recursive: true });
// The look and the header every page shares (../ui).
for (const f of ["fieldmap.css", "nav.js"]) cpSync(`../ui/${f}`, `dist/${f}`);
cpSync("node_modules/maplibre-gl/dist/maplibre-gl.css", "dist/maplibre-gl.css");
cpSync("node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs", "dist/maplibre-gl-worker.js");
console.log("built dist/");
