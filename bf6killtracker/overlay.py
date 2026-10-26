"""Text overlays for highlight videos, rendered to transparent PNGs with Pillow.

(Rendering the text ourselves works with any ffmpeg build; not every build has
the ``drawtext`` filter.) Look and placement of the kill call-outs are set by
:class:`CalloutStyle`.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

RGB = Tuple[int, int, int]

# named positions -> centre of the call-out as a fraction of the frame (x, y)
POSITIONS: Dict[str, Tuple[float, float]] = {
    "Top": (0.5, 0.10),
    "Upper third": (0.5, 0.26),
    "Centre": (0.5, 0.45),
    "Lower third": (0.5, 0.70),
    "Bottom": (0.5, 0.88),
    "Top left": (0.16, 0.10),
    "Top right": (0.84, 0.10),
    "Bottom left": (0.16, 0.88),
    "Bottom right": (0.84, 0.88),
    "Custom": (0.5, 0.26),
}


@dataclass
class CalloutStyle:
    position: str = "Upper third"     # one of POSITIONS; "Custom" uses x/y
    x: float = 0.5                    # centre, fraction of width  (Custom)
    y: float = 0.26                   # centre, fraction of height (Custom)
    size: int = 100                   # % of the default size (5% of the video height)
    font: str = ""                    # font file path; "" = bold system font
    color: RGB = (255, 255, 255)
    accent: bool = True               # coloured bar under the text
    accent_color: RGB = (255, 140, 30)
    outline: int = 100                # outline thickness, % of default (0 = none)
    outline_color: RGB = (0, 0, 0)
    shadow: bool = False              # soft drop shadow
    uppercase: bool = True
    duration: float = 1.8             # seconds on screen (shortened if the next kill comes sooner)
    # texts; {n} = kills so far in the clip, {name} = multi-kill name (DOUBLE KILL, ...)
    text_first: str = "KILL"
    text_streak: str = "{n} KILL STREAK"
    text_multi: str = "{name}"
    show_count: bool = True           # small "{n} KILLS" line under multi-kills

    def anchor(self) -> Tuple[float, float]:
        if self.position == "Custom" or self.position not in POSITIONS:
            return (min(1.0, max(0.0, self.x)), min(1.0, max(0.0, self.y)))
        return POSITIONS[self.position]

    def place(self, w: int, h: int, W: int, H: int) -> Tuple[int, int]:
        """Top-left pixel position for a ``w``×``h`` image so it's centred on the anchor (kept on screen)."""
        ax, ay = self.anchor()
        x = int(round(ax * W - w / 2))
        y = int(round(ay * H - h / 2))
        return max(0, min(W - w, x)), max(0, min(H - h, y))

    def texts(self, kills_here: int, running: int) -> Tuple[str, str]:
        """(main line, small line) for a kill event."""
        if kills_here > 1:
            main = self.text_multi.format(name=multikill_name(kills_here), n=running)
            sub = f"{running} KILLS" if self.show_count and running > kills_here else ""
        elif running == 1:
            main, sub = self.text_first.format(n=running, name="KILL"), ""
        else:
            main, sub = self.text_streak.format(n=running, name="KILL"), ""
        if self.uppercase:
            main, sub = main.upper(), sub.upper()
        return main, sub

    # -- (de)serialisation ------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: Optional[str]) -> "CalloutStyle":
        try:
            data = json.loads(text) if text else {}
        except (TypeError, ValueError):
            data = {}
        known = {f.name for f in fields(cls)}
        clean = {k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items() if k in known}
        return cls(**clean)


# --------------------------------------------------------------------------- fonts
_FONT_CANDIDATES = {
    "win32": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc"],
    "linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"],
}


def _plat() -> str:
    return "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")


def font_dirs() -> List[str]:
    if _plat() == "win32":
        win = os.environ.get("WINDIR", "C:/Windows")
        local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")
        return [os.path.join(win, "Fonts"), local]
    if _plat() == "darwin":
        return ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts")]


@lru_cache(maxsize=1)
def system_fonts(limit: int = 800) -> List[Tuple[str, str]]:
    """[(display name, file path)] of installed TrueType/OpenType fonts, sorted by name."""
    out: Dict[str, str] = {}
    for d in font_dirs():
        if not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if not path.lower().endswith((".ttf", ".otf")) or os.path.basename(path).startswith("."):
                continue
            try:
                fam, style = ImageFont.truetype(path, 12).getname()
            except Exception:
                continue
            name = f"{fam} {style}".strip() if style and style.lower() != "regular" else (fam or "")
            if name and not name.startswith(".") and name not in out:
                out[name] = path
            if len(out) >= limit:
                break
    return sorted(out.items(), key=lambda kv: kv[0].lower())


@lru_cache(maxsize=64)
def font(size: int, path: str = "") -> ImageFont.FreeTypeFont:
    for p in ([path] if path else []) + _FONT_CANDIDATES[_plat()]:
        if p and os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)   # bundled with Pillow; always works


def multikill_name(n: int) -> str:
    return {1: "KILL", 2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUAD KILL", 5: "PENTA KILL"}.get(n, f"{n}× MULTI-KILL")


# --------------------------------------------------------------------------- rendering
def render_image(text: str, height_px: int, *, color: RGB = (255, 255, 255), accent: Optional[RGB] = None,
                 sub: str = "", font_path: str = "", outline_pct: int = 100, outline_color: RGB = (0, 0, 0),
                 shadow: bool = False) -> Image.Image:
    f = font(max(10, height_px), font_path)
    fs = font(max(8, int(height_px * 0.45)), font_path)
    stroke = int(round(max(2, height_px // 12) * outline_pct / 100))
    pad = max(stroke * 2, 2) + 4 + (height_px // 6 if shadow else 0)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), text, font=f, stroke_width=stroke)
    w, h = r - l, b - t
    sw = sh = 0
    sub_stroke = max(1, stroke // 2) if stroke else 0
    if sub:
        sl, st, sr, sb = probe.textbbox((0, 0), sub, font=fs, stroke_width=sub_stroke)
        sw, sh = sr - sl, sb - st
    bar = max(3, height_px // 10) if accent else 0
    W, H = max(w, sw) + 2 * pad, h + (bar * 2 if accent else 0) + (sh + pad // 2 if sub else 0) + 2 * pad
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0 = (W - w) // 2 - l, pad - t
    bar_y = pad + h + bar // 2
    sub_y = pad + h + (bar * 2 if accent else 0) + pad // 2
    if shadow:
        sh_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh_img)
        off = max(2, height_px // 18)
        sd.text((x0 + off, y0 + off), text, font=f, fill=(0, 0, 0, 170), stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
        if sub:
            sd.text(((W - sw) // 2 + off, sub_y + off), sub, font=fs, fill=(0, 0, 0, 170))
        img = Image.alpha_composite(img, sh_img.filter(ImageFilter.GaussianBlur(max(1, height_px // 14))))
        d = ImageDraw.Draw(img)
    if accent:
        d.rectangle([pad, bar_y, pad + w, bar_y + bar], fill=tuple(accent) + (230,))
    d.text((x0, y0), text, font=f, fill=tuple(color) + (255,), stroke_width=stroke,
           stroke_fill=tuple(outline_color) + (210,))
    if sub:
        d.text(((W - sw) // 2, sub_y), sub, font=fs, fill=(235, 235, 235, 255), stroke_width=sub_stroke,
               stroke_fill=tuple(outline_color) + (210,))
    return img


def render_text(path: str, text: str, height_px: int, **kw) -> Tuple[int, int]:
    """Write ``text`` (and an optional smaller ``sub`` line) as a PNG; returns its size."""
    img = render_image(text, height_px, **kw)
    img.save(path)
    return img.size


def render_callout(style: CalloutStyle, main: str, sub: str, H: int) -> Image.Image:
    return render_image(main, max(10, int(H * 0.05 * style.size / 100)), color=style.color,
                        accent=style.accent_color if style.accent else None, sub=sub, font_path=style.font,
                        outline_pct=style.outline, outline_color=style.outline_color, shadow=style.shadow)


def preview(style: CalloutStyle, background: Optional[Image.Image] = None, size: Tuple[int, int] = (960, 540),
            kills_here: int = 2, running: int = 3) -> Image.Image:
    """The call-out as it will appear on a frame (for the style editor)."""
    W, H = size
    bg = (background.convert("RGB").resize((W, H)) if background is not None
          else Image.new("RGB", (W, H), (40, 44, 52)))
    main, sub = style.texts(kills_here, running)
    co = render_callout(style, main, sub, H)
    x, y = style.place(co.width, co.height, W, H)
    out = bg.convert("RGBA")
    out.alpha_composite(co, (x, y))
    return out.convert("RGB")
