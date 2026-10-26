"""Build a single highlight video out of kill streaks, cutting out everything in between."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .video import find_ffmpeg, probe


class ReelCancelled(Exception):
    pass


@dataclass
class Segment:
    video: str
    start: float
    end: float
    label: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def plan_segments(
    items: Iterable[Tuple[str, float, float, str]],
    durations: Dict[str, float],
    pad_before: float = 3.0,
    pad_after: float = 2.0,
    order: Optional[Sequence[str]] = None,
) -> List[Segment]:
    """Turn ``(video, first_kill_time, last_kill_time, label)`` items into cut segments.

    Adds padding, clamps to the video length and merges segments of the same
    video that would overlap (so footage is never shown twice). Segments are
    ordered by ``order`` (video list order) and then by time.
    """
    by_video: Dict[str, List[Segment]] = {}
    for video, start, end, label in items:
        dur = durations.get(video) or float("inf")
        s = max(0.0, start - pad_before)
        e = min(dur, end + pad_after)
        if e - s < 0.1:
            continue
        by_video.setdefault(video, []).append(Segment(video, s, e, label))
    rank = {v: i for i, v in enumerate(order or [])}
    out: List[Segment] = []
    for video in sorted(by_video, key=lambda v: (rank.get(v, len(rank)), v)):
        segs = sorted(by_video[video], key=lambda g: g.start)
        merged = [segs[0]]
        for g in segs[1:]:
            last = merged[-1]
            if g.start <= last.end:
                last.end = max(last.end, g.end)
                last.label = " + ".join(x for x in (last.label, g.label) if x)
            else:
                merged.append(g)
        out.extend(merged)
    return out


_TIME_RE = re.compile(r"out_time_(?:ms|us)=(\d+)")


def _even(v: int) -> int:
    return v - (v % 2)


def build_reel(
    segments: Sequence[Segment],
    out_path: str,
    *,
    size: Optional[Tuple[int, int]] = None,
    fps: Optional[float] = None,
    fade: float = 0.25,
    crf: int = 22,
    preset: str = "veryfast",
    progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    ffmpeg: Optional[str] = None,
) -> str:
    """Encode ``segments`` back to back into ``out_path`` (H.264/AAC MP4).

    Every segment is scaled/letterboxed to one size and frame rate (default:
    those of the first segment's video) so clips from different recordings
    join cleanly. ``progress(fraction, message)`` is called as work proceeds.
    """
    if not segments:
        raise ValueError("no segments to put in the reel")
    ffmpeg = ffmpeg or find_ffmpeg()
    infos = {v: probe(v, ffmpeg) for v in {s.video for s in segments}}
    first = infos[segments[0].video]
    W, H = size or (first.width, first.height)
    W, H = _even(int(W)), _even(int(H))
    F = fps or min(60.0, round(first.fps) or 30.0)
    total = sum(s.duration for s in segments) or 1.0
    done = 0.0

    tmp = tempfile.mkdtemp(prefix="bf6reel_")
    try:
        parts: List[str] = []
        for n, seg in enumerate(segments):
            info = infos[seg.video]
            part = os.path.join(tmp, f"part{n:04d}.mp4")
            d = seg.duration
            fade_d = min(fade, d / 4)
            # out_range=tv: game captures are often full-range; players expect limited range
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic:out_range=tv,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={F:g},format=yuv420p")
            af = "aresample=48000,aformat=channel_layouts=stereo"
            if fade_d > 0:
                vf += f",fade=t=in:st=0:d={fade_d:.3f},fade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
                af += f",afade=t=in:st=0:d={fade_d:.3f},afade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
            cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{seg.start:.3f}", "-t", f"{d:.3f}", "-i", seg.video]
            if info.has_audio:
                amap = "0:a:0"
            else:  # silent track so every part has the same streams
                cmd += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
                amap = "1:a:0"
            cmd += ["-map", "0:v:0", "-map", amap, "-vf", vf, "-af", af,
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-color_range", "tv",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    "-video_track_timescale", "90000", "-shortest",
                    "-progress", "pipe:1", "-nostats", part]
            label = seg.label or os.path.basename(seg.video)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, errors="replace")
            assert proc.stdout is not None
            for line in proc.stdout:
                if should_cancel and should_cancel():
                    proc.kill()
                    proc.wait()
                    raise ReelCancelled()
                m = _TIME_RE.search(line)
                if m and progress:
                    t = int(m.group(1)) / 1e6
                    progress(min(0.99, (done + min(t, d)) / total), f"Clip {n + 1}/{len(segments)}: {label}")
            err = proc.stderr.read() if proc.stderr else ""
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg failed on clip {n + 1}: {err.strip()[-1500:]}")
            parts.append(part)
            done += d

        if progress:
            progress(0.995, "Joining clips")
        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w", encoding="utf-8") as f:
            for p in parts:
                f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
        out_dir = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(out_dir, exist_ok=True)
        cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listing,
               "-c", "copy", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed joining clips: {r.stderr.strip()[-1500:]}")
        if progress:
            progress(1.0, "Done")
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
