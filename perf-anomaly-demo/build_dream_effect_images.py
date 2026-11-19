#!/usr/bin/env python3
"""Create clean, editable-looking DREAM presentation graphics."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


OUT = Path("/root/new")
W, H = 1600, 900
NAVY = "#0F1D37"
NAVY2 = "#172B4D"
WHITE = "#FFFFFF"
MUTED = "#9FB2CD"
BLUE = "#2770DC"
CYAN = "#16A2B5"
PURPLE = "#7750BE"
GREEN = "#16A36C"
ORANGE = "#E77B1B"
PALE = "#DCE8F7"
GRID = "#314A6B"


def font(size, bold=False):
    path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    return ImageFont.truetype(path, size)


def txt(draw, xy, value, size, color=WHITE, bold=False, anchor=None):
    draw.text(xy, value, font=font(size, bold), fill=color, anchor=anchor)


def rounded(draw, box, radius=28, fill=NAVY2, outline=None, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def base(title_color=PURPLE):
    image = Image.new("RGB", (W, H), NAVY)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 14), fill=title_color)
    return image, draw


def coverage():
    image, draw = base(PURPLE)
    txt(draw, (80, 72), "DREAM", 34, WHITE, True)
    txt(draw, (80, 122), "TPC-DS", 22, MUTED, True)

    # Funnel / portfolio path.
    stages = [
        ("99", "", BLUE, 270),
        ("95", "", PURPLE, 310),
        ("3", "", ORANGE, 350),
        ("9", "", GREEN, 390),
    ]
    xs = [350, 610, 870, 1130]
    for i, ((number, _, color, diameter), x) in enumerate(zip(stages, xs)):
        y = 365
        draw.ellipse((x-diameter//2, y-diameter//2, x+diameter//2, y+diameter//2), fill=color)
        txt(draw, (x, y-8), number, 58, WHITE, True, "mm")
        txt(draw, (x, y+62), ["SQL", "RO", "cases", "runs"][i], 20, PALE, True, "ma")
        if i < len(xs)-1:
            draw.line((x+diameter//2+22, y, xs[i+1]-stages[i+1][3]//2-22, y), fill=MUTED, width=8)
            draw.polygon(((xs[i+1]-stages[i+1][3]//2-22, y), (xs[i+1]-stages[i+1][3]//2-46, y-18), (xs[i+1]-stages[i+1][3]//2-46, y+18)), fill=MUTED)
    txt(draw, (800, 740), "+73.8%", 64, GREEN, True, "mm")
    txt(draw, (800, 812), "", 20, MUTED, False, "ma")
    return image


def comparison():
    image, draw = base(BLUE)
    txt(draw, (80, 72), "DREAM", 34, WHITE, True)
    txt(draw, (80, 122), "", 22, MUTED, True)
    chart = (150, 170, 1450, 760)
    left, top, right, bottom = chart
    for value in (0, 25, 50, 75, 100):
        y = bottom - (bottom-top) * value / 100
        draw.line((left, y, right, y), fill=GRID, width=2)
        txt(draw, (left-24, y), str(value), 18, MUTED, False, "rm")
    labels = ["Q31", "Q44", "Q88"]
    optimized = [12.1, 41.9, 19.2]
    colors = [BLUE, PURPLE, ORANGE]
    centers = [480, 800, 1120]
    bar_w = 78
    for label, value, color, center in zip(labels, optimized, colors, centers):
        base_h = bottom - (bottom-top) * 1.0
        opt_h = bottom - (bottom-top) * value / 100
        draw.rounded_rectangle((center-bar_w-14, base_h, center-14, bottom), radius=12, fill="#60728D")
        draw.rounded_rectangle((center+14, opt_h, center+bar_w+14, bottom), radius=12, fill=color)
        txt(draw, (center, bottom+48), label, 26, WHITE, True, "ma")
        txt(draw, (center+bar_w+14, opt_h-18), f"{value:.0f}", 20, color, True, "ms")
    txt(draw, (right-10, top-26), "100", 20, MUTED, True, "ra")
    return image


def loop():
    image, draw = base(CYAN)
    txt(draw, (80, 72), "DREAM", 34, WHITE, True)
    txt(draw, (80, 122), "", 22, MUTED, True)
    nodes = [
        (300, 430, BLUE, "01"),
        (590, 250, PURPLE, "02"),
        (1010, 250, ORANGE, "03"),
        (1300, 430, GREEN, "04"),
        (1010, 650, CYAN, "05"),
        (590, 650, BLUE, "06"),
    ]
    for i in range(len(nodes)):
        x1, y1, _, _ = nodes[i]
        x2, y2, _, _ = nodes[(i + 1) % len(nodes)]
        draw.line((x1, y1, x2, y2), fill=MUTED, width=7)
        # Arrow head at 82% of each edge.
        ax = x1 + (x2-x1)*0.82
        ay = y1 + (y2-y1)*0.82
        txt(draw, (ax, ay), "›", 56, MUTED, True, "mm")
    for x, y, color, label in nodes:
        draw.ellipse((x-92, y-92, x+92, y+92), fill=color, outline=WHITE, width=4)
        txt(draw, (x, y), label, 42, WHITE, True, "mm")
    # Font-independent circular refresh mark in the center.
    draw.arc((735, 365, 865, 495), start=35, end=300, fill=WHITE, width=12)
    draw.polygon(((850, 372), (878, 390), (844, 405)), fill=WHITE)
    return image


if __name__ == "__main__":
    outputs = {
        "dream_effect_01_coverage.png": coverage(),
        "dream_effect_02_comparison.png": comparison(),
        "dream_effect_03_loop.png": loop(),
    }
    for name, image in outputs.items():
        path = OUT / name
        image.save(path, "PNG", optimize=True)
        print(path)
