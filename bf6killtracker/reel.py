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

from .overlay import CalloutStyle
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
    captions: List[Tuple[float, str]] = field(default_factory=list)  # (source time, "HEADSHOT · RIFLE")

    @property
    def headshots(self) -> List[float]:
        return [t for t, c in self.captions if "HEADSHOT" in c.upper()]

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
    callout: Optional["CalloutStyle"] = None    # look/placement of call-outs (default style if None)
    titles: bool = False                        # clip title (e.g. video name/date) at the start of each clip
    # replay
    replay: bool = False                        # slow-motion replay of each clip's key kill
    replay_speed: float = 0.5
    replay_before: float = 1.2
    replay_after: float = 0.8
    replay_finale_only: bool = False            # one replay, of the last clip (the montage's best moment)
    # transitions between clips: "fade" (dip to black), "cut", "crossfade", "whip", "zoom"
    transition: str = "fade"
    transition_s: float = 0.35
    # kill-cam effects: "none", "zoom" (punch in on the crosshair at each kill), "shake" (on headshots), "both"
    killcam: str = "none"
    captions: bool = False                      # weapon / HEADSHOT caption under each kill
    # branding
    intro_text: str = ""                        # e.g. your gamertag; shown on a title card
    intro_sub: str = ""
    outro_text: str = ""                        # e.g. "Thanks for watching"
    outro_sub: str = ""                         # e.g. "Subscribe for more"
    card_s: float = 2.5
    watermark_text: str = ""
    watermark_image: str = ""
    watermark_pos: str = "Top right"            # Top left / Top right / Bottom left / Bottom right
    watermark_opacity: float = 0.7
    # audio
    audio_track: int = 0                        # which audio track of the recording (0 = first; OBS can record more)
    denoise: bool = False                       # reduce background noise/hiss (useful for mic tracks)
    loudnorm: bool = False                      # even out loudness (YouTube's -14 LUFS)
    # look
    grade: str = "None"                         # colour grade, see GRADES
    letterbox: bool = False                     # cinematic black bars
    vignette: bool = False
    grain: bool = False                         # film grain
    # MLG mode (see mlg.py): hitmarkers, air horns, soda cans, meme text, strobe, "OH BABY A TRIPLE"
    mlg: bool = False
    mlg_intensity: str = "normal"               # mild | normal | max (how much stuff on screen)
    mlg_strobe: str = "normal"                  # off | light (colour only) | normal (+ flashes) | max (rapid flashing)
    mlg_airhorn: bool = True
    mlg_cans: bool = True
    mlg_hitmarkers: bool = True
    mlg_text: bool = True                       # "OH", "WOW", "GET REKT"...
    mlg_extras: bool = True                     # money rain, illuminati, pixel shades on multi-kills/triples
    mlg_triple: bool = True                     # "OH BABY A TRIPLE" on 3 kills in a few seconds
    mlg_triple_sound: str = ""                  # your own clip; empty = the computer's voice
    mlg_airhorn_sound: str = ""                 # your own air horn; empty = the built-in one
    mlg_seed: int = 0                           # change for a different random layout
    mlg_deepfry: str = "triples"                # off | triples | always (every kill)
    mlg_airhorn_chance: float = 1.0             # share of single kills that get a horn (multi-kills always do)
    mlg_sfx_volume: float = 1.0                 # loudness of all MLG sounds
    mlg_words: str = ""                         # your own meme words, comma separated (empty = built-in)
    mlg_triple_text: str = "OH BABY A TRIPLE"
    mlg_chips: bool = True
    mlg_logo: bool = True
    mlg_voices: bool = False
    mlg_illuminati: bool = True
    mlg_shades: bool = True
    mlg_ohscreen: bool = True
    mlg_hitflood: bool = True
    mlg_explosions: bool = True
    mlg_scope: bool = True
    mlg_smoke: bool = False
    mlg_spin: bool = True
    mlg_shake: bool = True
    mlg_wub: bool = True
    mlg_fbi: bool = True
    mlg_wow: bool = True
    mlg_what: bool = True
    mlg_omgwow: bool = True
    mlg_money_sound: bool = True
    mlg_dota: bool = True
    mlg_boompow: bool = True
    mlg_headshot_sound: bool = True
    mlg_mtndew_sound: bool = True
    mlg_intro: bool = True
    mlg_auto: bool = False                      # scale the chaos to each clip (big moments get more)
    mlg_suggest: bool = False                   # pick MLG settings from the clips themselves (mlg.suggest)
    mlg_music: str = "off"                      # off | sandstorm | sandstorm_flute (from your sounds folder)
    mlg_siren: bool = False
    mlg_bassboost: bool = True


# name -> option overrides
PRESETS: Dict[str, dict] = {
    "Custom": {},
    "YouTube 1440p60": dict(size=(2560, 1440), fps=60, crf=18),
    "YouTube 1080p60": dict(size=(1920, 1080), fps=60, crf=19),
    "Vertical 9:16 (TikTok / Shorts / Reels)": dict(size=(1080, 1920), fps=60, crf=20, vertical=True),
    "Discord (under 25 MB)": dict(size=(1280, 720), fps=30, target_mb=24.0),
    "GIF (small, no sound)": dict(size=(480, 270), fps=12),
}


GRADES: Dict[str, str] = {
    "None": "",
    "Vivid": "eq=saturation=1.35:contrast=1.08",
    "Warm": "colorbalance=rs=.06:gs=.01:bs=-.07:rm=.05:bm=-.05,eq=saturation=1.1",
    "Cool": "colorbalance=rs=-.05:bs=.07:rm=-.04:bm=.06",
    "Teal & orange": "colorbalance=rs=-.08:bs=.08:rh=.08:gh=.02:bh=-.09,eq=saturation=1.2:contrast=1.08",
    "Cinematic": "eq=contrast=1.12:saturation=0.88:gamma=0.95,colorbalance=rs=-.05:bs=.05:rh=.05:bh=-.04",
    "Black & white": "hue=s=0,eq=contrast=1.25",
    "Sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
    "Vintage": "curves=preset=vintage,eq=saturation=0.85",
    "Neon": "eq=saturation=1.9:contrast=1.15,hue=h=15",
    "Night vision": "hue=s=0,lutrgb=r=val*0.25:g=min(255\\,val*1.3):b=val*0.25,noise=alls=10:allf=t",
    "Deep fried": "eq=saturation=3:contrast=1.6,unsharp=7:7:3,noise=alls=14:allf=t",
}

# keys a template always sets (back to defaults if it doesn't mention them), so looks don't mix
LOOK_KEYS = ("grade", "letterbox", "vignette", "grain", "transition", "killcam", "captions", "replay_speed")

_MLG = dict(mlg=True, transition="cut", killcam="both", overlays=True, captions=False, replay=True,
            replay_speed=0.5, beat_sync=True, loudnorm=True,
            callout=dict(position="Upper third", size=120, color=(255, 230, 0), shadow=True,
                         text_multi="{name}!!", text_streak="{n} IN A ROW"))

# looks that bundle effects; "callout" entries override the call-out style
TEMPLATES: Dict[str, dict] = {
    "Clean": dict(transition="fade", killcam="none", overlays=False, captions=False, replay=False),
    "Montage": dict(transition="whip", killcam="zoom", overlays=True, captions=True, replay=True, beat_sync=True,
                    loudnorm=True, grade="Vivid", callout=dict(position="Upper third", size=110, shadow=True)),
    "Cinematic": dict(transition="crossfade", transition_s=0.6, killcam="zoom", overlays=False, captions=False,
                      replay=True, replay_speed=0.35, grade="Cinematic", letterbox=True, vignette=True,
                      loudnorm=True),
    "Esports": dict(transition="cut", killcam="zoom", overlays=True, captions=True, replay=True, beat_sync=True,
                    loudnorm=True, grade="Vivid",
                    callout=dict(position="Upper third", size=120, color=(255, 255, 255), accent=True,
                                 accent_color=(0, 200, 255), shadow=True)),
    "Sniper elite": dict(transition="zoom", killcam="both", overlays=True, captions=True, replay=True,
                         replay_speed=0.3, grade="Cool", vignette=True,
                         callout=dict(position="Bottom", size=100, color=(200, 230, 255), text_first="TARGET DOWN")),
    "Teal & orange": dict(transition="whip", killcam="zoom", overlays=True, captions=True, replay=True,
                          beat_sync=True, grade="Teal & orange", vignette=True, loudnorm=True),
    "Retro VHS": dict(transition="crossfade", killcam="shake", overlays=True, captions=False, replay=True,
                      grade="Vintage", grain=True, vignette=True,
                      callout=dict(position="Bottom left", size=90, color=(255, 255, 255), uppercase=True)),
    "Noir": dict(transition="fade", transition_s=0.6, killcam="none", overlays=True, captions=False, replay=True,
                 replay_speed=0.35, grade="Black & white", letterbox=True, vignette=True, grain=True,
                 callout=dict(position="Bottom", size=80, color=(240, 240, 240), uppercase=False)),
    "Night ops": dict(transition="cut", killcam="shake", overlays=True, captions=True, replay=False,
                      grade="Night vision", vignette=True,
                      callout=dict(position="Upper third", size=90, color=(120, 255, 120))),
    "Neon": dict(transition="zoom", killcam="both", overlays=True, captions=True, replay=True, beat_sync=True,
                 grade="Neon", loudnorm=True,
                 callout=dict(position="Centre", size=130, color=(255, 60, 220), shadow=True)),
    "Chill": dict(transition="crossfade", transition_s=0.8, killcam="none", overlays=False, captions=False,
                  replay=False, grade="Warm", loudnorm=True),
    "Deep fried": dict(transition="cut", killcam="both", overlays=True, captions=True, replay=True,
                       grade="Deep fried", callout=dict(position="Centre", size=150, color=(255, 0, 0))),
    "TikTok hype": dict(vertical=True, size=(1080, 1920), transition="zoom", killcam="both", overlays=True,
                        captions=True, replay=False, beat_sync=True, loudnorm=True, grade="Vivid",
                        callout=dict(position="Centre", size=150, color=(255, 220, 40), shadow=True,
                                     text_streak="{n} IN A ROW", text_multi="{name}!")),
    "Funny moments": dict(transition="crossfade", killcam="shake", overlays=True, captions=False, replay=True,
                          replay_speed=0.35, callout=dict(position="Bottom", size=90, uppercase=False,
                                                         text_first="gottem", text_streak="{n} and counting",
                                                         text_multi="{name}?!")),
    "MLG": dict(_MLG),
    "MLG lite": dict(_MLG, mlg_intensity="mild", mlg_strobe="light", mlg_deepfry="off", mlg_airhorn_chance=0.5,
                     mlg_hitflood=False, mlg_spin=False, mlg_bassboost=False, mlg_wub=False),
    "MLG MAX": dict(_MLG, mlg_intensity="max", mlg_deepfry="always", mlg_voices=True, mlg_smoke=True,
                    mlg_siren=True, grade="Vivid", mlg_music="sandstorm"),
    "MLG 360 no scope": dict(_MLG, mlg_scope=True, mlg_spin=True, mlg_hitflood=True,
                             mlg_words="360 NO SCOPE,NO SCOPE,QUICKSCOPED,REKT,GET REKT,#rekt,ur dead m8"),
    "MLG sounds only": dict(_MLG, mlg_strobe="off", mlg_deepfry="off", mlg_cans=False, mlg_chips=False,
                            mlg_text=False, mlg_extras=False, mlg_illuminati=False, mlg_shades=False,
                            mlg_ohscreen=False, mlg_hitflood=False, mlg_explosions=False, mlg_scope=False,
                            mlg_spin=False, mlg_shake=False, mlg_voices=True, mlg_siren=True),
    "MLG TikTok": dict(_MLG, vertical=True, size=(1080, 1920)),
    "MLG auto (fits your clips)": dict(_MLG, mlg_suggest=True, mlg_auto=True),
}


def template_values(name: str) -> dict:
    """A template's settings with every look/MLG key filled in (defaults where the template is silent)."""
    from .mlg import defaults as mlg_defaults
    base = ReelOptions()
    t = {k: getattr(base, k) for k in LOOK_KEYS}
    t.update({k: v for k, v in mlg_defaults().items()
              if k not in ("mlg_triple_sound", "mlg_airhorn_sound", "mlg_seed")})   # keep the user's own sounds
    t.update(TEMPLATES.get(name, {}))
    return t


def apply_template(name: str, base: Optional[ReelOptions] = None) -> ReelOptions:
    o = replace(base or ReelOptions())
    t = template_values(name) if name in TEMPLATES else {}
    co = t.pop("callout", None)
    for k, v in t.items():
        setattr(o, k, v)
    if co is not None:
        from .overlay import CalloutStyle
        st = CalloutStyle.from_json((o.callout or CalloutStyle()).to_json())
        for k, v in co.items():
            setattr(st, k, v)
        o.callout = st
    return o


def caption_for(types: Sequence[str], weapons: Sequence[str]) -> str:
    """'HEADSHOT · RIFLE' style caption for one kill event."""
    names = {"gun_rifle": "RIFLE", "gun_m16": "M16", "gun_smg": "SMG", "gun_pistol": "PISTOL",
             "explosive_c4": "C4"}
    w = next((names.get(x, x.split("_", 1)[-1].upper()) for x in weapons if x and x != "other"), "")
    parts = ["HEADSHOT"] if "headshot" in types else (["EXPLOSIVE"] if "explosive" in types and not w else [])
    if w:
        parts.append(w)
    return " · ".join(parts)


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
    """Turn ``(video, first_kill, last_kill, label[, kills[, title[, captions]]])`` items into cut segments.

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
        caps = list(it[6]) if len(it) > 6 and it[6] else []
        dur = durations.get(video) or float("inf")
        s = max(0.0, start - pad_before)
        e = min(dur, end + pad_after)
        if e - s < 0.1:
            continue
        raw.append(Segment(video, s, e, label, kills, title, caps))

    def merge_into(a: Segment, b: Segment) -> None:
        a.start, a.end = min(a.start, b.start), max(a.end, b.end)
        a.label = " + ".join(x for x in (a.label, b.label) if x)
        a.kills = sorted(a.kills + b.kills)
        a.title = a.title or b.title
        a.captions = sorted(a.captions + b.captions)

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
class _Stalled(Exception):
    pass


def _run(cmd: List[str], dur: float, on_time: Callable[[float], None], should_cancel, what: str) -> None:
    """Run ffmpeg with progress; if it stops making progress (some ffmpeg builds occasionally freeze
    on overlay-heavy graphs), kill it and try once more."""
    stall = float(os.environ.get("BF6KT_STALL_S", "20"))
    for attempt in range(2):
        try:
            return _run_once(cmd, dur, on_time, should_cancel, what, stall)
        except _Stalled:
            if attempt:
                raise RuntimeError(f"ffmpeg stopped responding on {what}")


def _run_once(cmd, dur, on_time, should_cancel, what, stall: float) -> None:
    import queue
    import threading
    import time as _time
    # stderr goes to a file: a pipe nobody reads fills up on chatty filters and stalls ffmpeg
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats", "-stats_period", "0.5"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                                universal_newlines=True, errors="replace", **NO_WINDOW)
        assert proc.stdout is not None
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump() -> None:
            for ln in proc.stdout:            # type: ignore[union-attr]
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        last = _time.monotonic()
        pos_seen = -1
        while True:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait()
                raise ReelCancelled()
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                if _time.monotonic() - last > stall:
                    proc.kill()
                    proc.wait()
                    raise _Stalled()
                continue
            if line is None:
                break
            m = _TIME_RE.search(line)
            if m:
                pos = int(m.group(1))
                if pos != pos_seen:             # frozen ffmpeg keeps printing the same time
                    pos_seen, last = pos, _time.monotonic()
                on_time(min(dur, pos / 1e6))
            if _time.monotonic() - last > stall:
                proc.kill()
                proc.wait()
                raise _Stalled()
        if proc.wait() != 0:
            errf.seek(0)
            raise RuntimeError(f"ffmpeg failed on {what}: {errf.read().strip()[-1500:]}")


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
    for i, seg in enumerate(segments):
        parts.append(_Part(seg, seg.start, seg.duration))
        want = o.replay and (not o.replay_finale_only or i == len(segments) - 1)
        k = seg.key_kill() if want else None
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
    from .overlay import CalloutStyle, render_callout, render_text
    st = o.callout or CalloutStyle()
    common = dict(font_path=st.font, outline_pct=st.outline, outline_color=st.outline_color, shadow=st.shadow)
    specs = []
    if p.replay:
        path = os.path.join(tmp, f"ov{n}_replay.png")
        w, h = render_text(path, "REPLAY", int(H * 0.045), color=(255, 210, 80), **common)
        specs.append((path, int(W * 0.04), int(H * 0.06), 0.0, p.out_dur))
        return specs
    if o.titles and p.seg.title:
        path = os.path.join(tmp, f"ov{n}_title.png")
        w, h = render_text(path, p.seg.title, int(H * 0.035), color=st.color, **common)
        specs.append((path, (W - w) // 2, int(H * 0.82) - h, 0.2, min(p.out_dur, 3.0)))
    if o.overlays and p.seg.kills:
        running = 0
        ks = sorted(p.seg.kills)
        for i, (t, c) in enumerate(ks):
            running += c
            rel = (t - p.start) / p.speed
            if rel < 0 or rel > p.out_dur:
                continue
            nxt = (ks[i + 1][0] - p.start) / p.speed if i + 1 < len(ks) else rel + st.duration
            until = min(p.out_dur, rel + st.duration, max(rel + 0.6, nxt))
            main, sub = st.texts(c, running)
            if not main.strip():
                continue
            img = render_callout(st, main, sub, H)
            path = os.path.join(tmp, f"ov{n}_{i}.png")
            img.save(path)
            x, y = st.place(img.width, img.height, W, H)
            specs.append((path, x, y, rel + 0.15, until))
    if o.captions and p.seg.captions:
        for i, (t, text) in enumerate(sorted(p.seg.captions)):
            rel = (t - p.start) / p.speed
            if not text or rel < 0 or rel > p.out_dur:
                continue
            path = os.path.join(tmp, f"ov{n}_cap{i}.png")
            w, h = render_text(path, text, int(H * 0.028), color=(235, 235, 235), **common)
            ax, ay = st.anchor()
            cy = int(ay * H) + int(H * 0.05 * st.size / 100) + int(H * 0.03)   # just under the call-out
            if cy + h > H:
                cy = int(ay * H) - int(H * 0.09)
            specs.append((path, max(0, min(W - w, int(ax * W - w / 2))), max(0, cy), rel + 0.25,
                          min(p.out_dur, rel + st.duration)))
    return specs


def _look_filters(o: ReelOptions, W: int, H: int) -> str:
    vf = ""
    g = GRADES.get(o.grade, "")
    if g:
        vf += "," + g + ",format=yuv420p"
    if o.grain:
        vf += ",noise=alls=9:allf=t+u"
    if o.vignette:
        vf += ",vignette=PI/4.5"
    if o.letterbox and not o.vertical:
        bar = max(2, int(round((H - W / 2.39) / 2)))
        vf += f",drawbox=x=0:y=0:w={W}:h={bar}:color=black:t=fill,drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=black:t=fill"
    return vf


def demo_segment(folder: str, ffmpeg: Optional[str] = None, seconds: float = 7.0) -> Segment:
    """A made-up clip (moving colours + rumble) with a kill, a headshot, a triple and a C4 double kill,
    for previewing a look when no recording is at hand."""
    ffmpeg = ffmpeg or find_ffmpeg()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "bf6kt_demo2.mp4")
    if not os.path.isfile(path):
        _simple(ffmpeg, ["-f", "lavfi", "-i", f"gradients=s=1280x720:r=30:d={seconds}:speed=0.02:n=4:c0=0x3a4a32:c1=0x6a5a40:"
                         "c2=0x2a3442:c3=0x8a8270",
                         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=brown:a=0.08",
                         "-vf", "drawgrid=w=160:h=90:t=1:c=white@0.15,drawbox=x=630:y=350:w=20:h=20:c=white:t=2",
                         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                         "-shortest", path], "making the demo clip")
    kills = [(0.8, 1), (1.9, 1), (3.0, 1), (5.2, 2)]
    caps = [(0.8, "RIFLE"), (1.9, "HEADSHOT · RIFLE"), (3.0, "RIFLE"), (5.2, "C4")]
    return Segment(path, 0.0, seconds, "Demo", kills, "Preview", caps)


def preview_options(o: ReelOptions) -> ReelOptions:
    """Fast, small settings for a look preview (keeps every effect)."""
    p = replace(o, crf=30, preset="ultrafast", target_mb=None, encoder="libx264", fps=30.0,
                card_s=min(o.card_s, 1.5), beat_sync=False)
    p.size = (360, 640) if o.vertical else (640, 360)
    return p


def _killcam_filters(p: "_Part", o: ReelOptions, W: int, H: int, F: float) -> str:
    """Zoom punch-in at kills and/or screen shake on headshots (output-clip time)."""
    if p.replay or o.killcam in ("", "none"):
        return ""
    out = ""
    rel = lambda t: (t - p.start) / p.speed  # noqa: E731
    if o.killcam in ("zoom", "both") and p.seg.kills:
        bumps = "+".join(f"max(0\\,1-abs(it-{rel(t) + 0.08:.3f})/0.45)" for t, _ in p.seg.kills
                         if 0 <= rel(t) <= p.out_dur)
        if bumps:
            z = f"min(1.22\\,1+0.2*({bumps}))"
            out += (f",zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={F:g}")
    if o.killcam in ("shake", "both") and p.seg.headshots:
        on = "+".join(f"between(t\\,{rel(t):.3f}\\,{rel(t) + 0.35:.3f})" for t in p.seg.headshots
                      if 0 <= rel(t) <= p.out_dur)
        if on:
            sh = max(4, int(H * 0.012))
            out += (f",crop={W - 2 * sh}:{H - 2 * sh}:x='{sh}+{sh}*sin(t*97)*({on})':"
                    f"y='{sh}+{sh}*cos(t*71)*({on})',scale={W}:{H},setsar=1")
    return out


def _part_cmd(ffmpeg: str, p: _Part, info, o: ReelOptions, W: int, H: int, F: float, enc_args: List[str],
              out: str, overlays, watermark=None, fades: bool = True, mlg=None) -> List[str]:
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{p.start:.3f}", "-t", f"{p.dur:.3f}", "-i", p.seg.video]
    idx = 1
    if not info.has_audio:
        cmd += ["-f", "lavfi", "-t", f"{p.dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        a_in = f"{idx}:a"
        idx += 1
    else:
        a_in = f"0:a:{max(0, min(o.audio_track, (info.audio_streams or 1) - 1))}"
    ov_inputs = []
    for path, *_ in overlays:
        cmd += ["-loop", "1", "-t", f"{p.out_dur:.3f}", "-i", path]
        ov_inputs.append(idx)
        idx += 1
    wm_in = None
    if watermark:
        cmd += ["-loop", "1", "-t", f"{p.out_dur:.3f}", "-i", watermark[0]]
        wm_in = idx
        idx += 1
    stickers = list(mlg.stickers) if mlg else []
    st_inputs = []
    for s_ in stickers:
        cmd += ["-loop", "1", "-framerate", f"{F:g}", "-t", f"{max(0.05, s_.b - s_.a):.3f}", "-i", s_.png]
        st_inputs.append(idx)
        idx += 1
    sfx_all = list(mlg.sfx) if mlg else []
    sfx_idx = [i for i, (t, _, _) in enumerate(sfx_all) if 0 <= t < p.out_dur]
    sfx = [sfx_all[i] for i in sfx_idx]
    sfx_inputs = []
    for _, f, _ in sfx:
        cmd += ["-i", f]
        sfx_inputs.append(idx)
        idx += 1
    d = p.out_dur
    fade_d = min(o.fade, d / 4) if fades else 0.0
    chain = "[0:v]"
    if p.speed != 1.0:
        chain += f"setpts=PTS/{p.speed:g},"
    if o.vertical:
        # 9:16 window that holds the crosshair and the kill notification to its left
        chain += "crop=ih*9/16:ih:max(0\\,min(iw-ih*9/16\\,iw/2-ih*0.375)):0,"
    chain += (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic:out_range=tv,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={F:g},format=yuv420p")
    chain += _killcam_filters(p, o, W, H, F)
    chain += _look_filters(o, W, H)
    if mlg and mlg.vf:
        chain += mlg.vf
    graph = [chain + "[v0]"]
    last = "v0"
    for j, (s_, inp) in enumerate(zip(stickers, st_inputs)):
        rot = (f",rotate=a='{s_.rot}':c=black@0:ow='hypot(iw\\,ih)':oh='hypot(iw\\,ih)'" if s_.rot else "")
        graph.append(f"[{inp}:v]format=rgba{rot},setpts=PTS+{s_.a:.3f}/TB[m{j}]")
        graph.append(f"[{last}][m{j}]overlay=x='{s_.x}':y='{s_.y}':eof_action=pass[mv{j}]")
        last = f"mv{j}"
    if mlg and mlg.vf_post:
        graph.append(f"[{last}]{mlg.vf_post[1:]}[mpost]")
        last = "mpost"
    for j, ((path, x, y, a, b), inp) in enumerate(zip(overlays, ov_inputs)):
        fin, fout = 0.15, 0.2
        graph.append(f"[{inp}:v]format=rgba,fade=t=in:st={a:.3f}:d={fin}:alpha=1,"
                     f"fade=t=out:st={max(a, b - fout):.3f}:d={fout}:alpha=1[o{j}]")
        graph.append(f"[{last}][o{j}]overlay={x}:{y}:enable='between(t,{a:.3f},{b:.3f})'[v{j + 1}]")
        last = f"v{j + 1}"
    if wm_in is not None:
        graph.append(f"[{wm_in}:v]format=rgba[wm]")
        graph.append(f"[{last}][wm]overlay={watermark[1]}:{watermark[2]}[vwm]")
        last = "vwm"
    vf_tail = "format=yuv420p"
    if fade_d > 0:
        vf_tail += f",fade=t=in:st=0:d={fade_d:.3f},fade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
    graph.append(f"[{last}]{vf_tail}[vout]")
    af = f"[{a_in}]aresample=48000,aformat=channel_layouts=stereo"
    if o.denoise and info.has_audio:
        af += ",highpass=f=80,afftdn=nf=-25"
    if p.speed != 1.0:
        sp = p.speed
        while sp < 0.5:              # atempo takes 0.5..2 per stage
            af += ",atempo=0.5"
            sp /= 0.5
        af += f",atempo={sp:g}"
    af += f",volume={o.game_volume:g}"
    if fade_d > 0:
        af += f",afade=t=in:st=0:d={fade_d:.3f},afade=t=out:st={d - fade_d:.3f}:d={fade_d:.3f}"
    if sfx:
        graph.append(af + "[ag]")
        names = []
        for k, ((t, _, g), inp) in enumerate(zip(sfx, sfx_inputs)):
            ms = int(round(t * 1000))
            # trim leading silence so downloaded clips hit on the kill
            cap = mlg.caps.get(sfx[k][1])
            choke = mlg.lens[sfx_idx[k]] if sfx_idx[k] < len(mlg.lens) else None
            if choke is not None:
                cap = min(cap, choke) if cap else choke
            fd = min(0.35, cap / 3) if cap else 0.0
            trim = (f",atrim=end={cap:.3f},afade=t=out:st={max(0.0, cap - fd):.3f}:d={fd:.3f}" if cap else "")
            graph.append(f"[{inp}:a]aresample=48000,aformat=channel_layouts=stereo,"
                         f"silenceremove=start_periods=1:start_threshold=-50dB{trim},volume={g:g},"
                         f"adelay={ms}|{ms}[sx{k}]")
            names.append(f"[sx{k}]")
        graph.append(f"[ag]{''.join(names)}amix=inputs={len(names) + 1}:duration=longest:normalize=0"
                     f"{mlg.af},asoftclip,apad[aout]")
    else:
        graph.append(af + (mlg.af if mlg else "") + ",apad[aout]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-t", f"{d:.3f}",
            *enc_args, "-r", f"{F:g}", "-color_range", "tv",
            *PART_AUDIO, "-video_track_timescale", "90000", out]
    return cmd


def _card(ffmpeg: str, text: str, sub: str, o: ReelOptions, W: int, H: int, F: float, enc_args: List[str],
          tmp: str, name: str) -> str:
    """A title card (intro/outro) clip with the given text."""
    from .overlay import CalloutStyle, render_text
    st = o.callout or CalloutStyle()
    png = os.path.join(tmp, f"{name}.png")
    w, h = render_text(png, text, int(H * 0.11), color=st.color, sub=sub, font_path=st.font,
                       accent=st.accent_color if st.accent else None, outline_pct=0, shadow=True)
    d = max(1.0, o.card_s)
    out = os.path.join(tmp, f"{name}.mov")
    graph = (f"[0:v]format=yuv420p[bg];[2:v]format=rgba,fade=t=in:st=0.15:d=0.4:alpha=1[t];"
             f"[bg][t]overlay=(W-w)/2:(H-h)/2,format=yuv420p,fade=t=in:st=0:d=0.3,"
             f"fade=t=out:st={d - 0.4:.3f}:d=0.4[vout]")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", f"color=c=0x101418:s={W}x{H}:r={F:g}:d={d:.3f}",
           "-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-loop", "1", "-t", f"{d:.3f}", "-i", png,
           "-filter_complex", graph, "-map", "[vout]", "-map", "1:a", "-t", f"{d:.3f}",
           *enc_args, "-r", f"{F:g}", "-color_range", "tv", *PART_AUDIO, "-video_track_timescale", "90000", out]
    _simple(ffmpeg, cmd[1:], f"making the {name} card")
    return out


def _watermark(o: ReelOptions, W: int, H: int, tmp: str):
    """(png, x, y) for the watermark, or None."""
    if not (o.watermark_text or o.watermark_image):
        return None
    from PIL import Image
    from .overlay import render_image
    if o.watermark_image and os.path.isfile(o.watermark_image):
        img = Image.open(o.watermark_image).convert("RGBA")
        th = max(16, int(H * 0.09))
        img = img.resize((max(1, int(img.width * th / img.height)), th))
    else:
        img = render_image(o.watermark_text, max(12, int(H * 0.03)), outline_pct=60, shadow=False)
    a = img.getchannel("A").point(lambda v: int(v * max(0.0, min(1.0, o.watermark_opacity))))
    img.putalpha(a)
    path = os.path.join(tmp, "watermark.png")
    img.save(path)
    m = int(H * 0.03)
    pos = o.watermark_pos.lower()
    x = W - img.width - m if "right" in pos else m
    y = H - img.height - m if "bottom" in pos else m
    return path, x, y


_XFADE = {"crossfade": "fade", "whip": "slideleft", "zoom": "zoomin"}


def make_thumbnail(video: str, t: float, out_path: str, text: str = "", sub: str = "",
                   style: Optional["CalloutStyle"] = None, size: Tuple[int, int] = (1280, 720)) -> str:
    """YouTube-style thumbnail: the frame just after a kill, punchier colours, big text."""
    import cv2
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    from .overlay import CalloutStyle, render_image
    from .video import grab_frame
    st = style or CalloutStyle()
    fr = grab_frame(video, max(0.0, t))
    img = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)).resize(size, Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Color(img).enhance(1.35)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80))
    W, H = size
    vign = Image.new("L", size, 0)
    ImageDraw.Draw(vign).ellipse([-W * 0.25, -H * 0.3, W * 1.25, H * 1.3], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(H // 6))
    img = Image.composite(img, Image.new("RGB", size, (0, 0, 0)), vign).convert("RGBA")
    if text:
        txt = render_image(text.upper() if st.uppercase else text, int(H * 0.15), color=st.color,
                           accent=st.accent_color if st.accent else None, sub=sub, font_path=st.font,
                           outline_pct=160, shadow=True)
        if txt.width > W * 0.92:
            txt = txt.resize((int(W * 0.92), int(txt.height * W * 0.92 / txt.width)))
        img.alpha_composite(txt, (int(W * 0.04), int(H * 0.96) - txt.height))
    img.convert("RGB").save(out_path, quality=92)
    return out_path


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
    from .winenv import fix_temp_dir
    fix_temp_dir()                               # packaged Windows Python: temp files ffmpeg can see
    gif = out_path.lower().endswith(".gif")
    if o.mlg:
        from .soundpack import ensure
        ensure()                                 # fetch the meme sound pack once, if it's missing
    if o.mlg and o.mlg_suggest:
        from .mlg import ClipFacts, suggest
        vals, _ = suggest([ClipFacts(list(sg.kills), list(sg.captions), sg.duration) for sg in segments],
                          has_music=bool(o.music))
        for k, val in vals.items():
            setattr(o, k, val)
    if o.mlg and not o.music and o.mlg_music not in ("", "off"):
        from .mlg import my_sound
        song = my_sound(o.mlg_music)
        if song:                                 # Sandstorm under the montage, looped
            o.music = song
            o.music_volume = min(o.music_volume, 0.55)
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
        abr = AUDIO_BITRATE
        if o.target_mb and not gif:
            budget = o.target_mb * 8_000_000 * 0.93 / total
            abr = "96k" if budget < 600_000 else ("128k" if budget < 1_500_000 else AUDIO_BITRATE)
            audio = int(abr[:-1]) * 1000              # the final AAC bitrate
            bitrate = max(300_000, int(o.target_mb * 8_000_000 * 0.93 / total) - audio)
        xf = _XFADE.get(o.transition) if not gif else None
        wm = _watermark(o, W, H, tmp)
        for attempt in range(3):
            files = _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report, should_cancel, total,
                                  wm, fades=o.transition == "fade")
            if files is None:            # GPU encoder failed: redo everything in software
                enc = "libx264"
                files = _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report,
                                      should_cancel, total, wm, fades=o.transition == "fade")
            durs = [p.out_dur for p in parts]
            ea = encoder_args(enc, o.crf, o.preset, bitrate)
            if o.intro_text and not gif:
                report(0.955, "Intro card")
                files.insert(0, _card(ffmpeg, o.intro_text, o.intro_sub, o, W, H, F, ea, tmp, "intro"))
                durs.insert(0, max(1.0, o.card_s))
            if o.outro_text and not gif:
                report(0.955, "Outro card")
                files.append(_card(ffmpeg, o.outro_text, o.outro_sub, o, W, H, F, ea, tmp, "outro"))
                durs.append(max(1.0, o.card_s))
            joined = os.path.join(tmp, "joined.mp4")
            report(0.96, "Joining clips")
            if xf and len(files) > 1:
                _xfade_join(ffmpeg, files, durs, joined, xf, min(o.transition_s, min(durs) / 3), ea, F)
                total = sum(durs) - (len(durs) - 1) * min(o.transition_s, min(durs) / 3)
            else:
                _concat(ffmpeg, files, joined, tmp, abr)
                total = sum(durs)
            final = joined
            if o.music and not gif:
                report(0.97, "Adding music")
                final = os.path.join(tmp, "music.mp4")
                _mix_music(ffmpeg, joined, o, total, final, abr)
            out_dir = os.path.dirname(os.path.abspath(out_path))
            os.makedirs(out_dir, exist_ok=True)
            if gif:
                report(0.98, "Making GIF")
                _to_gif(ffmpeg, final, out_path, W, F)
                break
            if o.loudnorm:
                report(0.985, "Evening out loudness")
                ln = os.path.join(tmp, "loud.mp4")
                _simple(ffmpeg, ["-i", final, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                                 "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", abr,
                                 "-ar", "48000", ln], "normalising loudness")
                final = ln
            _faststart(ffmpeg, final, out_path)
            if not o.target_mb or os.path.getsize(out_path) <= o.target_mb * 1_000_000 or attempt == 2:
                break
            bitrate = int((bitrate or 1_000_000) * 0.8)   # over budget: shrink and try again
            report(0.0, "File too big, re-encoding smaller")
        report(1.0, "Done")
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _encode_parts(ffmpeg, parts, infos, o, W, H, F, enc, bitrate, tmp, report, should_cancel, total,
                  watermark=None, fades=True):
    files: List[str] = []
    done = 0.0
    enc_args = encoder_args(enc, o.crf, o.preset, bitrate)
    assets = None
    if o.mlg:
        from .mlg import Assets
        assets = Assets(os.path.join(tmp, "mlg"), W, H, o.mlg_airhorn_sound, o.mlg_triple_sound)
    for n, p in enumerate(parts):
        out = os.path.join(tmp, f"part{n:04d}.mov")
        ovs = _overlay_specs(p, o, W, H, tmp, n)
        fx = _mlg_plan(p, o, W, H, assets, first=n == 0) if assets else None
        cmd = _part_cmd(ffmpeg, p, infos[p.seg.video], o, W, H, F, enc_args, out, ovs, watermark, fades, fx)
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


def _mlg_plan(p: _Part, o: ReelOptions, W: int, H: int, assets, first: bool = False):
    import random
    from .mlg import clip_level, plan
    if o.mlg_auto:
        lvl = clip_level(p.seg.kills)
        order = ("off", "light", "normal", "max")
        strobe = o.mlg_strobe if lvl != "mild" else order[min(order.index(o.mlg_strobe), 1)]
        o = replace(o, mlg_intensity=lvl, mlg_strobe=strobe)
    kills = [((t - p.start) / p.speed, c) for t, c in p.seg.kills]
    caps = [((t - p.start) / p.speed, c) for t, c in p.seg.captions]
    rng = random.Random(f"{os.path.basename(p.seg.video)}|{p.start:.2f}|{p.replay}|{o.mlg_seed}")
    return plan(kills, p.out_dur, W, H, o, assets, rng, captions=caps, first=first)


# audio of the intermediate clips: uncompressed, so joined clips add up to exactly their lengths
PART_AUDIO = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
AUDIO_BITRATE = "192k"


def _concat(ffmpeg: str, files: List[str], out: str, tmp: str, abr: str = AUDIO_BITRATE) -> None:
    listing = os.path.join(tmp, "list.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    # video is copied; the PCM audio is encoded once here, so it stays in step with the picture
    _simple(ffmpeg, ["-f", "concat", "-safe", "0", "-i", listing, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                     "-c:a", "aac", "-b:a", abr, "-ar", "48000", "-ac", "2", out], "joining clips")


def _xfade_join(ffmpeg: str, files: List[str], durs: List[float], out: str, kind: str, d: float,
                enc_args: List[str], F: float) -> None:
    """Join clips with an ``xfade`` transition (video) and a matching audio crossfade."""
    args: List[str] = []
    for f in files:
        args += ["-i", f]
    v, a = "[0:v]", "[0:a]"
    graph = []
    offset = 0.0
    for i in range(1, len(files)):
        offset += durs[i - 1] - d
        graph.append(f"{v}[{i}:v]xfade=transition={kind}:duration={d:.3f}:offset={offset:.3f}[v{i}]")
        graph.append(f"{a}[{i}:a]acrossfade=d={d:.3f}[a{i}]")
        v, a = f"[v{i}]", f"[a{i}]"
    graph.append(f"{v}format=yuv420p[vout]")
    _simple(ffmpeg, [*args, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", a, *enc_args,
                     "-r", f"{F:g}", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", out], "joining clips")


def _mix_music(ffmpeg: str, video: str, o: ReelOptions, total: float, out: str, abr: str = "192k") -> None:
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
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", abr,
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
