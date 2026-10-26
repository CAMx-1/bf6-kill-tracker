"""Watch folder, projects and library (headless Qt)."""
import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bf6killtracker import library  # noqa: E402
from bf6killtracker.analyzer import AnalysisResult  # noqa: E402
from bf6killtracker.events import KillEvent  # noqa: E402
from bf6killtracker.gui import LibraryDialog, MainWindow  # noqa: E402
from bf6killtracker.video import NO_WINDOW, find_ffmpeg  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BF6KT_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def make(path, secs=4):
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={secs}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True, **NO_WINDOW)


def wait(app, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def window(env):
    w = MainWindow(QSettings(str(env / "s.ini"), QSettings.IniFormat))
    w.cb_open.setChecked(False)
    return w


def test_watch_folder_adds_and_scans_new_recordings(app, env):
    d = env / "rec"
    d.mkdir()
    make(d / "old.mp4")
    w = window(env)
    w.start_watching(str(d))
    make(env / "tmp.mp4")
    shutil.move(str(env / "tmp.mp4"), str(d / "new.mp4"))
    w._check_watch_folder()                 # first look: size noted, waits for it to stop growing
    assert not w.videos
    w._check_watch_folder()                 # unchanged size: finished -> added and scanned
    assert [v.name for v in w.videos] == ["new.mp4"]
    assert wait(app, lambda: w.videos[0].result is not None and w._analyze_worker is None)
    w.stop_watching()
    w.close()


def test_project_roundtrip_and_library(app, env):
    v = env / "clip.mp4"
    make(v, 20)
    w = window(env)
    w.add_paths([str(v)])
    w.start_analysis()
    assert wait(app, lambda: w._analyze_worker is None and w.videos[0].result is not None)
    kills = [KillEvent(t, types=["headshot"]) for t in (2, 3, 4)] + [KillEvent(t) for t in (12, 13, 14)]
    w.videos[0].result = AnalysisResult(str(v), 20.0, 30.0, kills, [], 0, 0.0, 10.0, deaths=[8.0], feed_used=True)
    w.sp_min.setValue(3)
    w._regroup_all()
    top = w.tree.topLevelItem(0)
    w.tree.setCurrentItem(top.child(0))
    w._set_trim(0.5, 6.0)
    top.child(1).setCheckState(0, Qt.Unchecked)
    w.cb_overlays.setChecked(True)
    proj = str(env / "p" / "mine.bf6proj")
    os.makedirs(os.path.dirname(proj))
    w.save_project(proj)
    w.preview.shutdown()
    w.close()

    w2 = window(env)
    w2.cb_overlays.setChecked(False)
    w2.open_project(proj)
    assert wait(app, lambda: w2._analyze_worker is None)
    e = w2.videos[0]
    assert e.trims == {"s2.000": (0.5, 6.0)} and e.selected.get("s12.000") is False
    assert w2.cb_overlays.isChecked()

    data = library.load_project(proj)
    assert data["videos"][0]["rel"] == os.path.join("..", "clip.mp4")
    w2.preview.shutdown()
    w2.close()


def test_library_lists_scans(app, env):
    v = env / "a.mp4"
    make(v)
    from bf6killtracker import analyze
    analyze(str(v), use_cache=True)
    ss = library.sessions()
    assert [s.name for s in ss] == ["a.mp4"] and ss[0].kills == 0
    t = library.totals(ss)
    assert t["videos"] == 1 and t["kd"] == 0
    d = LibraryDialog(None, 5, 3)
    assert d.table.rowCount() == 1
