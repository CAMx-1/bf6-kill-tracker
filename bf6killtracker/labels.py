"""HUD label reader: score pop-ups, medals and banners.

BF6 shows boxed labels under the kill notification ("KILL", "HEADSHOT",
"ENEMY SPOT x3", "SQUAD REVIVE", "VEHICLE DESTROYED"…), medal badges above it
("DOUBLE KILL", "TANK BUSTER", "PAYBACK"…) and banners near the top of the
screen ("LAST SQUAD MEMBER ALIVE"). Each is matched against greyscale
templates in ``templates/labels/<group>__<name>.png`` (``_xN`` variants count
N); add a crop there to teach it a new label.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .detector import TEMPLATE_DIR

LABEL_DIR = os.path.join(TEMPLATE_DIR, "labels")
# search bands (reference 2560x1440 coordinates, default HUD): x, y, w, h
BANDS: Dict[str, Tuple[int, int, int, int]] = {
    "action": (360, 950, 820, 105),
    "medal": (560, 828, 520, 76),
    "banner": (880, 360, 800, 120),
}
MATCH_SCALE = 0.5
_XN = re.compile(r"_x(\d+)$")


@dataclass
class HudEvent:
    time: float
    kind: str            # e.g. "squad_revive", "enemy_spot", "tank_buster", "last_squad_member_alive", "squad_wipe"
    group: str           # "action", "medal", "banner" or "feed"
    count: int = 1

    @property
    def label(self) -> str:
        return self.kind.replace("_", " ").upper()

    def to_dict(self):
        return asdict(self)


@dataclass
class _Tpl:
    name: str
    group: str
    base: str
    n: int
    img: np.ndarray


class LabelReader:
    def __init__(self, dx: int = 0, dy: int = 0, scale: float = 1.0, ref_w: int = 2560, threshold: float = 0.78):
        self.thr = threshold
        self.tpls: List[_Tpl] = []
        for f in sorted(os.listdir(LABEL_DIR)) if os.path.isdir(LABEL_DIR) else []:
            if not f.endswith(".png") or "__" not in f:
                continue
            img = cv2.imread(os.path.join(LABEL_DIR, f), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            # matched at half resolution: 4x faster, text is still large enough
            img = cv2.resize(img, None, fx=scale * MATCH_SCALE, fy=scale * MATCH_SCALE, interpolation=cv2.INTER_AREA)
            group, name = os.path.splitext(f)[0].split("__", 1)
            m = _XN.search(name)
            self.tpls.append(_Tpl(name, group, _XN.sub("", name), int(m.group(1)) if m else 1, img))
        cx, cy = 1280, 720
        xs = (ref_w - 2560) // 2      # ultrawide: HUD centred
        self.rois: Dict[str, Tuple[int, int, int, int]] = {}
        for g, (x, y, w, h) in BANDS.items():
            if g == "banner":          # centred on the screen, not tied to the kill notification
                nx, ny = x + xs, y
            else:
                nx = int(round(cx + (x - cx) * scale)) + dx + xs
                ny = int(round(cy + (y - cy) * scale)) + dy
            pad = 20
            self.rois[g] = (max(0, nx - pad), max(0, ny - pad), int(w * scale) + 2 * pad, int(h * scale) + 2 * pad)

    def read(self, crops: Dict[str, np.ndarray]) -> Dict[str, Tuple[str, int, float]]:
        """{base label: (template name, xN count, score)} for labels visible in this frame."""
        found: Dict[str, Tuple[str, int, float]] = {}
        grays = {g: cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), None, fx=MATCH_SCALE, fy=MATCH_SCALE,
                               interpolation=cv2.INTER_AREA) for g, c in crops.items()}
        for t in self.tpls:
            g = grays.get(t.group)
            if g is None or g.shape[0] < t.img.shape[0] or g.shape[1] < t.img.shape[1]:
                continue
            sc = float(cv2.matchTemplate(g, t.img, cv2.TM_CCOEFF_NORMED).max())
            if sc >= self.thr:
                prev = found.get(t.base)
                if prev is None or sc > prev[2]:
                    found[t.base] = (t.name, t.n, sc)
        return found


class LabelTracker:
    """One event per appearance of a label; ``_xN`` upgrades raise its count."""

    def __init__(self, confirm: int = 2, gap_s: float = 1.0):
        self.confirm = confirm
        self.gap = gap_s
        self.groups = {}
        self.state: Dict[str, dict] = {}
        self.events: List[HudEvent] = []

    def update(self, t: float, found: Dict[str, Tuple[str, int, float]], groups: Dict[str, str]) -> None:
        for base, (_, n, _) in found.items():
            s = self.state.get(base)
            if s is None or t - s["last"] > self.gap:
                s = {"first": t, "last": t, "seen": 0, "n": 1, "event": None}
                self.state[base] = s
            s["last"] = t
            s["seen"] += 1
            s["n"] = max(s["n"], n)
            if s["event"] is None and s["seen"] >= self.confirm:
                s["event"] = HudEvent(round(s["first"], 3), base, groups.get(base, "action"), s["n"])
                self.events.append(s["event"])
            elif s["event"] is not None:
                s["event"].count = max(s["event"].count, s["n"])

    def finish(self) -> List[HudEvent]:
        # an "action" label's later xN upgrade means N of that action in total
        self.events.sort(key=lambda e: e.time)
        return self.events


def group_of(reader: LabelReader) -> Dict[str, str]:
    return {t.base: t.group for t in reader.tpls}


def summarize(events: List[HudEvent]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in events:
        out[e.kind] = out.get(e.kind, 0) + e.count
    return out
