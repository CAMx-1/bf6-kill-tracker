"""Timeline strip for one video: waveform, kill/death markers, clips with drag handles.

* Drag a clip's left or right edge to trim it (snaps to 0.1 s, never cuts off its kills).
* Click a clip to select it; click elsewhere to move the playhead.
* Mouse wheel zooms around the cursor; shift+wheel (or drag the empty area) scrolls.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

HANDLE_PX = 7


class TimelineClip:
    def __init__(self, key: str, start: float, end: float, kills: Sequence[float], label: str = "",
                 selected: bool = True, min_start: Optional[float] = None, max_end: Optional[float] = None):
        self.key, self.start, self.end, self.label = key, start, end, label
        self.kills = list(kills)
        self.ticked = selected
        # edges can't move past the first/last kill
        self.min_start = min_start
        self.max_end = max_end


class Timeline(QWidget):
    clipSelected = Signal(str)                  # key
    clipTrimmed = Signal(str, float, float)     # key, start, end (on mouse release)
    seekRequested = Signal(float)               # seconds

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.duration = 1.0
        self.wave = np.zeros(0, np.float32)     # 0..1 envelope, one value per wave_step seconds
        self.wave_step = 0.05
        self.kills: List[Tuple[float, int]] = []
        self.deaths: List[float] = []
        self.clips: List[TimelineClip] = []
        self.current: Optional[str] = None
        self.playhead: Optional[float] = None
        self.view = (0.0, 1.0)                  # visible time range
        self._drag = None                       # ("left"|"right"|"pan", key, anchor)

    # -- data ------------------------------------------------------------------
    def set_video(self, duration: float, wave: Optional[np.ndarray], kills, deaths, step: float = 0.05) -> None:
        self.duration = max(0.1, duration)
        self.wave = wave if wave is not None else np.zeros(0, np.float32)
        self.wave_step = step
        self.kills, self.deaths = list(kills), list(deaths)
        self.view = (0.0, self.duration)
        self.update()

    def set_clips(self, clips: List[TimelineClip], current: Optional[str] = None) -> None:
        self.clips = clips
        self.current = current
        self.update()

    def focus_on(self, a: float, b: float, margin: float = 0.6) -> None:
        span = max(4.0, (b - a) * (1 + 2 * margin))
        mid = (a + b) / 2
        lo = max(0.0, min(self.duration - span, mid - span / 2))
        self.view = (lo, min(self.duration, lo + span))
        self.update()

    def set_playhead(self, t: Optional[float]) -> None:
        self.playhead = t
        self.update()

    # -- geometry --------------------------------------------------------------
    def _x(self, t: float) -> float:
        a, b = self.view
        return (t - a) / max(1e-6, b - a) * self.width()

    def _t(self, x: float) -> float:
        a, b = self.view
        return a + x / max(1, self.width()) * (b - a)

    def _lanes(self):
        h = self.height()
        return QRectF(0, 18, self.width(), h * 0.42), QRectF(0, 18 + h * 0.42 + 4, self.width(), h - 26 - h * 0.42)

    def _hit(self, x: float, y: float):
        _, clip_lane = self._lanes()
        if not (clip_lane.top() - 4 <= y <= clip_lane.bottom() + 4):
            return None, None
        for c in sorted(self.clips, key=lambda c: c.key != self.current):
            x0, x1 = self._x(c.start), self._x(c.end)
            if abs(x - x0) <= HANDLE_PX:
                return c, "left"
            if abs(x - x1) <= HANDLE_PX:
                return c, "right"
            if x0 < x < x1:
                return c, "body"
        return None, None

    # -- painting --------------------------------------------------------------
    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(24, 26, 30))
        wave_lane, clip_lane = self._lanes()
        a, b = self.view
        # time ruler
        p.setPen(QColor(120, 125, 135))
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        span = b - a
        step = next(s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1e9) if span / s <= 14)
        t = (int(a / step) + 1) * step
        while t < b:
            x = self._x(t)
            p.drawLine(QPointF(x, 12), QPointF(x, 17))
            m, s = divmod(t, 60)
            p.drawText(QPointF(x + 2, 11), f"{int(m)}:{s:04.1f}" if step < 1 else f"{int(m)}:{int(s):02d}")
            t += step
        # waveform
        if len(self.wave):
            i0 = max(0, int(a / self.wave_step))
            i1 = min(len(self.wave), int(b / self.wave_step) + 2)
            seg = self.wave[i0:i1]
            if len(seg) > 1:
                cols = max(1, int(W))
                idx = np.linspace(0, len(seg) - 1, cols).astype(int)
                vals = seg[idx]
                mid = wave_lane.center().y()
                half = wave_lane.height() / 2
                path = QPainterPath()
                xs = np.linspace(self._x(i0 * self.wave_step), self._x((i1 - 1) * self.wave_step), cols)
                path.moveTo(xs[0], mid)
                for x, v in zip(xs, vals):
                    path.lineTo(x, mid - v * half)
                for x, v in zip(xs[::-1], vals[::-1]):
                    path.lineTo(x, mid + v * half)
                p.fillPath(path, QColor(70, 110, 160, 170))
        # clips
        for c in self.clips:
            x0, x1 = self._x(c.start), self._x(c.end)
            if x1 < 0 or x0 > W:
                continue
            r = QRectF(x0, clip_lane.top(), max(2, x1 - x0), clip_lane.height())
            base = QColor(230, 126, 34) if c.ticked else QColor(90, 90, 90)
            if c.key == self.current:
                base = base.lighter(125)
            fill = QColor(base)
            fill.setAlpha(150 if c.ticked else 90)
            p.setPen(QPen(base.lighter(140) if c.key == self.current else base, 2 if c.key == self.current else 1))
            p.setBrush(fill)
            p.drawRoundedRect(r, 4, 4)
            p.setBrush(base.lighter(160))
            for hx in (x0, x1):
                p.drawRect(QRectF(hx - 2, r.top() + r.height() * 0.25, 4, r.height() * 0.5))
            if r.width() > 40 and c.label:
                p.setPen(QColor(255, 255, 255))
                p.drawText(r.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, c.label)
        # kill / death markers
        for t, n in self.kills:
            x = self._x(t)
            if 0 <= x <= W:
                p.setPen(QPen(QColor(255, 215, 0), 2 if n > 1 else 1))
                p.drawLine(QPointF(x, wave_lane.top()), QPointF(x, clip_lane.bottom()))
                p.setBrush(QColor(255, 215, 0))
                p.drawEllipse(QPointF(x, wave_lane.top() + 3), 3 + n, 3 + n)
        for t in self.deaths:
            x = self._x(t)
            if 0 <= x <= W:
                p.setPen(QPen(QColor(220, 60, 60), 2))
                p.drawLine(QPointF(x - 4, wave_lane.top()), QPointF(x + 4, wave_lane.top() + 8))
                p.drawLine(QPointF(x + 4, wave_lane.top()), QPointF(x - 4, wave_lane.top() + 8))
        if self.playhead is not None:
            x = self._x(self.playhead)
            p.setPen(QPen(QColor(255, 255, 255), 1))
            p.drawLine(QPointF(x, 12), QPointF(x, H))
        p.end()

    # -- interaction -----------------------------------------------------------
    def mousePressEvent(self, e):  # noqa: N802
        pos = e.position() if hasattr(e, "position") else e.pos()
        c, part = self._hit(pos.x(), pos.y())
        if c is not None and part in ("left", "right"):
            self._drag = (part, c.key, None)
            self.current = c.key
            self.clipSelected.emit(c.key)
        elif c is not None:
            self.current = c.key
            self.clipSelected.emit(c.key)
            self.seekRequested.emit(self._t(pos.x()))
        else:
            self._drag = ("pan", None, (pos.x(), self.view))
            self.seekRequested.emit(self._t(pos.x()))
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position() if hasattr(e, "position") else e.pos()
        if self._drag is None:
            _, part = self._hit(pos.x(), pos.y())
            self.setCursor(Qt.SizeHorCursor if part in ("left", "right") else Qt.ArrowCursor)
            return
        kind, key, anchor = self._drag
        if kind == "pan":
            x0, (a, b) = anchor
            dt = (x0 - pos.x()) / max(1, self.width()) * (b - a)
            lo = max(0.0, min(self.duration - (b - a), a + dt))
            self.view = (lo, lo + (b - a))
            self.update()
            return
        c = next((c for c in self.clips if c.key == key), None)
        if c is None:
            return
        t = round(max(0.0, min(self.duration, self._t(pos.x()))) * 10) / 10
        if kind == "left":
            hi = c.min_start if c.min_start is not None else c.end - 0.3
            c.start = min(t, hi)
        else:
            lo = c.max_end if c.max_end is not None else c.start + 0.3
            c.end = max(t, lo)
        self.update()

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._drag and self._drag[0] in ("left", "right"):
            c = next((c for c in self.clips if c.key == self._drag[1]), None)
            if c is not None:
                self.clipTrimmed.emit(c.key, c.start, c.end)
        self._drag = None

    def wheelEvent(self, e):  # noqa: N802
        a, b = self.view
        d = e.angleDelta().y() or e.angleDelta().x()
        pos = e.position() if hasattr(e, "position") else e.pos()
        if e.modifiers() & Qt.ShiftModifier or e.angleDelta().x():
            dt = -d / 120 * (b - a) * 0.1
            lo = max(0.0, min(self.duration - (b - a), a + dt))
            self.view = (lo, lo + (b - a))
        else:
            t = self._t(pos.x())
            k = 0.8 if d > 0 else 1.25
            span = max(2.0, min(self.duration, (b - a) * k))
            lo = t - (t - a) * span / (b - a)
            lo = max(0.0, min(self.duration - span, lo))
            self.view = (lo, lo + span)
        self.update()


def waveform(path: str, step: float = 0.05) -> np.ndarray:
    """Audio loudness envelope 0..1 (one value per ``step`` seconds) for the timeline."""
    from .music import load_mono
    try:
        y = load_mono(path, sr=8000)
    except RuntimeError:
        return np.zeros(0, np.float32)
    n = max(1, int(8000 * step))
    k = len(y) // n
    if not k:
        return np.zeros(0, np.float32)
    env = np.sqrt((y[:k * n].reshape(k, n) ** 2).mean(axis=1))
    top = np.percentile(env, 99.5) or 1.0
    return np.clip(env / top, 0, 1).astype(np.float32)
