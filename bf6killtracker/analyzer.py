"""High-level API: analyse a video file for kills and kill streaks."""
from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

from .detector import FrameObservation, HudLayout, SkullDetector
from .events import (KillEvent, KillTracker, Life, Streak, TrackerConfig, find_streaks, fuse_feed,
                     max_kills_in_window, split_lives)
from .killfeed import FEED_ROI, FeedEvent, FeedReader, FeedTracker
from .video import probe, read_multi_roi_frames, read_roi_frames


class Cancelled(Exception):
    """Raise from a ``progress`` callback to stop an analysis early."""


@dataclass
class AnalysisResult:
    video: str
    duration: float
    fps: float
    kills: List[KillEvent]
    streaks: List[Streak]
    best_window_kills: int
    best_window_start: float
    window: float
    processing_s: float = 0.0
    counts: Optional[np.ndarray] = field(default=None, repr=False)  # per-frame skull count
    deaths: List[float] = field(default_factory=list)
    feed: List[FeedEvent] = field(default_factory=list)
    lives: List[Life] = field(default_factory=list)
    feed_used: bool = False
    hud: str = ""            # how the HUD layout was chosen (auto-calibration summary)

    @property
    def total_kills(self) -> int:
        return sum(k.count for k in self.kills)

    @property
    def kd(self) -> float:
        return self.total_kills / max(1, len(self.deaths))

    def type_counts(self) -> dict:
        out: dict = {}
        for k in self.kills:
            for t in k.type_list():
                out[t] = out.get(t, 0) + 1
        return out

    @property
    def best_life(self) -> int:
        return max((lf.kills for lf in self.lives), default=self.total_kills)

    def to_dict(self) -> dict:
        return {
            "video": self.video,
            "duration_s": round(self.duration, 2),
            "total_kills": self.total_kills,
            "kills": [k.to_dict() for k in self.kills],
            "streaks": [s.to_dict() for s in self.streaks],
            "most_kills_in_window": {
                "window_s": self.window,
                "kills": self.best_window_kills,
                "start": round(self.best_window_start, 2),
            },
            "deaths": self.deaths,
            "kd": round(self.kd, 2),
            "kill_types": self.type_counts(),
            "lives": [lf.to_dict() for lf in self.lives],
            "most_kills_in_one_life": self.best_life,
            "hud": self.hud,
            "processing_s": round(self.processing_s, 1),
        }


def scan_observations(path: str, detector: SkullDetector, sample_fps: Optional[float] = None,
                      progress: Optional[Callable[[float], None]] = None):
    """Yield ``(t, FrameObservation)`` for each frame of ``path``."""
    for t, roi in read_roi_frames(path, detector.roi, detector.layout.ref_size, sample_fps):
        if progress:
            progress(t)
        yield t, detector.observe(roi)


def analyze(path: str, *, sample_fps: Optional[float] = None, layout: Optional[HudLayout] = None,
            config: Optional[TrackerConfig] = None, streak_gap: float = 5.0, streak_min: int = 3,
            window: float = 10.0, progress: Optional[Callable[[float], None]] = None,
            use_feed: bool = True, feed_fps: float = 10.0, auto_calibrate: bool = True) -> AnalysisResult:
    started = time.time()
    info = probe(path)
    fps = sample_fps or info.fps
    calibration = None
    if layout is None:
        if auto_calibrate:
            from .calibrate import calibrate
            calibration = calibrate(path)
            layout = calibration.layout
        else:
            layout = HudLayout.for_video(info.width, info.height)
    det = SkullDetector(layout)
    tracker = KillTracker(fps, config)
    feed_events: List[FeedEvent] = []
    # the kill feed is anchored to the top-right corner of the screen
    fx, fy, fw, fh = FEED_ROI
    feed_roi = (fx + det.layout.ref_size[0] - 2560, fy, fw, fh)
    reader = FeedReader(feed_roi) if use_feed else None
    every = max(1, int(round(fps / feed_fps)))
    ft = FeedTracker(every / fps)
    rois = [det.roi] + ([reader.roi] if reader else [])

    def work(i: int, crops):
        obs = det.observe(crops[0])
        rows = reader.read(crops[1]) if reader is not None and i % every == 0 else None
        return obs, rows

    # OpenCV releases the GIL, so frames are processed in parallel while ffmpeg decodes;
    # results are consumed strictly in order
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    pending: deque = deque()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def drain(limit: int) -> None:
            while len(pending) > limit:
                t, fut = pending.popleft()
                obs, rows = fut.result()
                tracker.update(t, obs)
                if rows is not None:
                    ft.update(t, rows)

        for i, (t, crops) in enumerate(read_multi_roi_frames(path, rois, det.layout.ref_size, sample_fps)):
            if progress:
                progress(t)
            pending.append((t, pool.submit(work, i, crops)))
            drain(workers * 4)
        drain(0)
    if reader is not None:
        feed_events = ft.finish()
    kills = tracker.finish()
    deaths = [e.time for e in feed_events if e.kind == "death"]
    if use_feed:
        kills = fuse_feed(kills, [e for e in feed_events if e.kind == "kill"])
    streaks = find_streaks(kills, streak_gap, streak_min, deaths)
    best, best_t = max_kills_in_window(kills, window)
    return AnalysisResult(path, info.duration, fps, kills, streaks, best, best_t, window,
                          time.time() - started, tracker.counts, deaths, feed_events,
                          split_lives(kills, deaths, info.duration), use_feed,
                          calibration.describe() if calibration else "manual HUD layout")
