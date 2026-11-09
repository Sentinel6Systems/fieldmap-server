import geomagnetism from "geomagnetism";
import { toUTM } from "./utm.js";

/**
 * Magnetic declination and grid convergence for a point.
 * Declination and convergence are degrees, positive east.
 * G-M angle = declination - convergence (magnetic relative to grid north).
 */
export function declinationInfo(lat, lon, zone, date = new Date()) {
  let model, decl, declNext, modelName = "WMM", valid = "";
  try {
    model = geomagnetism.model(date);
    decl = model.point([lat, lon]).decl;
    const next = new Date(date.getTime() + 365.25 * 86400 * 1000);
    declNext = geomagnetism.model(next, { allowOutOfBoundsModel: true }).point([lat, lon]).decl;
    modelName = (model.name || "WMM").trim();
    valid = model.end_date ? model.end_date.getUTCFullYear() : "";
  } catch (e) {
    // Outside every bundled model's window: use the latest model anyway.
    model = geomagnetism.model(date, { allowOutOfBoundsModel: true });
    decl = model.point([lat, lon]).decl;
    declNext = decl;
    modelName = (model.name || "WMM").trim();
    valid = model.end_date ? model.end_date.getUTCFullYear() : "";
  }
  const annualChange = declNext - decl;
  const convergence = toUTM(lat, lon, zone).convergence;
  return {
    declination: decl,
    annualChange,
    convergence,
    gmAngle: decl - convergence,
    model: modelName,
    validThrough: valid,
    asOf: date,
  };
}

export function fmtAngle(deg, digits = 1) {
  const v = Math.abs(deg).toFixed(digits);
  if (Math.abs(deg) < 0.5 * Math.pow(10, -digits)) return `${v}°`;
  return `${v}°${deg < 0 ? "W" : "E"}`;
}
