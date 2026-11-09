// Sheet layout in inches. Everything else derives from this.

export const PAPERS = {
  letter: { w: 8.5, h: 11, label: "Letter 8.5 × 11" },
  legal: { w: 8.5, h: 14, label: "Legal 8.5 × 14" },
  tabloid: { w: 11, h: 17, label: "Tabloid 11 × 17" },
  superb: { w: 13, h: 19, label: "Super B 13 × 19" },
  archc: { w: 18, h: 24, label: "Arch C 18 × 24" },
};

/** Atlas grid choices (rows × columns). */
export const ATLAS_GRIDS = [1, 2, 3, 4, 6, 8];
export const ATLAS_OVERLAP_IN = 1 / 2.54;   // 1 cm of paper overlap between neighbouring pages
export const BINDER_MARGIN_IN = 0.7;        // page edge to neatline on the punched side (holes sit at ~0.5 in)

export const SCALES = [12000, 24000, 25000, 50000, 100000];

const OUTER = 0.25;      // page edge to grid-label gutter
const GUTTER = 0.25;    // grid labels live here, outside the neatline
const GAP = 0.08;       // neatline to bottom block

/**
 * Compute the sheet geometry.
 * Returns inches: page {w,h}, map {x,y,w,h}, block {x,y,w,h}.
 */
export function layout(paper, orientation, scale, options = {}) {
  const p = PAPERS[paper];
  const page = orientation === "landscape" ? { w: p.h, h: p.w } : { w: p.w, h: p.h };

  const blockH = 0.85;

  const left = options.binder ? BINDER_MARGIN_IN : OUTER + GUTTER;
  const top = OUTER + GUTTER;
  const right = OUTER + GUTTER;
  const bottom = OUTER + blockH + GAP + GUTTER;

  const map = { x: left, y: top, w: page.w - left - right, h: page.h - top - bottom };
  const block = { x: options.binder ? BINDER_MARGIN_IN - GUTTER : OUTER, y: page.h - OUTER - blockH, w: page.w - OUTER - (options.binder ? BINDER_MARGIN_IN - GUTTER : OUTER), h: blockH };
  return { page, map, block, gutter: GUTTER, blockH };
}

/** Ground footprint in metres for a layout at a scale. */
export function footprintMeters(lay, scale) {
  return { w: lay.map.w * 0.0254 * scale, h: lay.map.h * 0.0254 * scale };
}
