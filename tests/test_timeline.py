import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bf6killtracker.timeline import Timeline, TimelineClip  # noqa: E402


def test_drag_edges_and_limits():
    app = QApplication.instance() or QApplication([])
    tl = Timeline()
    tl.resize(1000, 120)
    tl.set_video(100.0, np.random.default_rng(0).random(2000).astype(np.float32), [(40, 1), (45, 2)], [60])
    tl.set_clips([TimelineClip("s40", 37, 47, [40, 45], "Streak", True, 40, 45.2)], "s40")
    got = []
    tl.clipTrimmed.connect(lambda k, a, b: got.append((k, a, b)))
    y = int(tl._lanes()[1].center().y())
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(47)), y))
    QTest.mouseMove(tl, QPoint(int(tl._x(55)), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(55)), y))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(37)), y))
    QTest.mouseMove(tl, QPoint(int(tl._x(44)), y))            # can't pass the first kill at 40
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(44)), y))
    assert got[0] == ("s40", 37, 55.0) and got[1][1] == 40
    tl.focus_on(40, 45)
    assert tl.view[0] < 40 and tl.view[1] > 45 and tl.view[1] - tl.view[0] < 100
    tl.grab()   # paints without errors
    app.processEvents()
