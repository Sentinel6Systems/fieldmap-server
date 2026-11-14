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


def tower(ratio):
    """Tower or mast: a ring with a dot (extras layer)."""
    w = h = 11; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    d.ellipse([1 * k, 1 * k, 10 * k, 10 * k], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=int(1.1 * k))
    d.ellipse([4 * k, 4 * k, 7 * k, 7 * k], fill=(0, 0, 0, 255))
    return finish(im, w, h, ratio), w, h


def spring(ratio):
    """Spring: a blue ring with a short outflow tail."""
    w, h = 14, 9; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    blue = (29, 90, 170, 255)
    d.line([(7 * k, 4.5 * k), (13 * k, 4.5 * k)], fill=blue, width=int(1.2 * k))
    d.ellipse([1 * k, 1 * k, 8 * k, 8 * k], fill=(255, 255, 255, 255), outline=blue, width=int(1.3 * k))
    return finish(im, w, h, ratio), w, h


def survey(ratio):
    """Survey mark: a small triangle with a centre dot."""
    w, h = 11, 10; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    d.polygon([(5.5 * k, 0.8 * k), (10.2 * k, 9.2 * k), (0.8 * k, 9.2 * k)], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=int(1.0 * k))
    d.ellipse([4.6 * k, 5.4 * k, 6.4 * k, 7.2 * k], fill=(0, 0, 0, 255))
    return finish(im, w, h, ratio), w, h


def obstacle(ratio):
    """Obstacle (FAA): the aeronautical-chart inverted V with a dot at its foot."""
    w, h = 11, 12; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    for col, wd in (((255, 255, 255, 255), 2.6), ((20, 40, 90, 255), 1.2)):
        d.line([(1.2 * k, 10.5 * k), (5.5 * k, 1.2 * k), (9.8 * k, 10.5 * k)], fill=col, width=int(wd * k), joint="curve")
    d.ellipse([4.3 * k, 8.8 * k, 6.7 * k, 11.2 * k], fill=(20, 40, 90, 255))
    return finish(im, w, h, ratio), w, h


def floodway(ratio):
    """Floodway: blue diagonal rules (tiles seamlessly)."""
    w = h = 12; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
    blue = (40, 110, 190, 150)
    for off in (-12, 0, 12):
        d.line([((off) * k, 12 * k), ((off + 12) * k, 0)], fill=blue, width=max(1, int(0.8 * k)))
    return finish(im, w, h, ratio), w, h


def interstate(width):
    """Interstate shield: blue body, red crown band, white rim; the number is white text from the style."""
    def draw(ratio):
        w, h = width, 18; k = ratio * SS; im = canvas(w, h, ratio); d = ImageDraw.Draw(im)
        def shape(inset):
            x0, x1, top, mid, bot = inset, w - inset, 1.2 + inset, 10.5, h - 0.6 - inset
            cx = w / 2
            pts = [(x0, top + 1.2), (cx * 0.5 + x0 * 0.5, top), (cx, top + 1.0), (cx * 1.5 - x0 * 0.5, top), (x1, top + 1.2),
                   (x1, mid), (x1 - (x1 - cx) * 0.25, mid + 3.2), (cx, bot), (x0 + (cx - x0) * 0.25, mid + 3.2), (x0, mid)]
            return [(x * k, y * k) for x, y in pts]
        d.polygon(shape(0.4), fill=(255, 255, 255, 255))
        d.polygon(shape(1.4), fill=(0, 59, 135, 255))
        band = Image.new("L", im.size, 0)
        ImageDraw.Draw(band).rectangle([0, 0, w * k, 5.6 * k], fill=255)
        red = Image.new("RGBA", im.size, (200, 16, 46, 255))
        mask = Image.new("L", im.size, 0); ImageDraw.Draw(mask).polygon(shape(1.4), fill=255)
        im.paste(red, (0, 0), Image.composite(mask, Image.new("L", im.size, 0), band))
        d.line([(1.4 * k, 5.6 * k), ((w - 1.4) * k, 5.6 * k)], fill=(255, 255, 255, 255), width=int(0.8 * k))
        return finish(im, w, h, ratio), w, h
    return draw


EXTRA = {"pat_marsh": marsh, "pat_sand": sand, "pat_rock": rock, "pat_cemetery": cemetery,
         "sym_peak": peak, "sym_spot": spot, "sym_flow": flow,
         "sym_tower": tower, "sym_spring": spring, "sym_survey": survey,
         "sym_obstacle": obstacle, "pat_floodway": floodway,
         **{f"usgs-interstate_{n}": interstate(wd) for n, wd in zip(range(1, 7), (17, 17, 21, 25, 29, 33))}}

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
