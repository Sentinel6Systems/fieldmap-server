import { inverse } from "./geo.js";
import { toUTM, zoneFor } from "./utm.js";
import { declinationInfo } from "./declination.js";

/** Range and true/grid/magnetic bearings for an arrow. Grid zone follows the sheet centre. */
export function arrowInfo(a, sheetCenter) {
  const inv = inverse(a.a.lat, a.a.lng, a.b.lat, a.b.lng);
  const zone = zoneFor(sheetCenter.lat, sheetCenter.lng);
  const conv = toUTM(a.a.lat, a.a.lng, zone).convergence;
  const decl = declinationInfo(a.a.lat, a.a.lng, zone).declination;
  return {
    distance: inv.distance,
    trueBearing: inv.initialBearing,
    gridBearing: inv.initialBearing - conv,
    magBearing: inv.initialBearing - decl,
  };
}
