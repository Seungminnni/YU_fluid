#!/usr/bin/env python3
"""Update the published CAD top projection for the reflected C05/C19 ports."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "Arial Bold.ttf" if bold else "Arial.ttf"
    return ImageFont.truetype(f"/System/Library/Fonts/Supplemental/{name}", size)


def update(path: Path) -> None:
    image = Image.open(path).convert("RGBA")
    draw = ImageDraw.Draw(image)
    width, _ = image.size
    draw.rectangle((0, 45, width, 115), fill="white")
    subtitle = "Blue inlet: C05 upper-left / Red outlet: C19 lower-right"
    draw.text((width / 2, 70), subtitle, anchor="mm", font=font(31, bold=True), fill="#19334d")

    radius = 104
    neutral = "#d5e0e7"
    outline = "#294d63"
    background = "#f5f9fb"
    # Clear both the former and new port labels before redrawing them.  This
    # also makes this script idempotent when a published bundle is refreshed.
    for x in (228, 1053):
        for y in (218, 1205):
            draw.rectangle((x-75, y-25, x+75, y+27), fill=background)
    locations = {
        "C01": (228, 1331), "C02": (228, 1087), "C05": (228, 354),
        "C19": (1053, 1331), "C20": (1053, 1087), "C23": (1053, 354),
    }
    for cell_id in ("C01", "C02", "C20", "C23"):
        x, y = locations[cell_id]
        draw.ellipse((x-radius-5, y-radius-5, x+radius+5, y+radius+5), fill=background)
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=neutral, outline=outline, width=3)
        draw.text((x, y), cell_id, anchor="mm", font=font(14), fill="#294d63")
    for cell_id, label, fill, stroke in (
        ("C05", "IN", "#9dbddd", "#0067c5"),
        ("C19", "OUT", "#d6a6aa", "#d62828"),
    ):
        x, y = locations[cell_id]
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill, outline=stroke, width=5)
        draw.text((x, y), cell_id, anchor="mm", font=font(14), fill="#294d63")
        draw.text((x, y-radius-24), label, anchor="ms", font=font(25, bold=True), fill=stroke)
    image.save(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.paths:
        update(path)
        print(path)


if __name__ == "__main__":
    main()
