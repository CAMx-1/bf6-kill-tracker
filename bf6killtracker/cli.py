"""Command line interface.

    python -m bf6killtracker analyze VIDEO_OR_FOLDER [...] [options]
    python -m bf6killtracker calibrate VIDEO --at SECONDS
    python -m bf6killtracker [gui]          (desktop app)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import List

from . import __version__
from .analyzer import AnalysisResult, analyze
from .detector import HudLayout, SkullDetector
from .events import TrackerConfig

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts"}


def fmt_time(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def collect_videos(paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                    out.append(os.path.join(p, name))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print(f"warning: not found: {p}", file=sys.stderr)
    return out


def safe_stem(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)


def print_report(r: AnalysisResult) -> None:
    print(f"\n{r.video}")
    deaths = f"  |  {len(r.deaths)} death(s), K/D {r.kd:.1f}" if r.feed_used else ""
    print(f"  duration {fmt_time(r.duration)}  |  {r.total_kills} kills{deaths}  |  "
          f"{len(r.streaks)} streak(s)  |  " +
          ("remembered from an earlier scan" if r.from_cache else f"processed in {r.processing_s:.1f}s"))
    if r.hud:
        print(f"  {r.hud}" + ("  |  your review corrections applied" if r.reviewed else ""))
    if r.feed_used and r.kills:
        tc = r.type_counts()
        print("  kill types: " + ", ".join(f"{n} {t}" for t, n in sorted(tc.items(), key=lambda x: -x[1])))
        print(f"  most kills in one life: {r.best_life}")
    if r.feed_used:
        top = [p for p in r.players() if p.total >= 2][:3]
        if top:
            print("  repeat opponents: " + "; ".join(
                f"#{i + 1} killed {len(p.kills)}x" + (f", killed you {len(p.deaths)}x" if p.deaths else "")
                + f" ({', '.join(fmt_time(t) for t in sorted(p.kills + p.deaths))})" for i, p in enumerate(top))
                + "  (names shown in the app's Players view)")
    for d in r.down:
        print(f"  down at {fmt_time(d.start)} for {d.duration:.1f}s")
    for b in r.scoreboards:
        print(f"  scoreboard at {fmt_time(b.time)}: score {b.score}, K {b.kills} / D {b.deaths} / A {b.assists} "
              "(whole match so far, from the game)")
    if r.events:
        ec = r.event_counts()
        acts = [k for k in ec if k not in ("kill", "headshot") and not k.endswith("_kill")]
        print("  events: " + ", ".join(f"{ec[k]} {k.replace('_', ' ')}" for k in sorted(acts, key=lambda k: -ec[k])))
    events = sorted([(k.time, "kill", k) for k in r.kills] + [(d, "death", None) for d in r.deaths],
                    key=lambda e: e[0])
    if events:
        print("  timeline:")
        for t, kind, k in events:
            if kind == "death":
                print(f"    {fmt_time(t)}  -- died --")
                continue
            mult = f" x{k.count}" if k.count > 1 else ""
            types = f"  ({', '.join(k.type_list())})" if r.feed_used else ""
            tag = f"  [streak {k.streak_id + 1}]" if k.streak_id >= 0 else ""
            print(f"    {fmt_time(t)}{mult}{types}{tag}")
    for s in r.streaks:
        print(f"  STREAK {s.id + 1}: {s.kills} kills in {s.duration:.1f}s  "
              f"({fmt_time(s.start)} - {fmt_time(s.end)})")
    if r.kills:
        print(f"  most kills in any {r.window:g}s window: {r.best_window_kills} "
              f"(starting {fmt_time(r.best_window_start)})")


def write_csv(results: List[AnalysisResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video", "time_s", "timestamp", "event", "kills", "types", "weapons", "streak", "evidence"])
        for r in results:
            rows = [(k.time, k) for k in r.kills] + [(d, None) for d in r.deaths]
            for t, k in sorted(rows, key=lambda x: x[0]):
                if k is None:
                    w.writerow([r.video, f"{t:.3f}", fmt_time(t), "death", "", "", "", "", "feed"])
                    continue
                w.writerow([r.video, f"{k.time:.3f}", fmt_time(k.time), "kill", k.count, ";".join(k.type_list()),
                            ";".join(k.weapons), k.streak_id + 1 if k.streak_id >= 0 else "", k.evidence])


def cmd_analyze(a: argparse.Namespace) -> int:
    if a.low_priority:
        from .video import set_low_priority
        set_low_priority(True)
    videos = collect_videos(a.inputs)
    if not videos:
        print("error: no video files given", file=sys.stderr)
        return 2
    layout = HudLayout().scaled(a.hud_scale) if a.hud_scale != 1.0 else None
    results: List[AnalysisResult] = []
    for v in videos:
        last = [-10.0]

        def progress(t: float, _v=v) -> None:
            if not a.quiet and t - last[0] >= 5:
                last[0] = t
                print(f"\r  scanning {os.path.basename(_v)}: {fmt_time(t)}", end="", file=sys.stderr, flush=True)

        try:
            r = analyze(v, sample_fps=a.sample_fps, layout=layout, config=TrackerConfig(),
                        streak_gap=a.streak_gap, streak_min=a.streak_min, window=a.window, progress=progress,
                        use_feed=not a.no_feed, use_cache=True, refresh=a.rescan)
            if a.streak_type:
                from .events import find_streaks
                r.streaks = find_streaks(r.kills, a.streak_gap, a.streak_min, r.deaths, a.streak_type)
        except Exception as exc:  # keep going with the remaining files
            print(f"\nerror: {v}: {exc}", file=sys.stderr)
            continue
        if not a.quiet:
            print("\r" + " " * 70 + "\r", end="", file=sys.stderr)
        results.append(r)
        if not a.json_stdout:
            print_report(r)

        if a.clips:
            os.makedirs(a.clips, exist_ok=True)
            from .video import cut_clip
            jobs = []
            for s in r.streaks:
                jobs.append((s.start - a.pad_before, s.end + a.pad_after,
                             f"{safe_stem(v)}_streak{s.id + 1}_{s.kills}kills.mp4"))
            if a.clip_every_kill:
                for i, k in enumerate(r.kills):
                    jobs.append((k.time - a.pad_before, k.time + a.pad_after,
                                 f"{safe_stem(v)}_kill{i + 1:03d}_{int(k.time)}s.mp4"))
            for start, end, name in jobs:
                out = os.path.join(a.clips, name)
                cut_clip(v, start, min(end, r.duration or end), out)
                print(f"  clip: {out}")

    if a.reel:
        from .reel import PRESETS, ReelOptions, apply_preset, build_reel, caption_for, plan_segments
        items = []
        if a.montage:
            from .hype import MontageSpec, build_montage, music_length, score_result
            cands = [c for r in results for c in score_result(r)]
            target = music_length(a.music) if a.montage_fit_music and a.music else a.montage
            spec = MontageSpec(target or 60, a.montage_theme, a.montage_min_hype, a.montage_order,
                               a.montage_per_video, a.montage_no_repeat_weapon, a.montage_min_kills)
            picks = build_montage(cands, spec, a.pad_before, a.pad_after)
            print(f"\nmontage: {len(picks)} of {len(cands)} clips ({a.montage_theme}, {a.montage_order}):")
            for c in picks:
                print(f"  {c.score:5.1f} {c.stars}  {os.path.basename(c.video)} {fmt_time(c.start)}  "
                      f"({', '.join(c.reasons)})")
                r0 = next(r for r in results if r.video == c.video)
                ks = [k for k in r0.kills if c.start - 0.01 <= k.time <= c.end + 0.01]
                items.append((c.video, c.start, c.end, f"{c.title} {fmt_time(c.start)}", c.kill_times, c.title,
                              [(k.time, caption_for(k.type_list(), k.weapons)) for k in ks]))
        else:
            for r in results:
                for s in r.streaks:
                    kev = [k for k in r.kills if k.streak_id == s.id]
                    items.append((r.video, s.start, s.end, f"{os.path.basename(r.video)} streak {s.id + 1}",
                                  [(k.time, k.count) for k in kev], os.path.splitext(os.path.basename(r.video))[0],
                                  [(k.time, caption_for(k.type_list(), k.weapons)) for k in kev]))
        segs = plan_segments(items, {r.video: r.duration for r in results}, a.pad_before, a.pad_after,
                             order=[r.video for r in results], keep_order=bool(a.montage))
        if segs:
            from .reel import apply_template
            o = apply_preset(a.preset, ReelOptions(encoder=a.encoder))
            if a.template:
                o = apply_template(a.template, o)
            o.music, o.music_volume, o.beat_sync = a.music, a.music_volume, a.beat_sync
            o.overlays, o.titles, o.replay = a.overlays or o.overlays, a.titles, a.replay or o.replay
            o.beat_sync = a.beat_sync or o.beat_sync
            o.callout = callout_style_from_args(a, o.callout)
            if a.montage and a.replay_finale:
                o.replay, o.replay_finale_only = True, True
            for k in ("transition", "killcam", "intro_text", "intro_sub", "outro_text", "outro_sub",
                      "watermark_text", "watermark_image", "watermark_pos"):
                if getattr(a, k, None):
                    setattr(o, k, getattr(a, k))
            o.captions = a.captions or o.captions
            o.loudnorm = a.loudnorm or o.loudnorm
            o.denoise = a.denoise or o.denoise
            if a.audio_track:
                o.audio_track = a.audio_track - 1
            apply_look_args(a, o)
            if a.max_mb:
                o.target_mb = a.max_mb

            def reel_progress(f: float, msg: str) -> None:
                if not a.quiet:
                    print(f"\r  highlight video: {f * 100:5.1f}%  {msg[:50]:<50}", end="", file=sys.stderr, flush=True)
            build_reel(segs, a.reel, options=o, progress=reel_progress)
            if a.thumbnail:
                from .hype import rank, score_result
                from .reel import make_thumbnail
                best = rank([c for r in results for c in score_result(r)])
                if best:
                    b = best[0]
                    t = max(b.kill_times, key=lambda k: (k[1], k[0]))[0] if b.kill_times else b.start
                    make_thumbnail(b.video, t + 0.5, a.thumbnail, f"{b.kills} KILL STREAK" if b.kills > 2
                                   else ("DOUBLE KILL" if b.kills == 2 else "KILL"), "", o.callout)
                    print(f"wrote {a.thumbnail}")
            if not a.quiet:
                print(file=sys.stderr)
            print(f"\nwrote {a.reel} ({len(segs)} clips)")
        else:
            print("\nno streaks found: highlight video not written")
    if a.markers:
        from .markers import FORMATS, export
        if a.markers_dir:
            os.makedirs(a.markers_dir, exist_ok=True)
        for r in results:
            stem = os.path.splitext(os.path.basename(r.video))[0]
            for fmt in a.markers:
                path = os.path.join(a.markers_dir, stem + FORMATS[fmt]) if a.markers_dir else None
                print(f"wrote {export(r, fmt, path)}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
        print(f"\nwrote {a.json}")
    if a.json_stdout:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    if a.csv:
        write_csv(results, a.csv)
        print(f"wrote {a.csv}")
    if a.shorts and results:
        from .hype import score_result
        from .shorts import export_shorts, plan_shorts, short_options
        cands = [c for r in results for c in score_result(r)]
        picks = plan_shorts(cands, {r.video: r.duration for r in results}, a.shorts_count, a.shorts_min,
                            a.shorts_max)
        o = short_options(a.shorts_template)
        apply_look_args(a, o)

        def sp(f: float, msg: str) -> None:
            if not a.quiet:
                print(f"\r  shorts: {f * 100:5.1f}%  {msg[:50]:<50}", end="", file=sys.stderr, flush=True)

        for f in export_shorts(picks, a.shorts, o, progress=sp):
            print(f"\nwrote {f}")
    return 0 if results else 1


def _hex(v: str):
    v = v.strip().lstrip("#")
    if len(v) != 6:
        raise SystemExit(f"error: colour must be 6 hex digits, got {v!r}")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def callout_style_from_args(a: argparse.Namespace, base=None):
    from .overlay import CalloutStyle
    st = CalloutStyle.from_json(base.to_json()) if base is not None else CalloutStyle()
    if getattr(a, "callout_style", None):
        with open(a.callout_style, encoding="utf-8") as f:
            st = CalloutStyle.from_json(f.read())
    if a.callout_pos:
        st.position = a.callout_pos
    if a.callout_xy:
        st.position, st.x, st.y = "Custom", a.callout_xy[0], a.callout_xy[1]
    if a.callout_size:
        st.size = a.callout_size
    if a.callout_font:
        st.font = a.callout_font
    if a.callout_color:
        st.color = _hex(a.callout_color)
    if a.callout_accent:
        if a.callout_accent.lower() == "none":
            st.accent = False
        else:
            st.accent, st.accent_color = True, _hex(a.callout_accent)
    if a.callout_text:
        st.text_first, st.text_streak, st.text_multi = a.callout_text
    return st


def cmd_calibrate(a: argparse.Namespace) -> int:
    """Save an annotated frame showing where the tracker looks, plus the slot scores."""
    import cv2

    from .video import grab_frame

    layout = HudLayout().scaled(a.hud_scale) if a.hud_scale != 1.0 else HudLayout()
    det = SkullDetector(layout)
    frame = grab_frame(a.video, a.at)
    ref = cv2.resize(frame, layout.ref_size, interpolation=cv2.INTER_AREA) \
        if frame.shape[1::-1] != layout.ref_size else frame.copy()
    x, y, w, h = det.roi
    obs = det.observe(ref[y:y + h, x:x + w])
    tw, th = layout.icon_size
    for k, sx in enumerate(layout.slot_xs()):
        hit = obs.slot_scores[k] >= TrackerConfig().on_threshold
        cv2.rectangle(ref, (sx, layout.icon_y), (sx + tw, layout.icon_y + th), (0, 255, 0) if hit else (0, 0, 255), 2)
        cv2.putText(ref, f"{obs.slot_scores[k]:.2f}", (sx, layout.icon_y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 255), 1, cv2.LINE_AA)
    px, py, pw, ph = layout.pop_box
    cv2.rectangle(ref, (px, py), (px + pw, py + ph), (255, 200, 0), 1)
    bx, by, bw, bh = layout.score_box
    cv2.rectangle(ref, (bx, by), (bx + bw, by + bh), (200, 200, 200), 1)
    crop = ref[max(0, y - 150):y + h + 150, max(0, x - 300):x + w + 300]
    out = a.output or f"calibrate_{safe_stem(a.video)}_{a.at:g}s.png"
    cv2.imwrite(out, crop)
    print(f"slot scores (right to left): {[round(float(s), 2) for s in obs.slot_scores]}")
    print(f"pop score: {obs.pop_score:.2f}")
    print(f"wrote {out}  (green box = skull detected; boxes should sit on the skull icons)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bf6killtracker", description="Track kills and kill streaks in Battlefield 6 videos.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    an = sub.add_parser("analyze", help="find kills and kill streaks in videos")
    an.add_argument("inputs", nargs="+", help="video files and/or folders of videos")
    an.add_argument("--json", metavar="FILE", help="write results as JSON")
    an.add_argument("--json-stdout", action="store_true", help="print JSON instead of the text report")
    an.add_argument("--csv", metavar="FILE", help="write one row per kill as CSV")
    an.add_argument("--clips", metavar="DIR", help="export a video clip of every kill streak into DIR")
    an.add_argument("--reel", metavar="FILE.mp4",
                    help="join every streak (from all inputs) into one highlight video")
    from .reel import PRESETS
    an.add_argument("--preset", choices=list(PRESETS), default="Custom", help="highlight video export preset")
    an.add_argument("--music", metavar="FILE", help="background music for the highlight video")
    an.add_argument("--music-volume", type=float, default=0.8)
    an.add_argument("--beat-sync", action="store_true", help="cut clips on the music's beats")
    an.add_argument("--overlays", action="store_true", help="show kill call-outs (DOUBLE KILL, streak count)")
    an.add_argument("--titles", action="store_true", help="show the video name at the start of each clip")
    an.add_argument("--replay", action="store_true", help="add a slow-motion replay of each clip's key kill")
    from .overlay import POSITIONS
    an.add_argument("--callout-style", metavar="FILE.json", help="call-out style saved as JSON (see README)")
    an.add_argument("--callout-pos", choices=list(POSITIONS), help="where call-outs appear")
    an.add_argument("--callout-xy", nargs=2, type=float, metavar=("X", "Y"),
                    help="custom call-out centre as fractions of the frame, e.g. 0.5 0.3")
    an.add_argument("--callout-size", type=int, metavar="PCT", help="call-out size, %% of default")
    an.add_argument("--callout-font", metavar="FILE", help="font file (.ttf/.otf) for overlays")
    an.add_argument("--callout-color", metavar="HEX", help="text colour, e.g. ffd700")
    an.add_argument("--callout-accent", metavar="HEX|none", help="underline bar colour, or 'none'")
    an.add_argument("--callout-text", nargs=3, metavar=("FIRST", "STREAK", "MULTI"),
                    help='texts, e.g. "KILL" "{n} IN A ROW" "{name}"')
    an.add_argument("--max-mb", type=float, help="keep the highlight video under this many MB")
    an.add_argument("--encoder", default="auto",
                    choices=["auto", "libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"],
                    help="video encoder (auto: GPU on Windows when available)")
    an.add_argument("--markers", nargs="+", choices=["edl", "fcpxml", "premiere", "chapters", "csv"],
                    help="write kill/streak markers for video editors (next to each video, or --markers-dir)")
    an.add_argument("--markers-dir", metavar="DIR", help="folder for marker files")
    an.add_argument("--montage", type=float, metavar="SECONDS",
                    help="with --reel: auto-pick the highest-hype clips (streaks and single kills) to fill about "
                         "this many seconds, ordered to build up to the best one")
    from .reel import TEMPLATES
    an.add_argument("--template", choices=list(TEMPLATES), help="a ready-made look for the highlight video")
    an.add_argument("--transition", choices=["fade", "cut", "crossfade", "whip", "zoom"])
    an.add_argument("--killcam", choices=["none", "zoom", "shake", "both"],
                    help="zoom in on each kill and/or shake on headshots")
    an.add_argument("--captions", action="store_true", help="caption each kill (HEADSHOT · RIFLE...)")
    an.add_argument("--intro-text"), an.add_argument("--intro-sub")
    an.add_argument("--outro-text"), an.add_argument("--outro-sub")
    an.add_argument("--watermark-text"), an.add_argument("--watermark-image")
    an.add_argument("--watermark-pos", choices=["Top left", "Top right", "Bottom left", "Bottom right"])
    an.add_argument("--audio-track", type=int, metavar="N", help="which audio track to use (1 = first)")
    an.add_argument("--denoise", action="store_true", help="reduce background noise in the game/mic audio")
    an.add_argument("--loudnorm", action="store_true", help="even out loudness (YouTube level)")
    an.add_argument("--thumbnail", metavar="FILE.jpg", help="with --reel: also save a YouTube thumbnail")
    an.add_argument("--shorts", metavar="FOLDER", help="also export one vertical Short per top moment into FOLDER")
    an.add_argument("--shorts-count", type=int, default=5, metavar="N")
    an.add_argument("--shorts-min", type=float, default=15.0, metavar="S", help="shortest Short (s)")
    an.add_argument("--shorts-max", type=float, default=30.0, metavar="S", help="longest Short (s)")
    an.add_argument("--shorts-template", default="TikTok hype", choices=list(TEMPLATES),
                    help="look of the Shorts (e.g. \"MLG TikTok\")")
    add_look_args(an)
    from .hype import ORDERS, THEMES
    an.add_argument("--montage-theme", choices=list(THEMES), default="Everything")
    an.add_argument("--montage-order", choices=list(ORDERS), default="Build-up (best last)")
    an.add_argument("--montage-min-hype", type=float, default=0.0)
    an.add_argument("--montage-min-kills", type=int, default=1)
    an.add_argument("--montage-per-video", type=int, default=0, help="max clips from one video (0 = no limit)")
    an.add_argument("--montage-no-repeat-weapon", action="store_true")
    an.add_argument("--montage-fit-music", action="store_true", help="montage as long as the --music track")
    an.add_argument("--replay-finale", action="store_true", help="with --montage: slow-mo replay of the last clip")
    an.add_argument("--clip-every-kill", action="store_true", help="with --clips, also export each kill")
    an.add_argument("--pad-before", type=float, default=4.0, help="seconds of video before a clip (default 4)")
    an.add_argument("--pad-after", type=float, default=3.0, help="seconds of video after a clip (default 3)")
    an.add_argument("--streak-gap", type=float, default=5.0,
                    help="max seconds between kills in the same streak (default 5)")
    an.add_argument("--streak-min", type=int, default=3, help="kills needed to call it a streak (default 3)")
    an.add_argument("--window", type=float, default=10.0,
                    help="also report the most kills inside any window of this many seconds (default 10)")
    an.add_argument("--streak-type", nargs="+", choices=["headshot", "gun", "explosive", "other"],
                    help="only count these kill types toward streaks, e.g. --streak-type headshot")
    an.add_argument("--low-priority", action="store_true",
                    help="scan at low CPU priority (play while it runs in the background)")
    an.add_argument("--rescan", action="store_true", help="ignore remembered results and scan again")
    an.add_argument("--no-feed", action="store_true", help="don't read the kill feed (faster, no deaths/types)")
    an.add_argument("--sample-fps", type=float, default=None,
                    help="analyse at this frame rate (e.g. 30 for speed); default: the video's own rate")
    an.add_argument("--hud-scale", type=float, default=1.0, help="in-game HUD scale if not default (e.g. 0.9)")
    an.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    an.set_defaults(func=cmd_analyze)

    pv = sub.add_parser("preview", help="render a few seconds showing a template/look (demo clip or your video)")
    pv.add_argument("output", help="output .mp4")
    pv.add_argument("--template", choices=list(TEMPLATES))
    pv.add_argument("--video", help="use this recording instead of the demo clip")
    pv.add_argument("--start", type=float, default=0.0, help="with --video: start time (s)")
    pv.add_argument("--length", type=float, default=8.0)
    pv.add_argument("--kills", default="", metavar="T:N,...",
                    help="with --video: kill times (s) and counts, e.g. 2.0:1,3.1:1,4.0:1")
    pv.add_argument("--transition", choices=["fade", "cut", "crossfade", "whip", "zoom"])
    pv.add_argument("--killcam", choices=["none", "zoom", "shake", "both"])
    pv.add_argument("--full-size", action="store_true", help="render at full size instead of a quick 640x360")
    add_look_args(pv)
    pv.set_defaults(func=cmd_preview)

    be = sub.add_parser("benchmark", help="find the fastest decoder (CPU / GPU) for scanning on this PC")
    be.add_argument("video", help="one of your recordings")
    be.add_argument("--seconds", type=float, default=15.0)
    be.add_argument("--no-save", action="store_true", help="just report; don't change the decoder used")
    be.set_defaults(func=cmd_benchmark)

    so = sub.add_parser("sounds", help="download the MLG meme sound pack into your sounds folder")
    so.add_argument("--force", action="store_true", help="download again even if present")
    so.set_defaults(func=cmd_sounds)

    gu = sub.add_parser("gui", help="open the desktop app (default when run without arguments)")
    gu.add_argument("videos", nargs="*", help="videos to add on start")
    gu.set_defaults(func=lambda a: _gui(a.videos))

    ca = sub.add_parser("calibrate", help="save an annotated frame to check the HUD regions line up")
    ca.add_argument("video")
    ca.add_argument("--at", type=float, required=True, help="time (s) of a frame that shows a kill skull")
    ca.add_argument("-o", "--output", help="output PNG path")
    ca.add_argument("--hud-scale", type=float, default=1.0)
    ca.set_defaults(func=cmd_calibrate)
    return p


def add_look_args(p: argparse.ArgumentParser) -> None:
    from .mlg import DEEPFRY, INTENSITIES, STROBES, TOGGLES
    from .reel import GRADES
    p.add_argument("--grade", choices=list(GRADES), help="colour grade")
    p.add_argument("--letterbox", action="store_true", help="cinematic black bars")
    p.add_argument("--vignette", action="store_true")
    p.add_argument("--grain", action="store_true", help="film grain")
    p.add_argument("--mlg", action="store_true",
                   help="MLG montage mode: hitmarkers, air horns, cans, meme text, strobe, OH BABY A TRIPLE")
    p.add_argument("--mlg-intensity", choices=INTENSITIES)
    p.add_argument("--mlg-strobe", choices=STROBES, help="'max' flashes rapidly (photosensitivity warning)")
    p.add_argument("--mlg-deepfry", choices=DEEPFRY)
    p.add_argument("--mlg-words", metavar="A,B,C", help="your own meme words")
    from .mlg import MUSIC
    p.add_argument("--mlg-music", choices=list(MUSIC),
                   help="loop Sandstorm under the video (file in your sounds folder; see the README)")
    p.add_argument("--mlg-triple-text", metavar="TEXT")
    p.add_argument("--mlg-sfx-volume", type=float, metavar="X", help="sound effects volume (1 = normal)")
    p.add_argument("--mlg-airhorn-chance", type=float, metavar="0-1", help="share of single kills with a horn")
    p.add_argument("--mlg-seed", type=int, metavar="N", help="different random layout")
    names = ", ".join(k[4:] for k in TOGGLES)
    p.add_argument("--mlg-on", metavar="LIST", help=f"switch MLG elements on: {names}")
    p.add_argument("--mlg-off", metavar="LIST", help="switch MLG elements off (same names)")
    p.add_argument("--triple-sound", dest="mlg_triple_sound", metavar="FILE",
                   help="your own 'OH BABY A TRIPLE' clip (default: the computer's voice)")
    p.add_argument("--airhorn-sound", dest="mlg_airhorn_sound", metavar="FILE", help="your own air horn sound")
    for k in ("airhorn", "cans", "hitmarkers", "text", "extras", "triple"):
        p.add_argument(f"--no-{k}", action="store_true", help=f"MLG mode without the {k}")


def apply_look_args(a: argparse.Namespace, o) -> None:
    from .mlg import TOGGLES
    for k in ("grade", "mlg_intensity", "mlg_strobe", "mlg_deepfry", "mlg_words", "mlg_triple_text",
              "mlg_sfx_volume", "mlg_airhorn_chance", "mlg_seed", "mlg_triple_sound", "mlg_airhorn_sound",
              "mlg_music"):
        if getattr(a, k, None) is not None:
            setattr(o, k, getattr(a, k))
    for k in ("letterbox", "vignette", "grain", "mlg"):
        if getattr(a, k, False):
            setattr(o, k, True)
    for k in ("airhorn", "cans", "hitmarkers", "text", "extras", "triple"):
        if getattr(a, "no_" + k, False):
            setattr(o, "mlg_" + k, False)
    for arg, state in (("mlg_on", True), ("mlg_off", False)):
        for name in filter(None, (x.strip() for x in (getattr(a, arg, None) or "").split(","))):
            key = "mlg_" + name.lower().replace("-", "")
            if key not in TOGGLES:
                raise SystemExit(f"unknown MLG element {name!r}; choose from: "
                                 + ", ".join(k[4:] for k in TOGGLES))
            setattr(o, key, state)


def cmd_preview(a: argparse.Namespace) -> int:
    from .reel import (ReelOptions, Segment, apply_template, build_reel, demo_segment, preview_options)
    import tempfile
    o = apply_template(a.template, ReelOptions()) if a.template else ReelOptions()
    for k in ("transition", "killcam"):
        if getattr(a, k):
            setattr(o, k, getattr(a, k))
    apply_look_args(a, o)
    if a.video:
        kills = [(float(x.split(":")[0]), int(x.split(":")[1]) if ":" in x else 1)
                 for x in a.kills.split(",") if x.strip()]
        seg = Segment(a.video, a.start, a.start + a.length, "preview", kills)
    else:
        seg = demo_segment(os.path.join(tempfile.gettempdir(), "bf6kt_preview"))
    if not a.full_size:
        o = preview_options(o)
    build_reel([seg], a.output, options=o)
    print(f"wrote {a.output}")
    return 0


def cmd_benchmark(a: argparse.Namespace) -> int:
    from .video import benchmark_decode, save_decoder
    res = benchmark_decode(a.video, a.seconds,
                           progress=lambda i, n, m: print(f"  testing {m}…", file=sys.stderr) if m != "done" else None)
    for name, fps in res:
        print(f"{name:14} {f'{fps:6.0f} frames/s' if fps else 'failed'}")
    ok = [n for n, f in res if f]
    if not ok:
        return 1
    if not a.no_save:
        save_decoder(ok[0], res)
        print(f"scanning will use: {ok[0]}  (set BF6KT_HWACCEL to override)")
    return 0


def cmd_sounds(a: argparse.Namespace) -> int:
    from .mlg import my_sounds_dir
    from .soundpack import download_all
    res = download_all(only_missing=not a.force,
                   progress=lambda i, n, m: print(f"  {i + 1}/{n} {m}" if m != "done" else "", file=sys.stderr))
    for slot, r in res.items():
        print(f"{slot:16} {r}")
    print(f"folder: {my_sounds_dir()}")
    return 0 if all(r in ("ok", "skipped") for r in res.values()) else 1


def _gui(videos: List[str]) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(f"The GUI needs PySide6: pip install PySide6-Essentials ({exc})", file=sys.stderr)
        return 1
    return gui_main(videos)


def main(argv: List[str] | None = None) -> int:
    from .winenv import fix_temp_dir
    fix_temp_dir()   # Store/packaged Windows Python: keep temp files where ffmpeg can see them
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return _gui([])
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
