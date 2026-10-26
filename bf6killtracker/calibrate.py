"""Automatic HUD calibration: find the kill-skull position and size in a recording.

Different HUD scale settings, resolutions and aspect ratios move and resize
the kill notification. Before scanning, a few keyframes (cheap to decode) are
searched for skull icons at several sizes; if enough consistent skulls are
found, the HUD layout is scaled and shifted to match.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .detector import TEMPLATE_DIR, HudLayout
from .video import NO_WINDOW, find_ffmpeg, probe

SCALES = np.round(np.arange(0.70, 1.351, 0.05), 2)
# where the matched template's top-left sits relative to (slot0_x, icon_y) at scale 1 (measured)
MATCH_OFFSET = (1, -2)


@dataclass
class Calibration:
    layout: HudLayout
    scale: float = 1.0
    dx: int = 0
    dy: int = 0
    hits: int = 0           # keyframes with skulls used for the estimate
    applied: bool = False   # False: not enough evidence, or matches the default layout

    def describe(self) -> str:
        if not self.applied:
            return f"default HUD layout ({self.hits} skull frame(s) checked)"
        return f"HUD scale {self.scale:.2f}, offset ({self.dx:+d}, {self.dy:+d}) px from {self.hits} frames"


def _keyframes(path: str, ref_size: Tuple[int, int], box: Tuple[int, int, int, int], limit: int):
    """Yield keyframes (scaled to ``ref_size``, cropped to ``box``) until the caller stops."""
    ff = find_ffmpeg()
    rw, rh = ref_size
    x, y, w, h = box
    base = [ff, "-v", "error", "-nostdin", "-skip_frame", "nokey", "-i", path, "-an", "-sn",
            "-vf", f"scale={rw}:{rh},format=bgr24,crop={w}:{h}:{x}:{y}"]
    tail = ["-frames:v", str(limit), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    size = w * h * 3
    # output keyframes only, without duplicating them up to the frame rate
    # (the option name differs between ffmpeg versions)
    for sync in (["-fps_mode", "passthrough"], ["-vsync", "0"]):
        proc = subprocess.Popen(base + sync + tail, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **NO_WINDOW)
        got = 0
        try:
            assert proc.stdout is not None
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                got += 1
                yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if got or proc.returncode == 0:
            return


def calibrate(path: str, max_frames: int = 200, min_hits: int = 3, threshold: float = 0.86,
              enough: int = 12) -> Calibration:
    info = probe(path)
    base = HudLayout.for_video(info.width, info.height)
    rw, rh = base.ref_size
    # search a generous window around the default position
    x0 = max(0, base.slot0_x - 500)
    y0 = max(0, base.icon_y - 300)
    x1 = min(rw, base.slot0_x + 400)
    y1 = min(rh, base.icon_y + 260)
    tpl = cv2.imread(os.path.join(TEMPLATE_DIR, "skull.png"))[:, :, 2]
    tpls = {s: cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) for s in SCALES}

    hits: List[Tuple[float, int, int]] = []    # (scale, rightmost x, y) per frame
    for f in _keyframes(path, base.ref_size, (x0, y0, x1 - x0, y1 - y0), max_frames):
        red = f[:, :, 2]
        best = (threshold, None, None)
        for s, t in tpls.items():
            r = cv2.matchTemplate(red, t, cv2.TM_CCOEFF_NORMED)
            mx = float(r.max())
            if mx > best[0]:
                best = (mx, s, r)
        if best[1] is None:
            continue
        s, r = best[1], best[2]
        ys, xs = np.nonzero(r >= max(threshold, best[0] - 0.06))
        i = int(np.argmax(xs))                       # rightmost skull = newest slot
        hits.append((s, int(xs[i]) + x0, int(ys[i]) + y0))
        if len(hits) >= enough:
            break
    cal = Calibration(base, hits=len(hits))
    if len(hits) < min_hits:
        return cal
    # the biggest group of hits with the same size and height is the kill notification
    # (other skulls, e.g. the small multi-kill badge above it, form smaller groups)
    groups: dict = {}
    for h in hits:
        groups.setdefault((h[0], h[2] // 10), []).append(h)
    key = max(groups, key=lambda k: len(groups[k]))
    near = [h for h in hits if h[0] == key[0] and abs(h[2] - np.median([g[2] for g in groups[key]])) <= 10]
    if len(near) < min_hits:
        return cal
    s = key[0]
    my_ = int(np.median([h[2] for h in near]))
    # the rightmost skull is usually in slot 0, but not always (e.g. vehicle gear icon
    # in the slot): take a high percentile of x rather than the median
    xs_sorted = sorted(h[1] for h in near)
    mx_ = xs_sorted[min(len(xs_sorted) - 1, int(len(xs_sorted) * 0.75))]
    scaled = base.scaled(s) if abs(s - 1.0) > 0.03 else base
    dx = mx_ - MATCH_OFFSET[0] - scaled.slot0_x
    dy = my_ - MATCH_OFFSET[1] - scaled.icon_y
    if scaled is base and abs(dx) <= 4 and abs(dy) <= 4:
        return Calibration(base, 1.0, 0, 0, len(near), False)
    return Calibration(scaled.shifted(dx, dy), round(s, 2), dx, dy, len(near), True)
