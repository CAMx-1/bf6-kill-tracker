"""Music analysis for highlight videos: tempo/beat detection (no extra dependencies)."""
from __future__ import annotations

import subprocess
from typing import List, Optional, Tuple

import numpy as np

from .video import NO_WINDOW, find_ffmpeg

SR = 11025
HOP = 256


def load_mono(path: str, sr: int = SR, max_s: Optional[float] = None, ffmpeg: Optional[str] = None) -> np.ndarray:
    cmd = [ffmpeg or find_ffmpeg(), "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(sr)]
    if max_s:
        cmd += ["-t", f"{max_s:.2f}"]
    cmd += ["-f", "f32le", "-"]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"Can't read audio from {path}: {r.stderr.decode(errors='replace').strip()[-500:]}")
    return np.frombuffer(r.stdout, np.float32)


def onset_envelope(y: np.ndarray, hop: int = HOP) -> np.ndarray:
    """Spectral-flux onset strength, one value per ``hop`` samples."""
    n_fft = hop * 4
    if len(y) < n_fft:
        return np.zeros(0, np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop] * np.hanning(n_fft)
    mag = np.log1p(30 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")   # remove slow loudness changes
    return np.maximum(flux, 0).astype(np.float32)


def detect_beats(y: np.ndarray, sr: int = SR, bpm_range: Tuple[float, float] = (70, 180)) -> Tuple[float, List[float]]:
    """Return ``(bpm, beat_times)`` for a mono signal using autocorrelation tempo + grid phase."""
    env = onset_envelope(y)
    if len(env) < 64 or env.max() <= 0:
        return 0.0, []
    fps = sr / HOP
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lo, hi = int(fps * 60 / bpm_range[1]), int(fps * 60 / bpm_range[0])
    hi = min(hi, len(ac) - 1)
    if hi <= lo:
        return 0.0, []
    # weight towards ~120 bpm so double/half tempo errors prefer the common choice
    lags = np.arange(lo, hi + 1)
    bpms = 60 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    lag = int(lags[np.argmax(ac[lo:hi + 1] * weight)])
    # refine to sub-frame precision with a parabola through the peak
    if 1 <= lag < len(ac) - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        period = lag + (0.5 * (a - c) / denom if denom else 0.0)
    else:
        period = float(lag)
    # phase: offset whose beat grid collects the most onset energy
    best_phase, best_score = 0.0, -1.0
    for ph in np.arange(0, period, 0.5):
        idx = np.round(np.arange(ph, len(env) - 1, period)).astype(int)
        sc = float(env[idx].sum())
        if sc > best_score:
            best_phase, best_score = ph, sc
    # a frame registers an onset once it enters the analysis window: shift back to the onset itself
    lag_s = (HOP * 4 - HOP / 2) / sr
    beats = [float(t) + lag_s for t in np.arange(best_phase, len(env), period) / fps]
    return round(60.0 * fps / period, 1), beats


def snap_to_beats(durations: List[float], beats: List[float], min_len: float = 1.5,
                  max_change: float = 0.6) -> List[float]:
    """Adjust clip lengths so every cut lands on a beat (each change at most ``max_change`` s)."""
    if not beats:
        return list(durations)
    b = np.asarray(beats)
    out: List[float] = []
    cum = 0.0
    for d in durations:
        target = cum + d
        cands = b[(b >= cum + min(min_len, d)) & (np.abs(b - target) <= max_change)]
        cut = float(cands[np.argmin(np.abs(cands - target))]) if len(cands) else target
        out.append(cut - cum)
        cum = cut
    return out
