"""Kill-feed reader (top-right of the screen).

The local player's name in the kill feed is always preceded by a small white
"▸" marker. A row where the marker is on the *killer* side (left) is one of
your kills; a row where it is on the *victim* side (killer name in red to the
left of it) is a death. Rows appear at the bottom of the feed and scroll up.

Per row we also read:
* whether it was a headshot (red skull icon between the weapon and the victim);
* the weapon icon, matched against ``templates/weapons/<category>_<name>.png``
  (e.g. ``gun_rifle``, ``explosive_c4``); unknown icons are ``other``. Add a
  cropped icon there to teach it a new weapon (knife, launcher, vehicle...);
* squad-wipe rows (red "XX" icon, no weapon) are ignored.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .detector import TEMPLATE_DIR

# reference coordinates (2560x1440)
FEED_ROI = (1700, 60, 860, 400)   # x, y, w, h
MARKER_FILE = "feed_marker.png"


@dataclass
class FeedRow:
    x: int          # marker position (reference coordinates)
    y: int
    kind: str       # "kill" or "death"
    headshot: bool = False
    weapon: str = ""  # template name, e.g. "gun_rifle", "explosive_c4", or "other"
    sig: Optional[np.ndarray] = field(default=None, repr=False, compare=False)  # picture of the other name


@dataclass
class FeedEvent:
    time: float
    kind: str               # "kill" or "death"
    headshot: bool = False
    weapon: str = ""        # e.g. "gun_rifle", "explosive_c4", "other"

    @property
    def category(self) -> str:
        """Kill type: "headshot", "explosive", "gun" or "other"."""
        if self.headshot:
            return "headshot"
        return self.weapon.split("_")[0] if self.weapon else "other"

    def to_dict(self):
        d = asdict(self)
        d["category"] = self.category
        return d


def _runs(cols: np.ndarray, min_gap: int) -> List[Tuple[int, int]]:
    """Contiguous True runs in a 1-D bool array, merging gaps shorter than ``min_gap``."""
    idx = np.flatnonzero(cols)
    if not len(idx):
        return []
    runs = [[int(idx[0]), int(idx[0])]]
    for i in idx[1:]:
        if i - runs[-1][1] <= min_gap:
            runs[-1][1] = int(i)
        else:
            runs.append([int(i), int(i)])
    return [(a, b + 1) for a, b in runs]


class FeedReader:
    def __init__(self, roi: Tuple[int, int, int, int] = FEED_ROI, marker_threshold: float = 0.75):
        self.roi = roi
        # weapon icons: templates/weapons/<category>_<name>.png
        wdir = os.path.join(TEMPLATE_DIR, "weapons")
        self.weapons: List[Tuple[str, np.ndarray]] = []
        for f in sorted(os.listdir(wdir)) if os.path.isdir(wdir) else []:
            img = cv2.imread(os.path.join(wdir, f), cv2.IMREAD_COLOR)
            if img is not None:
                self.weapons.append((os.path.splitext(f)[0], img.max(axis=2)))
        self.thr = marker_threshold
        tpl = cv2.imread(os.path.join(TEMPLATE_DIR, MARKER_FILE), cv2.IMREAD_COLOR)
        if tpl is None:
            raise FileNotFoundError(MARKER_FILE)
        self.tpl = tpl.min(axis=2)        # white: bright in every channel
        self.th, self.tw = self.tpl.shape

    @staticmethod
    def _masks(band: np.ndarray):
        b, g, r = (band[:, :, i].astype(np.int16) for i in range(3))
        green = (g > 140) & (g - r > 20) & (g - b > 10)
        red = (r > 140) & (r - g > 50) & (r - b > 50)
        return green, red

    @staticmethod
    def _name_sig(red: np.ndarray) -> np.ndarray:
        """Normalised picture of the other player's (red) name, starting at its first letter."""
        cols = np.flatnonzero(red.sum(axis=0) >= 2)
        if not len(cols):
            return np.zeros(0, np.float32)
        m = red[:, cols[0]:cols[0] + 300].astype(np.float32)
        m = np.pad(m, ((0, 0), (0, 300 - m.shape[1])))
        rows = np.flatnonzero(m.any(axis=1))
        m = m[rows[0]:rows[-1] + 1]
        v = cv2.resize(m, (150, 10), interpolation=cv2.INTER_AREA).ravel()
        v = v - v.mean()
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v

    def _weapon(self, zone: np.ndarray, min_score: float = 0.6) -> str:
        """Best-matching weapon template name, or "other" (e.g. knife, vehicle, unknown gadget)."""
        if zone.size == 0:
            return "other"
        lum = zone.max(axis=2)
        best, best_name = min_score, "other"
        for name, tpl in self.weapons:
            th, tw = tpl.shape
            if lum.shape[0] < th or lum.shape[1] < tw:
                pad = cv2.copyMakeBorder(lum, 0, max(0, th - lum.shape[0]), 0, max(0, tw - lum.shape[1]),
                                         cv2.BORDER_REPLICATE)
            else:
                pad = lum
            sc = float(cv2.matchTemplate(pad, tpl, cv2.TM_CCOEFF_NORMED).max())
            if sc > best:
                best, best_name = sc, name
        return best_name

    def read(self, roi_bgr: np.ndarray) -> List[FeedRow]:
        rx, ry, _, _ = self.roi
        white = roi_bgr.min(axis=2)
        res = cv2.matchTemplate(white, self.tpl, cv2.TM_CCOEFF_NORMED)
        rows: List[FeedRow] = []
        H, W = white.shape
        while True:
            _, mx, _, (x, y) = cv2.minMaxLoc(res)
            if mx < self.thr:
                break
            res[max(0, y - 15):y + 15, max(0, x - 15):x + 15] = -1
            y0, y1 = max(0, y - 6), min(H, y + self.th + 6)
            right = roi_bgr[y0:y1, x + self.tw + 2:min(W, x + self.tw + 520)]
            left = roi_bgr[y0:y1, max(0, x - 320):max(0, x - 4)]
            if right.shape[1] < 60:
                continue
            g_r, r_r = self._masks(right)
            gcols = g_r.sum(axis=0) >= 2
            # the player's own name (green) must follow the marker directly
            if gcols[:50].sum() < 12:
                continue
            green_runs = _runs(gcols, 7)
            rcols = r_r.sum(axis=0) >= 2
            red_runs = [rr for rr in _runs(rcols, 4) if rr[1] - rr[0] >= 6]
            if not red_runs:
                # nothing after your name: you are the victim, the killer is on the left
                _, r_l = self._masks(left) if left.size else (None, np.zeros((1, 1), bool))
                if (r_l.sum(axis=0) >= 2).sum() >= 15:
                    rows.append(FeedRow(x + rx, y + ry, "death", sig=self._name_sig(r_l[:, ::-1])))
                continue
            if len(green_runs) < 2 or red_runs[0][0] < green_runs[1][0]:
                continue   # no weapon icon (squad wipe etc.)
            name_end = green_runs[0][1]
            first_red = red_runs[0]
            # headshot: a red skull icon (taller than the text) right before the victim's name
            ys = np.flatnonzero(r_r[:, first_red[0]:first_red[1]].any(axis=1))
            hs = len(red_runs) >= 2 and len(ys) > 0 and (ys[-1] - ys[0] + 1) >= 24 \
                and first_red[1] - first_red[0] <= 45
            zone = right[:, name_end + 6:max(name_end + 7, first_red[0] - 4)]
            if g_r[:, name_end + 6:max(name_end + 7, first_red[0] - 4)].sum() < 25:
                # no green weapon icon: red "XX" + squad name = you wiped a squad (not an extra kill)
                if first_red[1] - first_red[0] <= 45 and len(red_runs) >= 2:
                    rows.append(FeedRow(x + rx, y + ry, "wipe"))
                continue
            victim = r_r[:, red_runs[1][0] if hs else first_red[0]:]
            rows.append(FeedRow(x + rx, y + ry, "kill", hs, self._weapon(zone), self._name_sig(victim)))
        rows.sort(key=lambda r: r.y)
        return rows


@dataclass
class _Track:
    row: FeedRow
    first: float
    last: float
    seen: int = 1
    hs_votes: int = 0
    weapons: List[str] = field(default_factory=list)
    reported: bool = False
    sigs: List[np.ndarray] = field(default_factory=list)

    def signature(self) -> Optional[np.ndarray]:
        if not self.sigs:
            return None
        v = np.mean(self.sigs[len(self.sigs) // 2:], axis=0)   # later sightings: row has settled
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None


class FeedTracker:
    """Turns per-frame feed rows into one event per new row.

    A row keeps its x position (it's right-aligned and its width depends on
    the names) and only ever moves *up* as newer rows arrive at the bottom.
    """

    def __init__(self, sample_dt: float, confirm: int = 3, lost_after_s: float = 1.0, revive_s: float = 3.0,
                 dup_window_s: float = 1.0, dup_sim: float = 0.88):
        self.dup_window = dup_window_s
        self.dup_sim = dup_sim
        self.dt = sample_dt
        self.confirm = confirm
        self.lost_after = lost_after_s
        self.revive = revive_s
        self.tracks: List[_Track] = []
        self.dead: List[_Track] = []
        self._reported: List[_Track] = []

    @staticmethod
    def _match(prev: FeedRow, cur: FeedRow) -> bool:
        # (x jitters a little while a row slides in)
        return prev.kind == cur.kind and abs(prev.x - cur.x) <= 24 and -130 <= cur.y - prev.y <= 8

    def update(self, t: float, rows: List[FeedRow]) -> None:
        used = set()
        for tr in sorted(self.tracks, key=lambda k: k.row.y):
            best = None
            for i, r in enumerate(rows):
                if i not in used and self._match(tr.row, r):
                    cost = abs(r.x - tr.row.x) + 0.2 * abs(r.y - tr.row.y)
                    if best is None or cost < best[0]:
                        best = (cost, i)
            if best is not None:
                used.add(best[1])
                r = rows[best[1]]
                tr.row, tr.last, tr.seen = r, t, tr.seen + 1
                tr.hs_votes += int(r.headshot)
                tr.weapons.append(r.weapon)
                if r.sig is not None and r.sig.size:
                    tr.sigs.append(r.sig)
        lowest = max((rows[i].y for i in used), default=-1)
        for i, r in enumerate(rows):
            if i in used:
                continue
            back = next((d for d in self.dead if t - d.last <= self.revive and self._match(d.row, r)), None)
            if back is not None:   # a row that briefly vanished (smoke, flash, scoreboard)
                self.dead.remove(back)
                back.row, back.last, back.seen = r, t, back.seen + 1
                self.tracks.append(back)
                continue
            if r.y < lowest - 45:
                continue   # new rows enter at the bottom; this is an old row flickering back
            self.tracks.append(_Track(r, t, t, 1, int(r.headshot), [r.weapon],
                                      sigs=[r.sig] if r.sig is not None and r.sig.size else []))
        for tr in self.tracks:
            if not tr.reported and tr.seen >= self.confirm:
                tr.reported = True
                self._reported.append(tr)
        alive = []
        for tr in self.tracks:
            if t - tr.last > self.lost_after:
                if tr.reported:
                    self.dead.append(tr)
            else:
                alive.append(tr)
        self.tracks = alive
        self.dead = [d for d in self.dead if t - d.last <= self.revive]

    def finish(self) -> List[FeedEvent]:
        for tr in self.tracks:
            if not tr.reported and tr.seen >= self.confirm:
                tr.reported = True
                self._reported.append(tr)
        # merge duplicates: the same row tracked twice while it slid in (same name, close in time)
        merged: List[_Track] = []
        for tr in sorted(self._reported, key=lambda k: k.first):
            sig = tr.signature()
            dup = None
            for m in merged:
                ms = m.signature()
                if m.row.kind == tr.row.kind and tr.first - m.first <= self.dup_window and \
                        sig is not None and ms is not None and float(np.dot(sig, ms)) >= self.dup_sim:
                    dup = m
                    break
            if dup is None:
                merged.append(tr)
            else:
                dup.seen += tr.seen
                dup.hs_votes += tr.hs_votes
                dup.weapons += tr.weapons
                dup.sigs += tr.sigs
        self.merged = merged
        events = []
        for tr in merged:
            kill = tr.row.kind == "kill"
            named = [w for w in tr.weapons if w and w != "other"] or tr.weapons
            w = max(set(named), key=named.count) if named else ""
            events.append(FeedEvent(round(tr.first, 3), tr.row.kind,
                                    kill and tr.hs_votes * 2 >= tr.seen, w if kill else ""))
        events.sort(key=lambda e: e.time)
        return events
