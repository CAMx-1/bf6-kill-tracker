"""On Windows, ffmpeg must never open a console window (they pop up over the GUI)."""
import subprocess
import sys

import pytest

from bf6killtracker import video as video_mod
from bf6killtracker.reel import Segment, build_reel
from bf6killtracker.video import NO_WINDOW, cut_clip, find_ffmpeg, grab_frame, probe, read_roi_frames


def test_no_window_flag_matches_platform():
    if sys.platform == "win32":
        # the window flag must always be set (low-priority mode may add a priority class on top)
        assert NO_WINDOW["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert NO_WINDOW == {}


def test_every_ffmpeg_call_hides_its_window(tmp_path, monkeypatch):
    ff = find_ffmpeg()
    src = str(tmp_path / "v.mp4")
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", src], check=True, **NO_WINDOW)

    calls = []
    real_popen = subprocess.Popen

    class Recorder(real_popen):
        def __init__(self, args, *a, **kw):
            if isinstance(args, (list, tuple)) and args and args[0] == ff:
                calls.append(kw.get("creationflags", 0))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Recorder)

    probe(src)
    list(read_roi_frames(src, (0, 0, 64, 64), (320, 240)))
    grab_frame(src, 1.0)
    cut_clip(src, 0.5, 1.5, str(tmp_path / "c.mp4"))
    build_reel([Segment(src, 0.0, 1.0)], str(tmp_path / "r.mp4"))

    assert len(calls) >= 7   # probe x several, scan, grab, cut, reel parts + join
    expected = NO_WINDOW.get("creationflags", 0)
    assert all(c == expected for c in calls), calls
    assert video_mod.NO_WINDOW is NO_WINDOW


def test_low_priority(tmp_path):
    import subprocess as sp
    import sys as _s
    code = "from bf6killtracker.video import set_low_priority, is_low_priority; set_low_priority(); print(is_low_priority())"
    out = sp.run([_s.executable, "-c", code], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).parents[1]))
    assert out.stdout.strip() == "True", out.stderr
