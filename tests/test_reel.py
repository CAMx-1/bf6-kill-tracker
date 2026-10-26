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
