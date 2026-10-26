"""Full-screen states: the "MAN DOWN" death screen and the scoreboard.

* Death screen: "MAN DOWN" under the revive icon at the bottom centre. Each
  appearance is a death (a second source next to the kill feed), and how long
  it stays up is the time spent down (until revived or skipping to deploy).
* Scoreboard (TAB or end of round): the header "SCORE K D A" is matched; your
  row is the one highlighted in green, and its kills/deaths/assists are read
  with digit templates - the game's own numbers, to compare with ours.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .detector import TEMPLATE_DIR

MAN_DOWN_ROI = (1080, 1180, 400, 90)       # reference 2560x1440
SCOREBOARD_ROI = (0, 200, 2560, 960)
# narrow strips over each team's header + number columns (much cheaper to decode than the whole board)
BOARD_STRIPS = [(842, 200, 440, 960), (2058, 200, 440, 960)]
HEADER_STRIPS = [(842, 200, 440, 160), (2058, 200, 440, 160)]   # scanned every few frames


@dataclass
class DownPeriod:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self):
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d


@dataclass
class Scoreboard:
    time: float
    score: Optional[int]
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]

    def to_dict(self):
        return asdict(self)


class ScreenReader:
    def __init__(self, ref_w: int = 2560):
        xs = (ref_w - 2560) // 2
        x, y, w, h = MAN_DOWN_ROI
        self.down_roi = (x + xs, y, w, h)
        x, y, w, h = SCOREBOARD_ROI
        self.board_roi = (x + xs, y, w, h)
        self.strips = [(sx + xs, sy, sw, sh) for sx, sy, sw, sh in BOARD_STRIPS]
        self.header_strips = [(sx + xs, sy, sw, sh) for sx, sy, sw, sh in HEADER_STRIPS]

        self.man_down = cv2.imread(os.path.join(TEMPLATE_DIR, "man_down.png"), cv2.IMREAD_GRAYSCALE)
        self.header = cv2.imread(os.path.join(TEMPLATE_DIR, "scoreboard_header.png"), cv2.IMREAD_GRAYSCALE)
        self.digits: List[Tuple[int, np.ndarray]] = []
        ddir = os.path.join(TEMPLATE_DIR, "digits")
        for f in sorted(os.listdir(ddir)) if os.path.isdir(ddir) else []:
            img = cv2.imread(os.path.join(ddir, f), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                self.digits.append((int(os.path.splitext(f)[0]), img.astype(np.float32)))

    def board_showing(self, header_crops: List[np.ndarray]) -> bool:
        return any(self.board_headers(c) for c in header_crops)

    def read_frame(self, frame: np.ndarray):
        """Scoreboard values from a full frame (reference size)."""
        return self.read_strips([frame[y:y + h, x:x + w] for x, y, w, h in self.strips])

    def is_down(self, crop: np.ndarray, thr: float = 0.7) -> bool:
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.matchTemplate(g, self.man_down, cv2.TM_CCOEFF_NORMED).max()) >= thr

    def board_headers(self, crop: np.ndarray, thr: float = 0.75) -> List[Tuple[int, int]]:
        """(x, y) of each team's 'SCORE K D A' header inside ``crop`` (empty if no scoreboard)."""
        g = cv2.cvtColor(crop[:160], cv2.COLOR_BGR2GRAY)
        r = cv2.matchTemplate(g, self.header, cv2.TM_CCOEFF_NORMED)
        out = []
        for _ in range(2):
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx < thr:
                break
            out.append(loc)
            r[:, max(0, loc[0] - 300):loc[0] + 300] = -1
        return sorted(out)

    # -- digits
    def _read_number(self, gray: np.ndarray) -> Optional[int]:
        m = (gray > 150).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(m)
        boxes = sorted((s for s in st[1:] if 14 <= s[3] <= 28 and 3 <= s[2] <= 20), key=lambda s: s[0])
        if not boxes:
            return None
        out = ""
        for x, y, w, h, _ in boxes:
            g = cv2.resize(gray[y:y + h, x:x + w], (12, 20)).astype(np.float32)
            g = (g - g.mean()) / (g.std() + 1e-6)
            best, bd = -1.0, None
            for d, t in self.digits:
                tt = (t - t.mean()) / (t.std() + 1e-6)
                sc = float((g * tt).mean())
                if sc > best:
                    best, bd = sc, d
            if best < 0.5:
                return None
            out += str(bd)
        return int(out) if out else None

    # number columns relative to a team's header (measured on 2560x1440 scoreboards)
    COLUMNS = {"score": (-8, 118), "k": (128, 196), "d": (200, 258), "a": (262, 335)}

    def read_board(self, crop: np.ndarray, headers: List[Tuple[int, int]]) -> Optional[Tuple]:
        """(score, kills, deaths, assists) from your (green-outlined) row."""
        b, g, r = (crop[:, :, i].astype(np.int16) for i in range(3))
        green = (g > 150) & (g - r > 40) & (g - b > 20)
        for hx, hy in headers:
            band = green[:, max(0, hx - 820):hx + 440]
            need = 200 if band.shape[1] > 600 else 120     # narrow strip: fewer outline pixels per row
            rows = np.flatnonzero(band.sum(axis=1) > need)        # the outline's top/bottom edges
            rows = rows[rows > hy + 20]
            if len(rows) < 2:
                continue
            top = int(rows[0])
            close = rows[(rows > top + 25) & (rows < top + 70)]
            bot = int(close[0]) if len(close) else top + 50     # bottom edge can be faint; rows are ~50 px
            gray = cv2.cvtColor(crop[top + 4:bot - 3], cv2.COLOR_BGR2GRAY)
            vals = []
            for key in ("score", "k", "d", "a"):
                a, c = self.COLUMNS[key]
                sub = gray[:, max(0, hx + a):hx + c]
                vals.append(self._read_number(sub) if sub.size else None)
            if any(v is not None for v in vals):
                return tuple(vals)
        return None


    def read_strips(self, strips: List[np.ndarray]):
        """Scoreboard values from the two narrow strips, or None if no scoreboard is showing."""
        for crop in strips:
            hd = self.board_headers(crop)
            if hd:
                v = self.read_board(crop, hd)
                if v is not None:
                    return v
        return None


class ScreenTracker:
    def __init__(self, confirm: int = 3, gap_s: float = 1.0):
        self.confirm, self.gap = confirm, gap_s
        self.down: List[DownPeriod] = []
        self._run: Optional[List[float]] = None   # [first, last, seen]
        self.boards: List[Scoreboard] = []
        self._last_board = -1e9
        self._views: List[List[float]] = []

    def update_down(self, t: float, is_down: bool) -> None:
        if is_down:
            if self._run is None or t - self._run[1] > self.gap:
                self._close()
                self._run = [t, t, 1]
            else:
                self._run[1], self._run[2] = t, self._run[2] + 1
        elif self._run is not None and t - self._run[1] > self.gap:
            self._close()

    def _close(self) -> None:
        if self._run is not None and self._run[2] >= self.confirm:
            self.down.append(DownPeriod(round(self._run[0], 2), round(self._run[1], 2)))
        self._run = None

    def update_showing(self, t: float, showing: bool) -> None:
        """Record when the scoreboard is on screen; values are read afterwards from one frame each."""
        if showing:
            if self._views and t - self._views[-1][1] <= 1.5:
                self._views[-1][1] = t
            else:
                self._views.append([t, t])

    def update_board(self, t: float, vals) -> None:
        if vals is None:
            return
        s, k, d, a = vals
        if self.boards and t - self._last_board < 3.0:
            prev = self.boards[-1]          # same viewing: keep the most complete reading
            if sum(v is not None for v in vals) >= sum(v is not None for v in
                                                      (prev.score, prev.kills, prev.deaths, prev.assists)):
                prev.score, prev.kills, prev.deaths, prev.assists = s, k, d, a
        else:
            self.boards.append(Scoreboard(round(t, 2), s, k, d, a))
        self._last_board = t

    def finish(self):
        self._close()
        return self.down, self.boards
