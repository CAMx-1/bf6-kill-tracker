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
        from .reel import PRESETS, ReelOptions, apply_preset, build_reel, plan_segments
        items = []
        if a.montage:
            from .hype import pick_montage, score_result
            cands = [c for r in results for c in score_result(r)]
            picks = pick_montage(cands, a.montage, a.pad_before, a.pad_after)
            print(f"\nmontage: {len(picks)} of {len(cands)} clips, best last:")
            for c in picks:
                print(f"  {c.score:5.1f} {c.stars}  {os.path.basename(c.video)} {fmt_time(c.start)}  "
                      f"({', '.join(c.reasons)})")
                items.append((c.video, c.start, c.end, f"{c.title} {fmt_time(c.start)}", c.kill_times, c.title))
        else:
            for r in results:
                for s in r.streaks:
                    ks = [(k.time, k.count) for k in r.kills if k.streak_id == s.id]
                    items.append((r.video, s.start, s.end, f"{os.path.basename(r.video)} streak {s.id + 1}", ks,
                                  os.path.splitext(os.path.basename(r.video))[0]))
        segs = plan_segments(items, {r.video: r.duration for r in results}, a.pad_before, a.pad_after,
                             order=[r.video for r in results], keep_order=bool(a.montage))
        if segs:
            o = apply_preset(a.preset, ReelOptions(encoder=a.encoder))
            o.music, o.music_volume, o.beat_sync = a.music, a.music_volume, a.beat_sync
            o.overlays, o.titles, o.replay = a.overlays, a.titles, a.replay
            o.callout = callout_style_from_args(a)
            if a.max_mb:
                o.target_mb = a.max_mb

            def reel_progress(f: float, msg: str) -> None:
                if not a.quiet:
                    print(f"\r  highlight video: {f * 100:5.1f}%  {msg[:50]:<50}", end="", file=sys.stderr, flush=True)
            build_reel(segs, a.reel, options=o, progress=reel_progress)
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
    return 0 if results else 1


def _hex(v: str):
    v = v.strip().lstrip("#")
    if len(v) != 6:
        raise SystemExit(f"error: colour must be 6 hex digits, got {v!r}")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def callout_style_from_args(a: argparse.Namespace):
    from .overlay import CalloutStyle
    st = CalloutStyle()
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


def _gui(videos: List[str]) -> int:
    try:
        from .gui import main as gui_main
    except ImportError as exc:
        print(f"The GUI needs PySide6: pip install PySide6-Essentials ({exc})", file=sys.stderr)
        return 1
    return gui_main(videos)


def main(argv: List[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return _gui([])
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
