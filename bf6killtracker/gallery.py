"""Template gallery: a small still + looping GIF of every template, rendered once and cached."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

from .reel import TEMPLATES, ReelOptions, Segment, apply_template, build_reel, demo_segment
from .video import NO_WINDOW, find_ffmpeg

THUMB_W, THUMB_H = 384, 216
GALLERY_VERSION = 3          # bump when the look of the thumbnails changes


def gallery_dir() -> str:
    from .mlg import my_sounds_dir
    return os.path.join(os.path.dirname(my_sounds_dir()), "gallery")


def gallery_segment(seg: Optional[Segment] = None) -> Segment:
    """3 s around the busiest moment of ``seg`` (default: the demo clip's triple)."""
    if seg is None:
        import tempfile
        d = demo_segment(os.path.join(tempfile.gettempdir(), "bf6kt_preview"))
        a, b = 0.3, 4.3                      # the demo's kill, headshot and triple
        return Segment(d.video, a, b, "Demo", [k for k in d.kills if a <= k[0] <= b], "Preview",
                       [c for c in d.captions if a <= c[0] <= b])
    from .mlg import find_triples
    t = (find_triples(seg.kills) or [max(seg.kills, key=lambda k: (k[1], k[0]))[0] if seg.kills else seg.start])[0]
    a = max(seg.start, t - 3.0)
    b = min(seg.end, t + 1.4)
    return Segment(seg.video, a, b, seg.label, [k for k in seg.kills if a <= k[0] <= b], seg.title,
                   [c for c in seg.captions if a <= c[0] <= b])


def _key(name: str, seg: Segment) -> str:
    try:
        st = os.stat(seg.video)
        src = f"{os.path.abspath(seg.video)}|{st.st_size}|{seg.start:.2f}|{seg.end:.2f}"
    except OSError:
        src = seg.video
    t = json.dumps(TEMPLATES.get(name, {}), sort_keys=True, default=str)
    return hashlib.md5(f"{GALLERY_VERSION}|{name}|{t}|{src}".encode()).hexdigest()[:16]


def thumb_paths(name: str, seg: Segment, folder: Optional[str] = None) -> Tuple[str, str]:
    d = folder or gallery_dir()
    k = _key(name, seg)
    return os.path.join(d, f"{k}.jpg"), os.path.join(d, f"{k}.gif")


def render_thumb(name: str, seg: Segment, folder: Optional[str] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> Tuple[str, str]:
    """Render ``name`` on ``seg`` and keep a still (.jpg) and a looping preview (.gif). Cached."""
    jpg, gif = thumb_paths(name, seg, folder)
    if os.path.isfile(jpg) and os.path.isfile(gif):
        return jpg, gif
    os.makedirs(os.path.dirname(jpg), exist_ok=True)
    o = apply_template(name, ReelOptions())
    vertical = o.vertical
    o = replace(o, crf=30, preset="ultrafast", encoder="libx264", fps=24.0, target_mb=None, beat_sync=False,
                replay=False, intro_text="", outro_text="", fade=0.0, mlg_voices=False, loudnorm=False,
                size=(THUMB_H // 2 * 2, THUMB_W) if vertical else (THUMB_W, THUMB_H))
    mp4 = jpg[:-4] + ".mp4"
    build_reel([seg], mp4, options=o, should_cancel=should_cancel)
    ff = find_ffmpeg()
    peak = min(seg.duration - 0.1, max(0.1, (seg.kills[-1][0] - seg.start + 1.0) if seg.kills else 1.0))
    subprocess.run([ff, "-v", "error", "-y", "-ss", f"{peak:.2f}", "-i", mp4, "-frames:v", "1", "-q:v", "3",
                    "-update", "1", jpg], check=True, timeout=60, stdin=subprocess.DEVNULL, **NO_WINDOW)
    subprocess.run([ff, "-v", "error", "-y", "-i", mp4, "-vf",
                    "fps=10,scale=288:-2,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer",
                    "-loop", "0", gif], check=True, timeout=120, stdin=subprocess.DEVNULL, **NO_WINDOW)
    try:
        os.remove(mp4)
    except OSError:
        pass
    return jpg, gif


def render_all(names: Optional[List[str]] = None, seg: Optional[Segment] = None, folder: Optional[str] = None,
               progress: Optional[Callable[[int, int, str, Tuple[str, str]], None]] = None,
               should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Tuple[str, str]]:
    """Thumbnails for every template (already-cached ones are instant)."""
    names = list(names or TEMPLATES)
    seg = gallery_segment(seg)
    out: Dict[str, Tuple[str, str]] = {}
    for i, n in enumerate(names):
        if should_cancel and should_cancel():
            break
        try:
            out[n] = render_thumb(n, seg, folder, should_cancel)
        except Exception:
            continue
        if progress:
            progress(i + 1, len(names), n, out[n])
    return out
