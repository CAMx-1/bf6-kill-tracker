"""High-level API: analyse a video file for kills and kill streaks."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

from .detector import FrameObservation, HudLayout, SkullDetector
from .events import KillEvent, KillTracker, Streak, TrackerConfig, find_streaks, max_kills_in_window
from .video import probe, read_roi_frames


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

    @property
    def total_kills(self) -> int:
        return sum(k.count for k in self.kills)

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
            window: float = 10.0, progress: Optional[Callable[[float], None]] = None) -> AnalysisResult:
    started = time.time()
    info = probe(path)
    fps = sample_fps or info.fps
    det = SkullDetector(layout)
    tracker = KillTracker(fps, config)
    for t, obs in scan_observations(path, det, sample_fps, progress):
        tracker.update(t, obs)
    kills = tracker.finish()
    streaks = find_streaks(kills, streak_gap, streak_min)
    best, best_t = max_kills_in_window(kills, window)
    return AnalysisResult(path, info.duration, fps, kills, streaks, best, best_t, window,
                          time.time() - started, tracker.counts)
