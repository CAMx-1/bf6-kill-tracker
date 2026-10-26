"""GUI smoke test (headless Qt): add a video, scan it, build a reel."""
import os
import subprocess
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from bf6killtracker.analyzer import AnalysisResult  # noqa: E402
from bf6killtracker.events import KillEvent  # noqa: E402
from bf6killtracker.gui import MainWindow  # noqa: E402
from bf6killtracker.video import find_ffmpeg, probe  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def video(tmp_path):
    p = str(tmp_path / "gameplay.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=20",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", p], check=True)
    return p


def test_add_scan_and_build(app, video, tmp_path):
    w = MainWindow()
    w.cb_open.setChecked(False)
    assert w.add_paths([video, video]) == 1          # duplicates ignored
    assert w.table.rowCount() == 1
    w.start_analysis()
    assert wait_until(app, lambda: w._analyze_worker is None and w.videos[0].result is not None)
    assert w.videos[0].result.total_kills == 0       # test pattern has no kills
    assert "no streaks" in w.tree.topLevelItem(0).text(0)
    assert not w.btn_reel.isEnabled()

    # inject a known result to exercise streak grouping, selection and reel building
    kills = [KillEvent(t) for t in (2, 3, 4, 12, 13, 14, 15)]
    w.videos[0].result = AnalysisResult(video, 20.0, 30.0, kills, [], 0, 0.0, 10.0)
    w._regroup(w.videos[0])
    w._rebuild_tree()
    assert w.tree.topLevelItem(0).childCount() == 2
    assert "2 streak(s), 7 kills" in w.lbl_reel.text()
    w.sp_min.setValue(4)                              # stricter rule regroups without rescanning
    assert w.tree.topLevelItem(0).childCount() == 1
    w.sp_min.setValue(3)
    from PySide6.QtCore import Qt
    w.tree.topLevelItem(0).child(0).setCheckState(0, Qt.Unchecked)
    assert "1 streak(s), 4 kills" in w.lbl_reel.text()

    out = str(tmp_path / "reel.mp4")
    w.start_reel(out)
    assert wait_until(app, lambda: w._reel_worker is None, timeout=120)
    assert os.path.isfile(out)
    assert abs(probe(out).duration - (15 + 2 - (12 - 3))) < 0.3   # 9..17 s of the source
    w.close()
