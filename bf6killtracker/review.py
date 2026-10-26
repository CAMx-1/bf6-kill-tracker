"""Review corrections: confirm, reject or add kills and deaths, per video.

Corrections are stored as JSON in ``<cache dir>/../reviews/`` keyed by the
video's path and size, and applied on top of every scan (so they survive
re-scans and detector updates). The same files are the labelled data for
accuracy tests and future model training - see :func:`export_dataset`.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .cache import cache_dir

MATCH_S = 0.6   # a correction applies to a detection this close in time


@dataclass
class Review:
    video: str
    confirmed: List[float] = field(default_factory=list)       # detected kills marked correct
    rejected: List[float] = field(default_factory=list)        # detected kills that aren't kills
    added: List[Dict] = field(default_factory=list)            # missed kills: {"time", "count", "type"}
    rejected_deaths: List[float] = field(default_factory=list)
    added_deaths: List[float] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)       # "time" -> corrected kill count

    def is_empty(self) -> bool:
        return not (self.confirmed or self.rejected or self.added or self.rejected_deaths or self.added_deaths
                    or self.counts)


def _dir() -> str:
    return os.path.join(os.path.dirname(cache_dir()), "reviews")


def _path(video: str) -> str:
    try:
        size = os.path.getsize(video)
    except OSError:
        size = 0
    key = f"{os.path.abspath(video)}|{size}".encode("utf-8")
    return os.path.join(_dir(), hashlib.sha1(key).hexdigest()[:20] + ".json")


def load(video: str) -> Review:
    try:
        with open(_path(video), encoding="utf-8") as f:
            d = json.load(f)
        return Review(**{k: v for k, v in d.items() if k in Review.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return Review(os.path.abspath(video))


def save(rv: Review) -> None:
    p = _path(rv.video)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if rv.is_empty():
        if os.path.exists(p):
            os.remove(p)
        return
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(rv), f, indent=1)
    os.replace(tmp, p)


def _near(t: float, ts: List[float]) -> bool:
    return any(abs(t - x) <= MATCH_S for x in ts)


def apply(result, rv: Optional[Review] = None, streak_gap: float = 5.0, streak_min: int = 3):
    """Apply corrections to ``result`` in place (kills, deaths, streaks, lives); returns it."""
    from .events import KillEvent, find_streaks, split_lives
    rv = rv or load(result.video)
    if rv.is_empty():
        result.reviewed = False
        return result
    kills = []
    for k in result.kills:
        if _near(k.time, rv.rejected):
            continue
        for t, n in rv.counts.items():
            if abs(float(t) - k.time) <= MATCH_S:
                k.count = int(n)
                k.types = k.types[:k.count]
        k.evidence = "confirmed" if _near(k.time, rv.confirmed) else k.evidence
        kills.append(k)
    for a in rv.added:
        kills.append(KillEvent(float(a["time"]), int(a.get("count", 1)), "added", -1,
                               [a["type"]] if a.get("type") else []))
    kills.sort(key=lambda k: k.time)
    deaths = sorted([d for d in result.deaths if not _near(d, rv.rejected_deaths)] + list(rv.added_deaths))
    result.kills, result.deaths = kills, deaths
    result.streaks = find_streaks(kills, streak_gap, streak_min, deaths)
    result.lives = split_lives(kills, deaths, result.duration)
    result.reviewed = True
    return result


def accuracy(result_raw, rv: Review) -> Dict[str, float]:
    """How the raw detections compare with a review (precision/recall of kill events)."""
    det = [k.time for k in result_raw.kills]
    fp = sum(1 for t in det if _near(t, rv.rejected))
    fn = len(rv.added)
    tp = len(det) - fp
    return {"true": tp, "false": fp, "missed": fn,
            "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn)}


def export_dataset(out_dir: str) -> int:
    """Write every reviewed video's corrected kill/death times to ``out_dir`` (one JSON per video)."""
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    d = _dir()
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                data = json.load(fh)
            with open(os.path.join(out_dir, f), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            n += 1
    return n
