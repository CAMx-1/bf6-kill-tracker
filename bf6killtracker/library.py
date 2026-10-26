"""Library and stats: everything you've scanned, plus saved projects.

The library is built from the scan cache (see :mod:`cache`), so every video
scanned from the app or the command line shows up automatically.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import cache
from .events import find_streaks, split_lives


@dataclass
class SessionStats:
    video: str
    recorded: float          # file modification time (epoch s)
    duration: float
    kills: int
    deaths: int
    best_streak: int         # most kills in one streak (default rules)
    best_life: int
    types: Dict[str, int] = field(default_factory=dict)
    feed_used: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.video)

    @property
    def kd(self) -> float:
        return self.kills / max(1, self.deaths)

    @property
    def kills_per_hour(self) -> float:
        return self.kills / self.duration * 3600 if self.duration else 0.0

    @property
    def headshot_rate(self) -> float:
        known = sum(n for t, n in self.types.items() if t != "unknown")
        return self.types.get("headshot", 0) / known if known else 0.0


def sessions(streak_gap: float = 5.0, streak_min: int = 3) -> List[SessionStats]:
    """One entry per scanned video still on disk (newest scan per video), oldest first."""
    d = cache.cache_dir()
    best: Dict[str, dict] = {}
    if os.path.isdir(d):
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            fp = data.get("fingerprint", {})
            path = fp.get("path")
            if not path or not os.path.isfile(path) or fp.get("version") != cache.DETECTOR_VERSION:
                continue
            opts = fp.get("options", {})
            prev = best.get(path)
            # prefer full scans (with kill feed), then the most recent file version
            rank = (bool(opts.get("feed")), fp.get("mtime", 0))
            if prev is None or rank > prev["_rank"]:
                data["_rank"] = rank
                best[path] = data
    out: List[SessionStats] = []
    for path, data in best.items():
        hit = cache.load(path, data["fingerprint"]["options"])
        if hit is None:
            continue
        kills, deaths = hit["kills"], hit["deaths"]
        streaks = find_streaks(kills, streak_gap, streak_min, deaths)
        types: Dict[str, int] = {}
        for k in kills:
            for t in k.type_list():
                types[t] = types.get(t, 0) + 1
        lives = split_lives(kills, deaths, hit["duration"])
        out.append(SessionStats(path, data["fingerprint"]["mtime"], hit["duration"], sum(k.count for k in kills),
                                len(deaths), max((s.kills for s in streaks), default=0),
                                max((lf.kills for lf in lives), default=0), types, hit["feed_used"]))
    out.sort(key=lambda s: s.recorded)
    return out


def totals(ss: List[SessionStats]) -> dict:
    kills = sum(s.kills for s in ss)
    deaths = sum(s.deaths for s in ss)
    dur = sum(s.duration for s in ss)
    hs = sum(s.types.get("headshot", 0) for s in ss)
    known = sum(n for s in ss for t, n in s.types.items() if t != "unknown")
    return {
        "videos": len(ss), "hours": dur / 3600, "kills": kills, "deaths": deaths,
        "kd": kills / max(1, deaths), "kills_per_hour": kills / dur * 3600 if dur else 0.0,
        "headshot_rate": hs / known if known else 0.0,
        "best_streak": max((s.best_streak for s in ss), default=0),
        "best_life": max((s.best_life for s in ss), default=0),
    }


# --------------------------------------------------------------------------- projects
PROJECT_EXT = ".bf6proj"


def save_project(path: str, data: dict) -> None:
    """Write a project file (JSON): videos, ticked clips, trims, clip order and settings."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"format": 1, **data}, f, indent=2)
    os.replace(tmp, path)


def load_project(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != 1:
        raise ValueError("Not a BF6 Kill Tracker project (or made by a newer version)")
    # videos stored relative to the project file still open after moving both together
    base = os.path.dirname(os.path.abspath(path))
    vids = []
    for v in data.get("videos", []):
        p = v["path"]
        if not os.path.isfile(p) and v.get("rel"):
            cand = os.path.normpath(os.path.join(base, v["rel"]))
            if os.path.isfile(cand):
                p = cand
        vids.append({**v, "path": p})
    data["videos"] = vids
    return data


def project_video_entry(path: str, project_path: str, **extra) -> dict:
    try:
        rel: Optional[str] = os.path.relpath(path, os.path.dirname(os.path.abspath(project_path)))
    except ValueError:      # different drive on Windows
        rel = None
    return {"path": os.path.abspath(path), "rel": rel, **extra}
