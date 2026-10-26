"""Text overlays for highlight videos, rendered to transparent PNGs with Pillow.

(Rendering the text ourselves works with any ffmpeg build; not every build has
the ``drawtext`` filter.)
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = {
    "win32": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc"],
    "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"],
}


@lru_cache(maxsize=32)
def font(size: int) -> ImageFont.FreeTypeFont:
    plat = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    for path in _FONT_CANDIDATES[plat]:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)   # bundled with Pillow; always works


def multikill_name(n: int) -> str:
    return {1: "KILL", 2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL", 5: "PENTA KILL"}.get(n, f"{n}× MULTI-KILL")


def render_text(path: str, text: str, height_px: int, *, color: Tuple[int, int, int] = (255, 255, 255),
                accent: Optional[Tuple[int, int, int]] = None, sub: str = "") -> Tuple[int, int]:
    """Write ``text`` (and an optional smaller ``sub`` line) as a PNG; returns its size."""
    f = font(max(10, height_px))
    fs = font(max(8, int(height_px * 0.45)))
    stroke = max(2, height_px // 12)
    pad = stroke * 2 + 4
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    w, h = r - l, b - t
    sw = sh = 0
    if sub:
        sl, st, sr, sb = probe.textbbox((0, 0), sub, font=fs, stroke_width=stroke // 2)
        sw, sh = sr - sl, sb - st
    W, H = max(w, sw) + 2 * pad, h + (sh + pad // 2 if sub else 0) + 2 * pad
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if accent:   # underline bar
        bar = max(3, height_px // 10)
        d.rectangle([pad, pad + h + bar // 2, pad + w, pad + h + bar // 2 + bar], fill=accent + (230,))
    d.text(((W - w) // 2 - l, pad - t), text, font=f, fill=color + (255,), stroke_width=stroke,
           stroke_fill=(0, 0, 0, 200))
    if sub:
        d.text(((W - sw) // 2, pad + h + pad // 2), sub, font=fs, fill=(230, 230, 230, 255),
               stroke_width=max(1, stroke // 2), stroke_fill=(0, 0, 0, 200))
    img.save(path)
    return W, H
