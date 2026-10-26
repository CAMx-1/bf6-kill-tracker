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
    from PySide6.QtCore import QSettings
    w = MainWindow(QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))  # fresh defaults
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

    # single kills outside streaks (e.g. a double kill on a tank) can be shown and ticked in
    w.videos[0].result.kills.append(KillEvent(8.0, count=2))
    w.videos[0].result.kills.sort(key=lambda k: k.time)
    w.sp_gap.setValue(2.0)                            # regroups: 8 s is now outside both streaks
    w.cb_singles.setChecked(False)
    w._rebuild_tree()
    assert w.tree.topLevelItem(0).childCount() == 2
    w.cb_singles.setChecked(True)
    top = w.tree.topLevelItem(0)
    assert [top.child(i).text(0) for i in range(top.childCount())] == ["Streak 1", "Double kill", "Streak 2"]
    assert "1 other kill clip(s)" in w.lbl_reel.text() and "6 kills" in w.lbl_reel.text()

    # in-app preview: selecting a clip plays it at thumbnail size
    w.tree.setCurrentItem(top.child(2))
    assert wait_until(app, lambda: w.preview.screen_lbl.pixmap() is not None
                      and not w.preview.screen_lbl.pixmap().isNull(), timeout=20)
    assert wait_until(app, lambda: w.preview.pos > 0.5, timeout=20)   # it's actually playing
    w.preview.toggle()
    paused_at = w.preview.pos
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    assert abs(w.preview.pos - paused_at) < 0.1                       # and pauses
    top.child(1).setCheckState(0, Qt.Unchecked)                         # leave the double kill out

    out = str(tmp_path / "reel.mp4")
    w.start_reel(out)
    assert wait_until(app, lambda: w._reel_worker is None, timeout=120)
    assert os.path.isfile(out)
    assert abs(probe(out).duration - (15 + 2 - (12 - 3))) < 0.3   # 9..17 s of the source
    w.close()


def test_trim_reorder_types_and_options(app, video, tmp_path):
    from PySide6.QtCore import QSettings, Qt
    w = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w.cb_open.setChecked(False)
    w.add_paths([video])
    kills = [KillEvent(2, types=["headshot"]), KillEvent(3, types=["gun"]), KillEvent(4, types=["headshot"]),
             KillEvent(12, types=["explosive"]), KillEvent(13, count=2, types=["explosive", "explosive"])]
    res = AnalysisResult(video, 20.0, 30.0, kills, [], 0, 0.0, 10.0, deaths=[8.0], feed_used=True)
    w.videos[0].result, w.videos[0].status = res, "Done"
    w._regroup_all()
    top = w.tree.topLevelItem(0)
    assert top.childCount() == 2 and "1 death(s)" in top.text(2)
    assert w.table.item(0, 4).text() == "1" and w.table.item(0, 5).text() == "6.0"
    assert top.child(0).text(2) == "2 HS, 1 gun"

    # streak rule by kill type: explosives only -> just the second streak
    w.cb_types.setCurrentIndex(3)
    assert w.tree.topLevelItem(0).childCount() == 1
    w.cb_types.setCurrentIndex(0)

    # reorder: move the first clip down
    assert [k[1] for k in w.reel_order] == ["s2.000", "s12.000"]
    w.order_list.setCurrentRow(0)
    w._move_clip(1)
    segs = w._segments()
    assert [round(g.start) for g in segs] == [9, 0]      # 12-3 first, then 2-3 (clamped to 0)

    # trim the 2-4 s streak to exactly 1.0-6.0 s
    top = w.tree.topLevelItem(0)
    w.tree.setCurrentItem(top.child(0))
    w._set_trim(1.0, 6.0)
    assert w.videos[0].trims["s2.000"] == (1.0, 6.0)
    seg = next(g for g in w._segments() if g.start < 5)
    assert (seg.start, seg.end) == (1.0, 6.0)
    w._set_trim(3.0, 3.5)                                   # can't cut kills off
    assert w.videos[0].trims["s2.000"] == (2.0, 4.2)
    w._reset_trim()
    assert "s2.000" not in w.videos[0].trims

    # reel options from the UI
    w.cb_preset.setCurrentText("Vertical 9:16 (TikTok / Shorts / Reels)")
    w.cb_overlays.setChecked(True)
    w.cb_replay.setChecked(True)
    o = w.reel_options()
    assert o.vertical and o.size == (1080, 1920) and o.overlays and o.replay and not w.cb_res.isEnabled()
    w.preview.shutdown()
    w.close()
