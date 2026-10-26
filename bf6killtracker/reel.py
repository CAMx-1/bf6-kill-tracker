"""Build a single highlight video out of kill streaks, cutting out everything in between.

Optional extras (see :class:`ReelOptions`): background music with game-audio
ducking and beat-synced cuts, on-screen kill call-outs and titles, slow-motion
instant replays of the key kill, export presets (YouTube, vertical 9:16,
Discord-size, GIF) and GPU encoders (NVENC / Quick Sync / AMF).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .video import NO_WINDOW, find_ffmpeg, probe


class ReelCancelled(Exception):
    pass


@dataclass
class Segment:
    video: str
    start: float
    end: float
    label: str = ""
    kills: List[Tuple[float, int]] = field(default_factory=list)   # (source time, kills) inside the clip
    title: str = ""                                                 # shown at the start with overlays on

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def key_kill(self) -> Optional[float]:
        """The kill worth replaying: the biggest multi-kill, else the last kill."""
        if not self.kills:
            return None
        return max(self.kills, key=lambda k: (k[1], k[0]))[0]


@dataclass
class ReelOptions:
    size: Optional[Tuple[int, int]] = None      # output size; default: first video's size
    fps: Optional[float] = None                 # default: first video's frame rate (max 60)
    fade: float = 0.25                          # fade in/out of every clip (s)
    crf: int = 22                               # quality (lower = better/bigger)
    preset: str = "veryfast"                    # x264 speed preset
    encoder: str = "auto"                       # auto | libx264 | h264_nvenc | h264_qsv | h264_amf | h264_videotoolbox
    vertical: bool = False                      # 9:16 crop around the crosshair and kill notification
    target_mb: Optional[float] = None           # keep the file under this size (e.g. 25 for Discord)
    # music
    music: Optional[str] = None
    music_volume: float = 0.8
    game_volume: float = 1.0
    duck: bool = True                           # lower the music while the game is loud
    beat_sync: bool = False                     # nudge cuts onto the music's beats
    # overlays
    overlays: bool = False                      # kill call-outs ("TRIPLE KILL", running count)
    titles: bool = False                        # clip title (e.g. video name/date) at the start of each clip
    # replay
    replay: bool = False                        # slow-motion replay of each clip's key kill
    replay_speed: float = 0.5
    replay_before: float = 1.2
    replay_after: float = 0.8


# name -> option overrides
PRESETS: Dict[str, dict] = {
    "Custom": {},
    "YouTube 1440p60": dict(size=(2560, 1440), fps=60, crf=18),
    "YouTube 1080p60": dict(size=(1920, 1080), fps=60, crf=19),
    "Vertical 9:16 (TikTok / Shorts / Reels)": dict(size=(1080, 1920), fps=60, crf=20, vertical=True),
    "Discord (under 25 MB)": dict(size=(1280, 720), fps=30, target_mb=24.0),
    "GIF (small, no sound)": dict(size=(480, 270), fps=12),
}


def apply_preset(name: str, base: Optional[ReelOptions] = None) -> ReelOptions:
    return replace(base or ReelOptions(), **PRESETS.get(name, {}))


def plan_segments(
    items: Iterable[tuple],
    durations: Dict[str, float],
    pad_before: float = 3.0,
    pad_after: float = 2.0,
    order: Optional[Sequence[str]] = None,
    keep_order: bool = False,
) -> List[Segment]:
    """Turn ``(video, first_kill, last_kill, label[, kills[, title]])`` items into cut segments.

    Adds padding, clamps to the video length and merges segments of the same
    video that would overlap (so footage is never shown twice). Segments are
    ordered by ``order`` (video list order) then by time - or, with
    ``keep_order``, in the order given (overlaps are still merged).
    """
    raw: List[Segment] = []
    for it in items:
        video, start, end, label = it[:4]
        kills = list(it[4]) if len(it) > 4 and it[4] else []
        title = it[5] if len(it) > 5 else ""
        dur = durations.get(video) or float("inf")
        s = max(0.0, start - pad_before)
        e = min(dur, end + pad_after)
        if e - s < 0.1:
            continue
        raw.append(Segment(video, s, e, label, kills, title))

    def merge_into(a: Segment, b: Segment) -> None:
        a.start, a.end = min(a.start, b.start), max(a.end, b.end)
        a.label = " + ".join(x for x in (a.label, b.label) if x)
        a.kills = sorted(a.kills + b.kills)
        a.title = a.title or b.title

    if keep_order:
        out: List[Segment] = []
        for g in raw:
            hit = next((m for m in out if m.video == g.video and g.start <= m.end and m.start <= g.end), None)
            if hit:
                merge_into(hit, g)
            else:
                out.append(g)
        return out
    by_video: Dict[str, List[Segment]] = {}
    for g in raw:
        by_video.setdefault(g.video, []).append(g)
    rank = {v: i for i, v in enumerate(order or [])}
    out = []
    for video in sorted(by_video, key=lambda v: (rank.get(v, len(rank)), v)):
        segs = sorted(by_video[video], key=lambda g: g.start)
        merged = [segs[0]]
        for g in segs[1:]:
            if g.start <= merged[-1].end:
                merge_into(merged[-1], g)
            else:
                merged.append(g)
        out.extend(merged)
    return out


_TIME_RE = re.compile(r"out_time_(?:ms|us)=(\d+)")


def _even(v: float) -> int:
    v = int(round(v))
    return v - (v % 2)


# --------------------------------------------------------------------------- encoders
HW_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")


@lru_cache(maxsize=16)
def encoder_works(ffmpeg: str, enc: str) -> bool:
    """Try a tiny encode: the encoder must exist *and* have a usable GPU/driver."""
    pix = "nv12" if enc == "h264_qsv" else "yuv420p"
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=black:s=320x240:r=30",
           "-frames:v", "5", "-pix_fmt", pix, "-c:v", enc, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, **NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pick_encoder(ffmpeg: str, choice: str = "auto") -> str:
    if choice and choice != "auto":
        return choice if encoder_works(ffmpeg, choice) else "libx264"
    if sys.platform == "win32":
        for enc in HW_ENCODERS:
            if encoder_works(ffmpeg, enc):
                return enc
    return "libx264"


def encoder_args(enc: str, crf: int, preset: str, bitrate: Optional[int]) -> List[str]:
    rate = ["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(2 * bitrate)] if bitrate else []
    if enc == "h264_nvenc":
        q = rate or ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        return ["-c:v", enc, "-preset", "p5", *q, "-pix_fmt", "yuv420p"]
    if enc == "h264_qsv":
        q = rate or ["-global_quality", str(crf)]
        return ["-c:v", enc, "-preset", "medium", *q, "-pix_fmt", "nv12"]
    if enc == "h264_amf":
        q = (["-rc", "vbr_peak", *rate] if rate else ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)])
        return ["-c:v", enc, "-quality", "quality", *q, "-pix_fmt", "yuv420p"]
    if enc == "h264_videotoolbox":
        q = rate or ["-q:v", str(max(1, min(100, 100 - 2 * crf)))]
        return ["-c:v", enc, *q, "-pix_fmt", "yuv420p"]
    q = rate or ["-crf", str(crf)]
    return ["-c:v", "libx264", "-preset", preset, *q, "-pix_fmt", "yuv420p"]


# --------------------------------------------------------------------------- building
def _run(cmd: List[str], dur: float, on_time: Callable[[float], None], should_cancel, what: str) -> None:
    proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                            errors="replace", **NO_WINDOW)
    assert proc.stdout is not None
    for line in proc.stdout:
        if should_cancel and should_cancel():
            proc.kill()
            proc.wait()
            raise ReelCancelled()
        m = _TIME_RE.search(line)
        if m:
            on_time(min(dur, int(m.group(1)) / 1e6))
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed on {what}: {err.strip()[-1500:]}")


@dataclass
class _Part:
    seg: Segment
    start: float
    dur: float
    speed: float = 1.0           # < 1: slow motion replay
    replay: bool = False

    @property
    def out_dur(self) -> float:
        return self.dur / self.speed


def _plan_parts(segments: Sequence[Segment], o: ReelOptions, infos) -> List[_Part]:
    parts: List[_Part] = []
    for seg in segments:
        parts.append(_Part(seg, seg.start, seg.duration))
        k = seg.key_kill() if o.replay else None
        if k is not None:
            a = max(0.0, k - o.replay_before)
            b = min(infos[seg.video].duration or k + o.replay_after, k + o.replay_after)
            if b - a > 0.3:
                parts.append(_Part(seg, a, b - a, o.replay_speed, True))
    return parts


def _beat_sync(parts: List[_Part], beats: List[float], infos, max_change: float = 0.6) -> None:
    """Change the length of normal clips (not replays) so cuts land on beats."""
    import numpy as np
    b = np.asarray(beats)
    cum = 0.0
    for p in parts:
        target = cum + p.out_dur
        if not p.replay and len(b):
            limit = infos[p.seg.video].duration or float("inf")
            lo = cum + min(1.5, p.out_dur)
            cands = b[(b >= lo) & (np.abs(b - target) <= max_change)]
            cands = cands[cands - cum <= limit - p.start]
            if len(cands):
                p.dur = float(cands[np.argmin(np.abs(cands - target))]) - cum
        cum += p.out_dur


def _overlay_specs(p: _Part, o: ReelOptions, W: int, H: int, tmp: str, n: int):
    """[(png_path, x, y, t_from, t_to)] in output-clip time."""
    from .overlay import multikill_name, render_text
    specs = []
    if p.replay:
        path = os.path.join(tmp, f"ov{n}_replay.png")
        w, h = render_text(path, "REPLAY", int(H * 0.045), color=(255, 210, 80))
        specs.append((path, int(W * 0.04), int(H * 0.06), 0.0, p.out_dur))
        return specs
    if o.titles and p.seg.title:
        path = os.path.join(tmp, f"ov{n}_title.png")
        w, h = render_text(path, p.seg.title, int(H * 0.035))
        specs.append((path, (W - w) // 2, int(H * 0.82) - h, 0.2, min(p.out_dur, 3.0)))
    if o.overlays and p.seg.kills:
        running = 0
        ks = sorted(p.seg.kills)
        for i, (t, c) in enumerate(ks):
            running += c
            rel = t - p.start
            if rel < 0 or rel > p.out_dur:
                continue
            nxt = ks[i + 1][0] - p.start if i + 1 < len(ks) else rel + 1.8
            until = min(p.out_dur, rel + 1.8, max(rel + 0.6, nxt))
            main = multikill_name(c) if c > 1 else ("KILL" if running == 1 else f"{running} KILL STREAK")
            sub = f"{running} KILLS" if c > 1 and running > c else ""
            path = os.path.join(tmp, f"ov{n}_{i}.png")
            w, h = render_text(path, main, int(H * 0.05), accent=(255, 140, 30), sub=sub)
            specs.append((path, (W - w) // 2, int(H * 0.22), rel + 0.15, until))
    return specs


def _part_cmd(ffmpeg: str, p: _Part, info, o: ReelOptions, W: int, H: int, F: float, enc_args: List[str],
              out: str, overlays) -> List[str]:
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{p.start:.3f}", "-t", f"{p.dur:.3f}", "-i", p.seg.video]
    idx = 1
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{p.dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        a_in = f"{idx}:a"
        idx += 1
    else:
        a_in = "0:a:0"
    ov_inputs = []
    for path, *_ in overlays:
        cmd += ["-loop", "1", "-t", f"{p.out_dur:.3f}", "-i", path]
        ov_inputs.append(idx)
        idx += 1
    d = p.out_dur
    fade_d = min(o.fade, d / 4)
    chain = "[0:v]"
    if p.speed != 1.0:
        chain += f"setpts=PTS/{p.speed:g},"
    if o.vertical:
        # 9:16 window that holds the crosshair and the kill notification to its left
        chain += "crop=ih*9/16:ih:max(0\\,min(iw-ih*9/16\\,iw/2-ih*0.375)):0,"
    chain += (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic:out_range=tv,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={F:g},format=yuv420p")
    graph = [chain + "[v0]"]
    last = "v0"
    for j, ((path, x, y, a, b), inp) in enumerate(zip(overlays, ov_inputs)):
        fin, fout = 0.15, 0.2
        graph.append(f"[{inp}:v]format=rgba,fade=t=in:st={a:.3f}:d={fin}:alpha=1,"
                     f"fade=t=out:st={max(a, b - fout):.3f}:d={fout}:alpha=1[o{j}]")
        graph.append(f"[{last}][o{j}]overlay={x}:{y}:enable='between(t,{a:.3f},{b:.3f})'[v{j + 1}]")
        last = f"v{j + 1}"
    vf_tail = "format=yuv420p"
    if fade_d > 0:
        vf_tail += f",fade=t=in:st=0:d={fade_d:.3f},fade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
    graph.append(f"[{last}]{vf_tail}[vout]")
    af = f"[{a_in}]aresample=48000,aformat=channel_layouts=stereo"
    if p.speed != 1.0:
        sp = p.speed
        while sp < 0.5:              # atempo takes 0.5..2 per stage
            af += ",atempo=0.5"
            sp /= 0.5
        af += f",atempo={sp:g}"
    af += f",volume={o.game_volume:g}"
    if fade_d > 0:
        af += f",afade=t=in:st=0:d={fade_d:.3f},afade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
    graph.append(af + "[aout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", f"{d:.3f}",
            *enc_args, "-r", f"{F:g}", "-color_range", "tv",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-video_track_timescale", "90000", out]
    return cmd


def build_reel(
    segments: Sequence[Segment],
    out_path: str,
    *,
    options: Optional[ReelOptions] = None,
    size: Optional[Tuple[int, int]] = None,
    fps: Optional[float] = None,
    fade: Optional[float] = None,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
    progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    ffmpeg: Optional[str] = None,
) -> str:
    """Encode ``segments`` back to back into ``out_path`` (H.264/AAC MP4, or GIF for ``.gif``).

    Every clip is scaled/letterboxed to one size and frame rate so recordings
    of different formats join cleanly. ``progress(fraction, message)`` is
    called as work proceeds.
    """
    if not segments:
        raise ValueError("no segments to put in the reel")
    o = replace(options or ReelOptions())
    for k, v in dict(size=size, fps=fps, fade=fade, crf=crf, preset=preset).items():
        if v is not None:
            setattr(o, k, v)
    ffmpeg = ffmpeg or find_ffmpeg()
    gif = out_path.lower().endswith(".gif")
    infos = {v: probe(v, ffmpeg) for v in {s.video for s in segments}}
    first = infos[segments[0].video]
    if o.size:
        W, H = _even(o.size[0]), _even(o.size[1])
    elif o.vertical:
        W, H = 1080, 1920
    else:
        W, H = _even(first.width), _even(first.height)
    F = o.fps or min(60.0, round(first.fps) or 30.0)
    report = progress or (lambda f, m: None)

    parts = _plan_parts(segments, o, infos)
    beats: List[float] = []
    if o.music and o.beat_sync and not gif:
        from .music import detect_beats, load_mono
        _, beats = detect_beats(load_mono(o.music, max_s=sum(p.out_dur for p in parts) + 30))
        _beat_sync(parts, beats, infos)
    total = sum(p.out_dur for p in parts) or 1.0

    enc = pick_encoder(ffmpeg, o.encoder)
    tmp = tempfile.mkdtemp(prefix="bf6reel_")
    try:
        bitrate = None
        if o.target_mb and not gif:
            audio = 160_000 if not o.music else 192_000
            bitrate = max(300_000, int(o.target_mb * 8_000_000 * 0.93 / total) - audio)
        for attempt in range(3):
            files = _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report, should_cancel, total)
            if files is None:            # GPU encoder failed: redo everything in software
                enc = "libx264"
                files = _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report,
                                      should_cancel, total)
            joined = os.path.join(tmp, "joined.mp4")
            report(0.96, "Joining clips")
            _concat(ffmpeg, files, joined, tmp)
            final = joined
            if o.music and not gif:
                report(0.97, "Adding music")
                final = os.path.join(tmp, "music.mp4")
                _mix_music(ffmpeg, joined, o, total, final)
            out_dir = os.path.dirname(os.path.abspath(out_path))
            os.makedirs(out_dir, exist_ok=True)
            if gif:
                report(0.98, "Making GIF")
                _to_gif(ffmpeg, final, out_path, W, F)
                break
            _faststart(ffmpeg, final, out_path)
            if not o.target_mb or os.path.getsize(out_path) <= o.target_mb * 1_000_000 or attempt == 2:
                break
            bitrate = int((bitrate or 1_000_000) * 0.8)   # over budget: shrink and try again
            report(0.0, "File too big, re-encoding smaller")
        report(1.0, "Done")
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report, should_cancel, total):
    files: List[str] = []
    done = 0.0
    enc_args = encoder_args(enc, o.crf, o.preset, bitrate)
    for n, p in enumerate(parts):
        out = os.path.join(tmp, f"part{n:04d}.mp4")
        ovs = _overlay_specs(p, o, W, H, tmp, n)
        cmd = _part_cmd(ffmpeg, p, infos[p.seg.video], o, W, H, F, enc_args, out, ovs)
        label = ("Replay: " if p.replay else "") + (p.seg.label or os.path.basename(p.seg.video))
        base = done
        try:
            _run(cmd, p.out_dur, lambda t, b=base: report(min(0.95, 0.95 * (b + t) / total),
                                                          f"Clip {n + 1}/{len(parts)}: {label}"),
                 should_cancel, f"clip {n + 1}")
        except RuntimeError:
            if enc != "libx264" and n == 0:
                return None
            raise
        files.append(out)
        done += p.out_dur
    return files


def _concat(ffmpeg: str, files: List[str], out: str, tmp: str) -> None:
    listing = os.path.join(tmp, "list.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    _simple(ffmpeg, ["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", out], "joining clips")


def _mix_music(ffmpeg: str, video: str, o: ReelOptions, total: float, out: str) -> None:
    fade_out = min(3.0, total / 4)
    music = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=N/SR/TB,"
             f"volume={o.music_volume:g},afade=t=in:st=0:d=0.5,"
             f"afade=t=out:st={max(0.0, total - fade_out):.3f}:d={fade_out:.3f}")
    if o.duck:
        graph = (f"{music}[m];[0:a]asplit=2[g][sc];"
                 "[m][sc]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=350[md];"
                 "[md][g]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        graph = f"{music}[m];[m][0:a]amix=inputs=2:duration=longest:normalize=0[a]"
    _simple(ffmpeg, ["-i", video, "-stream_loop", "-1", "-i", o.music, "-filter_complex", graph,
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                     "-t", f"{total:.3f}", out], "adding music")


def _to_gif(ffmpeg: str, video: str, out: str, W: int, F: float) -> None:
    w = min(W, 480)
    graph = (f"[0:v]fps={min(F, 12):g},scale={w}:-1:flags=lanczos,split[a][b];"
             "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
    _simple(ffmpeg, ["-i", video, "-filter_complex", graph, "-loop", "0", out], "making the GIF")


def _faststart(ffmpeg: str, src: str, out: str) -> None:
    _simple(ffmpeg, ["-i", src, "-c", "copy", "-movflags", "+faststart", out], "writing the video")


def _simple(ffmpeg: str, args: List[str], what: str) -> None:
    r = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, errors="replace",
                       **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed {what}: {r.stderr.strip()[-1500:]}")
