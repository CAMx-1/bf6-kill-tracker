"""Group kill-feed rows by the other player's name: who you kill most (and who kills you).

Names are compared as pictures (the kill-feed text), not read as text, so this
needs no OCR and works with any characters. A group shows a crop of the name.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import cv2
import numpy as np


@dataclass
class Player:
    kills: List[float] = field(default_factory=list)     # times you killed them
    deaths: List[float] = field(default_factory=list)    # times they killed you
    sig: Optional[np.ndarray] = None
    image: Optional[np.ndarray] = None                    # BGR crop of the name

    @property
    def total(self) -> int:
        return len(self.kills) + len(self.deaths)

    def png_b64(self) -> str:
        if self.image is None or not self.image.size:
            return ""
        ok, buf = cv2.imencode(".png", self.image)
        return base64.b64encode(buf.tobytes()).decode() if ok else ""


def _sim(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None or a.shape != b.shape:
        return -1.0
    return float(np.dot(a, b))


def group_players(feed_events: Sequence, threshold: float = 0.8) -> List[Player]:
    """Players sorted by how often they appear (kills + deaths)."""
    players: List[Player] = []
    for e in feed_events:
        if e.kind not in ("kill", "death") or not getattr(e, "counted", True) \
                or getattr(e, "name_sig", None) is None or not e.name_sig.size:
            continue
        best, bs = None, threshold
        for p in players:
            s = _sim(p.sig, e.name_sig)
            if s > bs:
                best, bs = p, s
        if best is None:
            best = Player(sig=e.name_sig, image=e.name_img)
            players.append(best)
        (best.kills if e.kind == "kill" else best.deaths).append(e.time)
        if e.name_img is not None and (best.image is None or e.name_img.shape[1] > best.image.shape[1]):
            best.image = e.name_img
    players.sort(key=lambda p: (-p.total, -len(p.kills)))
    return players
