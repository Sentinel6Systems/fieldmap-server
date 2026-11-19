// fieldmap-server: the header every page shares. A page carries
//   <header class="fm-bar" id="fm-bar" data-page="maps|print|guide|access|phones|builder" data-root="<its own folder>"></header>
// and loads this file right after it. The header shows the name, a link to each page that
// is there (the current one marked), and a Home button when the site sets one.
//
// Maps, Print Map and the User Guide are for everyone. What not everyone needs is under the
// Admin tab: remote access, the devices and the map builder now, more later. On those pages a
// second row under the header names them (when there is more than one).
//
// Where the other pages are is found out, not configured, on this page's own host:
//   the print tool    /mapprinter/   (its config.json answers)
//   the Maps page     /maps/ behind Internet-in-a-Box, else /   (styles.json answers)
//   the User Guide    /guide/        (its fieldmap.css answers)
//   the map builder   /mapadmin/     (answers; "Unavailable" when only its place is there)
//   the Devices page  /phones/       (the Fieldmap app; the same, and only on the box's own network,
//                                    where the page itself answers), and Remote access beside it
//                                    at /phones/remote.html
// A page that is not found gets no link. Home is homeUrl (and homeName) in the print
// tool's config.json: the address of a landing page in front of these pages, if any.
(() => {
  const bar = document.getElementById("fm-bar");
  if (!bar) return;
  const here = bar.dataset.page;
  const own = new URL(bar.dataset.root || "./", location.href).pathname;
  const PAGES = [["maps", "Maps", "Maps"], ["print", "Print Map", "Print"], ["guide", "User Guide", "Guide"], ["admin", "Admin", "Admin"]];
  // Under Admin, in this order. The Admin tab opens the first one that is running.
  const ADMIN = [["access", "Remote Access"], ["phones", "Devices"], ["builder", "Map Builder"]];
  const inAdmin = ADMIN.some(([key]) => key === here);
  const CACHE = "fieldmap.nav";

  const el = (tag, props = {}, ...kids) => {
    const n = Object.assign(document.createElement(tag), props);
    n.append(...kids);
    return n;
  };
  const MARK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">' +
    '<rect x="3" y="3" width="18" height="18"/><path d="M3 10h18M10 3v18"/><circle cx="15.5" cy="15.5" r="1.6" fill="currentColor" stroke="none"/></svg>';

  // Where each page is: its address, "off" (installed, not running) or nothing (not there).
  const place = (found, key) => (key === here ? (key === "access" ? location.pathname : own) : found[key]);

  function draw(found) {
    const mark = el("div", { className: "fm-mark" });
    mark.innerHTML = MARK + "<span>FIELDMAP</span>";
    const nav = el("nav", { className: "fm-nav", ariaLabel: "Pages" });
    for (const [key, long, short] of PAGES) {
      const text = [el("span", { className: "fm-long", textContent: long }), el("span", { className: "fm-short", textContent: short })];
      let href = place(found, key), current = key === here;
      if (key === "admin") {
        const running = ADMIN.map(([k]) => place(found, k)).filter((p) => p && p !== "off");
        current = inAdmin;
        href = inAdmin ? place(found, here) : running[0] || (ADMIN.some(([k]) => found[k] === "off") ? "off" : null);
      }
      if (!current && href === "off") nav.append(el("span", { className: "fm-off", title: `${long} is unavailable on this server.` }, ...text, el("small", { textContent: "Unavailable" })));
      else if (current || href) {
        const a = el("a", { href, ariaLabel: long }, ...text);
        a.dataset.page = key;
        if (current) a.ariaCurrent = "page";
        nav.append(a);
      }
    }
    bar.replaceChildren(mark, nav);
    if (found.home) bar.append(el("a", { className: "fm-home", href: found.home.url, ariaLabel: found.home.name }, "‹", el("span", { textContent: found.home.name })));
    drawAdmin(found);
  }

  // The second row on the Admin pages: each of them, the current one marked.
  function drawAdmin(found) {
    const old = bar.nextElementSibling;
    if (old && old.classList.contains("fm-sub")) old.remove();
    if (!inAdmin) return;
    const items = [];
    for (const [key, long] of ADMIN) {
      const href = place(found, key);
      if (key !== here && href === "off") items.push(el("span", { className: "fm-off", title: `${long} is unavailable on this server.` }, long, el("small", { textContent: "Unavailable" })));
      else if (href) {
        const a = el("a", { href }, long);
        if (key === here) a.ariaCurrent = "page";
        items.push(a);
      }
    }
    if (items.length > 1) bar.after(el("nav", { className: "fm-sub", ariaLabel: "Admin pages" }, ...items));
  }

  // The Maps page and the print tool both keep the place in the address (#zoom/lat/lon):
  // going from one to the other opens the same place.
  bar.addEventListener("click", (e) => {
    const a = e.target.closest(".fm-nav a");
    const place = /^#[\d.]+\/-?[\d.]+\/-?[\d.]+/.test(location.hash);
    const mapPage = (key) => key === "maps" || key === "print";
    if (a && place && mapPage(here) && mapPage(a.dataset.page)) a.href = a.href.split("#")[0] + location.hash;
  });

  const get = async (url) => { try { return await fetch(url, { cache: "no-store" }); } catch { return null; } };
  const json = async (url) => { const r = await get(url); try { return r && r.ok ? await r.json() : null; } catch { return null; } };

  async function find() {
    const found = {};
    const printRoot = here === "print" ? own : "/mapprinter/";
    const [config, mapsRoot, guide, builder, phones] = await Promise.all([
      json(printRoot + "config.json"),
      here === "maps" ? own : (async () => {
        for (const root of ["/maps/", "/"]) if (Array.isArray(await json(root + "styles.json"))) return root;
        return null;
      })(),
      here === "guide" ? own : (async () => {
        const r = await get("/guide/fieldmap.css");
        return r && r.ok && (r.headers.get("Content-Type") || "").includes("css") ? "/guide/" : null;
      })(),
      here === "builder" ? own : (async () => {
        const r = await get("/mapadmin/style.css");
        if (r && r.ok && (r.headers.get("Content-Type") || "").includes("css")) return "/mapadmin/";
        return r && r.status === 503 ? "off" : null;
      })(),
      (async () => {
        if (here === "phones" || here === "access") return own;
        const r = await get("/phones/info");
        let info = null;
        try { info = r && r.ok ? await r.json() : null; } catch { /* not the service */ }
        if (info && info.service === "fieldmap-remote") return "/phones/";
        return r && r.status === 503 ? "off" : null;
      })(),
    ]);
    if (config && typeof config === "object") {
      found.print = printRoot;
      // Only a web address: never a script.
      try {
        const url = config.homeUrl && new URL(config.homeUrl, location.href);
        if (url && /^https?:$/.test(url.protocol)) found.home = { url: url.href, name: String(config.homeName || "Home").slice(0, 24) };
      } catch { /* not an address: no Home button */ }
    }
    if (mapsRoot) found.maps = mapsRoot;
    if (guide) found.guide = guide;
    if (builder) found.builder = builder;
    if (phones) found.phones = phones;
    // Remote access is a page of the same service, beside Devices.
    if (phones) found.access = phones === "off" ? "off" : phones + "remote.html";
    return found;
  }

  // What the last page found is shown at once, then checked again.
  let known = {};
  try { known = JSON.parse(sessionStorage.getItem(CACHE) || "{}"); } catch { /* start empty */ }
  draw(known);
  find().then((found) => {
    const text = JSON.stringify(found);
    if (text !== JSON.stringify(known)) draw(found);
    try { sessionStorage.setItem(CACHE, text); } catch { /* private window: found again on the next page */ }
  });
})();
