// The magnetic model notice at the top of the panel: shown from SOON_DAYS before the World
// Magnetic Model in use runs out, with an "Update Magnetic Model" button when the server runs
// tools/wmm-update.py serve (at wmm-api/). Without the service, or without Internet on the
// server, printing carries on with the model it has.
import { loadServedModel, currentModel } from "./declination.js";

const SOON_DAYS = 180;
const fmtDate = (d) => d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });

export async function initWmmNotice(root) {
  const text = root.querySelector(".wmm-text"), btn = root.querySelector("button"), result = root.querySelector(".wmm-result");
  await loadServedModel();
  let api = false;
  try { api = (await fetch("wmm-api/status", { cache: "no-store" })).ok; } catch { api = false; }

  const show = () => {
    const m = currentModel(), days = (m.end - Date.now()) / 864e5;
    root.hidden = days > SOON_DAYS;
    if (root.hidden) return;
    text.textContent = days < 0
      ? `The magnetic model ${m.name} expired on ${fmtDate(m.end)}. The declination still comes from this model and becomes less accurate each year.`
      : `The magnetic model ${m.name} expires on ${fmtDate(m.end)}.`;
    if (!api) text.textContent += " Run tools/wmm-update.py update on the server to update it.";
    btn.hidden = !api;
  };

  btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "Updating…"; result.textContent = "";
    try {
      const res = await fetch("wmm-api/update", { method: "POST", headers: { "X-Fieldmap": "wmm-update" } });
      const r = await res.json().catch(() => ({}));
      result.textContent = r.message || `The server returned HTTP status ${res.status}.`;
      if (r.updated) await loadServedModel();
    } catch {
      result.textContent = "The server did not respond. The current model stays in use.";
    }
    btn.disabled = false; btn.textContent = "Update Magnetic Model";
    show();
    if (root.hidden && result.textContent) { root.hidden = false; text.textContent = ""; btn.hidden = true; }
  });
  show();
}
