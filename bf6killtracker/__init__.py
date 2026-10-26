"""Battlefield 6 kill tracker: finds kill-skull notifications in gameplay videos."""
from .analyzer import AnalysisResult, analyze
from .detector import HudLayout, SkullDetector
from .events import KillEvent, KillTracker, Streak, TrackerConfig, find_streaks, max_kills_in_window

__all__ = [
    "AnalysisResult", "analyze", "HudLayout", "SkullDetector", "KillEvent", "KillTracker",
    "Streak", "TrackerConfig", "find_streaks", "max_kills_in_window",
]
__version__ = "0.3.0"
