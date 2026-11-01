#!/usr/bin/env python3
"""Rasterize the Nexus logo. Run: python3 interface/brand/make_icons.py (needs Pillow).

The logo is the slash N from the splash screen, as written and not bold, at
every size. Monaspace Argon Regular draws the large sizes. A font cannot hold a
one-pixel `_/` at the small sizes, so those place the same marks on the pixel grid.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT = HERE.parent.parent / "nexus-nsh" / "fonts" / "MonaspaceArgon-Regular.otf"
ART = ["    _/      _/", "   _/_/    _/", "  _/  _/  _/", " _/    _/_/", "_/      _/"]
INK = (244, 244, 245, 255)
GROUND = (0, 0, 0, 255)
SS = 4  # supersampling


def art(size: int, ground: bool) -> Image.Image:
    """The logo as written, centered on a square canvas."""
    big = size * SS
    im = Image.new("RGBA", (big, big), GROUND if ground else (0, 0, 0, 0))
    em = big * 0.72 / (14 * 0.6)
    font = ImageFont.truetype(str(FONT), round(em))
    line = em * 1.15
    advance = font.getlength("_")
    cols = max(len(row) for row in ART)
    x0 = (big - cols * advance) / 2
    y0 = (big - line * len(ART)) / 2 - em * 0.08
    d = ImageDraw.Draw(im)
    for r, row in enumerate(ART):
        for c, ch in enumerate(row):
            if ch != " ":
                d.text((x0 + c * advance, y0 + r * line), ch, font=font, fill=INK)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def grid(size: int, ground: bool) -> Image.Image:
    """The same marks on the pixel grid: each `_/` is a foot and a one-pixel slash."""
    im = Image.new("RGBA", (size, size), GROUND if ground else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    col = max(1, (size - 2) // 14)
    row = max(3, (size - 2) // 5 if size < 32 else round(size * 0.72 / 5 * 0.85))
    width, height = col * 14, row * 5
    x0, y0 = (size - width) // 2, (size - height) // 2
    for r, text in enumerate(ART):
        for c, ch in enumerate(text):
            x, y = x0 + c * col, y0 + r * row
            if ch == "_":
                d.line([(x, y + row - 1), (x + col - 1, y + row - 1)], fill=INK)
            elif ch == "/":
                d.line([(x, y + row - 1), (x + col, y)], fill=INK)
    return im


def main() -> None:
    for size in (1024, 512, 192, 180):
        art(size, True).save(HERE / f"nexus-{size}.png")
    for size in (64, 48, 32, 16):
        grid(size, True).save(HERE / f"nexus-{size}.png")
    art(1024, False).save(HERE / "nexus-logo-transparent.png")
    frames = [grid(size, True) for size in (48, 32, 16)]
    frames[0].save(HERE / "favicon.ico", append_images=frames[1:], sizes=[(48, 48), (32, 32), (16, 16)])


if __name__ == "__main__":
    main()
