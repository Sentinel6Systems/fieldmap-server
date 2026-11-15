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
  { key: "basemap", name: "Base map", line: "Roads, towns, rivers, borders and place names.",
    help: "This is the map itself, made from OpenStreetMap. Without it the other layers have nothing to sit on, so leave it ticked unless you only want to renew one of the others." },
  { key: "contours", name: "Contour lines", line: "Lines that show height, as on a hiking map.",
    help: "Every line joins places of the same height. Lines close together mean steep ground. This is usually the largest layer and the slowest to build." },
  { key: "hillshade", name: "Hillshade", line: "Soft shadows that make hills and valleys stand out.",
    help: "A picture of the ground lit from the north-west. It makes the shape of the land easy to see at a glance, on screen and on paper." },
  { key: "elevation", name: "Elevation for the terrain tools", line: "Lets the print tool work out what can be seen from a hill and where a radio reaches.",
    help: "The heights themselves, stored as numbers. Nothing is drawn from this layer; the print tool uses it for its line-of-sight and radio coverage tools. Leave it out if you do not use them." },
  { key: "extras", name: "Extra details for printed maps", line: "Power lines, fences, towers, springs and other landmarks.",
    help: "Small features that help you find your way on the ground. They are drawn on the printed map styles." },
  { key: "usdata", name: "US government data", line: "Aircraft obstacles, official place names, survey marks and public land.", us: true,
    help: "Free data from United States agencies (FAA, USGS, NOAA), drawn on the printed map styles. It exists only for the United States." },
];
// Not a layer to tick: the 1 m area of "More options", which is its own step of a build.
const DETAIL = { key: "detail", name: "Very sharp terrain area" };
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
const ticked = new Set(JSON.parse(localStorage.getItem("ticked") || '["basemap","contours","hillshade","elevation","extras","usdata"]'));

// ---------- talking to the server ----------

// Paths are relative: the page is served as /mapadmin/ by the web front, or at / on its own port.
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { showLogin(); throw new Error("pin"); }
  if (!r.ok) throw new Error(data.error || "The device did not answer. Is the map builder still running?");
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
};

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
  if (above) return say(`${REG[id].name} is already included, as part of ${REG[above].name}.`);
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
    text = `${r.name} is already included, because all of ${REG[above].name} is chosen.`;
    buttons.push(el("button", { className: "primary", textContent: `Choose only parts of ${REG[above].name} instead`,
      onclick: () => { chosen.delete(above); save(); closeSheet(); draw(); refresh(); say(`Now tap the parts of ${REG[above].name} you want.`); } }));
  } else {
    const all = chosen.has(id);
    text = all ? `All of ${r.name} is chosen.` : hasChosenInside(id) ? `Part of ${r.name} is chosen.` : `${r.name}: all of it, or only parts of it?`;
    buttons.push(el("button", { className: all ? "" : "primary", textContent: all ? "Take it off" : `Add all of ${r.name}`,
      onclick: () => { closeSheet(); toggle(id); } }));
    buttons.push(el("button", { className: all ? "primary" : "", textContent: all ? "Choose only parts of it instead ›" : "Choose parts of it ›",
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
    ? `Placed: ${detail.km} × ${detail.km} km (${Math.round(detail.km * 0.62)} × ${Math.round(detail.km * 0.62)} miles) around ${Math.abs(detail.lat).toFixed(3)}° ${detail.lat >= 0 ? "N" : "S"}, ${Math.abs(detail.lon).toFixed(3)}° ${detail.lon >= 0 ? "E" : "W"}. It is the orange square on the map.`
    : "Not placed yet.";
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
  if (!view) return c.append(el("span", { className: "here", textContent: "The world" }));
  c.append(el("button", { textContent: "‹ The world", onclick: () => lookInside("") }));
  for (const id of trail.slice(0, -1)) c.append(el("button", { textContent: `‹ ${REG[id].name}`, onclick: () => lookInside(id) }));
  c.append(el("span", { className: "here", textContent: REG[view].name }));
}

function regionRow(id, where) {
  const r = REG[id], above = chosenAbove(id);
  const li = el("li");
  li.dataset.id = id;
  li.append(el("span", { className: "name" }, r.name,
    el("small", { textContent: where || (above ? `included with ${REG[above].name}` : hasChosenInside(id) ? "part of it is chosen" : "") })));
  if (where) li.append(el("button", { textContent: "Show", title: `Move the map to ${r.name}`, onclick: () => showOnMap(id) }));
  else if (KIDS[id]) li.append(el("button", { textContent: "Its parts ›", title: `Show the parts of ${r.name}, to choose only some of them`, onclick: () => lookInside(id) }));
  if (!above) li.append(el("button", { className: chosen.has(id) ? "on" : "", textContent: chosen.has(id) ? "✓ Chosen" : "Add",
    title: chosen.has(id) ? "Click to take it off your list" : "Add this region to your list", onclick: () => toggle(id) }));
  li.addEventListener("pointerenter", () => setHot(id));
  li.addEventListener("pointerleave", () => setHot(null));
  return li;
}

function drawList() {
  const q = $("search").value.trim().toLowerCase(), list = $("list");
  list.replaceChildren();
  let ids;
  if (q) {
    $("list-title").textContent = "Search results";
    ids = Object.keys(REG).filter(id => REG[id].name.toLowerCase().includes(q))
      .sort((a, b) => REG[a].name.toLowerCase().indexOf(q) - REG[b].name.toLowerCase().indexOf(q) || REG[a].name.localeCompare(REG[b].name)).slice(0, 40);
  } else {
    const whole = view && (chosen.has(view) ? view : chosenAbove(view));
    $("list-title").textContent = (view ? `Parts of ${REG[view].name}` : "Parts of the world") + (whole ? ` (all of ${REG[whole].name} is chosen)` : "");
    ids = [...(KIDS[view] || [])].sort((a, b) => REG[a].name.localeCompare(REG[b].name));
  }
  if (!ids.length) list.append(el("li", { className: "empty", textContent: q ? "Nothing with that name. Try a country or a state." : "This region has no smaller parts." }));
  for (const id of ids) list.append(regionRow(id, q && REG[id].parent ? `in ${REG[REG[id].parent].name}` : ""));
}
$("search").addEventListener("input", drawList);

function drawChosen() {
  const ul = $("chosen");
  ul.replaceChildren();
  if (!chosen.size) return ul.append(el("li", { className: "none", textContent: "None yet. Click a region on the map." }));
  for (const id of [...chosen].sort((a, b) => REG[a].name.localeCompare(REG[b].name)))
    ul.append(el("li", {}, REG[id].name, el("button", { textContent: "×", title: `Take ${REG[id].name} off the list`, ariaLabel: `Take ${REG[id].name} off the list`, onclick: () => toggle(id) })));
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
    if (now && now.present) state.append("On this device now: ", el("b", { textContent: now.names ? now.names.join(", ") : "a map built outside this page" }),
      now.built ? ` (${day(now.built)}, ${gb(now.size_gb)})` : ` (${gb(now.size_gb)})`);
    else state.textContent = "Not on this device yet";
    const li = el("li", {}, el("label", {}, box, what), state, help);
    if (item && ticked.has(L.key)) li.append(el("div", { className: "size", textContent: `${item.kept_gb < 0.05 ? "Under 50 MB" : "About " + gb(item.kept_gb)} when finished` + (now && now.present ? "; replaces what is there now" : "") }));
    else if (now && now.present && !ticked.has(L.key)) li.append(el("div", { className: "size", textContent: "Not ticked: stays as it is" }));
    ul.append(li);
  }
}

for (const b of document.querySelectorAll("button.help[data-help]"))
  b.addEventListener("click", ev => { ev.preventDefault(); const p = $(b.dataset.help); p.hidden = !p.hidden; });
for (const id of ["opt-terrain", "opt-units", "opt-language", "opt-world", "opt-detail-km"])
  $(id).addEventListener("change", () => { if (detail) localStorage.setItem("detail", JSON.stringify({ ...detail, km: +$("opt-detail-km").value })); save(); draw(); refresh(); });

function selection() {
  const us = [...chosen].some(id => isUS(REG[id]));
  return { regions: [...chosen], layers: LAYERS.filter(L => ticked.has(L.key) && (!L.us || us)).map(L => L.key),
    options: { terrain: $("opt-terrain").value, units: $("opt-units").value, language: $("opt-language").value, world: $("opt-world").checked,
      detail: detail && { ...detail, km: +$("opt-detail-km").value } } };
}

// ---------- step 3: space and time ----------

let pending = 0;
async function refresh() {
  drawLayers();
  drawDetail();
  const sel = selection(), mine = ++pending;
  $("build-error").textContent = "";
  if (!sel.regions.length || !sel.layers.length) {
    estimate = null;
    $("space").hidden = true; $("space-empty").hidden = false;
    $("space-empty").textContent = "Choose a region and at least one layer to see how much space the build needs.";
    $("start").disabled = true;
    $("start-why").textContent = !sel.regions.length ? "Choose at least one region first." : "Tick at least one layer first.";
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
  $("verdict").textContent = cls === "over"
    ? `This does not fit. It needs about ${gb(e.need_gb)} and only ${gb(e.free_gb)} is free. Choose fewer or smaller regions, or untick a layer.`
    : `${cls === "tight" ? "This fits, but only just" : "This fits"}: it needs about ${gb(e.need_gb)} while it works, and ${gb(e.free_gb)} is free. About ${gb(e.kept_gb)} stays in use afterwards.`;
  $("time").textContent = `It will take ${hours(e.hours)} on this device, plus the time to download (that depends on your internet connection).`;
  const body = $("space-table").tBodies[0];
  body.replaceChildren();
  for (const i of e.items) body.append(el("tr", {}, el("td", { textContent: i.label }), el("td", { textContent: gb(i.kept_gb) }),
    el("td", { textContent: i.temp_gb ? gb(i.temp_gb) : "–" }), el("td", { textContent: hours(i.hours) })));
  $("start").disabled = !e.ok;
  $("start-why").textContent = e.ok ? "" : "There is not enough free space for this build.";
}

// ---------- step 4: start ----------

$("start").onclick = () => {
  const sel = selection(), b = $("confirm-body");
  b.replaceChildren(
    el("p", {}, "Regions: ", el("b", { textContent: sel.regions.map(id => REG[id].name).join(", ") })),
    el("p", { textContent: "This will build:" }),
    el("ul", {}, ...sel.layers.map(k => { const L = layerOf(k), now = status.layers[k];
      return el("li", {}, el("b", { textContent: L.name }), now.present ? ` — replaces the one on this device${now.names ? ` (${now.names.join(", ")})` : ""}` : " — new"); }),
      ...(sel.options.terrain === "1m" ? [el("li", {}, el("b", { textContent: DETAIL.name }), ` — ${sel.options.detail.km} × ${sel.options.detail.km} km, drawn over the hillshade`)] : [])),
    el("p", { textContent: `It needs about ${gb(estimate.need_gb)} of space and takes ${hours(estimate.hours)}, plus download time. The maps you have now keep working until it is finished.` }));
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

let logAt = 0, logFor = null;
function drawJob() {
  const job = status.job, show = job && (job.state === "running" || String(job.started) !== dismissed);
  $("progress").hidden = !show;
  $("chooser").hidden = !!(job && job.state === "running");
  if (!show) return;
  const running = job.state === "running", names = job.request.names.join(", ");
  $("progress-title").textContent = { running: "Building your maps…", done: "Your maps are ready", failed: "The build did not finish", cancelled: "The build was stopped" }[job.state];
  $("progress-text").textContent = running ? `Regions: ${names}.` : `${job.message} (Regions: ${names}.)`;
  const steps = $("progress-steps");
  steps.replaceChildren();
  for (const k of job.request.layers) {
    const L = layerOf(k), done = job.done.includes(k) || job.state === "done";
    steps.append(el("li", { className: done ? "done" : job.current === k ? "now" : "", textContent: L.name + (done ? "" : job.current === k ? " — working on it now" : "") }));
  }
  $("progress-note").textContent = running
    ? `Started ${ago(job.started)}. You can close this page and come back later: the build carries on. The maps you have now keep working until it is finished.`
    : job.finished ? `Ended ${ago(job.finished)}.` : "";
  $("cancel").hidden = !running;
  $("again").hidden = running;
  // Beside the viewer and the print tool when this page is /mapadmin/ on the web front; else on its own port.
  const base = location.pathname.startsWith("/mapadmin") ? location.origin
    : `${location.protocol}//${location.hostname}${status.web_port === 80 ? "" : ":" + status.web_port}`;
  $("open-map").hidden = $("open-print").hidden = job.state !== "done";
  $("open-map").href = `${base}/`; $("open-print").href = `${base}/mapprinter/`;
  if (job.state === "failed") $("details").open = true;
  if (logFor !== job.started) { logFor = job.started; logAt = 0; $("log").textContent = ""; }
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
  if (!confirm("Stop the build? The maps on this device stay as they are. What was already downloaded is kept for next time.")) return;
  await api("api/cancel", {}).catch(() => {});
  poll();
};
$("again").onclick = () => { dismissed = String(status.job.started); sessionStorage.setItem("dismissed", dismissed); drawJob(); refresh(); };

function drawDisk() {
  const used = 1 - status.free_gb / status.total_gb;
  $("disk").replaceChildren(el("b", { textContent: gb(status.free_gb) }), ` free of ${gb(status.total_gb)} on this device`,
    el("div", { className: "bar" }, el("i", { style: `width:${Math.round(used * 100)}%` })));
}

let wasRunning = false;
async function poll() {
  try { status = await api("api/status"); } catch { return; }
  drawDisk();
  drawJob();
  const running = !!(status.job && status.job.state === "running");
  if (wasRunning && !running) refresh();        // free space and what is on the device have changed
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
    const lang = { "": "every language (largest)", en: "English", zh: "Chinese", hi: "Hindi", es: "Spanish", ar: "Arabic", fr: "French", bn: "Bengali", pt: "Portuguese", ru: "Russian", id: "Indonesian" };
    for (const code of ["", ...status.languages]) $("opt-language").append(el("option", { value: code, textContent: lang[code] || code }));
    // The browser's language when the build can keep it, else every language.
    $("opt-language").value = status.languages.includes(navigator.language.slice(0, 2)) ? navigator.language.slice(0, 2) : "";
    const o = JSON.parse(localStorage.getItem("options") || "{}");
    if (o.terrain) $("opt-terrain").value = o.terrain;
    if (o.units) $("opt-units").value = o.units;
    if (typeof o.language === "string") $("opt-language").value = o.language;
    $("opt-world").checked = !!o.world;
    document.querySelector("details.more").open = (o.terrain && o.terrain !== "30m") || !!o.world;
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
