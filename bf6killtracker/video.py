"""Cross-platform video frame reader built on an ffmpeg subprocess.

Only the HUD region of interest is decoded into Python, which keeps the
per-frame cost low even for 1440p/4K HEVC recordings.

ffmpeg lookup order:
  1. the ``BF6KT_FFMPEG`` environment variable
  2. ``ffmpeg`` / ``ffmpeg.exe`` on PATH
  3. the binary bundled with the ``imageio-ffmpeg`` package (works on Windows,
     macOS and Linux without a separate install)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

import numpy as np


# On Windows every console program (ffmpeg) opens its own console window unless
# told not to. From the GUI that means a window popping up - and stealing focus -
# for every probe, thumbnail and clip. Pass this to every subprocess call.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def find_ffmpeg() -> str:
    env = os.environ.get("BF6KT_FFMPEG")
    if env and os.path.isfile(env):
        return env
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "ffmpeg not found. Install it (https://ffmpeg.org) or run "
            "'pip install imageio-ffmpeg'."
        ) from exc


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool = False


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VID_RE = re.compile(r"Stream #.*Video:.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*tbr")


def probe(path: str, ffmpeg: Optional[str] = None) -> VideoInfo:
    """Read basic stream info by parsing ``ffmpeg -i`` output (no ffprobe needed)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        **NO_WINDOW,
    )
    err = proc.stderr
    if "No such file" in err or "Invalid data" in err:
        raise RuntimeError(f"Cannot open video: {path}")
    m = _DUR_RE.search(err)
    duration = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    w = h = 0
    fps = 0.0
    for line in err.splitlines():
        if "Video:" in line:
            mv = _VID_RE.search(line)
            if mv:
                w, h = int(mv.group(1)), int(mv.group(2))
            # prefer the nominal rate (tbr): game recordings are often variable frame rate
            mf = _TBR_RE.search(line) or _FPS_RE.search(line)
            if mf:
                fps = float(mf.group(1))
            break
    if not w:
        raise RuntimeError(f"No video stream found in {path}")
    has_audio = any("Audio:" in line for line in err.splitlines() if "Stream #" in line)
    return VideoInfo(w, h, fps or 30.0, duration, has_audio)


def read_roi_frames(
    path: str,
    roi: Tuple[int, int, int, int],
    ref_size: Tuple[int, int],
    sample_fps: Optional[float] = None,
    ffmpeg: Optional[str] = None,
) -> Iterator[Tuple[float, np.ndarray]]:
    """Yield ``(timestamp_seconds, bgr_roi)`` for every decoded frame.

    The frame is first scaled to ``ref_size`` (the resolution the HUD layout
    was measured at) and then cropped to ``roi = (x, y, w, h)`` in those
    reference coordinates.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    info = probe(path, ffmpeg)
    fps = sample_fps or info.fps
    x, y, w, h = roi
    rw, rh = ref_size
    # always resample to a constant rate so frame index / fps is the true timestamp
    vf = [f"fps={fps:g}"]
    if (info.width, info.height) != (rw, rh):
        vf.append(f"scale={rw}:{rh}:flags=bicubic")
    vf.append("format=bgr24")  # crop in RGB so odd offsets/sizes are exact
    vf.append(f"crop={w}:{h}:{x}:{y}")
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-i", path,
        "-an", "-sn", "-vf", ",".join(vf),
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    frame_bytes = w * h * 3
    # stderr goes to a temp file so a chatty ffmpeg can never block on a full pipe
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                                bufsize=frame_bytes * 4, **NO_WINDOW)
        killed = False
        try:
            idx = 0
            assert proc.stdout is not None
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                yield idx / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                idx += 1
        finally:
            if proc.poll() is None:
                killed = True
                proc.kill()
            proc.wait()
        if not killed and proc.returncode != 0:
            errf.seek(0)
            msg = errf.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {msg[-2000:]}")


def read_multi_roi_frames(
    path: str,
    rois: List[Tuple[int, int, int, int]],
    ref_size: Tuple[int, int],
    sample_fps: Optional[float] = None,
    ffmpeg: Optional[str] = None,
) -> Iterator[Tuple[float, List[np.ndarray]]]:
    """Like :func:`read_roi_frames` but returns several crops per frame from a single decode.

    The crops are stacked vertically inside ffmpeg (one rawvideo stream) and split again here.
    """
    if len(rois) == 1:
        for t, f in read_roi_frames(path, rois[0], ref_size, sample_fps, ffmpeg):
            yield t, [f]
        return
    ffmpeg = ffmpeg or find_ffmpeg()
    info = probe(path, ffmpeg)
    fps = sample_fps or info.fps
    rw, rh = ref_size
    W = max(r[2] for r in rois)
    H = sum(r[3] for r in rois)
    pre = [f"fps={fps:g}"]
    if (info.width, info.height) != (rw, rh):
        pre.append(f"scale={rw}:{rh}:flags=bicubic")
    labels = "".join(f"[s{i}]" for i in range(len(rois)))
    parts = [f"[0:v]{','.join(pre)},split={len(rois)}{labels}"]
    for i, (x, y, w, h) in enumerate(rois):
        # crop an even-aligned box in YUV (cheap), convert just that to BGR, then crop exactly
        ex, ey = x - x % 2, y - y % 2
        ew, eh = w + (x - ex) + ((w + x - ex) % 2), h + (y - ey) + ((h + y - ey) % 2)
        parts.append(f"[s{i}]crop={ew}:{eh}:{ex}:{ey},format=bgr24,crop={w}:{h}:{x - ex}:{y - ey},"
                     f"pad={W}:{h}:0:0[c{i}]")
    parts.append("".join(f"[c{i}]" for i in range(len(rois))) + f"vstack=inputs={len(rois)}[out]")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-an", "-sn",
           "-filter_complex", ";".join(parts), "-map", "[out]",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    frame_bytes = W * H * 3
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                                bufsize=frame_bytes * 2, **NO_WINDOW)
        killed = False
        try:
            idx = 0
            assert proc.stdout is not None
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                full = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                out, y0 = [], 0
                for (_, _, w, h) in rois:
                    out.append(full[y0:y0 + h, :w])
                    y0 += h
                yield idx / fps, out
                idx += 1
        finally:
            if proc.poll() is None:
                killed = True
                proc.kill()
            proc.wait()
        if not killed and proc.returncode != 0:
            errf.seek(0)
            msg = errf.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {msg[-2000:]}")


def grab_frame(path: str, t: float, ffmpeg: Optional[str] = None) -> np.ndarray:
    """Return one full-resolution BGR frame at time ``t`` (used for calibration)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    info = probe(path, ffmpeg)
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", path,
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.decode(errors='replace').strip()[-1500:]}")
    out = r.stdout
    return np.frombuffer(out, np.uint8).reshape(info.height, info.width, 3)


def cut_clip(path: str, start: float, end: float, out_path: str, ffmpeg: Optional[str] = None) -> None:
    """Export ``[start, end]`` of the video (re-encoded H.264 for accurate cuts)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, start):.3f}", "-i", path,
           "-t", f"{max(0.1, end - start):.3f}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.decode(errors='replace').strip()[-1500:]}")

