#!/usr/bin/env python3
"""Build sprites/usgs*: the topo sprite sheet plus the area patterns and point
symbols used by styles/topo-usgs.json. Needs Pillow. Run from the repository root:

    python3 tools/make-usgs-sprite.py
"""
import json
from PIL import Image, ImageDraw

SS = 4  # supersampling for smooth edges


def canvas(w, h, ratio):
    return Image.new("RGBA", (w * ratio * SS, h * ratio * SS), (0, 0, 0, 0))


def finish(img, w, h, ratio):
    return img.resize((w * ratio, h * ratio), Image.LANCZOS)


def marsh(ratio):
    """Blue marsh tufts on broken water lines (tiles seamlessly)."""
    w = h = 32; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    blue = (44, 111, 187, 255)
    for (x, y) in ((4, 9), (20, 25)):
        d.line([(x * k, y * k), ((x + 9) * k, y * k)], fill=blue, width=k)
        for dx, top in ((2.5, 5), (4.5, 6.5), (6.5, 5)):
            d.line([((x + dx) * k, (y - 1) * k), ((x + dx + (dx - 4.5) * 0.5) * k, (y - top) * k)], fill=blue, width=max(1, int(k * 0.8)))
    for (x, y) in ((19, 9), (3, 25)):
        d.line([(x * k, y * k), ((x + 6) * k, y * k)], fill=blue, width=k)
    return finish(im, w, h, ratio), w, h


def sand(ratio):
    w = h = 24; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    col = (150, 110, 60, 255)
    for (x, y) in ((3, 4), (11, 2), (19, 6), (7, 11), (15, 13), (22, 16), (3, 18), (11, 21), (18, 22)):
        r = 0.55 * k
        d.ellipse([x * k - r, y * k - r, x * k + r, y * k + r], fill=col)
    return finish(im, w, h, ratio), w, h


def rock(ratio):
    w = h = 24; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    col = (95, 85, 78, 255)
    for (x, y) in ((3, 6), (15, 4), (9, 15), (19, 18)):
        d.line([(x * k, y * k), ((x + 2) * k, (y - 3) * k), ((x + 4) * k, y * k)], fill=col, width=max(1, int(k * 0.8)), joint="curve")
    return finish(im, w, h, ratio), w, h


def cemetery(ratio):
    w = h = 20; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    col = (40, 40, 40, 255)
    for (x, y) in ((5, 5), (15, 15)):
        d.line([(x * k, (y - 3) * k), (x * k, (y + 3) * k)], fill=col, width=max(1, int(k * 0.8)))
        d.line([((x - 2) * k, (y - 1) * k), ((x + 2) * k, (y - 1) * k)], fill=col, width=max(1, int(k * 0.8)))
    return finish(im, w, h, ratio), w, h


def peak(ratio):
    w, h = 11, 10; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    d.polygon([(5.5 * k, 0.5 * k), (10.5 * k, 9.5 * k), (0.5 * k, 9.5 * k)], fill=(255, 255, 255, 255))
    d.polygon([(5.5 * k, 2 * k), (9 * k, 8.7 * k), (2 * k, 8.7 * k)], fill=(107, 74, 43, 255))
    return finish(im, w, h, ratio), w, h


def spot(ratio):
    w = h = 7; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    for col, wd in (((255, 255, 255, 255), 2.4), ((74, 47, 23, 255), 1.0)):
        d.line([(1.2 * k, 1.2 * k), (5.8 * k, 5.8 * k)], fill=col, width=int(wd * k))
        d.line([(1.2 * k, 5.8 * k), (5.8 * k, 1.2 * k)], fill=col, width=int(wd * k))
    return finish(im, w, h, ratio), w, h


def flow(ratio):
    """Arrowhead pointing right (line direction = downstream)."""
    w, h = 10, 8; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    d.polygon([(0.5 * k, 0.3 * k), (9.7 * k, 4 * k), (0.5 * k, 7.7 * k), (3 * k, 4 * k)], fill=(255, 255, 255, 255))
    d.polygon([(2 * k, 1.6 * k), (8 * k, 4 * k), (2 * k, 6.4 * k), (3.8 * k, 4 * k)], fill=(29, 68, 120, 255))
    return finish(im, w, h, ratio), w, h


EXTRA = {"pat_marsh": marsh, "pat_sand": sand, "pat_rock": rock, "pat_cemetery": cemetery,
         "sym_peak": peak, "sym_spot": spot, "sym_flow": flow}

for suffix, ratio in (("", 1), ("@2x", 2)):
    base = Image.open(f"sprites/topo{suffix}.png").convert("RGBA")
    index = json.load(open(f"sprites/topo{suffix}.json"))
    made = [(name, *fn(ratio)) for name, fn in EXTRA.items()]
    pad = ratio
    strip_h = max(im.height for _, im, _, _ in made) + 2 * pad
    sheet = Image.new("RGBA", (max(base.width, sum(im.width + pad for _, im, _, _ in made) + pad), base.height + strip_h), (0, 0, 0, 0))
    sheet.paste(base, (0, 0))
    x = pad
    for name, im, w, h in made:
        sheet.paste(im, (x, base.height + pad))
        index[name] = {"x": x, "y": base.height + pad, "width": im.width, "height": im.height, "pixelRatio": ratio}
        x += im.width + pad
    sheet.save(f"sprites/usgs{suffix}.png", optimize=True)
    json.dump(index, open(f"sprites/usgs{suffix}.json", "w"), indent=1)
    print(f"sprites/usgs{suffix}: {sheet.width}x{sheet.height}, {len(index)} images")
