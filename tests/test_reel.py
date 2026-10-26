"""Highlight-reel tests on small generated videos (fast, every OS)."""
import os
import subprocess

import pytest

from bf6killtracker.reel import Segment, build_reel, plan_segments
from bf6killtracker.video import find_ffmpeg, probe


def test_plan_merges_overlaps_and_clamps():
    items = [
        ("a.mp4", 10, 12, "s1"),
        ("a.mp4", 15, 16, "s2"),   # 12+2 >= 15-3 -> merged with s1
        ("a.mp4", 1, 2, "s0"),     # clamped at 0
        ("b.mp4", 58, 59, "s3"),   # clamped at the end (60)
    ]
    segs = plan_segments(items, {"a.mp4": 100, "b.mp4": 60}, pad_before=3, pad_after=2, order=["b.mp4", "a.mp4"])
    assert [(g.video, g.start, g.end) for g in segs] == [
        ("b.mp4", 55, 60), ("a.mp4", 0, 4), ("a.mp4", 7, 18)]
    assert segs[2].label == "s1 + s2"


def test_plan_empty():
    assert plan_segments([], {}) == []


def _make_video(path, size, rate, seconds, audio):
    ff = find_ffmpeg()
    cmd = [ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", path]
    subprocess.run(cmd, check=True)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("vids")
    a, b = str(d / "a with space.mp4"), str(d / "b's clip.mp4")
    _make_video(a, "640x360", 30, 8, audio=True)
    _make_video(b, "320x240", 25, 6, audio=False)   # different size/fps, no audio
    return a, b


def test_build_reel_joins_mixed_sources(videos, tmp_path):
    a, b = videos
    segs = [Segment(a, 1.0, 3.0), Segment(b, 2.0, 3.5), Segment(a, 5.0, 7.0)]
    seen = []
    out = build_reel(segs, str(tmp_path / "reel.mp4"), size=(640, 360), fps=30,
                     progress=lambda f, m: seen.append(f))
    info = probe(out)
    assert (info.width, info.height) == (640, 360)
    assert info.has_audio
    assert abs(info.duration - 5.5) < 0.25
    assert seen and seen[-1] == 1.0
    # decodes cleanly end to end
    r = subprocess.run([find_ffmpeg(), "-v", "error", "-i", out, "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0 and not r.stderr.strip()


def test_build_reel_cancel(videos, tmp_path):
    from bf6killtracker.reel import ReelCancelled
    a, _ = videos
    with pytest.raises(ReelCancelled):
        build_reel([Segment(a, 0, 8)], str(tmp_path / "x.mp4"), should_cancel=lambda: True)
    assert not os.path.exists(tmp_path / "x.mp4")


def test_build_reel_requires_segments(tmp_path):
    with pytest.raises(ValueError):
        build_reel([], str(tmp_path / "x.mp4"))


# ---------------------------------------------------------------- reel extras
from bf6killtracker.music import detect_beats, load_mono, snap_to_beats  # noqa: E402
from bf6killtracker.reel import ReelOptions, apply_preset, encoder_args, pick_encoder  # noqa: E402
from bf6killtracker.video import NO_WINDOW  # noqa: E402


def _audio_level(path):
    r = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True, **NO_WINDOW)
    line = next(ln for ln in r.stderr.splitlines() if "mean_volume" in ln)
    return float(line.split("mean_volume:")[1].split("dB")[0])


@pytest.fixture(scope="module")
def click_track(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("music") / "clicks.wav")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=1000:duration=0.05,apad=pad_dur=0.45",
                    "-filter_complex", "aloop=loop=-1:size=22050,atrim=0:30,asetpts=N/SR/TB", "-ar", "44100", p],
                   check=True, **NO_WINDOW)
    return p


def test_beat_detection(click_track):
    bpm, beats = detect_beats(load_mono(click_track))
    assert bpm == pytest.approx(120, abs=1.5)
    assert all(abs((b + 0.25) % 0.5 - 0.25) < 0.03 for b in beats[:20])   # on the clicks
    assert snap_to_beats([3.2, 2.1], beats) == pytest.approx([3.0, 2.0], abs=0.03)


def test_music_mixed_in_and_cuts_on_beats(videos, click_track, tmp_path):
    _, b = videos                                       # silent source
    out = str(tmp_path / "m.mp4")
    o = ReelOptions(size=(320, 240), fps=25, music=click_track, beat_sync=True, duck=False)
    build_reel([Segment(b, 0.0, 2.2), Segment(b, 3.0, 5.4)], out, options=o)
    assert _audio_level(out) > -60                      # music audible
    assert abs(probe(out).duration - 4.5) < 0.15        # 2.2 -> 2.0, 2.4 -> 2.5: cuts land at 2.0 s and 4.5 s


def test_replay_and_overlays(videos, tmp_path):
    a, _ = videos
    out = str(tmp_path / "r.mp4")
    seg = Segment(a, 1.0, 4.0, kills=[(2.0, 1), (3.0, 2)], title="Test clip")
    o = ReelOptions(size=(640, 360), fps=30, overlays=True, titles=True, replay=True, replay_speed=0.5,
                    replay_before=1.0, replay_after=0.5)
    build_reel([seg], out, options=o)
    assert seg.key_kill() == 3.0
    assert abs(probe(out).duration - (3.0 + 1.5 / 0.5)) < 0.25


def test_vertical_and_size_target(videos, tmp_path):
    a, _ = videos
    v = str(tmp_path / "v.mp4")
    build_reel([Segment(a, 0, 3)], v, options=apply_preset("Vertical 9:16 (TikTok / Shorts / Reels)"))
    assert (probe(v).width, probe(v).height) == (1080, 1920)
    d = str(tmp_path / "d.mp4")
    o = apply_preset("Discord (under 25 MB)")
    o.target_mb = 0.3
    build_reel([Segment(a, 0, 7)], d, options=o)
    assert os.path.getsize(d) <= 300_000


def test_gif(videos, tmp_path):
    a, _ = videos
    g = str(tmp_path / "x.gif")
    build_reel([Segment(a, 0, 2)], g, options=apply_preset("GIF (small, no sound)"))
    with open(g, "rb") as f:
        assert f.read(6) in (b"GIF89a", b"GIF87a")


def test_encoder_selection_falls_back():
    ff = find_ffmpeg()
    assert pick_encoder(ff, "libx264") == "libx264"
    assert pick_encoder(ff, "h264_does_not_exist") == "libx264"
    assert "-crf" in encoder_args("libx264", 20, "fast", None)
    assert "-b:v" in encoder_args("h264_nvenc", 20, "fast", 2_000_000)


# ---------------------------------------------------------------- call-out style
from bf6killtracker.overlay import CalloutStyle, preview, render_callout  # noqa: E402


def test_callout_texts_and_placement():
    st = CalloutStyle(text_streak="{n} in a row", text_multi="{name}!", uppercase=True)
    assert st.texts(1, 1) == ("KILL", "")
    assert st.texts(1, 4) == ("4 IN A ROW", "")
    assert st.texts(2, 5) == ("DOUBLE KILL!", "5 KILLS")
    st.show_count = False
    assert st.texts(2, 5)[1] == ""
    assert CalloutStyle(position="Bottom right").place(200, 50, 1920, 1080) == (1513, 925)
    assert CalloutStyle(position="Custom", x=1.2, y=-1).place(200, 50, 1920, 1080) == (1720, 0)  # kept on screen
    assert CalloutStyle.from_json(st.to_json()) == st
    assert CalloutStyle.from_json("not json") == CalloutStyle()
    assert CalloutStyle.from_json('{"size": 150, "unknown": 1}').size == 150


def test_callout_size_and_colour():
    small = render_callout(CalloutStyle(size=50), "KILL", "", 1080)
    big = render_callout(CalloutStyle(size=200), "KILL", "", 1080)
    assert big.height > 3 * small.height
    img = preview(CalloutStyle(position="Top left", color=(255, 0, 0), accent=False, outline=0), size=(640, 360),
                  kills_here=1, running=1)
    px = [img.getpixel((x, y)) for x in range(0, 200) for y in range(0, 80)]
    assert any(r > 200 and g < 60 and b < 60 for r, g, b in px)              # red text top-left
    assert not any(r > 200 and g < 60 and b < 60 for r, g, b in
                   (img.getpixel((x, y)) for x in range(400, 640) for y in range(250, 360)))


def test_reel_uses_callout_position(videos, tmp_path):
    import cv2
    import numpy as np
    a, _ = videos
    out = str(tmp_path / "c.mp4")
    st = CalloutStyle(position="Bottom right", size=150)
    seg = Segment(a, 0.0, 3.0, kills=[(1.0, 1)])
    frames = []
    for overlays in (False, True):
        out = str(tmp_path / f"c{overlays}.mp4")
        build_reel([seg], out, options=ReelOptions(size=(640, 360), fps=30, fade=0, overlays=overlays, callout=st))
        cap = cv2.VideoCapture(out)
        cap.set(cv2.CAP_PROP_POS_MSEC, 1700)
        ok, f = cap.read()
        assert ok
        frames.append(f.astype(int))
    diff = np.abs(frames[1] - frames[0]).max(axis=2) > 60       # pixels changed by the call-out
    ys, xs = np.nonzero(diff)
    assert len(xs) > 200
    assert xs.mean() > 400 and ys.mean() > 250                  # bottom right of 640x360


# ---------------------------------------------------------------- hype
from bf6killtracker.events import KillEvent  # noqa: E402
from bf6killtracker.hype import Scored, build_up_order, pick_montage, score_clip  # noqa: E402
from bf6killtracker.labels import HudEvent  # noqa: E402


def test_hype_scoring():
    plain, _ = score_clip([KillEvent(10)])
    multi, why = score_clip([KillEvent(10, count=3)])
    assert multi > 3 * plain and "triple kill" in why
    hs, why = score_clip([KillEvent(10, types=["headshot"])])
    assert hs > plain and "1 headshot" in why
    fast, _ = score_clip([KillEvent(10), KillEvent(11), KillEvent(12)])
    slow, _ = score_clip([KillEvent(10), KillEvent(14), KillEvent(18)])
    assert fast > slow
    clutch, why = score_clip([KillEvent(10)], [HudEvent(9.0, "last_squad_member_alive", "banner")])
    assert clutch > plain and "last squad member alive" in why
    far, _ = score_clip([KillEvent(10)], [HudEvent(30.0, "last_squad_member_alive", "banner")])
    assert far == plain


def test_montage_pick_and_order():
    cs = [Scored("v", f"c{i}", i * 20, i * 20 + d, 1, sc) for i, (d, sc) in
          enumerate([(2, 10), (4, 90), (1, 30), (3, 50), (5, 20), (2, 70)])]
    m = pick_montage(cs, 40, 3, 2)
    assert sum(c.end - c.start + 5 for c in m) <= 44
    assert m[-1].score == 90 and m[0].score == 70          # best last, second-best opens
    assert [c.score for c in m[1:-1]] == sorted(c.score for c in m[1:-1])
    assert [c.score for c in build_up_order(cs[:2])] == [10, 90]
