#!/usr/bin/env python3
"""
Generate recon's 24x24 RGBA launcher icon.

The launcher blits each icon over a coloured squircle and drops its alpha when
the app is not selected, so icons are drawn as bold shapes on transparency.

Everything is drawn at 4x and downsampled, which is the cheapest way to get
clean edges at this size.

    python3 tools/make_icon.py
"""

import math
import os

from PIL import Image, ImageDraw

S = 4          # supersample factor
N = 24         # final icon size
D = N * S      # working canvas

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
APPS = ROOT


def canvas():
    img = Image.new("RGBA", (D, D), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def save(img, app):
    out = os.path.join(APPS, app, "icon.png")
    img.resize((N, N), Image.LANCZOS).save(out)
    print("wrote %s" % os.path.relpath(out, ROOT))


def recon():
    """A radar sweep with contacts."""
    img, d = canvas()
    cx, cy = D * 0.5, D * 0.52
    teal = (80, 220, 235, 255)
    amber = (246, 190, 40, 255)

    for r in (D * 0.42, D * 0.28, D * 0.14):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=teal, width=int(S * 1.1))

    # Sweep wedge.
    r = D * 0.42
    d.pieslice([cx - r, cy - r, cx + r, cy + r], -70, -20,
               fill=(80, 220, 235, 90))
    d.line([(cx, cy), (cx + r * 0.94, cy - r * 0.34)], fill=teal, width=int(S * 1.3))

    # Contacts.
    for fx, fy in ((0.72, 0.30), (0.30, 0.66), (0.62, 0.74)):
        x, y = D * fx, D * fy
        d.ellipse([x - S * 1.8, y - S * 1.8, x + S * 1.8, y + S * 1.8], fill=amber)
    save(img, "recon")


if __name__ == "__main__":
    recon()
