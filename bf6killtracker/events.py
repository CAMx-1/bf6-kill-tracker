"""Turn per-frame skull detections into kill events and kill streaks.

The core signal is the *onset* of a skull in the rightmost icon slot: every
kill notification pops a fresh icon into that slot, so the slot is briefly
empty (showing the pop square) and then holds a skull again.

Onsets that are not kills - the icon flickering (headshot skulls blink), or
reappearing after smoke/muzzle flash/the scoreboard hid it - are rejected
unless the pop square was seen or the points counter changed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

import numpy as np

from .detector import FrameObservation


@dataclass
class TrackerConfig:
    on_threshold: float = 0.72      # slot holds a skull when its smoothed score >= this
    off_threshold: float = 0.55     # ... and is empty once the score drops below this
    smooth_s: float = 0.15          # rolling-max window over slot scores (hides blinking)
    pop_threshold: float = 0.45     # pop-square score of a new notification
    pop_min_s: float = 0.03         # pop must be seen for at least this long (2 frames at 60 fps)
    pop_merge_s: float = 0.4        # pop detections closer than this belong to one animation
    skull_after_pop_s: float = 1.0  # a skull must settle in the slot within this time after a pop
    empty_before_s: float = 1.2     # skull with no pop counts only if the slot was (almost) empty
    empty_max_fraction: float = 0.15  #   for this long: at most this fraction of frames had a skull
    persist_window_s: float = 0.6   # a real skull stays: present for >= persist_fraction
    persist_fraction: float = 0.6   #   of the first persist_window_s seconds
    count_window_s: float = 1.0     # how long after an onset to count the skulls shown


@dataclass
class KillEvent:
    time: float
    count: int = 1          # kills registered at once (2+ for a multi-kill from one shot)
    evidence: str = ""      # "pop" (pop animation seen) or "appear" (skull appeared in an empty HUD)
    streak_id: int = -1

    def to_dict(self):
        return asdict(self)


@dataclass
class Streak:
    id: int
    start: float
    end: float
    kills: int

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self):
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d


def _rolling_max(a: np.ndarray, n: int) -> np.ndarray:
    """Trailing rolling max along axis 0."""
    if n <= 1:
        return a.copy()
    out = a.copy()
    for k in range(1, n):
        out[k:] = np.maximum(out[k:], a[:-k])
    return out


def _rolling_median_int(a: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return a.copy()
    pad = np.pad(a, (n // 2, n - 1 - n // 2), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(pad, n)
    return np.median(windows, axis=-1).astype(a.dtype)


def _hysteresis(x: np.ndarray, on: float, off: float) -> np.ndarray:
    state = np.zeros(len(x), bool)
    cur = False
    for i, v in enumerate(x):
        if cur and v < off:
            cur = False
        elif not cur and v >= on:
            cur = True
        state[i] = cur
    return state


class KillTracker:
    """Collects frame observations and derives kill events from them."""

    def __init__(self, fps: float, config: Optional[TrackerConfig] = None):
        self.cfg = config or TrackerConfig()
        self.fps = float(fps)
        self._t: List[float] = []
        self._s: List[np.ndarray] = []
        self._pop: List[float] = []
        self._mask: List[Optional[np.ndarray]] = []
        self._fill: List[float] = []
        self.counts: Optional[np.ndarray] = None   # per-frame skull count (after finish)

    def update(self, t: float, obs: FrameObservation) -> None:
        self._t.append(t)
        self._s.append(np.asarray(obs.slot_scores, np.float32))
        self._pop.append(obs.pop_score)
        self._mask.append(obs.score_mask)
        self._fill.append(obs.score_mask_fill)

    # -- helpers -----------------------------------------------------------
    def _mask_at(self, i: int, direction: int, limit: int) -> Optional[np.ndarray]:
        """First usable points-counter mask from frame ``i`` stepping in ``direction``."""
        n = len(self._mask)
        for k in range(limit):
            j = i + direction * k
            if not 0 <= j < n:
                break
            m = self._mask[j]
            if m is not None and self._fill[j] <= self.cfg.max_score_fill:
                return m
        return None

    def _same_score(self, a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[bool]:
        if a is None or b is None:
            return None
        union = np.logical_or(a, b).sum()
        if union < 20:
            return None
        return bool(np.logical_and(a, b).sum() / union >= self.cfg.same_score_iou)

    # -- main --------------------------------------------------------------
    def finish(self) -> List[KillEvent]:
        cfg, fps = self.cfg, self.fps
        if not self._t:
            self.counts = np.zeros(0, np.int8)
            return []
        t = np.asarray(self._t)
        S = np.stack(self._s)
        pop = np.asarray(self._pop)
        F = len(t)

        sm = _rolling_max(S, max(1, int(round(cfg.smooth_s * fps))))
        present = np.stack([_hysteresis(sm[:, k], cfg.on_threshold, cfg.off_threshold)
                            for k in range(S.shape[1])], axis=1)
        # icons are packed against the points counter, so count contiguous slots from the right
        contiguous = np.cumprod(present, axis=1).sum(axis=1).astype(np.int8)
        counts = _rolling_median_int(contiguous, max(1, int(round(0.15 * fps))) | 1)
        self.counts = counts
        p0 = present[:, 0]

        def persists(i: int) -> bool:
            """Skull really settled in the slot (not a one-off false match)."""
            seg = p0[i:i + int(cfg.persist_window_s * fps)]
            return len(seg) > 0 and seg.mean() >= cfg.persist_fraction

        def n_skulls(i: int, stop: int) -> int:
            seg = counts[i:min(stop, i + int(cfg.count_window_s * fps))]
            return int(seg.max()) if len(seg) else 0

        def n_before(i: int) -> int:
            seg = counts[max(0, i - int(0.3 * fps)):i]
            return int(np.median(seg)) if len(seg) else 0

        events: List[KillEvent] = []

        # 1) pop animations followed by a skull in the rightmost slot
        is_pop = pop >= cfg.pop_threshold
        merge = int(cfg.pop_merge_s * fps)
        pops: List[Tuple[int, int]] = []
        for i in np.flatnonzero(is_pop):
            if pops and i - pops[-1][1] <= merge:
                pops[-1] = (pops[-1][0], int(i))
            else:
                pops.append((int(i), int(i)))
        pop_starts = [a for a, _ in pops]
        for idx, (a, b) in enumerate(pops):
            if is_pop[a:b + 1].sum() < max(1, int(round(cfg.pop_min_s * fps))):
                continue
            nxt_pop = pop_starts[idx + 1] if idx + 1 < len(pops) else F
            look_end = min(F, b + int(cfg.skull_after_pop_s * fps), nxt_pop)
            on = np.flatnonzero(p0[b:look_end])
            if not len(on) or not persists(b + int(on[0])):
                continue   # pop of a non-kill icon (spot, assist, order...)
            i = b + int(on[0])
            n = max(1, n_skulls(i, nxt_pop) - n_before(a))
            events.append(KillEvent(round(float(t[a]), 3), n, "pop"))

        # 2) skull appearing in an empty HUD without a detected pop (motion blur, smoke)
        claimed = np.zeros(F, bool)
        for e in events:
            a = int(np.searchsorted(t, e.time))
            claimed[max(0, a - int(0.5 * fps)):a + int((cfg.skull_after_pop_s + 0.2) * fps)] = True
        onsets = np.flatnonzero(p0[1:] & ~p0[:-1]) + 1
        if F and p0[0]:
            onsets = np.concatenate([[0], onsets])
        empty_n = int(cfg.empty_before_s * fps)
        for i in onsets:
            if claimed[i] or not persists(i):
                continue
            before = p0[max(0, i - empty_n):i]
            if len(before) and before.mean() > cfg.empty_max_fraction:
                continue   # icons were on screen recently: flicker / occlusion, not a new kill
            nxt = np.flatnonzero(~p0[i:])
            stop = i + int(nxt[0]) if len(nxt) else F
            events.append(KillEvent(round(float(t[i]), 3), max(1, n_skulls(i, stop)), "appear"))
            claimed[i:stop + empty_n] = True   # later blips of the same icon are not new kills

        events.sort(key=lambda e: e.time)
        return events


def find_streaks(kills: List[KillEvent], max_gap: float = 5.0, min_kills: int = 3) -> List[Streak]:
    """Group kills where each follows the previous within ``max_gap`` seconds.

    Groups with at least ``min_kills`` kills are returned as streaks, and the
    kill events are tagged with their streak id.
    """
    streaks: List[Streak] = []
    group: List[KillEvent] = []

    def flush():
        total = sum(k.count for k in group)
        if group and total >= min_kills:
            s = Streak(len(streaks), group[0].time, group[-1].time, total)
            for k in group:
                k.streak_id = s.id
            streaks.append(s)

    for k in sorted(kills, key=lambda e: e.time):
        if group and k.time - group[-1].time > max_gap:
            flush()
            group = []
        group.append(k)
    flush()
    return streaks


def max_kills_in_window(kills: List[KillEvent], window: float) -> Tuple[int, float]:
    """Highest number of kills inside any ``window``-second span, and where it starts."""
    times: List[float] = []
    for k in sorted(kills, key=lambda e: e.time):
        times.extend([k.time] * k.count)
    best, best_t, j = 0, 0.0, 0
    for i in range(len(times)):
        while times[i] - times[j] > window:
            j += 1
        if i - j + 1 > best:
            best, best_t = i - j + 1, times[j]
    return best, best_t
