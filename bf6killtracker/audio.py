"""Game-audio analysis.

Tested on the reference clips: BF6 has no kill-confirm sound distinct enough
to detect kills from audio alone (the best kill-aligned pattern is the
gunfire just before a kill, and 30-60 % of its matches are not kills). So
audio is used as a supporting signal only:

* ``intensity`` - transient high-frequency energy (gunfire), per 0.1 s.
* ``gunfire_bursts`` - bursts of shooting; bursts with no detected kill
  shortly after are offered as "possible missed kill" hints in review mode.

(Intensity was also tried as a hype-score input, but on the reference clips
it didn't separate good clips from quiet ones, so it isn't used there.)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from .music import load_mono

SR = 11025
HOP = 256
STEP_S = 0.1


def intensity(path: str, y: Optional[np.ndarray] = None) -> np.ndarray:
    """Firefight intensity 0..1 per ``STEP_S`` seconds (normalised to the clip's loud moments)."""
    if y is None:
        try:
            y = load_mono(path, sr=SR)
        except RuntimeError:          # no audio track
            return np.zeros(0, np.float32)
    n = HOP * 4
    if len(y) < n * 2:
        return np.zeros(0, np.float32)
    fr = np.lib.stride_tricks.sliding_window_view(y, n)[::HOP] * np.hanning(n)
    mag = np.log1p(30 * np.abs(np.fft.rfft(fr, axis=1)))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    band = (freqs >= 2000) & (freqs <= 5000)          # where gunshot cracks sit
    flux = np.maximum(0.0, np.diff(mag[:, band], axis=0)).sum(axis=1)
    per = max(1, int(round(STEP_S * SR / HOP)))
    k = len(flux) // per
    curve = flux[:k * per].reshape(k, per).sum(axis=1)
    lo, hi = np.percentile(curve, 20), np.percentile(curve, 98)
    return np.clip((curve - lo) / max(1e-6, hi - lo), 0, 1).astype(np.float32)


def mean_intensity(curve: np.ndarray, a: float, b: float) -> float:
    if not len(curve):
        return 0.0
    i, j = int(max(0, a) / STEP_S), int(max(a + STEP_S, b) / STEP_S) + 1
    seg = curve[i:j]
    return float(seg.mean()) if len(seg) else 0.0


def gunfire_bursts(curve: np.ndarray, thr: float = 0.6, min_s: float = 0.4, merge_s: float = 0.6) -> List[Tuple[float, float]]:
    """[(start, end)] of sustained shooting."""
    on = curve >= thr
    out: List[Tuple[float, float]] = []
    i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j:j + int(merge_s / STEP_S) + 1].any():
                j += 1
            if (j - i) * STEP_S >= min_s:
                out.append((i * STEP_S, j * STEP_S))
            i = j
        i += 1
    return out


def missed_kill_hints(curve: np.ndarray, kill_times: List[float], after_s: float = 2.0) -> List[float]:
    """End times of gunfire bursts with no detected kill during/just after them."""
    hints = []
    for a, b in gunfire_bursts(curve):
        if not any(a - 0.5 <= k <= b + after_s for k in kill_times):
            hints.append(round(b, 2))
    return hints
