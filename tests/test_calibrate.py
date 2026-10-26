"""Auto-calibration on generated videos with skulls drawn at a known HUD scale/position."""
import os
import subprocess

import cv2
import numpy as np
import pytest

from bf6killtracker.calibrate import MATCH_OFFSET, calibrate
from bf6killtracker.detector import TEMPLATE_DIR, HudLayout
from bf6killtracker.video import NO_WINDOW, find_ffmpeg


def make_video(path, size, layout, n_frames=8, skulls=2):
    w, h = size
    sk = cv2.imread(os.path.join(TEMPLATE_DIR, "skull.png"))
    tw, th = layout.icon_size
    sk = cv2.resize(sk, (tw, th), interpolation=cv2.INTER_AREA)
    rng = np.random.default_rng(1)
    cmd = [find_ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
           "-r", "10", "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", "-pix_fmt", "yuv420p", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, **NO_WINDOW)
    sx, sy = w / layout.ref_size[0], h / layout.ref_size[1]
    for _ in range(n_frames):
        ref = cv2.GaussianBlur(rng.integers(20, 90, (layout.ref_size[1], layout.ref_size[0], 3), dtype=np.uint8),
                               (9, 9), 0)
        for k in range(skulls):
            x = layout.slot_xs()[k] - MATCH_OFFSET[0]
            y = layout.icon_y - MATCH_OFFSET[1]
            ref[y:y + th, x:x + tw] = sk
        p.stdin.write(cv2.resize(ref, (w, h), interpolation=cv2.INTER_AREA).tobytes())
    p.stdin.close()
    p.wait()


@pytest.mark.parametrize("scale,shift", [(1.0, (0, 0)), (0.8, (0, 0)), (1.2, (30, -20))])
def test_finds_scale_and_offset(tmp_path, scale, shift):
    base = HudLayout()
    truth = (base.scaled(scale) if scale != 1.0 else base).shifted(*shift)
    path = str(tmp_path / "v.mp4")
    make_video(path, (1920, 1080), truth)          # 1080p: everything is rescaled to 1440 lines
    cal = calibrate(path)
    assert cal.scale == pytest.approx(scale, abs=0.051)
    assert abs(cal.layout.slot0_x - truth.slot0_x) <= 6
    assert abs(cal.layout.icon_y - truth.icon_y) <= 6
    assert cal.applied == (scale != 1.0 or shift != (0, 0))


def test_ultrawide_layout_is_centred():
    L = HudLayout.for_video(3440, 1440)
    assert L.ref_size == (3440, 1440) and L.slot0_x == HudLayout().slot0_x + 440
    assert HudLayout.for_video(1920, 1080) == HudLayout()


def test_no_skulls_keeps_default(tmp_path):
    path = str(tmp_path / "v.mp4")
    make_video(path, (1280, 720), HudLayout(), skulls=0)
    cal = calibrate(path)
    assert not cal.applied and cal.layout == HudLayout()
