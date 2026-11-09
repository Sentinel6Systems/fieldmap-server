export const PALETTE = [
  ["#d7263d", "Red"], ["#f46036", "Orange"], ["#e6b800", "Yellow"], ["#2a9d3f", "Green"],
  ["#1b998b", "Teal"], ["#1f5fa8", "Blue"], ["#6f2dbd", "Purple"], ["#e83e8c", "Pink"],
  ["#8b5a2b", "Brown"], ["#111111", "Black"], ["#6c757d", "Grey"], ["#ffffff", "White"],
];

export function colorName(hex) {
  const f = PALETTE.find((p) => p[0].toLowerCase() === String(hex).toLowerCase());
  return f ? f[1] : hex;
}

/** Build a swatch row. onPick(hex) is called on click. Returns the element. */
export function swatchRow(current, onPick) {
  const row = document.createElement("div");
  row.className = "swatches";
  for (const [hex, name] of PALETTE) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch" + (hex === current ? " selected" : "");
    b.style.background = hex;
    b.title = name;
    b.setAttribute("aria-label", name);
    b.onclick = (e) => { e.stopPropagation(); onPick(hex); };
    row.appendChild(b);
  }
  return row;
}
