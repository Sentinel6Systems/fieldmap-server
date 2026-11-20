// What the Fieldmap devices share through the server (tools/share-server.py, at /mapshare/ on
// this host): their locations, the shared markers, arrows and routes, and the messages. The
// map reads them the way the app does: it asks every few seconds (the server's "interval"),
// only while the page is in view; a page out of view looks for new messages once a minute.
//
// The browser takes part like a device without a location: it has an id of its own (made once,
// kept in this browser), reads the messages to it and to the group, and tells the server which
// ones it got, so that their senders see them delivered. Nothing here needs a sign-in: the
// sharing service answers callers on the server's own network, while Location Sharing is
// enabled on the Devices page.

export const BASE = "/mapshare/";
const KEYS = { uid: "fieldmap.map.uid", messages: "fieldmap.map.messages" };
const KEEP_MESSAGES = 500;          // messages kept in this browser
const HIDDEN_EVERY = 60;            // seconds between message checks while the page is out of view
const INFO_EVERY = 60;              // seconds between checks of whether sharing is enabled

/* ---------- pure parts (test/live.test.mjs) ---------- */

/** The shared markers after one answer of GET /state: all of them when it is full, else the
 *  ones before with the changes and removals. Positions are always the whole list. */
export function mergeState(before, answer, now = Date.now()) {
  const markers = answer.full || !before ? new Map() : new Map(before.markers);
  for (const id of answer.removed || []) markers.delete(String(id));
  for (const f of answer.markers || []) if (f && f.id != null) markers.set(String(f.id), f);
  return { rev: answer.rev, markers, positions: Array.isArray(answer.positions) ? answer.positions : [], at: now };
}

/** An id for this browser as the server takes one: 8 to 64 letters, digits or hyphens. */
export function newUid(random = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  return "web-" + [...random(8)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * The messages kept in this browser after one answer of GET /messages: new ones added (each id
 * once), sent ones marked Delivered. Returns { list, seq, received } where received are the
 * ids of the new messages to this browser and to the group, for POST /messages/delivered.
 */
export function mergeMessages(store, answer, me) {
  const list = [...(store.list || [])];
  const known = new Set(list.map((m) => m.id));
  const received = [];
  for (const m of answer.messages || []) {
    if (!m || typeof m.id !== "string" || m.from === me || (m.to !== me && m.to !== "*")) continue;
    received.push(m.id);
    if (known.has(m.id)) continue;
    known.add(m.id);
    list.push({ id: m.id, from: m.from, name: m.name, to: m.to, text: m.text, time: m.time, seq: m.seq, mine: false, read: false });
  }
  const delivered = new Set(answer.delivered || []);
  const group = answer.groupDelivered || {};
  for (const m of list) {
    if (!m.mine) continue;
    if (delivered.has(m.id)) m.status = "delivered";
    if (group[m.id]) m.deliveredTo = [...new Set([...(m.deliveredTo || []), ...group[m.id]])];
  }
  list.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return { list: list.slice(-KEEP_MESSAGES), seq: typeof answer.seq === "number" ? answer.seq : store.seq, received };
}

/** Who a message is with: "*" for the group, else the other person's id. */
export const partnerOf = (m, me) => (m.to === "*" ? "*" : m.from === me ? m.to : m.from);

/** The conversations, newest first: the group always first, then each person. */
export function conversations(list, me) {
  const by = new Map([["*", { partner: "*", name: "Group (Private)", last: null, unread: 0 }]]);
  for (const m of list) {
    const p = partnerOf(m, me);
    const c = by.get(p) || { partner: p, name: m.mine ? p : m.name, last: null, unread: 0 };
    if (!m.mine && p !== "*") c.name = m.name;
    c.last = m;
    if (!m.mine && !m.read) c.unread++;
    by.set(p, c);
  }
  const [group, ...people] = [...by.values()];
  people.sort((a, b) => (a.last.time < b.last.time ? 1 : -1));
  return [group, ...people];
}

/** How a location looks as it ages, as in the app (share/Share.kt, Ageing): live, then gray
 *  and fading from 0.85 to 0.4, then gone. */
export function ageLook(age, interval, stale) {
  const fresh = Math.max(1, Math.min(Math.max(6 * interval, 30), Math.floor(stale / 2)));
  if (age >= stale) return { fresh: false, opacity: 0 };
  if (age < fresh) return { fresh: true, opacity: 1 };
  return { fresh: false, opacity: 0.85 - (0.45 * (age - fresh)) / Math.max(1, stale - fresh) };
}

/** "now", "40 s ago", "12 min ago", "3 h ago": as in the app. */
export function ageText(age) {
  age = Math.max(0, Math.floor(age));
  return age < 5 ? "now" : age < 90 ? `${age} s ago` : age < 5400 ? `${Math.floor(age / 60)} min ago` : `${Math.floor(age / 3600)} h ago`;
}

/* ---------- talking to the server ---------- */

function stored(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function keep(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private window: kept until the page closes */ }
}

/** This browser's id: made the first time, then kept. */
export function browserUid() {
  let uid = stored(KEYS.uid, null);
  if (typeof uid !== "string" || !/^[A-Za-z0-9-]{8,64}$/.test(uid)) { uid = newUid(); keep(KEYS.uid, uid); }
  return uid;
}

/**
 * Starts reading the server. onChange(live) is called whenever something changed. live:
 *   status    "absent" (no sharing service), "disabled", "refused", "offline", "on"
 *   text      what to tell the person when the status is not "on"
 *   info      the server's /info answer
 *   state     { rev, markers: Map, positions, at } or null
 *   messages  { list, seq }
 *   uid       this browser's id
 * Returns the same object, with markRead(partner) and refresh().
 */
export function startLive(onChange) {
  const live = {
    status: "starting", text: "", info: null, state: null, uid: browserUid(),
    messages: stored(KEYS.messages, { list: [], seq: null }),
  };
  if (!Array.isArray(live.messages.list)) live.messages = { list: [], seq: null };
  let timer = null, infoAt = 0, messagesAt = 0, busy = false;

  const visible = () => document.visibilityState !== "hidden";
  const get = async (path) => {
    try {
      const r = await fetch(BASE + path, { cache: "no-store" });
      let body = null;
      try { body = await r.json(); } catch { /* no JSON */ }
      return { code: r.status, body };
    } catch { return { code: 0, body: null }; }
  };
  const post = async (path, body) => {
    try {
      const r = await fetch(BASE + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      return r.status;
    } catch { return 0; }
  };
  const set = (status, text = "") => {
    if (live.status === status && live.text === text) return false;
    live.status = status; live.text = text;
    return true;
  };

  /** One round: is sharing on, then the state, then the messages. Returns seconds to the next. */
  async function round() {
    const now = Date.now() / 1000;
    let changed = false;
    if (live.status !== "on" || now - infoAt > INFO_EVERY) {
      const r = await get("info");
      infoAt = now;
      if (r.code === 404) { changed = set("absent"); onChange(live); return 300; }
      if (r.code !== 200 || !r.body) {
        changed = set(r.code === 403 ? "refused" : "offline",
          r.code === 403 ? (r.body && r.body.error) || "Location sharing is available to local and paired devices only." : "The sharing service is not responding.");
        if (changed) onChange(live);
        return 30;
      }
      live.info = r.body;
      if (!r.body.enabled) {
        live.state = null;
        if (set("disabled", "Location sharing is disabled on the server. Enable it in Admin > Devices.")) onChange(live);
        return INFO_EVERY;
      }
      changed = set("on") || changed;
    }
    const interval = Math.max(2, Math.min(600, +live.info.interval || 5));
    if (!visible()) {
      if (now - messagesAt >= HIDDEN_EVERY) changed = (await messages()) || changed;
      if (changed) onChange(live);
      return HIDDEN_EVERY;
    }
    const since = live.state ? `?since=${live.state.rev}` : "";
    const r = await get("state" + since);
    if (r.code === 200 && r.body) { live.state = mergeState(live.state, r.body); changed = true; }
    else if (r.code === 503) { infoAt = 0; live.state = null; changed = true; }
    else if (r.code === 429) { if (changed) onChange(live); return Math.max(10, 2 * interval); }
    else changed = set("offline", "The sharing service is not responding.") || changed;
    if (live.info.messages) changed = (await messages()) || changed;
    if (changed) onChange(live);
    return interval;
  }

  /** New messages; the server is told which ones arrived. True when something changed. */
  async function messages() {
    messagesAt = Date.now() / 1000;
    const seq = live.messages.seq;
    const r = await get(`messages?for=${live.uid}${seq != null ? `&since=${seq}` : ""}`);
    if (r.code !== 200 || !r.body) return false;
    const before = live.messages.list.length;
    const merged = mergeMessages(live.messages, r.body, live.uid);
    for (let i = 0; i < merged.received.length; i += 50) {
      await post("messages/delivered", { ids: merged.received.slice(i, i + 50), by: live.uid });
    }
    live.messages = { list: merged.list, seq: merged.seq };
    keep(KEYS.messages, live.messages);
    return merged.list.length !== before || (r.body.delivered || []).length > 0 || Object.keys(r.body.groupDelivered || {}).length > 0;
  }

  async function loop() {
    clearTimeout(timer);
    if (busy) return;
    busy = true;
    let wait = 5;
    try { wait = await round(); } catch (e) { console.warn("live:", e); }
    busy = false;
    timer = setTimeout(loop, wait * 1000);
  }

  live.markRead = (partner) => {
    let changed = false;
    for (const m of live.messages.list) if (!m.mine && !m.read && partnerOf(m, live.uid) === partner) { m.read = true; changed = true; }
    if (changed) { keep(KEYS.messages, live.messages); onChange(live); }
  };
  live.refresh = loop;
  document.addEventListener("visibilitychange", () => { if (visible()) loop(); });
  loop();
  return live;
}
