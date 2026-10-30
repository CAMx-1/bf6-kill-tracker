"""Shorts batch export: one vertical 15-30 s clip per top moment, with a hook title and captions."""
from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .hype import Scored, rank
from .reel import ReelOptions, Segment, apply_preset, apply_template, build_reel, caption_for

VERTICAL = "Vertical 9:16 (TikTok / Shorts / Reels)"
WEAPON_WORDS = {"explosive_c4": "C4", "gun_smg": "SMG", "gun_pistol": "PISTOL", "gun_rifle": "RIFLE",
                "gun_m16": "M16"}


def _span(kill_times: Sequence[Tuple[float, int]]) -> float:
    ts = [t for t, _ in kill_times]
    return max(ts) - min(ts) if ts else 0.0


def hook_title(s: Scored) -> str:
    """A short, punchy line for the top of the Short ("3 KILLS IN 2 SECONDS", "TRIPLE C4"...)."""
    n = sum(c for _, c in s.kill_times) or s.kills
    span = _span(s.kill_times)
    hs = s.types.count("headshot")
    boom = s.types.count("explosive")
    w = WEAPON_WORDS.get(s.main_weapon(), "")
    if n >= 2 and any(c >= n for _, c in s.kill_times):           # all at once
        word = {2: "DOUBLE", 3: "TRIPLE", 4: "QUAD"}.get(n, f"{n}x")
        return f"{word} {w or 'KILL'}" + ("!" if n >= 3 else "")
    if n >= 3 and span <= 6:
        secs = max(1, int(round(span)))
        return f"{n} KILLS IN {secs} SECOND{'S' if secs != 1 else ''}"
    if hs >= 2:
        return f"{hs} HEADSHOTS"
    if boom >= 2:
        return f"{boom} {w or 'EXPLOSIVE'} KILLS"
    if n >= 3:
        return f"{n} KILL STREAK"
    if hs:
        return "CLEAN HEADSHOT"
    return f"{w} KILL" if w else "WATCH THIS"


def plan_shorts(candidates: Sequence[Scored], durations: Dict[str, float], count: int = 5,
                min_s: float = 15.0, max_s: float = 30.0, lead: float = 4.0, tail: float = 2.5,
                min_score: float = 0.0) -> List[Tuple[Scored, Segment]]:
    """Pick the ``count`` best moments that don't overlap and frame each as a ``min_s``-``max_s`` clip.

    The window starts ``lead`` s before the first kill (the set-up) and ends ``tail`` s after the last one;
    short moments get more set-up first, long streaks are trimmed from the start so the end stays in.
    """
    out: List[Tuple[Scored, Segment]] = []
    used: List[Tuple[str, float, float]] = []
    for s in rank([c for c in candidates if c.score >= min_score]):
        if len(out) >= count:
            break
        dur = durations.get(s.video) or (s.end + 60)
        a, b = s.start - lead, s.end + tail
        if b - a < min_s:                               # add set-up first (2/3), then aftermath
            extra = min_s - (b - a)
            a -= extra * 2 / 3
            b += extra / 3
        if b - a > max_s:
            a = b - max_s
        if a < 0:
            b, a = min(dur, b - a), 0.0
        if b > dur:
            a, b = max(0.0, a - (b - dur)), dur
        if any(v == s.video and a < e and b > st for v, st, e in used):
            continue
        used.append((s.video, a, b))
        caps = [(t, caption_for([ty], [w])) for (t, _), ty, w in zip(s.kill_times, s.types, s.weapons)]
        seg = Segment(s.video, a, b, s.title or os.path.basename(s.video), list(s.kill_times), hook_title(s), caps)
        out.append((s, seg))
    return out


def short_options(template: str = "TikTok hype", base: Optional[ReelOptions] = None) -> ReelOptions:
    o = apply_template(template, apply_preset(VERTICAL, base or ReelOptions()))
    o.vertical, o.size = True, (1080, 1920)
    o.titles, o.title_top, o.overlays, o.captions = True, True, True, True
    o.replay_finale_only = False
    o.intro_text = o.outro_text = ""                    # straight into the action
    return o


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:40] or "short"


def export_shorts(picks: Sequence[Tuple[Scored, Segment]], out_dir: str, options: ReelOptions,
                  progress: Optional[Callable[[float, str], None]] = None,
                  should_cancel: Optional[Callable[[], bool]] = None) -> List[str]:
    """Render each pick as its own vertical video in ``out_dir``. Returns the files written."""
    os.makedirs(out_dir, exist_ok=True)
    files: List[str] = []
    n = len(picks)
    for i, (s, seg) in enumerate(picks):
        stem = os.path.splitext(os.path.basename(seg.video))[0]
        out = os.path.join(out_dir, f"{_safe(stem)}_short{i + 1}_{_safe(seg.title)}.mp4")
        rep = (lambda f, m, i=i: progress((i + f) / n, f"Short {i + 1}/{n}: {m}")) if progress else None
        build_reel([seg], out, options=replace(options), progress=rep, should_cancel=should_cancel)
        files.append(out)
    if progress:
        progress(1.0, "Done")
    return files
