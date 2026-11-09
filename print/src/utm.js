// UTM forward/inverse on WGS84 (Karney/Krüger series, as popularised by
// Chris Veness). Accurate to well under a millimetre within a zone.
// All angles in degrees for the public API.

const a = 6378137;
const f = 1 / 298.257223563;
const k0 = 0.9996;
const e = Math.sqrt(f * (2 - f));
const n = f / (2 - f);
const n2 = n * n, n3 = n2 * n, n4 = n3 * n, n5 = n4 * n, n6 = n5 * n;
const A = (a / (1 + n)) * (1 + n2 / 4 + n4 / 64 + n6 / 256);

const alpha = [
  null,
  n / 2 - (2 / 3) * n2 + (5 / 16) * n3 + (41 / 180) * n4 - (127 / 288) * n5 + (7891 / 37800) * n6,
  (13 / 48) * n2 - (3 / 5) * n3 + (557 / 1440) * n4 + (281 / 630) * n5 - (1983433 / 1935360) * n6,
  (61 / 240) * n3 - (103 / 140) * n4 + (15061 / 26880) * n5 + (167603 / 181440) * n6,
  (49561 / 161280) * n4 - (179 / 168) * n5 + (6601661 / 7257600) * n6,
  (34729 / 80640) * n5 - (3418889 / 1995840) * n6,
  (212378941 / 319334400) * n6,
];

const beta = [
  null,
  n / 2 - (2 / 3) * n2 + (37 / 96) * n3 - (1 / 360) * n4 - (81 / 512) * n5 + (96199 / 604800) * n6,
  (1 / 48) * n2 + (1 / 15) * n3 - (437 / 1440) * n4 + (46 / 105) * n5 - (1118711 / 3870720) * n6,
  (17 / 480) * n3 - (37 / 840) * n4 - (209 / 4480) * n5 + (5569 / 90720) * n6,
  (4397 / 161280) * n4 - (11 / 504) * n5 - (830251 / 7257600) * n6,
  (4583 / 161280) * n5 - (108847 / 3991680) * n6,
  (20648693 / 638668800) * n6,
];

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

/** UTM zone number for a lon/lat, with the Norway and Svalbard exceptions. */
export function zoneFor(lat, lon) {
  let zone = Math.floor((lon + 180) / 6) + 1;
  if (zone === 61) zone = 60;
  if (lat >= 56 && lat < 64 && lon >= 3 && lon < 12) zone = 32;
  if (lat >= 72 && lat < 84) {
    if (lon >= 0 && lon < 9) zone = 31;
    else if (lon >= 9 && lon < 21) zone = 33;
    else if (lon >= 21 && lon < 33) zone = 35;
    else if (lon >= 33 && lon < 42) zone = 37;
  }
  return zone;
}

/** Latitude band letter (C..X, no I or O). */
export function bandFor(lat) {
  const bands = "CDEFGHJKLMNPQRSTUVWX";
  const i = Math.floor((lat + 80) / 8);
  return bands[Math.min(Math.max(i, 0), 19)];
}

/**
 * Forward projection. If `zone` is given, projects into that zone even when
 * the point lies outside it (used to extend a grid across a zone boundary).
 * Returns { zone, hemisphere, easting, northing, convergence (deg, +east), scale }.
 */
export function toUTM(lat, lon, zone) {
  zone = zone || zoneFor(lat, lon);
  const lon0 = (zone - 1) * 6 - 180 + 3;
  const phi = lat * D2R;
  const lambda = (lon - lon0) * D2R;

  const cosL = Math.cos(lambda), sinL = Math.sin(lambda), tanL = Math.tan(lambda);
  const tau = Math.tan(phi);
  const sigma = Math.sinh(e * Math.atanh((e * tau) / Math.sqrt(1 + tau * tau)));
  const tauP = tau * Math.sqrt(1 + sigma * sigma) - sigma * Math.sqrt(1 + tau * tau);

  const xiP = Math.atan2(tauP, cosL);
  const etaP = Math.asinh(sinL / Math.sqrt(tauP * tauP + cosL * cosL));

  let xi = xiP, eta = etaP, p = 1, q = 0;
  for (let j = 1; j <= 6; j++) {
    xi += alpha[j] * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
    eta += alpha[j] * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
    p += 2 * j * alpha[j] * Math.cos(2 * j * xiP) * Math.cosh(2 * j * etaP);
    q += 2 * j * alpha[j] * Math.sin(2 * j * xiP) * Math.sinh(2 * j * etaP);
  }

  let x = k0 * A * eta;
  let y = k0 * A * xi;

  const gammaP = Math.atan((tauP / Math.sqrt(1 + tauP * tauP)) * tanL);
  const gammaPP = Math.atan2(q, p);
  const convergence = (gammaP + gammaPP) * R2D;

  const sinPhi = Math.sin(phi);
  const kP = Math.sqrt(1 - e * e * sinPhi * sinPhi) * Math.sqrt(1 + tau * tau) / Math.sqrt(tauP * tauP + cosL * cosL);
  const kPP = (A / a) * Math.sqrt(p * p + q * q);
  const scale = k0 * kP * kPP;

  x += 500000;
  const hemisphere = lat >= 0 ? "N" : "S";
  if (lat < 0) y += 10000000;

  return { zone, hemisphere, easting: x, northing: y, convergence, scale };
}

/** Inverse projection. Returns { lat, lon } in degrees. */
export function fromUTM(zone, hemisphere, easting, northing) {
  const lon0 = (zone - 1) * 6 - 180 + 3;
  let x = easting - 500000;
  let y = hemisphere === "S" ? northing - 10000000 : northing;

  const xi = y / (k0 * A);
  const eta = x / (k0 * A);

  let xiP = xi, etaP = eta;
  for (let j = 1; j <= 6; j++) {
    xiP -= beta[j] * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    etaP -= beta[j] * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }

  const sinhEta = Math.sinh(etaP);
  const sinXi = Math.sin(xiP), cosXi = Math.cos(xiP);
  const tauP = sinXi / Math.sqrt(sinhEta * sinhEta + cosXi * cosXi);

  let tau = tauP, d;
  do {
    const sigma = Math.sinh(e * Math.atanh((e * tau) / Math.sqrt(1 + tau * tau)));
    const tauI = tau * Math.sqrt(1 + sigma * sigma) - sigma * Math.sqrt(1 + tau * tau);
    d = ((tauP - tauI) / Math.sqrt(1 + tauI * tauI)) *
      ((1 + (1 - e * e) * tau * tau) / ((1 - e * e) * Math.sqrt(1 + tau * tau)));
    tau += d;
  } while (Math.abs(d) > 1e-12);

  const phi = Math.atan(tau);
  const lambda = Math.atan2(sinhEta, cosXi);
  return { lat: phi * R2D, lon: lambda * R2D + lon0 };
}

/** 100 km square letters for an easting/northing in a zone (MGRS, AA scheme). */
export function squareId(zone, easting, northing) {
  const colLetters = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"][(zone - 1) % 3];
  const rowLetters = zone % 2 === 1 ? "ABCDEFGHJKLMNPQRSTUV" : "FGHJKLMNPQRSTUVABCDE";
  const col = Math.floor(easting / 100000) - 1;
  const row = Math.floor(northing / 100000) % 20;
  return colLetters[col] + rowLetters[row];
}

/** Full MGRS reference to `digits` (1..5) for a lat/lon. */
export function toMGRS(lat, lon, digits = 5) {
  const u = toUTM(lat, lon);
  const e100 = u.easting % 100000, n100 = u.northing % 100000;
  const div = Math.pow(10, 5 - digits);
  const es = String(Math.floor(e100 / div)).padStart(digits, "0");
  const ns = String(Math.floor(n100 / div)).padStart(digits, "0");
  return `${u.zone}${bandFor(lat)} ${squareId(u.zone, u.easting, u.northing)} ${es} ${ns}`;
}
