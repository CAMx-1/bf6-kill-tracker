import os
import subprocess

from bf6killtracker import analyze, cache
from bf6killtracker.video import NO_WINDOW, find_ffmpeg


def test_scan_is_remembered_and_invalidated(tmp_path, monkeypatch):
    monkeypatch.setenv("BF6KT_CACHE_DIR", str(tmp_path / "c"))
    v = str(tmp_path / "v.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", v], check=True, **NO_WINDOW)
    r1 = analyze(v, use_cache=True)
    assert not r1.from_cache
    r2 = analyze(v, use_cache=True, streak_min=2)
    assert r2.from_cache and r2.duration == r1.duration
    assert not analyze(v, use_cache=True, use_feed=False).from_cache     # different options
    st = os.stat(v)
    os.utime(v, (st.st_atime, st.st_mtime + 10))                         # file changed
    assert not analyze(v, use_cache=True).from_cache
    assert cache.clear() >= 2


def test_bad_hwaccel_falls_back_to_software(tmp_path, monkeypatch):
    monkeypatch.setenv("BF6KT_HWACCEL", "no-such-decoder")
    v = str(tmp_path / "v.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", v], check=True, **NO_WINDOW)
    assert analyze(v).duration > 1.5


def test_update_version_compare(monkeypatch):
    from bf6killtracker import update
    assert update.is_newer("0.2.0", "0.1.9") and update.is_newer("v1.0", "0.9.9")
    assert not update.is_newer("0.1.0", "0.1.0") and not update.is_newer("", "0.1.0")
    monkeypatch.setattr(update, "latest_release", lambda timeout=5.0: update.Release("99.0.0", "https://x"))
    assert update.check_for_update().version == "99.0.0"
    monkeypatch.setattr(update, "latest_release", lambda timeout=5.0: None)   # offline / no token
    assert update.check_for_update() is None
