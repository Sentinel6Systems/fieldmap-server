import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";
mkdirSync("dist", { recursive: true });
await build({
  entryPoints: ["src/main.js"],
  bundle: true, minify: true, sourcemap: false, format: "esm",
  target: ["es2022"], outfile: "dist/app.js", logLevel: "info",
});
cpSync("public", "dist", { recursive: true });
cpSync("node_modules/maplibre-gl/dist/maplibre-gl.css", "dist/maplibre-gl.css");
cpSync("node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs", "dist/maplibre-gl-worker.js");
console.log("built dist/");
