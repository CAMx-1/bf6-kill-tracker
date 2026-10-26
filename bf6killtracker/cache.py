"""Remember scan results so re-opening a video is instant.

Results are stored as JSON in the per-user cache folder:

* Windows: ``%LOCALAPPDATA%\\bf6killtracker\\scans``
* macOS:   ``~/Library/Caches/bf6killtracker/scans``
* Linux:   ``$XDG_CACHE_HOME/bf6killtracker/scans`` (``~/.cache/...``)

An entry is used only if the video's size and modification time are unchanged
and it was made by the same detector version, so edited/replaced videos and
detector improvements trigger a fresh scan automatically. Streaks are not
stored: they are recomputed from the cached kills with the current rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Optional

from .events import KillEvent, Life
from .killfeed import FeedEvent

# bump whenever detection logic changes in a way that changes results
DETECTOR_VERSION = 4


def cache_dir() -> str:
    env = os.environ.get("BF6KT_CACHE_DIR")
    if env:
        base = env
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"),
                            "bf6killtracker")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches/bf6killtracker")
    else:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "bf6killtracker")
    return os.path.join(base, "scans")


def _fingerprint(path: str, options: dict) -> Optional[dict]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime": int(st.st_mtime),
            "version": DETECTOR_VERSION, "options": options}


def _entry_path(fp: dict) -> str:
    key = json.dumps(fp, sort_keys=True).encode("utf-8")
    return os.path.join(cache_dir(), hashlib.sha1(key).hexdigest()[:20] + ".json")


def save(result, options: dict) -> None:
    fp = _fingerprint(result.video, options)
    if fp is None:
        return
    data = {
        "fingerprint": fp,
        "duration": result.duration,
        "fps": result.fps,
        "kills": [{"time": k.time, "count": k.count, "evidence": k.evidence,
                   "types": k.types, "weapons": k.weapons} for k in result.kills],
        "deaths": result.deaths,
        "feed": [{"time": e.time, "kind": e.kind, "headshot": e.headshot, "weapon": e.weapon}
                 for e in result.feed],
        "feed_used": result.feed_used,
        "events": [e.to_dict() for e in getattr(result, "events", [])],
        "hud": result.hud,
        "processing_s": result.processing_s,
    }
    path = _entry_path(fp)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass   # a cache that can't be written is not an error


def load(video: str, options: dict) -> Optional[dict]:
    """Cached data for ``video`` (dict with restored objects) or None."""
    fp = _fingerprint(video, options)
    if fp is None:
        return None
    path = _entry_path(fp)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != fp:
        return None
    data["kills"] = [KillEvent(k["time"], k["count"], k.get("evidence", ""), -1, k.get("types", []),
                               k.get("weapons", [])) for k in data["kills"]]
    from .labels import HudEvent
    data["events"] = [HudEvent(e["time"], e["kind"], e.get("group", "action"), e.get("count", 1))
                      for e in data.get("events", [])]
    data["feed"] = [FeedEvent(e["time"], e["kind"], e.get("headshot", False), e.get("weapon", ""))
                    for e in data.get("feed", [])]
    return data


def clear() -> int:
    """Delete every cached scan; returns how many were removed."""
    d = cache_dir()
    n = 0
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith(".json"):
                try:
                    os.remove(os.path.join(d, f))
                    n += 1
                except OSError:
                    pass
    return n


__all__ = ["cache_dir", "save", "load", "clear", "DETECTOR_VERSION", "Life"]
