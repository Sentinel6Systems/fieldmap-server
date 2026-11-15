import geomagnetism from "geomagnetism";
import Model from "geomagnetism/lib/model.js";
import { toUTM } from "./utm.js";

// A newer World Magnetic Model than the bundled one, downloaded on the server by
// tools/wmm-update.py and served next to the page as wmm/wmm.json. Null until loaded,
// or when the file is missing or no newer than the bundle.
let served = null;

const BUNDLED = geomagnetism.model(new Date(), { allowOutOfBoundsModel: true });

/** Load wmm/wmm.json when the server has one. Never throws: without it the bundled model is used. */
export async function loadServedModel() {
  try {
    const res = await fetch("wmm/wmm.json", { cache: "no-cache" });
    if (!res.ok) return null;
    const m = new Model(await res.json());
    if (!(m.n_max >= 1) || isNaN(m.end_date)) return null;
    served = m.end_date > BUNDLED.end_date ? m : null;
  } catch { served = null; }
  return served;
}

/** The model in use now: name, first and last valid day, and whether it came from the server. */
export function currentModel() {
  const m = served || BUNDLED;
  return { name: (m.name || "WMM").trim(), start: m.start_date, end: m.end_date, served: !!served };
}

// The newest model that covers `date`, or the newest of all when none does (its rate of change
// is carried on past its end; less accurate the further past it the date is).
function modelFor(date) {
  if (served && date >= served.start_date) return served.getTimedModel(date, true);
  try { return geomagnetism.model(date); }
  catch { return geomagnetism.model(date, { allowOutOfBoundsModel: true }); }
}

/**
 * Magnetic declination and grid convergence for a point.
 * Declination and convergence are degrees, positive east.
 * G-M angle = declination - convergence (magnetic relative to grid north).
 */
export function declinationInfo(lat, lon, zone, date = new Date()) {
  const model = modelFor(date);
  const decl = model.point([lat, lon]).decl;
  const next = new Date(date.getTime() + 365.25 * 86400 * 1000);
  const declNext = modelFor(next).point([lat, lon]).decl;
  const end = model.end_date;
  const convergence = toUTM(lat, lon, zone).convergence;
  return {
    declination: decl,
    annualChange: declNext - decl,
    convergence,
    gmAngle: decl - convergence,
    model: (model.name || "WMM").trim(),
    validThrough: end && !isNaN(end) ? end.getUTCFullYear() : "",
    expired: !!end && date > end,
    asOf: date,
  };
}

export function fmtAngle(deg, digits = 1) {
  const v = Math.abs(deg).toFixed(digits);
  if (Math.abs(deg) < 0.5 * Math.pow(10, -digits)) return `${v}°`;
  return `${v}°${deg < 0 ? "W" : "E"}`;
}
