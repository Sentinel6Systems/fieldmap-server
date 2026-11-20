// Map builder page. Plain JavaScript, no build step; the server is tools/admin-server.py.
"use strict";

const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  n.append(...kids);
  return n;
};

// The layers of step 2, in build order. `key` is the server's name for the layer.
const LAYERS = [
  { key: "basemap", name: "Base Map", line: "Roads, towns, rivers, borders, and place names.",
    help: "The map itself, built from OpenStreetMap. The other layers go on top of it, so keep it selected unless you only want to rebuild another layer." },
  { key: "contours", name: "Contour Lines", line: "Elevation lines, as on a topographic map.",
    help: "Each line connects points of the same elevation; lines close together show steep ground. This is usually the largest layer and the slowest to build." },
  { key: "hillshade", name: "Hillshade", line: "Shading that makes hills and valleys easy to see.",
    help: "Shaded relief lit from the northwest. It shows the shape of the land clearly on screen and on paper." },
  { key: "elevation", name: "Elevation Data", line: "Used by Print Map for viewshed and radio coverage.",
    help: "Elevation values for the terrain tools; the map does not display this layer. Print Map uses it for viewshed and radio coverage, so leave it unselected if you do not use these tools." },
  { key: "extras", name: "Printed Map Details", line: "Power lines, fences, towers, springs, and other landmarks.",
    help: "Small features that help with navigation on the ground. The print map styles show them." },
  { key: "usdata", name: "US Government Data", line: "Aircraft obstacles, official place names, survey marks, and public land.", us: true,
    help: "Free data from US agencies (FAA, USGS, NOAA), shown in the print map styles. Available for the United States only." },
  { key: "routing", name: "Routing Data", line: "Offline route planning on roads and trails in the Fieldmap app.",
    help: "BRouter routing files from brouter.de, in squares of 5 × 5 degrees that cover the selected regions. Devices download the files for their offline areas from the server. The size is an estimate." },
];
// Not a layer to tick: the 1 m area of "More options", which is its own step of a build.
const DETAIL = { key: "detail", name: "Very High Resolution Area" };
const layerOf = key => LAYERS.find(l => l.key === key) || DETAIL;

let LAND = [], PLACES = [];    // coastlines and town names behind the regions (admin/land.json, places.json)
let detail = JSON.parse(localStorage.getItem("detail") || "null");   // {lon, lat, km}: the 1 m area
let placing = false;
let REG = {};                 // id -> region (admin/regions.json)
let KIDS = {};                // parent id ("" for the top) -> ids
let view = "";                // the region whose parts the map shows; "" is the world
let status = null, estimate = null, hot = null;
let dismissed = sessionStorage.getItem("dismissed");   // the finished build the user has closed
const chosen = new Set(JSON.parse(localStorage.getItem("chosen") || "[]"));
const ALL_LAYERS = ["basemap", "contours", "hillshade", "elevation", "extras", "usdata", "routing"];
const ticked = new Set(JSON.parse(localStorage.getItem("ticked") || JSON.stringify(ALL_LAYERS)));

// ---------- talking to the server ----------

// Paths are relative: the page is served as /mapadmin/ by the web front, or at / on its own port.
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { showLogin(); throw new Error("pin"); }
  if (!r.ok) throw new Error(data.error || "The server did not respond. Check that the Map Builder service is running on the server.");
  return data;
}

function showLogin() {
  $("login").hidden = false;
  $("builder").hidden = true;
  $("pin").focus();
}

$("login-form").addEventListener("submit", async e => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("api/login", { pin: $("pin").value });
    $("login").hidden = true;
    start();
  } catch (err) {
    if (err.message !== "pin") $("login-error").textContent = err.message;
  }
});

// ---------- small helpers ----------

const isUS = r => r.path === "north-america/us" || r.path.startsWith("north-america/us/");
const ancestors = id => { const out = []; while (REG[id] && REG[REG[id].parent]) { id = REG[id].parent; out.push(id); } return out; };
const hasChosenInside = id => [...chosen].some(c => ancestors(c).includes(id));
const chosenAbove = id => ancestors(id).find(a => chosen.has(a));
const gb = v => v >= 100 ? `${Math.round(v)} GB` : v >= 10 ? `${v.toFixed(0)} GB` : v >= 1 ? `${v.toFixed(1)} GB` : v >= 0.05 ? `${Math.round(v * 1000 / 50) * 50} MB` : "under 50 MB";
const hours = h => h < 0.75 ? "under an hour" : h < 1.5 ? "about an hour" : h < 36 ? `about ${Math.round(h)} hours` : `about ${Math.round(h / 24)} days`;
const ago = t => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 2 ? "just now" : m < 90 ? `${m} minutes ago` : m < 2880 ? `${Math.round(m / 60)} hours ago` : `${Math.round(m / 1440)} days ago`; };
const day = t => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
// What was chosen is kept in this browser, so that a reload or a second visit starts from it.
const save = () => {
  localStorage.setItem("chosen", JSON.stringify([...chosen]));
  localStorage.setItem("ticked", JSON.stringify([...ticked]));
  localStorage.setItem("options", JSON.stringify({ terrain: $("opt-terrain").value, units: $("opt-units").value, language: $("opt-language").value, world: $("opt-world").checked }));
  // "Get the newest map data" is not kept: it is for one build.
};
// The options of "More options"; those o leaves out get their first-visit values.
function setOptions(o) {
  $("opt-terrain").value = o.terrain || "30m";
  $("opt-units").value = o.units || "ft";
  // Place names: the browser's language when the build can keep it, else every language.
  const own = navigator.language.slice(0, 2);
  $("opt-language").value = typeof o.language === "string" ? o.language : status.languages.includes(own) ? own : "";
  $("opt-world").checked = !!o.world;
  $("opt-fresh").checked = false;           // for one build only
  document.querySelector("details.more").open = $("opt-terrain").value !== "30m" || !!o.world;
}

// ---------- step 1: the map ----------

// Web Mercator in degrees, so the shapes look as they do on other web maps.
const mercY = lat => -Math.log(Math.tan(Math.PI / 4 + Math.max(-85, Math.min(85, lat)) * Math.PI / 360)) * 180 / Math.PI;
const SVG = "http://www.w3.org/2000/svg";
const map = $("map");
let box = { x: -180, y: mercY(80), w: 360, h: mercY(-58) - mercY(80) };

function pathOf(geom, shift) {
  let d = "";
  for (const poly of geom)
    for (const ring of poly) {
      for (let i = 0; i < ring.length; i += 2)
        d += `${i ? "L" : "M"}${(ring[i] + shift).toFixed(2)} ${mercY(ring[i + 1]).toFixed(2)}`;
      d += "Z";
    }
  return d;
}

function setBox(b) {
  if (!map.clientWidth) return;              // hidden (the PIN is being asked for)
  // Keep the element's shape, so that nothing is cut off.
  const r = map.clientWidth / Math.max(1, map.clientHeight);
  if (b.w / b.h > r) { const h = b.w / r; b = { x: b.x, y: b.y - (h - b.h) / 2, w: b.w, h }; }
  else { const w = b.h * r; b = { x: b.x - (w - b.w) / 2, y: b.y, w, h: b.h }; }
  b.w = Math.min(b.w, 720); b.h = b.w / r;
  box = b;
  map.setAttribute("viewBox", `${b.x} ${b.y} ${b.w} ${b.h}`);
  drawLabels();
}

// Town names, the largest first, as many as fit: they tell the user where they are.
function drawLabels() {
  const layer = $("labels"), sx = map.clientWidth / box.w, sy = map.clientHeight / box.h, out = [];
  const top = $("crumbs").offsetHeight + 18;       // below the "where you are" buttons, which wrap on a phone
  if (box.w < 200) for (const [name, lon, lat] of PLACES) {
    for (const shift of [0, 360]) {
      const x = (lon + shift - box.x) * sx, y = (mercY(lat) - box.y) * sy;
      if (x > 10 && y > top && x < map.clientWidth - 90 && y < map.clientHeight - 40 &&
          !out.some(o => Math.abs(o.y - y) < 16 && Math.abs(o.x - x) < 110)) out.push({ name, x, y });
    }
    if (out.length >= 28) break;
  }
  layer.replaceChildren(...out.map(o => el("span", { textContent: o.name, style: `left:${o.x}px;top:${o.y}px` })));
}

function fit(id = view) {
  if (!id) return setBox({ x: -180, y: mercY(80), w: 360, h: mercY(-58) - mercY(80) });
  let [w, s, e, n] = REG[id].bbox;
  if (w > e) e += 360;                       // across the 180 degree line: the copy drawn 360 further east
  const pad = Math.max(0.3, (e - w) * 0.06);
  setBox({ x: w - pad, y: mercY(n) - pad, w: e - w + 2 * pad, h: mercY(s) - mercY(n) + 2 * pad });
}

// Where most of a region's parts are: the map opens there, so that they are large enough to
// tap (the United States without Alaska, Hawaii and the islands; Europe without the Azores and
// Svalbard). The far ones are a zoom out away, and in the list.
function fitParts(id) {
  const kids = KIDS[id] || [];
  if (!id || kids.length < 8) return fit(id);
  const spans = kids.map(k => { let [w, s, e, n] = REG[k].bbox; if (w > e) e += 360; return { w, s, e, n, x: (w + e) / 2, y: (s + n) / 2 }; });
  const cut = key => { const v = spans.map(p => p[key]).sort((a, b) => a - b), d = Math.floor(v.length / 10); return [v[d], v[v.length - 1 - d]]; };
  const [x0, x1] = cut("x"), [y0, y1] = cut("y");
  const near = spans.filter(p => p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1);
  const w = Math.min(...near.map(p => p.w)), e = Math.max(...near.map(p => p.e));
  const s = Math.min(...near.map(p => p.s)), n = Math.max(...near.map(p => p.n)), pad = Math.max(0.3, (e - w) * 0.04);
  setBox({ x: w - pad, y: mercY(n) - pad, w: e - w + 2 * pad, h: mercY(s) - mercY(n) + 2 * pad });
}

function zoom(factor, cx = box.x + box.w / 2, cy = box.y + box.h / 2) {
  const w = Math.max(0.05, Math.min(720, box.w * factor)), k = w / box.w;
  setBox({ x: cx - (cx - box.x) * k, y: cy - (cy - box.y) * k, w, h: box.h * k });
}

function toMap(ev) {
  const r = map.getBoundingClientRect();
  return [box.x + (ev.clientX - r.left) / r.width * box.w, box.y + (ev.clientY - r.top) / r.height * box.h];
}

let drag = null;
map.addEventListener("pointerdown", ev => { drag = { x: ev.clientX, y: ev.clientY, box, moved: false }; });
window.addEventListener("pointermove", ev => {
  if (!drag) return;
  const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 5) return;
  drag.moved = true;
  map.classList.add("dragging");
  const r = map.getBoundingClientRect();
  setBox({ ...drag.box, x: drag.box.x - dx / r.width * drag.box.w, y: drag.box.y - dy / r.height * drag.box.h });
});
window.addEventListener("pointerup", () => { setTimeout(() => { drag = null; }, 0); map.classList.remove("dragging"); });
// The wheel scrolls the page, as everywhere else on it; with Ctrl held (and for a pinch on a
// touchpad, which browsers report the same way) it zooms the map.
map.addEventListener("wheel", ev => { if (!ev.ctrlKey) return; ev.preventDefault(); zoom(ev.deltaY > 0 ? 1.25 : 0.8, ...toMap(ev)); }, { passive: false });
$("zoom-in").onclick = () => zoom(0.6);
$("zoom-out").onclick = () => zoom(1 / 0.6);
$("zoom-fit").onclick = () => fit();
window.addEventListener("resize", () => setBox(box));

function toggle(id) {
  const above = chosenAbove(id);
  if (above) return say(`${REG[id].name} is already included in ${REG[above].name}.`);
  if (chosen.has(id)) chosen.delete(id);
  else {
    for (const c of [...chosen]) if (ancestors(c).includes(id)) chosen.delete(c);   // the whole replaces its parts
    chosen.add(id);
  }
  save();
  draw();
  refresh();
}

// A tap on a region. One without smaller parts is added or taken off at once. One with parts
// (a continent, a large country) opens a panel on the map: all of it, or choose among its parts.
function pick(id) {
  if (!chosenAbove(id) && !KIDS[id]) { closeSheet(); return toggle(id); }
  const r = REG[id], above = chosenAbove(id), sheet = $("sheet"), buttons = [];
  let text;
  if (above) {
    text = `All of ${REG[above].name} is selected, so ${r.name} is already included.`;
    buttons.push(el("button", { className: "primary", textContent: `Select Parts of ${REG[above].name} Instead`,
      onclick: () => { chosen.delete(above); save(); closeSheet(); draw(); refresh(); say(`Now click the parts of ${REG[above].name} that you want.`); } }));
  } else {
    const all = chosen.has(id);
    text = all ? `All of ${r.name} is selected.` : hasChosenInside(id) ? `${r.name} is partially selected.` : `${r.name}: select all of it, or only some parts?`;
    buttons.push(el("button", { className: all ? "" : "primary", textContent: all ? "Remove" : `Select All of ${r.name}`,
      onclick: () => { closeSheet(); toggle(id); } }));
    buttons.push(el("button", { className: all ? "primary" : "", textContent: all ? "Select Parts Instead ›" : "Select Parts ›",
      onclick: () => { if (all) { chosen.delete(id); save(); refresh(); } lookInside(id); } }));
  }
  sheet.replaceChildren(el("p", {}, el("b", { textContent: text })), el("div", { className: "row" }, ...buttons),
    el("button", { className: "close", textContent: "×", ariaLabel: "Close", onclick: closeSheet }));
  sheet.hidden = false;
}
function closeSheet() { $("sheet").hidden = true; }

function say(text) {
  const h = $("hover");
  h.textContent = text;
  h.hidden = false;
  h.style.left = "12px"; h.style.top = "52px";
  clearTimeout(say.t);
  say.t = setTimeout(() => { h.hidden = true; }, 3500);
}

function lookInside(id) {
  view = id;
  closeSheet();
  $("search").value = "";
  draw();
  fitParts(id);
}

// From a search result: the map moves to the region, among its neighbours.
function showOnMap(id) {
  closeSheet();
  view = REG[REG[id].parent] ? REG[id].parent : "";
  draw();
  fit(id);
  setHot(id);
}

function draw() {
  map.replaceChildren();
  const defs = document.createElementNS(SVG, "defs");
  defs.innerHTML = '<pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
    '<rect width="6" height="6" fill="var(--pick)"/><rect width="3" height="6" fill="var(--accent)" opacity=".7"/></pattern>';
  map.append(defs);
  // Backdrop: the land, so that the world is there at every level.
  for (const shift of [0, 360]) {
    const p = document.createElementNS(SVG, "path");
    p.setAttribute("d", pathOf(LAND, shift));
    p.setAttribute("class", "backdrop");
    map.append(p);
  }
  const ids = KIDS[view] || [];
  // Large regions first, so that small ones lie on top and can be clicked.
  for (const id of [...ids].sort((a, b) => REG[b].land_km2 - REG[a].land_km2)) for (const shift of [0, 360]) {
    const r = REG[id], p = document.createElementNS(SVG, "path");
    p.setAttribute("d", pathOf(r.geom, shift));
    p.setAttribute("fill-rule", "evenodd");
    p.dataset.id = id;
    p.setAttribute("class", "region" + (chosen.has(id) ? " chosen" : chosenAbove(id) ? " within" : hasChosenInside(id) ? " part" : ""));
    p.addEventListener("click", () => { if (!placing && (!drag || !drag.moved)) pick(id); });
    p.addEventListener("dblclick", () => { if (!placing && KIDS[id]) lookInside(id); });
    p.addEventListener("pointermove", ev => {
      const h = $("hover"), b = map.parentElement.getBoundingClientRect();
      h.textContent = r.name;
      h.hidden = false;
      h.style.left = `${ev.clientX - b.left}px`; h.style.top = `${ev.clientY - b.top}px`;
      setHot(id);
    });
    p.addEventListener("pointerleave", () => { $("hover").hidden = true; setHot(null); });
    map.append(p);
  }
  if (detail && $("opt-terrain").value === "1m") for (const shift of [0, 360]) {
    const half = detail.km / 2 / 111.32, hx = half / Math.cos(detail.lat * Math.PI / 180);
    const r = document.createElementNS(SVG, "rect");
    r.setAttribute("x", detail.lon - hx + shift); r.setAttribute("width", 2 * hx);
    r.setAttribute("y", mercY(detail.lat + half)); r.setAttribute("height", mercY(detail.lat - half) - mercY(detail.lat + half));
    r.setAttribute("class", "detail");
    map.append(r);
  }
  drawCrumbs();
  drawList();
  drawChosen();
}

// ---------- the 1 m area ----------

function setPlacing(on) {
  placing = on;
  $("placing").hidden = !on;
  map.classList.toggle("is-placing", on);
  if (on) $("step-regions").scrollIntoView({ behavior: "smooth" });
}
$("detail-place").onclick = () => setPlacing(true);
$("placing-stop").onclick = () => setPlacing(false);
map.addEventListener("click", ev => {
  if (!placing || (drag && drag.moved)) return;
  const [x, y] = toMap(ev);
  const lat = (2 * Math.atan(Math.exp(-y * Math.PI / 180)) - Math.PI / 2) * 180 / Math.PI;
  detail = { lon: ((x + 540) % 360) - 180, lat, km: +$("opt-detail-km").value };
  localStorage.setItem("detail", JSON.stringify(detail));
  setPlacing(false);
  draw();
  refresh();
});

function drawDetail() {
  const on = $("opt-terrain").value === "1m";
  $("detail-box").hidden = !on;
  if (!on) return;
  if (detail) detail.km = +$("opt-detail-km").value;
  $("detail-where").textContent = detail
    ? `Selected: ${detail.km} × ${detail.km} km (${Math.round(detail.km * 0.62)} × ${Math.round(detail.km * 0.62)} miles), centered at ${Math.abs(detail.lat).toFixed(3)}° ${detail.lat >= 0 ? "N" : "S"}, ${Math.abs(detail.lon).toFixed(3)}° ${detail.lon >= 0 ? "E" : "W"}. The orange square on the map shows the area.`
    : "No area selected.";
}

function setHot(id) {
  if (hot === id) return;
  hot = id;
  for (const n of document.querySelectorAll(".hot")) n.classList.remove("hot");
  if (id) for (const n of document.querySelectorAll(`[data-id="${CSS.escape(id)}"]`)) n.classList.add("hot");
}

function drawCrumbs() {
  const trail = view ? [...ancestors(view).reverse(), view] : [];
  const c = $("crumbs");
  c.replaceChildren();
  if (!view) return c.append(el("span", { className: "here", textContent: "World" }));
  c.append(el("button", { textContent: "‹ World", onclick: () => lookInside("") }));
  for (const id of trail.slice(0, -1)) c.append(el("button", { textContent: `‹ ${REG[id].name}`, onclick: () => lookInside(id) }));
  c.append(el("span", { className: "here", textContent: REG[view].name }));
}

function regionRow(id, where) {
  const r = REG[id], above = chosenAbove(id);
  const li = el("li");
  li.dataset.id = id;
  li.append(el("span", { className: "name" }, r.name,
    el("small", { textContent: where || (above ? `Included in ${REG[above].name}` : hasChosenInside(id) ? "Partially Selected" : "") })));
  if (where) li.append(el("button", { textContent: "Show", title: `Show ${r.name} on the map`, onclick: () => showOnMap(id) }));
  else if (KIDS[id]) li.append(el("button", { textContent: "Parts ›", title: `Show the parts of ${r.name} to select only some of them.`, onclick: () => lookInside(id) }));
  if (!above) li.append(el("button", { className: chosen.has(id) ? "on" : "", textContent: chosen.has(id) ? "✓ Selected" : "Add",
    title: chosen.has(id) ? "Click to remove this region from the selection." : "Add this region to the selection.", onclick: () => toggle(id) }));
  li.addEventListener("pointerenter", () => setHot(id));
  li.addEventListener("pointerleave", () => setHot(null));
  return li;
}

function drawList() {
  const q = $("search").value.trim().toLowerCase(), list = $("list");
  list.replaceChildren();
  let ids;
  if (q) {
    $("list-title").textContent = "Search Results";
    ids = Object.keys(REG).filter(id => REG[id].name.toLowerCase().includes(q))
      .sort((a, b) => REG[a].name.toLowerCase().indexOf(q) - REG[b].name.toLowerCase().indexOf(q) || REG[a].name.localeCompare(REG[b].name)).slice(0, 40);
  } else {
    const whole = view && (chosen.has(view) ? view : chosenAbove(view));
    $("list-title").textContent = (view ? `Visible Regions: ${REG[view].name}` : "Visible Regions") + (whole ? ` (all of ${REG[whole].name} selected)` : "");
    ids = [...(KIDS[view] || [])].sort((a, b) => REG[a].name.localeCompare(REG[b].name));
  }
  if (!ids.length) list.append(el("li", { className: "empty", textContent: q ? "No matching regions. Try a country or state name." : "This region has no smaller parts." }));
  for (const id of ids) list.append(regionRow(id, q && REG[id].parent ? `in ${REG[REG[id].parent].name}` : ""));
}
$("search").addEventListener("input", drawList);

function drawChosen() {
  const ul = $("chosen");
  ul.replaceChildren();
  if (!chosen.size) return ul.append(el("li", { className: "none", textContent: "No regions selected. Click a region on the map." }));
  for (const id of [...chosen].sort((a, b) => REG[a].name.localeCompare(REG[b].name)))
    ul.append(el("li", {}, REG[id].name, el("button", { textContent: "×", title: `Remove ${REG[id].name}`, ariaLabel: `Remove ${REG[id].name}`, onclick: () => toggle(id) })));
}

// ---------- step 2: layers and options ----------

function drawLayers() {
  const ul = $("layers"), us = [...chosen].some(id => isUS(REG[id]));
  ul.replaceChildren();
  for (const L of LAYERS) {
    if (L.us && !us) continue;
    const now = status && status.layers[L.key], item = estimate && estimate.items.find(i => i.key === L.key);
    const box = el("input", { type: "checkbox", checked: ticked.has(L.key), onchange: () => { box.checked ? ticked.add(L.key) : ticked.delete(L.key); save(); refresh(); } });
    const help = el("p", { className: "helptext", hidden: true, textContent: L.help });
    const q = el("button", { className: "help", textContent: "?", title: L.help, ariaLabel: `Explain ${L.name}`, onclick: ev => { ev.preventDefault(); help.hidden = !help.hidden; } });
    const what = el("span", { className: "what" }, el("b", {}, L.name, q), el("span", { textContent: L.line }));
    const state = el("div", { className: "now" });
    if (now && now.present) state.append("Installed: ", el("b", { textContent: now.names ? now.names.join(", ") : "built outside the Map Builder" }),
      now.built ? ` (${day(now.built)}, ${gb(now.size_gb)})` : ` (${gb(now.size_gb)})`);
    else state.textContent = "Not Installed";
    const li = el("li", {}, el("label", {}, box, what), state, help);
    if (item && ticked.has(L.key)) li.append(el("div", { className: "size", textContent: `Size after the build: ${item.kept_gb < 0.05 ? "under 50 MB" : "about " + gb(item.kept_gb)}.` + (now && now.present ? " It replaces the installed layer." : "") }));
    else if (now && now.present && !ticked.has(L.key)) li.append(el("div", { className: "size", textContent: "Not selected. The installed layer does not change." }));
    ul.append(li);
  }
}

for (const b of document.querySelectorAll("button.help[data-help]"))
  b.addEventListener("click", ev => { ev.preventDefault(); const p = $(b.dataset.help); p.hidden = !p.hidden; });
for (const id of ["opt-terrain", "opt-units", "opt-language", "opt-world", "opt-fresh", "opt-detail-km"])
  $(id).addEventListener("change", () => { if (detail) localStorage.setItem("detail", JSON.stringify({ ...detail, km: +$("opt-detail-km").value })); save(); draw(); refresh(); });

function selection() {
  const us = [...chosen].some(id => isUS(REG[id]));
  return { regions: [...chosen], layers: LAYERS.filter(L => ticked.has(L.key) && (!L.us || us)).map(L => L.key),
    options: { terrain: $("opt-terrain").value, units: $("opt-units").value, language: $("opt-language").value, world: $("opt-world").checked,
      fresh: $("opt-fresh").checked,
      detail: detail && { ...detail, km: +$("opt-detail-km").value } } };
}

// ---------- step 3: space and time ----------

let pending = 0;
// How old the map data on this device is, for the regions chosen.
function drawFreshness() {
  const ages = status ? status.osm_ages : {}, limit = status ? status.osm_refresh_days : 30;
  const got = [...chosen].map(id => ages[REG[id].path.split("/").pop()]);
  const have = got.filter(a => a !== undefined);
  const days = n => n === 0 ? "today" : n === 1 ? "yesterday" : `${n} days ago`;
  $("fresh-age").textContent = !chosen.size ? ""
    : !have.length ? "The server has no map data for these regions yet. The build downloads it."
    : `The server downloaded the map data for these regions ${days(Math.min(...have))}${Math.max(...have) !== Math.min(...have) ? ` to ${days(Math.max(...have))}` : ""}` +
      (have.length < got.length ? ". Some regions have no data yet" : "") +
      `. The build downloads again any data older than ${limit} days.`;
}

async function refresh() {
  drawLayers();
  drawDetail();
  drawFreshness();
  const sel = selection(), mine = ++pending;
  $("build-error").textContent = "";
  if (!sel.regions.length || !sel.layers.length) {
    estimate = null;
    $("space").hidden = true; $("space-empty").hidden = false;
    $("space-empty").textContent = "Select a region and at least one layer to see the storage the build needs.";
    $("start").disabled = true;
    $("start-why").textContent = !sel.regions.length ? "Select at least one region." : "Select at least one layer.";
    return drawLayers();
  }
  try {
    const e = await api("api/estimate", sel);
    if (mine !== pending) return;           // a newer choice is already on its way
    estimate = e;
  } catch (err) {
    if (mine !== pending || err.message === "pin") return;
    estimate = null;
    $("space").hidden = true; $("space-empty").hidden = false;
    $("space-empty").textContent = err.message;
    $("start").disabled = true;
    $("start-why").textContent = err.message;
    return;
  }
  drawLayers();
  const e = estimate, share = e.need_gb / Math.max(e.free_gb, 0.1);
  const cls = !e.ok ? "over" : share > 0.8 ? "tight" : "ok";
  $("space").hidden = false; $("space-empty").hidden = true;
  $("meter").className = `meter ${cls}`;
  $("meter-need").style.width = `${Math.min(100, share * 100)}%`;
  $("meter").setAttribute("aria-label", `Needs ${gb(e.need_gb)} of ${gb(e.free_gb)} free`);
  $("verdict").className = `verdict ${cls}`;
  // When the sharper terrain is most of it, that is the choice to change, not the small layers.
  const terrain = e.standard_need_gb !== undefined && e.standard_need_gb < e.need_gb * 0.7 && cls !== "ok";
  $("verdict").textContent = (cls === "over"
    ? `Not enough storage. The build needs about ${gb(e.need_gb)}, but only ${gb(e.free_gb)} is free.` + (terrain ? "" : " Select fewer or smaller regions, or fewer layers.")
    : `${cls === "tight" ? "This fits, but storage will be low" : "This fits"}. The build needs about ${gb(e.need_gb)} while it runs, and ${gb(e.free_gb)} is free. ` +
      (e.more_after_gb >= 0.05 ? `After the build, about ${gb(e.more_after_gb)} more storage is in use.` : "After the build, storage use does not increase.")) +
    (terrain ? ` Most of this is for the high-resolution terrain. With Terrain Resolution set to Standard (under Advanced Options), the build needs about ${gb(e.standard_need_gb)}.` : "");
  $("space-have").textContent = e.on_hand_gb >= 0.05
    ? `${gb(e.on_hand_gb)} of this is already on the server (elevation data from earlier builds, or parts of an unfinished build) and is not counted again.` : "";
  $("time").textContent = `Build time: ${hours(e.hours)} on the server, plus download time, which depends on the internet connection.`;
  const body = $("space-table").tBodies[0];
  body.replaceChildren();
  for (const i of e.items) body.append(el("tr", {}, el("td", { textContent: i.label }), el("td", { textContent: gb(i.kept_gb) }),
    el("td", { textContent: i.temp_gb ? gb(i.temp_gb) : "–" }), el("td", { textContent: hours(i.hours) })));
  $("start").disabled = !e.ok;
  $("start-why").textContent = e.ok ? "" : "Not enough free storage for this build.";
}

// ---------- step 4: start ----------

$("start").onclick = () => {
  const sel = selection(), b = $("confirm-body");
  b.replaceChildren(
    el("p", {}, "Regions: ", el("b", { textContent: sel.regions.map(id => REG[id].name).join(", ") })),
    el("p", { textContent: "Layers to build:" }),
    el("ul", {}, ...sel.layers.map(k => { const L = layerOf(k), now = status.layers[k];
      return el("li", {}, el("b", { textContent: L.name }), now.present ? ` — replaces the installed layer${now.names ? ` (${now.names.join(", ")})` : ""}` : " — new"); }),
      ...(sel.options.terrain === "1m" ? [el("li", {}, el("b", { textContent: DETAIL.name }), ` — ${sel.options.detail.km} × ${sel.options.detail.km} km, on top of the hillshade`)] : [])),
    ...(sel.options.fresh ? [el("p", { textContent: "The server downloads the latest map data." })] : []),
    ...(carriesOn(sel) ? [el("p", { textContent: "This build continues from where the last build stopped and reuses its completed parts." })] : []),
    el("p", { textContent: `The build needs about ${gb(estimate.need_gb)} of storage and takes ${hours(estimate.hours)}, plus download time. The current maps work until the build is complete.` }));
  $("confirm").showModal();
};
$("confirm-no").onclick = () => $("confirm").close();
$("confirm-yes").onclick = async () => {
  $("confirm").close();
  try {
    await api("api/build", selection());
    dismissed = null; sessionStorage.removeItem("dismissed");
    await poll();
    window.scrollTo({ top: 0 });
  } catch (err) {
    if (err.message !== "pin") $("build-error").textContent = err.message;
  }
};

// ---------- the running or last build ----------

// The last build's choices (the server keeps them with the build), back in steps 1 and 2.
async function repeatLast() {
  const r = status.job.request, o = r.options || {};
  chosen.clear();
  for (const id of r.regions) if (REG[id]) chosen.add(id);
  ticked.clear();
  for (const k of r.layers) if (k !== "detail") ticked.add(k);
  setOptions({ ...o, language: o.language || "" });
  if (o.detail) {
    detail = { ...o.detail };
    $("opt-detail-km").value = detail.km;
    localStorage.setItem("detail", JSON.stringify(detail));
  }
  save();
  dismissed = String(status.job.started); sessionStorage.setItem("dismissed", dismissed);
  drawJob();
  draw();
  await refresh();
  // Straight to the question "Start this build?", unless something is in the way (step 4 says what).
  if (!$("start").disabled) $("start").click();
  else $("step-build").scrollIntoView({ behavior: "smooth" });
}
$("repeat").onclick = $("repeat-last").onclick = repeatLast;

// Steps 1 and 2 as on a first visit: no regions, every layer ticked, the first options.
$("clear").onclick = () => {
  chosen.clear();
  ticked.clear();
  for (const k of ALL_LAYERS) ticked.add(k);
  detail = null;
  for (const k of ["chosen", "ticked", "options", "detail"]) localStorage.removeItem(k);
  setOptions({});
  $("opt-detail-km").value = 20;
  setPlacing(false);
  closeSheet();
  $("search").value = "";
  view = "";
  draw();
  fit();
  refresh();
  $("step-regions").scrollIntoView({ behavior: "smooth" });
};

// True when sel is the build that failed or was stopped last, so that it keeps what that one
// finished: the same regions and settings (scripts/build-region.sh --staged).
function carriesOn(sel) {
  const job = status.job;
  if (!job || !["failed", "cancelled"].includes(job.state)) return false;
  const key = (regions, o) => JSON.stringify([[...regions].sort(), o.terrain, o.units, o.language || "", !!o.world,
    o.terrain === "1m" && o.detail ? [o.detail.km, +o.detail.lon.toFixed(5), +o.detail.lat.toFixed(5)] : null]);
  return key(sel.regions, sel.options) === key(job.request.regions, job.request.options);
}

let logAt = 0, logFor = null;
function drawJob() {
  const job = status.job, show = job && (job.state === "running" || String(job.started) !== dismissed);
  $("progress").hidden = !show;
  // In step 4 once the card of the last build is closed: one of the two buttons at a time.
  $("repeat-last").hidden = $("repeat-what").hidden = !job || show;
  if (job && !show) $("repeat-what").textContent = `Last build${{ failed: " (failed)", cancelled: " (canceled)" }[job.state] || ""}: ${job.request.names.join(", ")}, ${day(job.started)}.`;
  $("chooser").hidden = !!(job && job.state === "running");
  if (!show) return;
  const running = job.state === "running", names = job.request.names.join(", ");
  $("progress-title").textContent = { running: "Building Maps…", done: "Build Complete", failed: "Build Failed", cancelled: "Build Canceled" }[job.state];
  $("progress-text").textContent = running ? `Regions: ${names}.` : `${job.message} (Regions: ${names}.)`;
  const steps = $("progress-steps");
  steps.replaceChildren();
  for (const k of job.request.layers) {
    const L = layerOf(k), done = job.done.includes(k) || job.state === "done";
    steps.append(el("li", { className: done ? "done" : job.current === k ? "now" : "", textContent: L.name + (done ? "" : job.current === k ? " — in progress" : "") }));
  }
  $("progress-note").textContent = running
    ? `Started ${ago(job.started)}. You can close this page; the build continues. The current maps work until the build is complete.`
    : job.finished ? `Finished ${ago(job.finished)}.` : "";
  $("cancel").hidden = !running;
  $("again").hidden = $("repeat").hidden = running;
  $("repeat").textContent = job.state === "done" ? "Rebuild" : "Resume Build";
  $("repeat").title = job.state === "done" ? "Uses the same regions, layers, and options. Asks before the build starts."
    : "Uses the same regions, layers, and options. The build continues from where it stopped.";
  // Beside the viewer and the print tool when this page is /mapadmin/ on the web front; else on its own port.
  const base = location.pathname.startsWith("/mapadmin") ? location.origin
    : `${location.protocol}//${location.hostname}${status.web_port === 80 ? "" : ":" + status.web_port}`;
  $("open-map").hidden = $("open-print").hidden = job.state !== "done";
  $("open-map").href = `${base}/`; $("open-print").href = `${base}/mapprinter/`;
  if (job.state === "failed") $("details").open = true;
  // A new build: show its last 200 kB, where a failure is explained, not its beginning.
  if (logFor !== job.started) {
    logFor = job.started;
    logAt = Math.max(0, job.log_size - 200000);
    $("log").textContent = logAt ? "… (earlier lines omitted)\n" : "";
  }
  if ($("details").open && job.log_size > logAt) readLog();
}

async function readLog() {
  const pre = $("log"), bottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  const part = await api(`api/log?from=${logAt}`).catch(() => null);
  if (!part || part.from !== logAt) return;
  logAt = part.next;
  // The tools redraw one line with carriage returns: keep what was drawn last.
  // The tools also colour their output: those codes are dropped.
  pre.textContent = (pre.textContent + part.text.replace(/\x1b\[[0-9;]*m/g, "")).split("\n").map(l => l.split("\r").pop()).join("\n").slice(-300000);
  if (bottom) pre.scrollTop = pre.scrollHeight;
}
$("details").addEventListener("toggle", () => { if ($("details").open && status && status.job) readLog(); });

$("cancel").onclick = async () => {
  if (!confirm("Cancel the build? The current maps do not change, and the server keeps the downloaded data for the next build.")) return;
  await api("api/cancel", {}).catch(() => {});
  poll();
};
$("again").onclick = () => { dismissed = String(status.job.started); sessionStorage.setItem("dismissed", dismissed); drawJob(); refresh(); };

function drawDisk() {
  const used = 1 - status.free_gb / status.total_gb;
  $("disk").replaceChildren(el("b", { textContent: gb(status.free_gb) }), ` free of ${gb(status.total_gb)} on the server`,
    el("div", { className: "bar" }, el("i", { style: `width:${Math.round(used * 100)}%` })));
}

let wasRunning = false;
async function poll() {
  try { status = await api("api/status"); } catch { return; }
  drawDisk();
  drawJob();
  const running = !!(status.job && status.job.state === "running");
  if (wasRunning && !running) refresh();        // free space and what is on the device have changed
  // Space freed or taken on the device by something else: the estimate follows.
  else if (!running && estimate && Math.abs(status.free_gb - estimate.free_gb) >= 1) refresh();
  wasRunning = running;
}

async function start() {
  try { status = await api("api/status"); } catch { return; }
  $("builder").hidden = false;
  if (!Object.keys(REG).length) {
    LAND = (await (await fetch("land.json")).json()).geom;
    PLACES = (await (await fetch("places.json")).json()).places;
    for (const km of status.detail_km) $("opt-detail-km").append(el("option", { value: km, textContent: `${km} × ${km} km (about ${Math.round(km * 0.62)} × ${Math.round(km * 0.62)} miles)` }));
    $("opt-detail-km").value = detail ? detail.km : 20;
    const data = await (await fetch("regions.json")).json();
    for (const r of data.regions) { REG[r.id] = r; }
    for (const r of data.regions) (KIDS[REG[r.parent] ? r.parent : ""] ||= []).push(r.id);
    for (const id of [...chosen]) if (!REG[id]) chosen.delete(id);
    const lang = { "": "All Languages (Largest)", en: "English", zh: "Chinese", hi: "Hindi", es: "Spanish", ar: "Arabic", fr: "French", bn: "Bengali", pt: "Portuguese", ru: "Russian", id: "Indonesian" };
    for (const code of ["", ...status.languages]) $("opt-language").append(el("option", { value: code, textContent: lang[code] || code }));
    setOptions(JSON.parse(localStorage.getItem("options") || "{}"));
    setInterval(poll, 3000);
  }
  draw();
  fit();
  drawDisk();
  drawJob();
  wasRunning = !!(status.job && status.job.state === "running");
  refresh();
}

start();
