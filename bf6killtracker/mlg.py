"""MLG mode: the 2014 "MLG montage parody" look on top of a highlight video.

At each kill: hitmarker (+ tick sound), air horn (graphic + BWAAA), flying
soda cans, meme text, colour strobe / deep-fry. On a triple (3 kills within a
few seconds): a screen of scrolling "OHHHHHH", "OH BABY A TRIPLE" (voice +
text), money rain, the illuminati and pixel shades dropping in.

Everything is generated here (Pillow drawings, numpy-synthesised sounds, the
system's text-to-speech voice), so nothing copyrighted ships with the app.
Users can point the triple / air-horn slots at their own sound files.
"""
from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

RGB = Tuple[int, int, int]
SR = 48000

INTENSITIES = ("mild", "normal", "max")
STROBES = ("off", "light", "normal", "max")
STROBE_LABELS = {"off": "Off", "light": "Colour only (no flashes)", "normal": "Colour + flash on kills",
                 "max": "Maximum (rapid flashing!)"}

WORDS = ["OH", "WOW", "o my god", "GET REKT", "REKT", "SKRRT", "#rekt", "NO SCOPE", "wombo combo", "OHHHH",
         "ur dead m8", "EZ", "DAMN SON", "360 NO SCOPE", "mom get the camera"]
TEXT_STYLES = ("rainbow", "pink", "blue", "red", "yellow")
TRIPLE_TEXT = "OH BABY A TRIPLE"

_MEME_FONTS = {
    "impact": {
        "win32": ["C:/Windows/Fonts/impact.ttf"],
        "darwin": ["/System/Library/Fonts/Supplemental/Impact.ttf", "/Library/Fonts/Impact.ttf"],
        "linux": ["/usr/share/fonts/truetype/msttcorefonts/Impact.ttf", "/usr/share/fonts/TTF/Impact.TTF"],
    },
    "comic": {
        "win32": ["C:/Windows/Fonts/comicbd.ttf", "C:/Windows/Fonts/comic.ttf"],
        "darwin": ["/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf",
                   "/Library/Fonts/Comic Sans MS Bold.ttf"],
        "linux": ["/usr/share/fonts/truetype/msttcorefonts/comicbd.ttf"],
    },
}


_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "mlg")


def bundled(name: str) -> Optional[str]:
    """Path of a shipped asset (templates/mlg/...), or None if missing."""
    p = os.path.join(_ASSETS, name)
    return p if os.path.isfile(p) else None


def mtn_dew_logo(width: int) -> Optional[Image.Image]:
    p = bundled("mtn_dew_logo.png")
    if not p:
        return None
    with Image.open(p) as im:
        im = im.convert("RGBA")
        return im.resize((width, max(1, int(im.height * width / im.width))), Image.LANCZOS)


def _plat() -> str:
    return "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")


def meme_font(size: int, kind: str = "impact") -> ImageFont.FreeTypeFont:
    for p in _MEME_FONTS.get(kind, {}).get(_plat(), []):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    from .overlay import font
    return font(size)


# ---------------------------------------------------------------- drawings
RAINBOW = [(255, 30, 30), (255, 150, 0), (255, 235, 0), (40, 220, 60), (0, 170, 255), (150, 70, 255),
           (255, 40, 200)]
_STYLE = {   # fill colours (gradient stops), outline
    "rainbow": (RAINBOW, (255, 255, 255)),
    "pink": ([(255, 120, 230), (200, 40, 255)], (255, 255, 255)),
    "blue": ([(90, 200, 255), (40, 90, 255)], (255, 255, 255)),
    "red": ([(255, 60, 40), (190, 0, 30)], (255, 230, 0)),
    "yellow": ([(255, 250, 90), (255, 170, 0)], (200, 0, 0)),
}


def _gradient(w: int, h: int, stops: Sequence[RGB], horizontal: bool = True) -> Image.Image:
    n = w if horizontal else h
    xs = np.linspace(0, len(stops) - 1, max(2, n))
    lo = np.clip(np.floor(xs).astype(int), 0, len(stops) - 1)
    hi = np.clip(lo + 1, 0, len(stops) - 1)
    f = (xs - lo)[:, None]
    s = np.asarray(stops, dtype=float)
    line = (s[lo] * (1 - f) + s[hi] * f).astype(np.uint8)
    arr = np.repeat(line[None, :, :], h, 0) if horizontal else np.repeat(line[:, None, :], w, 1)
    return Image.fromarray(arr)


def meme_text(text: str, height: int, style: str = "rainbow", kind: str = "impact") -> Image.Image:
    """Outlined, gradient-filled meme text with a hard black drop shadow."""
    f = meme_font(max(10, height), kind)
    s1 = max(2, height // 14)          # coloured outline
    s2 = s1 + max(2, height // 18)     # black outline outside it
    sh = max(2, height // 16)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=f, stroke_width=s2)
    w, h = x1 - x0 + sh + 2, y1 - y0 + sh + 2
    org = (-x0 + 1, -y0 + 1)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stops, outline = _STYLE.get(style, _STYLE["rainbow"])
    d.text((org[0] + sh, org[1] + sh), text, font=f, fill=(0, 0, 0, 200), stroke_width=s2, stroke_fill=(0, 0, 0, 200))
    d.text(org, text, font=f, fill=(0, 0, 0, 255), stroke_width=s2, stroke_fill=(0, 0, 0, 255))
    d.text(org, text, font=f, fill=outline + (255,), stroke_width=s1, stroke_fill=outline + (255,))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text(org, text, font=f, fill=255)
    img.paste(_gradient(w, h, stops, horizontal=len(stops) > 2), (0, 0), mask)
    return img


def soda_can(height: int) -> Image.Image:
    """A green Mountain Dew style can."""
    k = 2                                    # supersample
    H = height * k
    W = int(H * 0.52)
    rim = int(H * 0.06)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    xs = np.linspace(-1, 1, W)
    shade = np.clip(0.45 + 0.75 * np.cos((xs + 0.25) * math.pi / 2.2), 0.25, 1.25)
    spec = np.exp(-((xs + 0.45) / 0.09) ** 2) * 0.8          # shiny highlight
    green = np.array([20, 140, 45], float)
    body = np.clip(green[None, :] * shade[:, None] + 255 * spec[:, None], 0, 255).astype(np.uint8)
    silver = np.clip(np.array([190, 195, 200], float)[None, :] * shade[:, None] + 200 * spec[:, None],
                     0, 255).astype(np.uint8)
    body_img = Image.fromarray(np.repeat(body[None], H - 2 * rim, 0))
    silver_img = Image.fromarray(np.repeat(silver[None], H, 0))
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, rim // 2, W - 1, H - 1 - rim // 2), radius=W // 7, fill=255)
    img.paste(silver_img, (0, 0), mask)
    inner = Image.new("L", (W, H - 2 * rim), 0)
    ImageDraw.Draw(inner).rectangle((0, 0, W, H), fill=255)
    img.paste(body_img, (0, rim), inner)
    d = ImageDraw.Draw(img)
    d.ellipse((W * 0.08, 0, W * 0.92, rim * 1.4), fill=(215, 220, 225, 255), outline=(120, 125, 130, 255),
              width=max(1, k * 2))
    d.ellipse((W * 0.42, rim * 0.35, W * 0.62, rim * 0.95), fill=(150, 155, 160, 255))     # tab
    # red swoosh band + vertical white logo, like the real can
    logo = mtn_dew_logo(int(H * 1.0))
    if logo is not None:                     # the real logo running up the can, like the 2009+ cans
        label = logo.rotate(58, expand=True, resample=Image.BICUBIC)
        sc = (H * 0.86) / label.height
        label = label.resize((max(1, int(label.width * sc)), max(1, int(label.height * sc))), Image.LANCZOS)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.alpha_composite(label, ((W - label.width) // 2, (H - label.height) // 2))
        body = Image.new("L", (W, H), 0)
        ImageDraw.Draw(body).rectangle((0, rim, W, H - rim), fill=255)
        a = np.minimum(np.asarray(layer.getchannel("A")), np.asarray(body))
        # wrap shading onto the logo so it looks printed on the curved can
        rgb = np.asarray(layer.convert("RGB")).astype(float) * np.clip(shade, 0.5, 1.1)[None, :, None]
        rgb = np.clip(rgb + 160 * spec[None, :, None], 0, 255).astype(np.uint8)
        layer = Image.fromarray(rgb).convert("RGBA")
        layer.putalpha(Image.fromarray(a.astype(np.uint8)))
        img.alpha_composite(layer)
    else:
        band_h = int(H * 0.08)
        d.polygon([(0, H * 0.70), (W, H * 0.60), (W, H * 0.60 + band_h), (0, H * 0.70 + band_h)],
                  fill=(220, 20, 30, 235))
        label = Image.new("RGBA", (int(H * 0.75), int(W * 0.62)), (0, 0, 0, 0))
        ld = ImageDraw.Draw(label)
        f = meme_font(int(W * 0.42), "impact")
        ld.text((label.width // 2, label.height // 2), "MTN DEW", font=f, anchor="mm", fill=(255, 255, 255, 255),
                stroke_width=max(2, k * 3), stroke_fill=(10, 60, 15, 255))
        label = label.rotate(90, expand=True)
        img.alpha_composite(label, ((W - label.width) // 2, int(H * 0.1)))
    return img.resize((W // k, height), Image.LANCZOS)


def air_horn(height: int) -> Image.Image:
    """Red horn on a white canister (horn flaring to the right)."""
    k = 2
    H = height * k
    W = int(H * 1.05)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cw = int(W * 0.40)
    cx0 = int(W * 0.06)
    top = int(H * 0.38)
    xs = np.linspace(-1, 1, cw)
    shade = np.clip(0.80 + 0.25 * np.cos((xs + 0.3) * math.pi / 2), 0.55, 1.05)
    col = np.clip(np.array([245, 245, 245], float)[None] * shade[:, None], 0, 255).astype(np.uint8)
    can = Image.fromarray(np.repeat(col[None], H - top, 0))
    m = Image.new("L", (cw, H - top), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, cw - 1, H - top - 1), radius=cw // 6, fill=255)
    img.paste(can, (cx0, top), m)
    d = ImageDraw.Draw(img)
    red, dark = (230, 20, 30, 255), (150, 0, 10, 255)
    # nozzle block + cap on top of the can
    d.rounded_rectangle((cx0 + cw * 0.15, H * 0.22, cx0 + cw * 0.85, top + H * 0.03), radius=int(H * 0.03), fill=red)
    d.ellipse((cx0 + cw * 0.05, H * 0.08, cx0 + cw * 0.70, H * 0.30), fill=red)
    # horn: cone from the block to a flared mouth
    nx = cx0 + cw * 0.70
    d.polygon([(nx, H * 0.16), (W * 0.93, H * 0.02), (W * 0.93, H * 0.52), (nx, H * 0.30)], fill=red)
    d.ellipse((W * 0.86, H * 0.02, W * 0.99, H * 0.52), fill=(255, 60, 60, 255), outline=dark, width=k * 3)
    d.ellipse((W * 0.90, H * 0.12, W * 0.97, H * 0.42), fill=dark)
    d.line([(nx, H * 0.18), (W * 0.9, H * 0.07)], fill=(255, 120, 120, 255), width=k * 4)   # shine
    return img.resize((W // k, height), Image.LANCZOS)


def hitmarker(size: int) -> Image.Image:
    k = 3
    S = size * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, a, b = S / 2, S * 0.14, S * 0.46
    for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        p = [(c + sx * a, c + sy * a), (c + sx * b, c + sy * b)]
        d.line(p, fill=(0, 0, 0, 200), width=int(S * 0.09))
        d.line(p, fill=(255, 255, 255, 255), width=int(S * 0.05))
    return img.resize((size, size), Image.LANCZOS)


def dollar_bill(height: int) -> Image.Image:
    k = 2
    H = height * k
    W = int(H * 2.35)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W - 1, H - 1), fill=(165, 185, 150, 255), outline=(60, 80, 55, 255), width=k * 3)
    d.rectangle((W * 0.05, H * 0.1, W * 0.95, H * 0.9), outline=(80, 105, 70, 255), width=k * 2)
    d.ellipse((W * 0.36, H * 0.16, W * 0.64, H * 0.84), fill=(140, 165, 125, 255), outline=(60, 80, 55, 255),
              width=k * 2)
    f = meme_font(int(H * 0.32), "impact")
    d.text((W / 2, H / 2), "ONE", font=f, anchor="mm", fill=(55, 75, 50, 255))
    fs = meme_font(int(H * 0.26), "impact")
    for x, y in ((0.11, 0.24), (0.89, 0.24), (0.11, 0.76), (0.89, 0.76)):
        d.text((W * x, H * y), "1", font=fs, anchor="mm", fill=(55, 75, 50, 255))
    return img.resize((W // k, height), Image.LANCZOS)


def illuminati(height: int) -> Image.Image:
    k = 2
    H = height * k
    W = int(H * 1.15)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tri = [(W / 2, H * 0.04), (W * 0.97, H * 0.96), (W * 0.03, H * 0.96)]
    ImageDraw.Draw(glow).polygon(tri, fill=(255, 255, 120, 160))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(H * 0.03)))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).polygon(tri, fill=255)
    img.paste(_gradient(W, H, [(255, 240, 120), (220, 160, 20)], horizontal=False), (0, 0), m)
    d = ImageDraw.Draw(img)
    d.polygon(tri, outline=(120, 70, 0, 255), width=k * 4)
    ey, ew, eh = H * 0.6, W * 0.30, H * 0.13
    d.ellipse((W / 2 - ew, ey - eh, W / 2 + ew, ey + eh), fill=(255, 255, 255, 255), outline=(60, 30, 0, 255),
              width=k * 3)
    d.ellipse((W / 2 - eh * 0.9, ey - eh * 0.9, W / 2 + eh * 0.9, ey + eh * 0.9), fill=(30, 110, 60, 255))
    d.ellipse((W / 2 - eh * 0.4, ey - eh * 0.4, W / 2 + eh * 0.4, ey + eh * 0.4), fill=(0, 0, 0, 255))
    return img.resize((W // k, height), Image.LANCZOS)


def pixel_shades(width: int) -> Image.Image:
    """The pixel-art "deal with it" sunglasses."""
    grid = ["XXXXXXXXXXXXXXXXXXXXXXXX",
            "XXXXXXXXXXXXXXXXXXXXXXXX",
            "..XXWWXXXXXXX..XXWWXXXXX",
            "..XWWXXXXXXXX..XWWXXXXX.",
            "...XXXXXXXX.....XXXXXXX.",
            "....XXXXXX.......XXXXX.."]
    px = max(1, width // len(grid[0]))
    img = Image.new("RGBA", (px * len(grid[0]), px * len(grid)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch != ".":
                d.rectangle((x * px, y * px, x * px + px - 1, y * px + px - 1),
                            fill=(0, 0, 0, 255) if ch == "X" else (255, 255, 255, 255))
    return img


def oh_screen(W: int, H: int, extra: float = 0.6) -> Image.Image:
    """Diagonal rows of striped "OHHHHHHHH" text covering the screen (wider than W for scrolling)."""
    big = int(max(W, H) * 1.9)
    canvas = Image.new("RGBA", (int(big * (1 + extra)), big), (0, 0, 0, 0))
    row_h = max(24, H // 7)
    f = meme_font(row_h, "impact")
    word = "OH" + "H" * 60
    stripes = _stripes(canvas.width, row_h + 8)
    y = 0
    i = 0
    while y < canvas.height:
        mask = Image.new("L", (canvas.width, row_h + 8), 0)
        md = ImageDraw.Draw(mask)
        off = -(i * row_h) % (row_h * 2)
        md.text((-off, 0), word * 3, font=f, fill=255)
        outline = Image.new("L", mask.size, 0)
        ImageDraw.Draw(outline).text((-off, 0), word * 3, font=f, fill=255, stroke_width=max(2, row_h // 14))
        canvas.paste((0, 0, 0, 255), (0, y), outline)
        canvas.paste(stripes, (0, y), mask)
        y += int(row_h * 1.6)
        i += 1
    rot = canvas.rotate(28, resample=Image.BICUBIC, expand=False)
    cx, cy = rot.width // 2, rot.height // 2
    w = int(W * (1 + extra))
    return rot.crop((cx - w // 2, cy - H // 2, cx - w // 2 + w, cy - H // 2 + H))


def _stripes(w: int, h: int) -> Image.Image:
    cols = [(40, 80, 255), (255, 255, 255), (255, 140, 0), (255, 255, 255)]
    band = max(2, h // 6)
    arr = np.zeros((h, w, 3), np.uint8)
    for x in range(0, w, band):
        arr[:, x:x + band] = cols[(x // band) % len(cols)]
    return Image.fromarray(arr)


# ------------------------------------------------------------------ sounds
def _write_wav(path: str, x: np.ndarray, sr: int = SR) -> str:
    x = np.clip(x, -1, 1)
    st = np.stack([x, x], 1) if x.ndim == 1 else x
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((st * 32767).astype("<i2").tobytes())
    return path


def airhorn_wave(sr: int = SR, seed: int = 7) -> np.ndarray:
    """The MLG air horn: BWAAA-BWA-BWA-BWAAAAA, a brassy detuned chord."""
    rng = np.random.default_rng(seed)
    pattern = [(0.0, 0.42), (0.47, 0.13), (0.65, 0.13), (0.83, 0.85)]
    total = pattern[-1][0] + pattern[-1][1] + 0.15
    n = int(total * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    base = [233.0, 293.7, 349.2, 466.2]            # Bb major-ish horn chord
    for start, dur in pattern:
        i0, i1 = int(start * sr), int((start + dur) * sr)
        tt = t[i0:i1] - start
        bend = 1 - 0.06 * np.exp(-tt / 0.03)        # pitch scoops up at the attack
        tone = np.zeros_like(tt)
        for f in base:
            for det in (-0.004, 0.0, 0.005):
                ph = 2 * np.pi * np.cumsum(f * (1 + det) * bend) / sr + rng.uniform(0, 6.28)
                tone += 2 * (ph / (2 * np.pi) % 1) - 1          # sawtooth
        env = np.minimum(1, tt / 0.012) * np.minimum(1, (dur - tt) / 0.03)
        out[i0:i1] += tone * env
    out += rng.normal(0, 0.02, n) * (np.abs(out) > 0.01)        # air
    out = np.tanh(out / np.max(np.abs(out)) * 2.5)              # blown-out
    # crude low-pass to tame fizz
    k = np.ones(5) / 5
    out = np.convolve(out, k, "same")
    return out / np.max(np.abs(out)) * 0.85


def hit_wave(sr: int = SR) -> np.ndarray:
    n = int(0.09 * sr)
    t = np.arange(n) / sr
    x = (np.sin(2 * np.pi * 2600 * t) * 0.6 + np.sin(2 * np.pi * 4100 * t) * 0.3
         + np.random.default_rng(3).normal(0, 0.25, n)) * np.exp(-t / 0.018)
    return x / np.max(np.abs(x)) * 0.8


def speak(text: str, out_base: str, timeout: float = 20.0) -> Optional[str]:
    """Render ``text`` with the computer's built-in voice; returns the audio path or None."""
    from .video import NO_WINDOW
    try:
        if sys.platform == "darwin" and shutil.which("say"):
            out = out_base + ".aiff"
            subprocess.run(["say", "-r", "205", "-o", out, text], timeout=timeout, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            out = out_base + ".wav"
            ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.Rate = 2; $s.SetOutputToWaveFile($env:BF6KT_TTS_OUT); $s.Speak($env:BF6KT_TTS_TEXT); "
                  "$s.Dispose()")
            env = dict(os.environ, BF6KT_TTS_OUT=out, BF6KT_TTS_TEXT=text)    # no quoting issues
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                           timeout=timeout, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           **NO_WINDOW)
        else:
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            if not exe:
                return None
            out = out_base + ".wav"
            subprocess.run([exe, "-s", "190", "-w", out, text], timeout=timeout, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if os.path.isfile(out) and os.path.getsize(out) > 1000 else None


# ------------------------------------------------------------- more drawings
def explosion(height: int, seed: int = 4) -> Image.Image:
    """Cartoon fireball: jagged red/orange/yellow/white star bursts with a soft glow."""
    rng = random.Random(seed)
    k = 2
    S = height * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c = S / 2
    for frac, col in ((0.50, (200, 20, 10, 255)), (0.40, (255, 120, 0, 255)), (0.29, (255, 220, 40, 255)),
                      (0.16, (255, 255, 220, 255))):
        n = 18
        pts = []
        for i in range(n * 2):
            r = S * frac * (1.0 if i % 2 == 0 else rng.uniform(0.55, 0.75))
            a = math.pi * i / n + rng.uniform(-0.08, 0.08)
            pts.append((c + r * math.cos(a), c + r * math.sin(a)))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(pts, fill=col)
        img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(S * 0.006)))
    glow = img.filter(ImageFilter.GaussianBlur(S * 0.04))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.alpha_composite(glow)
    out.alpha_composite(img)
    return out.resize((height, height), Image.LANCZOS)


def nacho_chip(height: int, seed: int = 2) -> Image.Image:
    """A cheesy orange triangle chip (generic, no brand)."""
    rng = random.Random(seed)
    k = 2
    S = height * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tri = [(S * 0.5, S * 0.05), (S * 0.96, S * 0.92), (S * 0.04, S * 0.92)]
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).polygon(tri, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(S * 0.03)).point(lambda v: 255 if v > 128 else 0)
    img.paste(_gradient(S, S, [(255, 170, 30), (235, 100, 0)], horizontal=False), (0, 0), m)
    d = ImageDraw.Draw(img)
    for _ in range(40):                                # seasoning
        x, y = rng.uniform(S * 0.2, S * 0.8), rng.uniform(S * 0.35, S * 0.85)
        if m.getpixel((int(x), int(y))):
            r = rng.uniform(S * 0.008, S * 0.02)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(200, 50, 0, 255))
    return img.resize((height, height), Image.LANCZOS)


def scope(W: int, H: int) -> Image.Image:
    """Sniper scope: black screen with a round window and crosshair."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    r = int(H * 0.46)
    m = Image.new("L", (W, H), 255)
    ImageDraw.Draw(m).ellipse((W / 2 - r, H / 2 - r, W / 2 + r, H / 2 + r), fill=0)
    img.putalpha(m.filter(ImageFilter.GaussianBlur(H * 0.01)))
    d = ImageDraw.Draw(img)
    lw = max(2, H // 300)
    d.line([(W / 2 - r, H / 2), (W / 2 + r, H / 2)], fill=(0, 0, 0, 255), width=lw)
    d.line([(W / 2, H / 2 - r), (W / 2, H / 2 + r)], fill=(0, 0, 0, 255), width=lw)
    for i in range(1, 5):
        o = r * i / 5
        for s in (-1, 1):
            d.line([(W / 2 + s * o, H / 2 - H * 0.012), (W / 2 + s * o, H / 2 + H * 0.012)], fill=(0, 0, 0, 255),
                   width=lw)
    d.ellipse((W / 2 - r, H / 2 - r, W / 2 + r, H / 2 + r), outline=(0, 0, 0, 255), width=lw * 4)
    return img


def rainbow_smoke(height: int, seed: int = 5) -> Image.Image:
    rng = random.Random(seed)
    S = height
    img = Image.new("RGBA", (int(S * 1.4), S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(26):
        col = RAINBOW[i % len(RAINBOW)]
        x, y = rng.uniform(S * 0.2, S * 1.2), rng.uniform(S * 0.2, S * 0.8)
        r = rng.uniform(S * 0.1, S * 0.22)
        d.ellipse((x - r, y - r, x + r, y + r), fill=col + (150,))
    return img.filter(ImageFilter.GaussianBlur(S * 0.06))


def doge_shades_text(word: str, height: int) -> Image.Image:
    return meme_text(word, height, "yellow", "comic")


# ------------------------------------------------------------- more sounds
def wub_wave(sr: int = SR, seconds: float = 1.3, rate: float = 6.0) -> np.ndarray:
    """Dubstep wobble bass: saw at 55 Hz through an LFO-swept low-pass."""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    saw = sum(np.sin(2 * np.pi * 55 * h * t) / h for h in range(1, 30))
    lfo = 0.5 - 0.5 * np.cos(2 * np.pi * rate / 2 * t)        # 0..1, "wub" per half cycle
    out = np.zeros(n)
    y = 0.0
    for i in range(n):                                        # one-pole LP with moving cutoff
        a = 0.004 + 0.12 * lfo[i]
        y += a * (saw[i] - y)
        out[i] = y
    out *= np.minimum(1, t / 0.01) * np.minimum(1, (seconds - t) / 0.05)
    out = np.tanh(out / (np.max(np.abs(out)) + 1e-9) * 2.2)
    return out / np.max(np.abs(out)) * 0.9


def boom_wave(sr: int = SR, seconds: float = 1.2) -> np.ndarray:
    n = int(seconds * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(11)
    noise = rng.normal(0, 1, n)
    k = np.ones(40) / 40
    rumble = np.convolve(noise, k, "same") * 6
    thump = np.sin(2 * np.pi * np.cumsum(120 * np.exp(-t / 0.15) + 35) / sr) * 1.2
    x = (rumble + thump) * np.exp(-t / 0.35) * np.minimum(1, t / 0.004)
    x = np.tanh(x * 1.5)
    return x / np.max(np.abs(x)) * 0.95


def siren_wave(sr: int = SR, seconds: float = 1.4) -> np.ndarray:
    """MLG "wow wow" siren whoop."""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    f = 700 + 500 * (0.5 - 0.5 * np.cos(2 * np.pi * 2.2 * t))
    ph = 2 * np.pi * np.cumsum(f) / sr
    x = np.sign(np.sin(ph)) * 0.5 + np.sin(ph) * 0.5
    x *= np.minimum(1, t / 0.02) * np.minimum(1, (seconds - t) / 0.08)
    return np.convolve(x, np.ones(6) / 6, "same") * 0.6


VOICE_LINES = ["wow", "oh my god", "get rekt", "mom, get the camera", "damn son", "ez", "skrrt skrrt",
               "no scope", "wombo combo", "sick"]


# ------------------------------------------------------------------ assets
class Assets:
    """Generates the PNGs/WAVs for one render size into ``folder`` on first use."""

    def __init__(self, folder: str, W: int, H: int, airhorn_sound: str = "", triple_sound: str = "") -> None:
        self.dir, self.W, self.H = folder, W, H
        self.user_airhorn, self.user_triple = airhorn_sound, triple_sound
        self._cache: Dict[str, Optional[str]] = {}
        os.makedirs(folder, exist_ok=True)

    def _img(self, key: str, make) -> str:
        if key not in self._cache:
            path = os.path.join(self.dir, f"mlg_{len(self._cache):03d}.png")
            make().save(path)
            self._cache[key] = path
        return self._cache[key]  # type: ignore[return-value]

    def size(self, path: str) -> Tuple[int, int]:
        with Image.open(path) as im:
            return im.size

    def can(self) -> str:
        return self._img("can", lambda: soda_can(int(self.H * 0.22)))

    def horn(self) -> str:
        return self._img("horn", lambda: air_horn(int(self.H * 0.55)))

    def hit(self, scale: float = 1.0) -> str:
        return self._img(f"hit{scale}", lambda: hitmarker(int(self.H * 0.09 * scale)))

    def bill(self) -> str:
        return self._img("bill", lambda: dollar_bill(int(self.H * 0.11)))

    def eye(self) -> str:
        return self._img("eye", lambda: illuminati(int(self.H * 0.42)))

    def shades(self) -> str:
        return self._img("shades", lambda: pixel_shades(int(self.H * 0.42)))

    def tile(self) -> str:
        return self._img("tile", lambda: oh_screen(self.W, self.H))

    def boom(self) -> str:
        return self._img("boom", lambda: explosion(int(self.H * 0.5)))

    def chip(self) -> str:
        return self._img("chip", lambda: nacho_chip(int(self.H * 0.14)))

    def scope(self) -> str:
        return self._img("scope", lambda: scope(self.W, self.H))

    def smoke(self) -> str:
        return self._img("smoke", lambda: rainbow_smoke(int(self.H * 0.45)))

    def text(self, word: str, style: str, height: float = 0.11, kind: str = "impact") -> str:
        return self._img(f"t:{word}:{style}:{height}:{kind}", lambda: meme_text(word, int(self.H * height), style, kind))

    # sounds
    def _snd(self, key: str, make) -> Optional[str]:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def _synth(self, name: str, fn) -> str:
        return self._snd("snd:" + name, lambda: _write_wav(os.path.join(self.dir, name + ".wav"), fn()))  # type: ignore

    def airhorn_wav(self) -> str:
        if self.user_airhorn and os.path.isfile(self.user_airhorn):
            return self.user_airhorn
        return bundled("airhorn.wav") or self._synth("airhorn", airhorn_wave)   # real recording (CC0)

    def logo(self) -> Optional[str]:
        if not bundled("mtn_dew_logo.png"):
            return None
        return self._img("logo", lambda: mtn_dew_logo(int(self.W * 0.30)))

    def hit_wav(self) -> str:
        return self._synth("hit", hit_wave)

    def wub_wav(self) -> str:
        return self._synth("wub", wub_wave)

    def boom_wav(self) -> str:
        return self._synth("boom", boom_wave)

    def siren_wav(self) -> str:
        return self._synth("siren", siren_wave)

    def voice(self, line: str) -> Optional[str]:
        import hashlib
        name = "voice_" + hashlib.md5(line.encode("utf-8")).hexdigest()[:10]
        return self._snd("v:" + line, lambda: speak(line, os.path.join(self.dir, name)))

    def triple_wav(self, text: str = "Oh baby, a triple!") -> Optional[str]:
        if self.user_triple and os.path.isfile(self.user_triple):
            return self.user_triple
        return self.voice(text)


# every MLG switch: option name -> (label, default, tooltip)
TOGGLES: Dict[str, Tuple[str, bool, str]] = {
    "mlg_hitmarkers": ("Hitmarkers", True, "White X + tick sound on every kill"),
    "mlg_airhorn": ("Air horns", True, "Horn graphic + BWAAA (see the chance slider)"),
    "mlg_cans": ("Mountain Dew cans", True, "Cans thrown across the screen"),
    "mlg_chips": ("Nacho chips", True, "Chips thrown with the cans"),
    "mlg_logo": ("Mtn Dew logo", True, "The Mtn Dew logo spins across the screen on multi-kills and triples"),
    "mlg_text": ("Meme text", True, "OH, WOW, GET REKT... (your own words below)"),
    "mlg_voices": ("Voice lines", False, "The computer's voice says wow / oh my god / get rekt on kills"),
    "mlg_extras": ("Money rain", True, "Dollar bills rain on multi-kills and triples"),
    "mlg_illuminati": ("Illuminati", True, "The eye on triples"),
    "mlg_shades": ("Pixel shades", True, "Deal-with-it glasses drop in on triples"),
    "mlg_triple": ("OH BABY A TRIPLE", True, "Voice + text + OHHHH screen on 3 kills in a few seconds"),
    "mlg_ohscreen": ("OHHHH screen", True, "Scrolling striped OHHHHHH covering the screen on triples"),
    "mlg_hitflood": ("Hitmarker flood", True, "Dozens of hitmarkers on triples"),
    "mlg_explosions": ("Explosions", True, "Fireball + boom on explosive kills and multi-kills"),
    "mlg_scope": ("Sniper scope", True, "Scope overlay snaps on just before headshots"),
    "mlg_smoke": ("Rainbow smoke", False, "Rainbow clouds drifting on triples"),
    "mlg_spin": ("360 spin", True, "The whole picture does a 360 on triples"),
    "mlg_shake": ("Screen shake", True, "Shake on triples and explosions"),
    "mlg_wub": ("Dubstep wub", True, "Wobble bass on multi-kills"),
    "mlg_siren": ("Siren", False, "Wee-woo siren on triples"),
    "mlg_bassboost": ("Bass boost", True, "Distorted, bass-boosted audio during triples"),
}


def defaults() -> Dict[str, object]:
    """All MLG settings with their default values (also used by the GUI)."""
    d: Dict[str, object] = {k: v[1] for k, v in TOGGLES.items()}
    d.update(mlg=False, mlg_intensity="normal", mlg_strobe="normal", mlg_deepfry="triples", mlg_seed=0,
             mlg_airhorn_chance=1.0, mlg_sfx_volume=1.0, mlg_words="", mlg_triple_text=TRIPLE_TEXT,
             mlg_triple_sound="", mlg_airhorn_sound="")
    return d


DEEPFRY = ("off", "triples", "always")


# ------------------------------------------------------------------ planning
@dataclass
class Sticker:
    png: str
    x: str                  # ffmpeg overlay expressions (t = clip time; w/h = sticker size)
    y: str
    a: float                # shown from/to (clip time)
    b: float
    rot: str = ""           # rotate filter angle expression (sticker-local t), "" = none
    prio: int = 5           # lower = kept first when there are too many
    layer: int = 3          # drawing order (0 = bottom)


@dataclass
class Plan:
    stickers: List[Sticker] = field(default_factory=list)
    sfx: List[Tuple[float, str, float]] = field(default_factory=list)    # (clip time, file, gain)
    vf: str = ""                                                         # filters for the main video chain
    af: str = ""                                                         # filters after the sound mix
    vf_post: str = ""                                                    # filters on the finished picture
    triples: List[float] = field(default_factory=list)


_COUNTS = {"mild": dict(cans=1, words=1, bills=5, hits=8, max_stickers=24, max_sfx=10),
           "normal": dict(cans=2, words=1, bills=9, hits=14, max_stickers=40, max_sfx=16),
           "max": dict(cans=4, words=2, bills=14, hits=24, max_stickers=60, max_sfx=24)}


def find_triples(kills: Sequence[Tuple[float, int]], window: float = 4.0, gap: float = 4.0) -> List[float]:
    """Times where a 3rd kill lands within ``window`` seconds (multi-kill x3 counts at once)."""
    out: List[float] = []
    ks = sorted(kills)
    for i, (t, _) in enumerate(ks):
        n = sum(c for tt, c in ks[:i + 1] if t - window <= tt <= t)
        if n >= 3 and (not out or t - out[-1] >= gap):
            out.append(t)
    return out


def _b(a: float, b: float) -> str:
    return f"between(t\\,{a:.3f}\\,{b:.3f})"


def plan(kills: Sequence[Tuple[float, int]], dur: float, W: int, H: int, o, assets: Assets,
         rng: random.Random, triples: Optional[Sequence[float]] = None,
         captions: Sequence[Tuple[float, str]] = ()) -> Plan:
    """Stickers, sounds and filters for one clip. ``kills``/``captions`` are in clip time."""
    D = defaults()
    on = lambda k: bool(getattr(o, k, D.get(k)))  # noqa: E731
    pl = Plan()
    lvl = _COUNTS.get(getattr(o, "mlg_intensity", "normal"), _COUNTS["normal"])
    vol = float(getattr(o, "mlg_sfx_volume", 1.0))
    horn_p = float(getattr(o, "mlg_airhorn_chance", 1.0))
    words = [w.strip() for w in str(getattr(o, "mlg_words", "") or "").split(",") if w.strip()] or WORDS
    ttext = str(getattr(o, "mlg_triple_text", "") or TRIPLE_TEXT)
    ks = [(t, c) for t, c in sorted(kills) if 0 <= t <= dur]
    trip = [t for t in (find_triples(ks) if triples is None else triples) if 0 <= t <= dur] \
        if on("mlg_triple") else []
    pl.triples = list(trip)
    caps = [(t, c.upper()) for t, c in captions if 0 <= t <= dur]

    def cap_near(t: float, word: str) -> bool:
        return any(abs(ct - t) < 0.3 and word in c for ct, c in caps)

    cx = W * (2 / 3 if getattr(o, "vertical", False) else 0.5)
    cy = H / 2
    strobe: List[Tuple[float, float]] = []
    flashes: List[float] = []
    fry: List[Tuple[float, float]] = []
    shake: List[Tuple[float, float]] = []
    spins: List[Tuple[float, float]] = []
    bass: List[Tuple[float, float]] = []
    last_horn = -9.0

    def until(t: float, s: float) -> float:
        return min(dur, t + s)

    def snd(t: float, f: Optional[str], g: float, prio: int = 5) -> None:
        if f and 0 <= t < dur:
            pl.sfx.append((t, f, g * vol, prio))  # type: ignore[arg-type]

    for t, c in ks:
        headshot = cap_near(t, "HEADSHOT")
        boom = cap_near(t, "EXPLOSIVE") or cap_near(t, "C4") or cap_near(t, "GRENADE") or c >= 2
        if on("mlg_scope") and headshot:
            a = max(0.0, t - 0.45)
            pl.stickers.append(Sticker(assets.scope(), "0", "0", a, until(t, 0.08), prio=1, layer=0))
            pl.stickers.append(Sticker(assets.text("HEADSHOT", "red", 0.12), f"{W}/2-w/2", f"{H * 0.78:.0f}",
                                       t, until(t, 1.0), rot="0.05*sin(t*30)", prio=2, layer=5))
        if on("mlg_hitmarkers"):
            pl.stickers.append(Sticker(assets.hit(), f"{cx:.0f}-w/2", f"{cy:.0f}-h/2", t, until(t, 0.28),
                                       prio=0, layer=6))
            snd(t, assets.hit_wav(), 0.9, 1)
        if on("mlg_airhorn") and t - last_horn > 0.45 and (c >= 2 or t in trip or rng.random() < horn_p):
            hp = assets.horn()
            hw, hh = assets.size(hp)
            left = rng.random() < 0.5
            x0 = int(W * rng.uniform(0.0, 0.1)) if left else int(rng.uniform(W * 0.9, W) - hw)
            y0 = int(H * rng.uniform(0.05, 0.25))
            # pops in from below, wobbles while honking
            pl.stickers.append(Sticker(hp, f"{x0}+{max(3, W // 160)}*sin(t*70)",
                                       f"{y0}+max(0\\,1-(t-{t:.3f})/0.12)*{H}", t, until(t, 1.5),
                                       rot="0.06*sin(t*40)", prio=1, layer=4))
            snd(t, assets.airhorn_wav(), 0.75, 2)
            last_horn = t
        if on("mlg_explosions") and boom:
            bp = assets.boom()
            bw, bh = assets.size(bp)
            x = rng.uniform(W * 0.15, W * 0.85) - bw / 2
            y = rng.uniform(H * 0.3, H * 0.7) - bh / 2
            pl.stickers.append(Sticker(bp, f"{x:.0f}", f"{y:.0f}", t, until(t, 0.7), rot="0.3*t", prio=3, layer=3))
            snd(t, assets.boom_wav(), 0.8, 3)
            shake.append((t, until(t, 0.4)))
        for _ in range(lvl["cans"] + (1 if c >= 2 else 0)):
            if on("mlg_cans"):
                _flying(pl, assets.can(), assets, W, H, t + rng.uniform(0, 0.25), dur, rng, prio=3)
            if on("mlg_chips") and rng.random() < 0.6:
                _flying(pl, assets.chip(), assets, W, H, t + rng.uniform(0, 0.35), dur, rng, prio=4)
        if on("mlg_text"):
            for _ in range(lvl["words"] + (1 if c >= 2 else 0)):
                _word(pl, assets, W, H, t + rng.uniform(0.0, 0.3), dur, rng, words)
        if on("mlg_voices") and t not in trip and rng.random() < 0.6:
            snd(t + 0.25, assets.voice(rng.choice(VOICE_LINES)), 1.0, 4)
        if c >= 2 and on("mlg_logo") and assets.logo():
            _flying(pl, assets.logo(), assets, W, H, t + 0.2, dur, rng, prio=2)  # type: ignore[arg-type]
        if c >= 2 and t not in trip:
            if on("mlg_extras"):
                _money(pl, assets, W, H, t, dur, rng, lvl["bills"] // 2)
            if on("mlg_wub"):
                snd(t + 0.1, assets.wub_wav(), 0.7, 4)
        strobe.append((t, until(t, 0.55)))
        flashes.append(t)
        if getattr(o, "mlg_deepfry", "triples") == "always":
            fry.append((t, until(t, 0.8)))

    for t in trip:
        if on("mlg_ohscreen"):
            tp = assets.tile()
            tw, _ = assets.size(tp)
            span = max(0.5, until(t, 1.6) - t)
            pl.stickers.append(Sticker(tp, f"-(t-{t:.3f})/{span:.3f}*{tw - W}", "0", t, until(t, 1.6),
                                       prio=1, layer=0))
        if on("mlg_smoke"):
            for i in range(3):
                sp_ = assets.smoke()
                sw, sh = assets.size(sp_)
                y = H * (0.15 + 0.3 * i)
                pl.stickers.append(Sticker(sp_, f"-w+(t-{t:.3f})*{W * 0.5:.0f}+{i * W * 0.3:.0f}", f"{y:.0f}",
                                           t, until(t, 2.6), prio=6, layer=1))
        big = assets.text(ttext, "rainbow", 0.15)
        pl.stickers.append(Sticker(big, f"{W}/2-w/2+{W // 90}*sin(t*31)", f"{H * 0.62:.0f}-h/2", t + 0.1,
                                   until(t, 2.6), rot="0.07*sin(t*9)", prio=0, layer=5))
        if on("mlg_illuminati"):
            ep = assets.eye()
            pl.stickers.append(Sticker(ep, f"{W}/2-w/2", f"{H * 0.04:.0f}+{H // 60}*sin(t*6)", t + 0.4,
                                       until(t, 2.6), rot="0.12*sin(t*5)", prio=2, layer=4))
        if on("mlg_shades"):
            sp = assets.shades()
            land = int(H * 0.30)
            pl.stickers.append(Sticker(sp, f"{W}/2-w/2", f"min({land}\\,-h+(t-{t + 0.3:.3f})*{H * 1.4:.0f})",
                                       t + 0.3, until(t, 2.6), prio=2, layer=4))
        if on("mlg_extras"):
            _money(pl, assets, W, H, t, dur, rng, lvl["bills"])
        if on("mlg_hitflood"):
            for i in range(lvl["hits"]):
                a = t + rng.uniform(0, 1.2)
                if a >= dur:
                    continue
                hp = assets.hit(rng.choice((0.7, 1.0, 1.5)))
                x, y = rng.uniform(0, W), rng.uniform(0, H)
                pl.stickers.append(Sticker(hp, f"{x:.0f}-w/2", f"{y:.0f}-h/2", a, until(a, 0.25), prio=5, layer=6))
                if i % 3 == 0:
                    snd(a, assets.hit_wav(), 0.6, 6)
        if on("mlg_logo") and assets.logo():
            _flying(pl, assets.logo(), assets, W, H, t + 0.6, dur, rng, prio=2)  # type: ignore[arg-type]
        for _ in range(lvl["cans"] + 1):
            if on("mlg_cans"):
                _flying(pl, assets.can(), assets, W, H, t + rng.uniform(0.2, 1.2), dur, rng, prio=3)
            if on("mlg_chips"):
                _flying(pl, assets.chip(), assets, W, H, t + rng.uniform(0.2, 1.2), dur, rng, prio=4)
        snd(t + 0.15, assets.triple_wav(ttext.capitalize().replace("Oh baby a", "Oh baby, a") + "!"), 1.0, 0)
        if on("mlg_airhorn"):
            snd(t + 1.2, assets.airhorn_wav(), 0.6, 2)
        if on("mlg_siren"):
            snd(t + 0.4, assets.siren_wav(), 0.5, 4)
        if on("mlg_wub"):
            snd(t + 0.9, assets.wub_wav(), 0.6, 5)
        if on("mlg_spin"):
            spins.append((t + 0.05, until(t, 0.75)))
        if on("mlg_shake"):
            shake.append((t, until(t, 1.6)))
        if on("mlg_bassboost"):
            bass.append((t, until(t, 2.4)))
        strobe.append((t, until(t, 2.4)))
        if getattr(o, "mlg_deepfry", "triples") != "off":
            fry.append((t, until(t, 2.4)))

    pl.stickers.sort(key=lambda s: s.prio)
    pl.stickers = pl.stickers[:lvl["max_stickers"]]
    pl.stickers.sort(key=lambda s: s.layer)
    sfx = sorted(pl.sfx, key=lambda s: s[3])[:lvl["max_sfx"]]           # type: ignore[index]
    pl.sfx = sorted(((t, f, g) for t, f, g, _ in sfx), key=lambda s: s[0])  # type: ignore[misc]
    pl.vf = strobe_filters(getattr(o, "mlg_strobe", "normal"), strobe, flashes, fry)
    pl.vf_post = motion_filters(W, H, spins, shake)          # spin/shake everything, stickers included
    if bass:
        en = "+".join(_b(a, b) for a, b in bass)
        pl.af = f",bass=g=14:f=90:enable='{en}',volume=1.6:enable='{en}',asoftclip=type=hard:enable='{en}'"
    return pl


def motion_filters(W: int, H: int, spins: Sequence[Tuple[float, float]],
                   shake: Sequence[Tuple[float, float]]) -> str:
    vf = ""
    if spins:
        ang = "+".join(f"{_b(a, b)}*2*PI*(t-{a:.3f})/{max(0.1, b - a):.3f}" for a, b in spins)
        vf += f",rotate=a='{ang}':c=black"
    if shake:
        # shaken copy shown only during the windows, so the rest of the clip isn't cropped
        on = "+".join(_b(a, b) for a, b in shake)
        sh = max(6, int(H * 0.025))
        vf += (f",split[msa][msb];[msb]crop={W - 2 * sh}:{H - 2 * sh}:x='{sh}+{sh}*sin(t*83)':"
               f"y='{sh}+{sh}*cos(t*67)',scale={W}:{H},setsar=1[msc];[msa][msc]overlay=0:0:enable='{on}'")
    return vf


def _flying(pl: Plan, png: str, assets: Assets, W: int, H: int, t: float, dur: float, rng: random.Random,
            prio: int = 3) -> None:
    """A sticker thrown across the screen in an arc, spinning."""
    if t >= dur:
        return
    w, h = assets.size(png)
    life = rng.uniform(1.1, 1.7)
    left = rng.random() < 0.5
    x0 = -w if left else W
    vx = (W + w) / life * rng.uniform(0.7, 1.0) * (1 if left else -1)
    y0 = H * rng.uniform(0.45, 0.85)
    vy = -H * rng.uniform(0.9, 1.4)
    g = H * 1.6
    spin = rng.uniform(4, 9) * (1 if rng.random() < 0.5 else -1)
    tt = f"(t-{t:.3f})"
    pl.stickers.append(Sticker(png, f"{x0:.0f}+{vx:.1f}*{tt}", f"{y0:.0f}+{vy:.1f}*{tt}+{g / 2:.1f}*{tt}*{tt}",
                               t, min(dur, t + life), rot=f"{spin:.2f}*t", prio=prio, layer=1))


def _word(pl: Plan, assets: Assets, W: int, H: int, t: float, dur: float, rng: random.Random,
          words: Sequence[str] = WORDS) -> None:
    if t >= dur:
        return
    word = rng.choice(list(words))
    png = assets.text(word, rng.choice(TEXT_STYLES), rng.choice((0.09, 0.11, 0.14)),
                      rng.choice(("impact", "comic")))
    w, h = assets.size(png)
    x = int(rng.uniform(0, max(1, W - w)))
    y = int(rng.uniform(H * 0.05, max(H * 0.06, H * 0.85 - h)))
    tilt = rng.uniform(-0.25, 0.25)
    pl.stickers.append(Sticker(png, f"{x}", f"{y}", t, min(dur, t + rng.uniform(0.9, 1.5)),
                               rot=f"{tilt:.3f}+0.05*sin(t*25)", prio=5, layer=2))


def _money(pl: Plan, assets: Assets, W: int, H: int, t: float, dur: float, rng: random.Random, n: int) -> None:
    png = assets.bill()
    w, h = assets.size(png)
    for _ in range(n):
        a = t + rng.uniform(0, 0.8)
        if a >= dur:
            continue
        x = rng.uniform(-w * 0.3, W - w * 0.7)
        sp = H * rng.uniform(0.5, 0.9)
        pl.stickers.append(Sticker(png, f"{x:.0f}+{W // 40}*sin(t*3+{rng.uniform(0, 6):.2f})",
                                   f"-h+(t-{a:.3f})*{sp:.0f}", a, min(dur, a + (H + h * 2) / sp),
                                   rot=f"{rng.uniform(-0.6, 0.6):.2f}+0.4*sin(t*{rng.uniform(2, 5):.2f})", prio=4, layer=1))


def strobe_filters(level: str, windows: Sequence[Tuple[float, float]], flashes: Sequence[float],
                   fry: Sequence[Tuple[float, float]]) -> str:
    """Hue-cycling, over-saturation, white flashes (strobe) and deep-fry during the given windows."""
    vf = ""
    if level != "off" and windows:
        on = "+".join(_b(a, b) for a, b in windows)
        w = f"min(1\\,{on})"
        vf += f",hue=h='540*t*({w})':s='1+1.6*({w})'"
        if level in ("normal", "max"):
            fl = "+".join(_b(t, t + 0.06) for t in flashes) or "0"
            vf += f",eq=contrast='1+0.35*({w})':brightness='0.45*min(1\\,{fl})':eval=frame"
        if level == "max":
            vf += f",negate=enable='({on})*lt(mod(t*8\\,1)\\,0.3)'"
    if fry:
        fon = "+".join(_b(a, b) for a, b in fry)
        vf += (f",eq=saturation=2.6:contrast=1.4:enable='{fon}',unsharp=7:7:2.5:enable='{fon}',"
               f"noise=alls=12:allf=t:enable='{fon}'")
    return vf
