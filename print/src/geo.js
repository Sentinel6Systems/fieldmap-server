// Web Mercator helpers (MapLibre uses a 512 px tile, so world size = 512 * 2^z CSS px).
export const EARTH_CIRC = 40075016.686;
const D2R = Math.PI / 180;

export function worldSize(zoom) {
  return 512 * Math.pow(2, zoom);
}

/** lng/lat -> world pixel coords at zoom. */
export function project(lng, lat, zoom) {
  const ws = worldSize(zoom);
  const x = ((lng + 180) / 360) * ws;
  const s = Math.sin(lat * D2R);
  const y = (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * ws;
  return { x, y };
}

/** world pixel coords at zoom -> lng/lat. */
export function unproject(x, y, zoom) {
  const ws = worldSize(zoom);
  const lng = (x / ws) * 360 - 180;
  const n = Math.PI - (2 * Math.PI * y) / ws;
  const lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  return { lng, lat };
}

/**
 * Zoom (in MapLibre CSS-pixel terms) that gives exactly `scale` (e.g. 24000)
 * when the map canvas is printed at `dpi` with the given device pixelRatio.
 */
export function zoomForScale(scale, lat, dpi, pixelRatio) {
  const metersPerCssPx = (scale * 0.0254 * pixelRatio) / dpi;
  const metersPerPxZ0 = (EARTH_CIRC / 512) * Math.cos(lat * D2R);
  return Math.log2(metersPerPxZ0 / metersPerCssPx);
}

/** Bounds [[w,s],[e,n]] of a wCss x hCss viewport centred on lng/lat at zoom. */
export function viewportBounds(lng, lat, zoom, wCss, hCss) {
  const c = project(lng, lat, zoom);
  const sw = unproject(c.x - wCss / 2, c.y + hCss / 2, zoom);
  const ne = unproject(c.x + wCss / 2, c.y - hCss / 2, zoom);
  return [[sw.lng, sw.lat], [ne.lng, ne.lat]];
}

/* ---------- geodesics (WGS84) ---------- */

const _a = 6378137, _f = 1 / 298.257223563, _b = _a * (1 - _f);

/**
 * Vincenty inverse: distance (m) and initial/final true bearings (deg) from p1 to p2.
 * Falls back to haversine for near-antipodal points.
 */
export function inverse(lat1, lon1, lat2, lon2) {
  const φ1 = lat1 * D2R, φ2 = lat2 * D2R, L = (lon2 - lon1) * D2R;
  const tanU1 = (1 - _f) * Math.tan(φ1), cosU1 = 1 / Math.sqrt(1 + tanU1 * tanU1), sinU1 = tanU1 * cosU1;
  const tanU2 = (1 - _f) * Math.tan(φ2), cosU2 = 1 / Math.sqrt(1 + tanU2 * tanU2), sinU2 = tanU2 * cosU2;
  let λ = L, λp, iter = 0, sinλ, cosλ, sinσ, cosσ, σ, sinα, cos2α, cos2σm, C;
  do {
    sinλ = Math.sin(λ); cosλ = Math.cos(λ);
    const sinSqσ = (cosU2 * sinλ) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosλ) ** 2;
    sinσ = Math.sqrt(sinSqσ);
    if (sinσ === 0) return { distance: 0, initialBearing: 0, finalBearing: 0 };
    cosσ = sinU1 * sinU2 + cosU1 * cosU2 * cosλ;
    σ = Math.atan2(sinσ, cosσ);
    sinα = (cosU1 * cosU2 * sinλ) / sinσ;
    cos2α = 1 - sinα * sinα;
    cos2σm = cos2α !== 0 ? cosσ - (2 * sinU1 * sinU2) / cos2α : 0;
    C = (_f / 16) * cos2α * (4 + _f * (4 - 3 * cos2α));
    λp = λ;
    λ = L + (1 - C) * _f * sinα * (σ + C * sinσ * (cos2σm + C * cosσ * (-1 + 2 * cos2σm * cos2σm)));
  } while (Math.abs(λ - λp) > 1e-12 && ++iter < 200);
  if (iter >= 200) return haversine(lat1, lon1, lat2, lon2);
  const u2 = (cos2α * (_a * _a - _b * _b)) / (_b * _b);
  const A = 1 + (u2 / 16384) * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)));
  const B = (u2 / 1024) * (256 + u2 * (-128 + u2 * (74 - 47 * u2)));
  const Δσ = B * sinσ * (cos2σm + (B / 4) * (cosσ * (-1 + 2 * cos2σm * cos2σm) -
    (B / 6) * cos2σm * (-3 + 4 * sinσ * sinσ) * (-3 + 4 * cos2σm * cos2σm)));
  const s = _b * A * (σ - Δσ);
  const α1 = Math.atan2(cosU2 * sinλ, cosU1 * sinU2 - sinU1 * cosU2 * cosλ);
  const α2 = Math.atan2(cosU1 * sinλ, -sinU1 * cosU2 + cosU1 * sinU2 * cosλ);
  return { distance: s, initialBearing: norm360(α1 / D2R), finalBearing: norm360(α2 / D2R) };
}

function haversine(lat1, lon1, lat2, lon2) {
  const R = 6371008.8, φ1 = lat1 * D2R, φ2 = lat2 * D2R, Δφ = φ2 - φ1, Δλ = (lon2 - lon1) * D2R;
  const h = Math.sin(Δφ / 2) ** 2 + Math.cos(φ1) * Math.cos(φ2) * Math.sin(Δλ / 2) ** 2;
  const d = 2 * R * Math.asin(Math.sqrt(h));
  const y = Math.sin(Δλ) * Math.cos(φ2), x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  const b = norm360(Math.atan2(y, x) / D2R);
  return { distance: d, initialBearing: b, finalBearing: b };
}

/** Spherical destination point: start, bearing (deg), distance (m). Good enough for arrowheads. */
export function destination(lat, lon, bearing, dist) {
  const R = 6371008.8, δ = dist / R, θ = bearing * D2R, φ1 = lat * D2R, λ1 = lon * D2R;
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ) + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ));
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(δ) * Math.cos(φ1), Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2));
  return { lat: φ2 / D2R, lng: (((λ2 / D2R) + 540) % 360) - 180 };
}

export function norm360(d) { return ((d % 360) + 360) % 360; }

export function fmtDistance(m) {
  const mi = m / 1609.344;
  const metric = m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`;
  const imp = mi < 0.2 ? `${Math.round(m * 3.28084)} ft` : `${mi.toFixed(2)} mi`;
  return `${metric} (${imp})`;
}

export function fmtBearing(d) { return String(Math.round(norm360(d)) % 360).padStart(3, "0") + "°"; }
