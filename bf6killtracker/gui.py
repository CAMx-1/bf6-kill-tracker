"""Desktop GUI (PySide6 / Qt): add videos, find kill streaks, build a highlight reel.

    python -m bf6killtracker gui        (or just: python -m bf6killtracker)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from PySide6.QtCore import QFileSystemWatcher, QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QLineEdit, QListWidget, QSlider, QListWidgetItem, QScrollArea, QSpinBox, QSplitter, QStatusBar, QStyle,
    QTabWidget, QTableWidget, QTableWidgetItem, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .analyzer import AnalysisResult, Cancelled, analyze
from .cli import VIDEO_EXTS, fmt_time
from .events import KillEvent, Streak, find_streaks
from .overlay import CalloutStyle
from .reel import PRESETS, ReelCancelled, ReelOptions, Segment, apply_preset, build_reel, plan_segments
from . import library
from .preview import PreviewPlayer
from .timeline import Timeline, TimelineClip, waveform
from .video import cut_clip, find_ffmpeg, grab_frame, probe

APP_NAME = "BF6 Kill Tracker"
VIDEO_FILTER = "Videos (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTS)) + ");;All files (*)"


def open_with_system(path: str) -> None:
    """Open a file with the OS default application (video player / file browser)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


@dataclass
class Clip:
    """Something that can go into the highlight video: a streak or a kill outside streaks."""
    key: str                  # stable id used for the tick-box state
    kind: str                 # "streak" or "kill"
    start: float              # first kill
    end: float                # last kill
    kills: int
    events: List[KillEvent]
    title: str

    def types_text(self) -> str:
        tc: Dict[str, int] = {}
        for k in self.events:
            for t in k.type_list():
                if t != "unknown":
                    tc[t] = tc.get(t, 0) + 1
        short = {"headshot": "HS", "explosive": "expl.", "gun": "gun", "other": "other"}
        return ", ".join(f"{n} {short.get(t, t)}" for t, n in sorted(tc.items(), key=lambda x: -x[1]))

    @property
    def span(self) -> str:
        d = self.end - self.start
        return f"{d:.1f}s" if d >= 0.5 else ("at once" if self.kills > 1 else "")


def _kill_title(count: int) -> str:
    return {1: "Kill", 2: "Double kill", 3: "Triple kill"}.get(count, f"{count}× multi-kill")


@dataclass
class VideoEntry:
    path: str
    duration: float = 0.0
    status: str = "Waiting"
    progress: float = 0.0
    result: Optional[AnalysisResult] = None
    streaks: List[Streak] = field(default_factory=list)
    error: str = ""
    # clip key -> ticked for the highlight video
    selected: Dict[str, bool] = field(default_factory=dict)
    # clip key -> (start, end) in the source video, set by trimming in the preview
    trims: Dict[str, tuple] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def clips(self, include_singles: bool) -> List[Clip]:
        """Streaks, plus (optionally) kills that aren't part of a streak, in time order."""
        if not self.result:
            return []
        out: List[Clip] = []
        for st in self.streaks:
            ev = [k for k in self.result.kills if k.streak_id == st.id]
            out.append(Clip(f"s{st.start:.3f}", "streak", st.start, st.end, st.kills, ev, f"Streak {st.id + 1}"))
        if include_singles:
            for k in self.result.kills:
                if k.streak_id < 0:
                    out.append(Clip(f"k{k.time:.3f}", "kill", k.time, k.time, k.count, [k], _kill_title(k.count)))
        out.sort(key=lambda c: c.start)
        return out

    def is_selected(self, clip: Clip) -> bool:
        return self.selected.get(clip.key, True)


# ----------------------------------------------------------------------------
# background workers (each runs in its own QThread)
# ----------------------------------------------------------------------------
class Worker(QObject):
    finished = Signal()

    @Slot()
    def start_work(self) -> None:
        try:
            self.run()
        finally:
            self.finished.emit()

    def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class AnalyzeWorker(Worker):
    progress = Signal(str, float)            # path, fraction
    finished_one = Signal(str, object)       # path, AnalysisResult
    failed_one = Signal(str, str)            # path, message
    all_done = Signal()

    def __init__(self, paths: List[str]):
        super().__init__()
        self.paths = paths
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for path in self.paths:
            if self._cancel:
                break
            try:
                dur = probe(path).duration or 1.0
                last = [0.0]

                def cb(t: float, _p=path, _d=dur) -> None:
                    if self._cancel:
                        raise Cancelled()
                    if t - last[0] >= 0.5:
                        last[0] = t
                        self.progress.emit(_p, min(0.999, t / _d))

                res = analyze(path, progress=cb, use_cache=True)
                self.finished_one.emit(path, res)
            except Cancelled:
                self.failed_one.emit(path, "Cancelled")
                break
            except Exception as exc:  # report and continue with the next file
                self.failed_one.emit(path, str(exc))
        self.all_done.emit()


class ReelWorker(Worker):
    progress = Signal(float, str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, segments: List[Segment], out_path: str, options: ReelOptions):
        super().__init__()
        self.segments, self.out_path, self.options = segments, out_path, options
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            build_reel(self.segments, self.out_path, options=self.options,
                       progress=lambda f, m: self.progress.emit(f, m), should_cancel=lambda: self._cancel)
            self.done.emit(self.out_path)
        except ReelCancelled:
            self.failed.emit("Cancelled")
        except Exception as exc:
            self.failed.emit(str(exc))


class SoundPackWorker(Worker):
    progress = Signal(int, int, str)
    done = Signal(dict)

    def __init__(self, only_missing: bool = True):
        super().__init__()
        self.only_missing = only_missing

    def run(self) -> None:
        from .soundpack import download
        try:
            r = download(only_missing=self.only_missing, progress=lambda i, n, m: self.progress.emit(i, n, m))
        except Exception as exc:
            r = {"pack": str(exc)}
        self.done.emit(r)


class ThumbWorker(Worker):
    """Grabs small frames at each kill of a streak for the preview strip."""
    thumb = Signal(int, float, QImage)       # request id, time, image

    def __init__(self, req: int, path: str, times: List[float]):
        super().__init__()
        self.req, self.path, self.times = req, path, times

    def run(self) -> None:
        import cv2
        for t in self.times:
            try:
                img = grab_frame(self.path, max(0.0, t + 0.6))  # just after the skull pops in
                h, w = img.shape[:2]
                # centre of the screen: crosshair plus the kill skulls just below it
                img = img[h // 4:h * 3 // 4, w // 4:w * 3 // 4]
                h, w = img.shape[:2]
                small = cv2.resize(img, (320, int(320 * h / w)), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                q = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
                self.thumb.emit(self.req, t, q)
            except Exception:
                pass


class CalloutDialog(QDialog):
    """Edit where kill call-outs appear and how they look, with a live preview."""
    EXAMPLES = [("Double kill", 2, 3), ("Streak (4th kill)", 1, 4), ("First kill", 1, 1)]

    def __init__(self, parent, style: "CalloutStyle", frame: Optional[QImage] = None):
        super().__init__(parent)
        from .overlay import POSITIONS, system_fonts
        self.setWindowTitle("Kill call-out style")
        self.style_ = CalloutStyle.from_json(style.to_json())
        self.frame = frame
        self._busy = False
        lay = QHBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        self.cb_pos = QComboBox()
        self.cb_pos.addItems(list(POSITIONS))
        self.sl_x, self.sl_y = QSlider(Qt.Horizontal), QSlider(Qt.Horizontal)
        for sl in (self.sl_x, self.sl_y):
            sl.setRange(0, 100)
        self.sl_size = QSlider(Qt.Horizontal)
        self.sl_size.setRange(40, 300)
        self.lbl_size = QLabel()
        size_row = QHBoxLayout()
        size_row.addWidget(self.sl_size, 1)
        size_row.addWidget(self.lbl_size)
        self.cb_font = QComboBox()
        self.cb_font.addItem("Default (bold system font)", "")
        for name, path in system_fonts():
            self.cb_font.addItem(name, path)
        b_font = QPushButton("File…")
        b_font.clicked.connect(self._font_file)
        font_row = QHBoxLayout()
        font_row.addWidget(self.cb_font, 1)
        font_row.addWidget(b_font)
        self.btn_color = self._color_button("color")
        self.cb_accent = QCheckBox("Bar under the text")
        self.btn_accent = self._color_button("accent_color")
        accent_row = QHBoxLayout()
        accent_row.addWidget(self.cb_accent)
        accent_row.addWidget(self.btn_accent)
        accent_row.addStretch(1)
        self.sl_outline = QSlider(Qt.Horizontal)
        self.sl_outline.setRange(0, 300)
        self.btn_outline = self._color_button("outline_color")
        outline_row = QHBoxLayout()
        outline_row.addWidget(self.sl_outline, 1)
        outline_row.addWidget(self.btn_outline)
        self.cb_shadow = QCheckBox("Drop shadow")
        self.cb_upper = QCheckBox("ALL CAPS")
        self.ed_first, self.ed_streak, self.ed_multi = QLineEdit(), QLineEdit(), QLineEdit()
        self.ed_streak.setToolTip("{n} = kills so far in the clip")
        self.ed_multi.setToolTip("{name} = DOUBLE KILL, TRIPLE KILL…   {n} = kills so far")
        self.cb_count = QCheckBox("Show “N KILLS” under multi-kills")
        self.sp_dur = QDoubleSpinBox()
        self.sp_dur.setRange(0.6, 6.0)
        self.sp_dur.setSingleStep(0.2)
        self.sp_dur.setSuffix(" s on screen")

        form.addRow("<b>Placement</b>", QLabel(""))
        form.addRow("Position", self.cb_pos)
        form.addRow("Left ↔ right", self.sl_x)
        form.addRow("Top ↕ bottom", self.sl_y)
        form.addRow("Size", size_row)
        form.addRow("<b>Text</b>", QLabel("(or click the preview to place it)"))
        form.addRow("Font", font_row)
        form.addRow("Colour", self.btn_color)
        form.addRow("Accent", accent_row)
        form.addRow("Outline", outline_row)
        form.addRow("", self.cb_shadow)
        form.addRow("", self.cb_upper)
        form.addRow("First kill", self.ed_first)
        form.addRow("Streak", self.ed_streak)
        form.addRow("Multi-kill", self.ed_multi)
        form.addRow("", self.cb_count)
        form.addRow("Timing", self.sp_dur)

        right = QVBoxLayout()
        lay.addLayout(right, 1)
        self.cb_example = QComboBox()
        self.cb_example.addItems([n for n, *_ in self.EXAMPLES])
        ex_row = QHBoxLayout()
        ex_row.addWidget(QLabel("Preview:"))
        ex_row.addWidget(self.cb_example)
        ex_row.addStretch(1)
        right.addLayout(ex_row)
        self.preview_lbl = QLabel()
        self.preview_lbl.setFixedSize(640, 360)
        self.preview_lbl.setCursor(Qt.CrossCursor)
        self.preview_lbl.mousePressEvent = self._click_preview
        right.addWidget(self.preview_lbl)
        right.addStretch(1)
        btns = QHBoxLayout()
        b_reset = QPushButton("Reset to default")
        b_reset.clicked.connect(lambda: self._load(CalloutStyle()))
        b_ok = QPushButton("OK")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.accept)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_reset)
        btns.addStretch(1)
        btns.addWidget(b_cancel)
        btns.addWidget(b_ok)
        right.addLayout(btns)

        self._load(self.style_)
        for w in (self.cb_pos, self.cb_font, self.cb_example):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.sl_size, self.sl_outline):
            w.valueChanged.connect(self._changed)
        self.sl_x.valueChanged.connect(lambda *_: self._moved())
        self.sl_y.valueChanged.connect(lambda *_: self._moved())
        for w in (self.cb_accent, self.cb_shadow, self.cb_upper, self.cb_count):
            w.toggled.connect(self._changed)
        for w in (self.ed_first, self.ed_streak, self.ed_multi):
            w.textChanged.connect(self._changed)
        self.sp_dur.valueChanged.connect(self._changed)

    # -- helpers
    def _color_button(self, attr: str) -> QPushButton:
        b = QPushButton()
        b.setFixedWidth(60)
        b.clicked.connect(lambda: self._pick_color(attr))
        b._attr = attr
        return b

    def _paint_buttons(self) -> None:
        for b in (self.btn_color, self.btn_accent, self.btn_outline):
            r, g, bl = getattr(self.style_, b._attr)
            b.setStyleSheet(f"background-color: rgb({r},{g},{bl}); border: 1px solid #888; min-height: 18px;")

    def _pick_color(self, attr: str) -> None:
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor(*getattr(self.style_, attr)), self, "Choose colour")
        if c.isValid():
            setattr(self.style_, attr, (c.red(), c.green(), c.blue()))
            self._paint_buttons()
            self._render()

    def _font_file(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choose a font", os.path.expanduser("~"),
                                           "Fonts (*.ttf *.otf *.ttc);;All files (*)")
        if f:
            self.cb_font.addItem(os.path.basename(f), f)
            self.cb_font.setCurrentIndex(self.cb_font.count() - 1)

    def _load(self, st: "CalloutStyle") -> None:
        self._busy = True
        self.style_ = CalloutStyle.from_json(st.to_json())
        self.cb_pos.setCurrentText(st.position)
        ax, ay = st.anchor()
        self.sl_x.setValue(int(round(ax * 100)))
        self.sl_y.setValue(int(round(ay * 100)))
        self.sl_size.setValue(st.size)
        i = self.cb_font.findData(st.font)
        if i < 0 and st.font:
            self.cb_font.addItem(os.path.basename(st.font), st.font)
            i = self.cb_font.count() - 1
        self.cb_font.setCurrentIndex(max(0, i))
        self.cb_accent.setChecked(st.accent)
        self.sl_outline.setValue(st.outline)
        self.cb_shadow.setChecked(st.shadow)
        self.cb_upper.setChecked(st.uppercase)
        self.ed_first.setText(st.text_first)
        self.ed_streak.setText(st.text_streak)
        self.ed_multi.setText(st.text_multi)
        self.cb_count.setChecked(st.show_count)
        self.sp_dur.setValue(st.duration)
        self._busy = False
        self._paint_buttons()
        self._render()

    def _moved(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.cb_pos.setCurrentText("Custom")   # dragging a slider means a custom spot
        self._busy = False
        self._changed()

    def _click_preview(self, e) -> None:
        pos = e.position() if hasattr(e, "position") else e.pos()
        self._busy = True
        self.cb_pos.setCurrentText("Custom")
        self.sl_x.setValue(int(round(100 * pos.x() / self.preview_lbl.width())))
        self.sl_y.setValue(int(round(100 * pos.y() / self.preview_lbl.height())))
        self._busy = False
        self._changed()

    def _changed(self, *_):
        if self._busy:
            return
        st = self.style_
        st.position = self.cb_pos.currentText()
        if st.position == "Custom":
            st.x, st.y = self.sl_x.value() / 100, self.sl_y.value() / 100
        else:
            ax, ay = st.anchor()
            st.x, st.y = ax, ay
            self._busy = True
            self.sl_x.setValue(int(round(ax * 100)))
            self.sl_y.setValue(int(round(ay * 100)))
            self._busy = False
        st.size = self.sl_size.value()
        st.font = self.cb_font.currentData() or ""
        st.accent = self.cb_accent.isChecked()
        st.outline = self.sl_outline.value()
        st.shadow = self.cb_shadow.isChecked()
        st.uppercase = self.cb_upper.isChecked()
        st.text_first = self.ed_first.text()
        st.text_streak = self.ed_streak.text()
        st.text_multi = self.ed_multi.text()
        st.show_count = self.cb_count.isChecked()
        st.duration = self.sp_dur.value()
        self._render()

    def _render(self) -> None:
        from PIL import Image
        from .overlay import preview
        self.lbl_size.setText(f"{self.style_.size}%")
        bg = None
        if self.frame is not None and not self.frame.isNull():
            q = self.frame.convertToFormat(QImage.Format_RGB888)
            bg = Image.frombytes("RGB", (q.width(), q.height()), bytes(q.constBits())[:q.bytesPerLine() * q.height()],
                                 "raw", "RGB", q.bytesPerLine())
        _, kills_here, running = self.EXAMPLES[self.cb_example.currentIndex()]
        try:
            img = preview(self.style_, bg, (1280, 720), kills_here, running)
        except (KeyError, IndexError, ValueError) as exc:   # bad {placeholder} while typing
            self.preview_lbl.setText(f"Text error: {exc}")
            return
        data = img.tobytes()
        q = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()
        self.preview_lbl.setPixmap(QPixmap.fromImage(q).scaled(self.preview_lbl.size(), Qt.KeepAspectRatio,
                                                               Qt.SmoothTransformation))

    def result_style(self) -> "CalloutStyle":
        return self.style_


from .mlg import defaults as mlg_defaults  # noqa: E402

EFFECT_DEFAULTS = dict(template="", transition="fade", transition_s=0.35, killcam="none", captions=False,
                       intro_text="", intro_sub="", outro_text="", outro_sub="", card_s=2.5,
                       watermark_text="", watermark_image="", watermark_pos="Top right", watermark_opacity=0.7,
                       audio_track=0, denoise=False, loudnorm=False, thumbnail=False, replay_finale_only=False,
                       grade="None", letterbox=False, vignette=False, grain=False, **mlg_defaults())


class MontageDialog(QDialog):
    """Montage builder: length (or fit the song), theme, ordering, variety rules and a live pick list."""

    def __init__(self, parent: "MainWindow"):
        super().__init__(parent)
        from .hype import ORDERS, THEMES, MontageSpec
        self.mw = parent
        self.setWindowTitle("Montage builder")
        self.resize(900, 600)
        lay = QHBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        st = parent.settings
        self.sp_len = QSpinBox()
        self.sp_len.setRange(5, 1800)
        self.sp_len.setSuffix(" s")
        self.sp_len.setValue(int(st.value("montage_len", 60)))
        self.cb_fit = QCheckBox("Fit the length to the music")
        self.cb_fit.setEnabled(bool(parent.ed_music.text()))
        self.cb_fit.setToolTip("Uses the song chosen under Music" if parent.ed_music.text()
                               else "Choose music in the main window first")
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(list(THEMES))
        self.cb_theme.setCurrentText(str(st.value("montage_theme", "Everything")))
        self.cb_order = QComboBox()
        self.cb_order.addItems(list(ORDERS))
        self.cb_order.setCurrentText(str(st.value("montage_order", "Build-up (best last)")))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(0, 200)
        self.sp_min.setValue(int(st.value("montage_min", 0)))
        self.sp_min.setToolTip("Leave out clips below this hype score")
        self.sp_kills = QSpinBox()
        self.sp_kills.setRange(1, 20)
        self.sp_kills.setValue(int(st.value("montage_kills", 1)))
        self.sp_kills.setSuffix(" kill(s) per clip")
        self.sp_pervid = QSpinBox()
        self.sp_pervid.setRange(0, 50)
        self.sp_pervid.setSpecialValueText("no limit")
        self.sp_pervid.setValue(int(st.value("montage_pervid", 0)))
        self.cb_weapon = QCheckBox("Don't use the same weapon twice in a row")
        self.cb_weapon.setChecked(st.value("montage_weapon", "false") == "true")
        self.cb_finale = QCheckBox("Slow-mo replay of the finale only")
        self.cb_finale.setChecked(st.value("montage_finale", "true") == "true")
        self.cb_beat = QCheckBox("Cut on the beat (needs music)")
        self.cb_beat.setChecked(parent.cb_beat.isChecked() or bool(parent.ed_music.text()))
        self.cb_beat.setEnabled(bool(parent.ed_music.text()))
        self.sp_seed = QSpinBox()
        self.sp_seed.setRange(0, 9999)
        self.sp_seed.setPrefix("shuffle #")
        self.cb_mlg = QCheckBox("MLG mode (air horns, hitmarkers, cans, strobe, OH BABY A TRIPLE)")
        self.cb_mlg.setToolTip("Tune it under Effects… → MLG settings. Warning: contains flashing images.")
        self.cb_mlg.setChecked(bool(parent.effects.get("mlg")))
        form.addRow("Length", self.sp_len)
        form.addRow("", self.cb_fit)
        form.addRow("Theme", self.cb_theme)
        form.addRow("Order", self.cb_order)
        form.addRow("", self.sp_seed)
        form.addRow("Minimum hype", self.sp_min)
        form.addRow("At least", self.sp_kills)
        form.addRow("Max clips per video", self.sp_pervid)
        form.addRow("", self.cb_weapon)
        form.addRow("", self.cb_finale)
        form.addRow("", self.cb_beat)
        form.addRow("Style", self.cb_mlg)
        vers = QHBoxLayout()
        self.cb_vers = {}
        for n in (15, 30, 60):
            cb = QCheckBox(f"{n}s")
            self.cb_vers[n] = cb
            vers.addWidget(cb)
        vers.addStretch(1)
        form.addRow("Also export", vers)
        right = QVBoxLayout()
        lay.addLayout(right, 1)
        self.lbl = QLabel()
        right.addWidget(self.lbl)
        self.list = QListWidget()
        right.addWidget(self.list, 1)
        btns = QHBoxLayout()
        b_use = QPushButton("Use this montage")
        b_use.setToolTip("Ticks these clips in this order (then press Build highlight video)")
        b_use.clicked.connect(self._use)
        b_build = QPushButton("Build now…")
        b_build.setDefault(True)
        b_build.clicked.connect(self._build)
        b_cancel = QPushButton("Cancel")
        b_cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(b_cancel)
        btns.addWidget(b_use)
        btns.addWidget(b_build)
        right.addLayout(btns)
        for w in (self.sp_len, self.sp_min, self.sp_kills, self.sp_pervid, self.sp_seed):
            w.valueChanged.connect(self._refresh)
        for w in (self.cb_theme, self.cb_order):
            w.currentIndexChanged.connect(self._refresh)
        for w in (self.cb_fit, self.cb_weapon):
            w.toggled.connect(self._refresh)
        self.build_requested = False
        self._refresh()

    def spec(self, target: Optional[float] = None):
        from .hype import MontageSpec, music_length
        t = target or self.sp_len.value()
        if target is None and self.cb_fit.isChecked() and self.mw.ed_music.text():
            ml = music_length(self.mw.ed_music.text())
            if ml > 0:
                t = ml
        return MontageSpec(t, self.cb_theme.currentText(), self.sp_min.value(), self.cb_order.currentText(),
                           self.sp_pervid.value(), self.cb_weapon.isChecked(), self.sp_kills.value(),
                           self.sp_seed.value())

    def _refresh(self, *_):
        self.sp_seed.setVisible(self.cb_order.currentText() == "Shuffle")
        self.sp_len.setEnabled(not self.cb_fit.isChecked())
        picks = self.mw.montage_picks(self.spec())
        self.picks = picks
        self.list.clear()
        total = 0.0
        pre, post = self.mw.sp_pre.value(), self.mw.sp_post.value()
        for i, (v, c, s) in enumerate(picks):
            total += c.end - c.start + pre + post
            self.list.addItem(f"{i + 1}.  {s.score:5.0f} {s.stars}  {v.name} {fmt_time(c.start)} — {c.title} "
                              f"({', '.join(s.reasons)})")
        spec = self.spec()
        self.lbl.setText(f"<b>{len(picks)} clips, about {fmt_time(total)}</b> (target {fmt_time(spec.target_s)})"
                         + ("" if picks else " — nothing matches; loosen the filters"))

    def _save_settings(self) -> None:
        st = self.mw.settings
        for k, v in (("montage_len", self.sp_len.value()), ("montage_theme", self.cb_theme.currentText()),
                     ("montage_order", self.cb_order.currentText()), ("montage_min", self.sp_min.value()),
                     ("montage_kills", self.sp_kills.value()), ("montage_pervid", self.sp_pervid.value()),
                     ("montage_weapon", "true" if self.cb_weapon.isChecked() else "false"),
                     ("montage_finale", "true" if self.cb_finale.isChecked() else "false")):
            st.setValue(k, v)

    def _use(self) -> None:
        self._save_settings()
        if bool(self.mw.effects.get("mlg")) != self.cb_mlg.isChecked():
            import json as _json
            self.mw.effects["mlg"] = self.cb_mlg.isChecked()
            self.mw.settings.setValue("effects", _json.dumps(self.mw.effects))
        self.mw.apply_montage(self.picks, self.cb_finale.isChecked(), self.cb_beat.isChecked())
        self.accept()

    def _build(self) -> None:
        self._use()
        self.build_requested = True

    def extra_versions(self):
        return [n for n, cb in self.cb_vers.items() if cb.isChecked()]


class MlgDialog(QDialog):
    """Every MLG-mode switch, the chaos/strobe levels, sounds with listen buttons, and a preview."""
    COLS = 4

    def __init__(self, parent, values: dict):
        super().__init__(parent)
        from PySide6.QtWidgets import QGridLayout, QGroupBox, QSlider
        from .mlg import DEEPFRY, INTENSITIES, STROBE_LABELS, STROBES, TOGGLES
        self.setWindowTitle("MLG mode")
        self.v = {**mlg_defaults(), **{k: v for k, v in values.items() if k.startswith("mlg")}}
        lay = QVBoxLayout(self)
        self.cb_on = QCheckBox("MLG mode on")
        self.cb_on.setStyleSheet("font-weight: bold")
        lay.addWidget(self.cb_on)
        warn = QLabel("⚠ Strobe and flashes can trigger seizures in people with photosensitive epilepsy.")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        form = QFormLayout()
        lay.addLayout(form)
        self.cb_int = QComboBox()
        for k in INTENSITIES:
            self.cb_int.addItem(f"{k.capitalize()} chaos", k)
        self.cb_strobe = QComboBox()
        for k in STROBES:
            self.cb_strobe.addItem(STROBE_LABELS[k], k)
        self.cb_fry = QComboBox()
        for k, label in zip(DEEPFRY, ("Off", "On triples", "On every kill")):
            self.cb_fry.addItem(label, k)
        self.sp_seed = QSpinBox()
        self.sp_seed.setRange(0, 9999)
        self.sp_seed.setPrefix("layout #")
        self.sp_seed.setToolTip("Change for a different random placement")
        r1 = QHBoxLayout()
        for w_ in (self.cb_int, self.cb_strobe, self.cb_fry, self.sp_seed):
            r1.addWidget(w_)
        form.addRow("Chaos / strobe / deep-fry", r1)

        box = QGroupBox("What to add")
        grid = QGridLayout(box)
        self.toggles: Dict[str, QCheckBox] = {}
        for i, (k, (label, _, tip)) in enumerate(TOGGLES.items()):
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            self.toggles[k] = cb
            grid.addWidget(cb, i // self.COLS, i % self.COLS)
        allrow = QHBoxLayout()
        for text, state in (("All on", True), ("All off", False)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=state: [cb.setChecked(s) for cb in self.toggles.values()])
            allrow.addWidget(b)
        allrow.addStretch(1)
        grid.addLayout(allrow, len(TOGGLES) // self.COLS + 1, 0, 1, self.COLS)
        lay.addWidget(box)

        form2 = QFormLayout()
        lay.addLayout(form2)
        self.sl_horn = QSlider(Qt.Horizontal)
        self.sl_horn.setRange(0, 100)
        self.lbl_horn = QLabel()
        self.sl_horn.valueChanged.connect(lambda n: self.lbl_horn.setText(f"{n} % of single kills"))
        hr = QHBoxLayout()
        hr.addWidget(self.sl_horn, 1)
        hr.addWidget(self.lbl_horn)
        form2.addRow("Air horn chance", hr)
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 200)
        self.lbl_vol = QLabel()
        self.sl_vol.valueChanged.connect(lambda n: self.lbl_vol.setText(f"{n} %"))
        vr = QHBoxLayout()
        vr.addWidget(self.sl_vol, 1)
        vr.addWidget(self.lbl_vol)
        form2.addRow("Sound effects volume", vr)
        from .mlg import MUSIC
        self.cb_music = QComboBox()
        for k, label in MUSIC.items():
            self.cb_music.addItem(label, k)
        self.cb_music.setToolTip("Loops under the video when no other music is chosen (sandstorm.mp3 / "
                                 "sandstorm_flute.mp3 in your sounds folder)")
        form2.addRow("MLG music", self.cb_music)
        self.ed_words = QLineEdit()
        self.ed_words.setPlaceholderText("your own words, comma separated (empty = OH, WOW, GET REKT, 360 NO SCOPE…)")
        form2.addRow("Meme words", self.ed_words)
        self.ed_ttext = QLineEdit()
        form2.addRow("Triple text", self.ed_ttext)
        self.ed_snd: Dict[str, QLineEdit] = {}
        for key, label, ph, kind in (("mlg_triple_sound", "Triple sound", "another file (empty = your sounds folder, else computer voice)",
                                      "triple"),
                                     ("mlg_airhorn_sound", "Air horn sound", "another file (empty = your sounds folder, else built-in horn)",
                                      "airhorn")):
            row = QHBoxLayout()
            e = QLineEdit()
            e.setPlaceholderText(ph)
            b = QPushButton("Choose…")
            b.clicked.connect(lambda _=False, e=e: self._pick_sound(e))
            bl = QPushButton("▶ Listen")
            bl.clicked.connect(lambda _=False, k=kind: self.listen(k))
            for w_ in (e, b, bl):
                row.addWidget(w_, 1 if w_ is e else 0)
            self.ed_snd[key] = e
            form2.addRow(label, row)
        srow = QGridLayout()
        for i, (kind, label) in enumerate((("hit", "Hitmarker"), ("wub", "Wub"), ("boom", "Explosion"), ("siren", "Siren"),
                            ("fbi", "FBI"), ("wow", "WOW"),
                            ("what", "WHAT"), ("money", "Money"), ("omgwow", "OMG WOW"),
                            ("dotatriple", "TRIPLE KILL"), ("boompow", "BOOM POW"), ("headshot", "Headshot"),
                            ("mtndew", "Mtn Dew baby"), ("pwnage", "Intro sting"), ("sandstorm", "Sandstorm"),
                            ("sandstorm_flute", "Sandstorm flute"), ("voice", "Voice line"))):
            b = QPushButton("▶ " + label)
            b.setToolTip("Listen")
            b.clicked.connect(lambda _=False, k=kind: self.listen(k))
            srow.addWidget(b, i // 6, i % 6)
        form2.addRow("Sounds", srow)
        from .mlg import my_sound, my_sounds_dir
        mine = QHBoxLayout()
        self.lbl_mine = QLabel()
        self.lbl_mine.setWordWrap(True)
        self.lbl_mine.setToolTip("Files named triple, airhorn, explosion, fbi, wow, what, money, omgwow, dotatriple, boompow, hit, headshot, mtndew, pwnage, sandstorm or sandstorm_flute (.mp3/.wav/...) in this folder")
        b_dir = QPushButton("Open my sounds folder")
        b_dir.clicked.connect(lambda: (os.makedirs(my_sounds_dir(), exist_ok=True),
                                       open_with_system(my_sounds_dir())))
        self.b_pack = QPushButton("Download meme sounds")
        self.b_pack.setToolTip("Oh baby a triple, MLG air horn, FBI OPEN UP, Sandstorm… from myinstants.com "
                               "into your sounds folder (not included in the app because they're copyrighted)")
        self.b_pack.clicked.connect(self._pack_clicked)
        mine.addWidget(self.lbl_mine, 1)
        mine.addWidget(self.b_pack)
        mine.addWidget(b_dir)
        form2.addRow("My sounds", mine)

        btns = QHBoxLayout()
        b_pv = QPushButton("▶ Preview MLG")
        b_pv.setToolTip("Render a few seconds with these settings and play them")
        b_pv.clicked.connect(self._preview)
        b_pv.setEnabled(hasattr(parent, "preview"))
        btns.addWidget(b_pv)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self.last_sound = ""
        self._pack_worker = None
        self.pack_result: dict = {}
        self._load(self.v)
        self._pack_status()
        from .soundpack import missing
        if missing() and not os.environ.get("BF6KT_NO_SOUND_DOWNLOAD"):
            self.download_pack()          # first time: fetch them automatically

    def _load(self, v: dict) -> None:
        self.cb_on.setChecked(bool(v["mlg"]))
        self.cb_int.setCurrentIndex(max(0, self.cb_int.findData(v["mlg_intensity"])))
        self.cb_strobe.setCurrentIndex(max(0, self.cb_strobe.findData(v["mlg_strobe"])))
        self.cb_fry.setCurrentIndex(max(0, self.cb_fry.findData(v["mlg_deepfry"])))
        self.sp_seed.setValue(int(v["mlg_seed"]))
        for k, cb in self.toggles.items():
            cb.setChecked(bool(v[k]))
        self.sl_horn.setValue(int(round(float(v["mlg_airhorn_chance"]) * 100)))
        self.sl_vol.setValue(int(round(float(v["mlg_sfx_volume"]) * 100)))
        self.lbl_horn.setText(f"{self.sl_horn.value()} % of single kills")
        self.lbl_vol.setText(f"{self.sl_vol.value()} %")
        self.ed_words.setText(str(v["mlg_words"]))
        self.cb_music.setCurrentIndex(max(0, self.cb_music.findData(v["mlg_music"])))
        self.ed_ttext.setText(str(v["mlg_triple_text"]))
        for k, e in self.ed_snd.items():
            e.setText(str(v.get(k, "")))

    def values(self) -> dict:
        v = dict(self.v)
        v.update(mlg=self.cb_on.isChecked(), mlg_intensity=self.cb_int.currentData(),
                 mlg_strobe=self.cb_strobe.currentData(), mlg_deepfry=self.cb_fry.currentData(),
                 mlg_seed=self.sp_seed.value(), mlg_airhorn_chance=self.sl_horn.value() / 100,
                 mlg_sfx_volume=self.sl_vol.value() / 100, mlg_words=self.ed_words.text(),
                 mlg_music=self.cb_music.currentData(),
                 mlg_triple_text=self.ed_ttext.text() or "OH BABY A TRIPLE")
        for k, cb in self.toggles.items():
            v[k] = cb.isChecked()
        for k, e in self.ed_snd.items():
            v[k] = e.text()
        return v

    def _pick_sound(self, edit: "QLineEdit") -> None:
        start = os.path.dirname(edit.text()) or os.path.expanduser("~")
        f, _ = QFileDialog.getOpenFileName(self, "Choose a sound", start,
                                           "Audio (*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus);;All files (*)")
        if f:
            edit.setText(f)

    def sound_path(self, kind: str) -> Optional[str]:
        """Make (or find) the sound file for a listen button."""
        import random as _random
        from .mlg import VOICE_LINES, Assets
        v = self.values()
        a = Assets(os.path.join(tempfile.gettempdir(), "bf6kt_sounds"), 640, 360,
                   v["mlg_airhorn_sound"], v["mlg_triple_sound"])
        if kind == "triple":
            t = v["mlg_triple_text"].capitalize().replace("Oh baby a", "Oh baby, a") + "!"
            return a.triple_wav(t)
        if kind == "voice":
            return a.voice(_random.choice(VOICE_LINES))
        if kind in ("headshot", "mtndew", "pwnage", "sandstorm", "sandstorm_flute"):
            return a.mine(kind)
        return {"airhorn": a.airhorn_wav, "hit": a.hit_wav, "wub": a.wub_wav, "boom": a.boom_wav,
                "siren": a.siren_wav, "fbi": a.fbi_wav, "wow": a.wow_wav,
                "what": lambda: a.mine("what"), "money": lambda: a.mine("money"),
                "omgwow": lambda: a.mine("omgwow"), "dotatriple": lambda: a.mine("dotatriple"),
                "boompow": lambda: a.mine("boompow")}[kind]()

    def listen(self, kind: str) -> None:
        if kind not in self._BUILT_IN and not self._have(kind):
            QMessageBox.information(self, "Sound not downloaded",
                                    "This meme sound isn't downloaded yet. Press “Download meme sounds”.")
            return
        path = self.sound_path(kind)
        if not path:
            QMessageBox.information(self, "No sound", "Nothing to play: put fbi.mp3 in your sounds folder for FBI; "
                                    "for voices, this computer has no built-in voice (on Linux install "
                                    "espeak-ng). Choose your own sound file instead.")
            return
        self.last_sound = path
        if os.environ.get("BF6KT_NO_OPEN"):
            return
        from .mlg import play_sound
        try:
            how = play_sound(path)
        except Exception:
            how = ""
        if not how:                      # no built-in player available: hand it to the default app
            open_with_system(path)

    _BUILT_IN = ("hit", "wub", "boom", "siren", "voice", "triple", "airhorn")

    @staticmethod
    def _have(kind: str) -> bool:
        from .mlg import my_sound
        return bool(my_sound(kind))

    # -- meme sound pack -------------------------------------------------------
    def _pack_status(self) -> None:
        from .soundpack import PACK, missing
        miss = missing()
        self.lbl_mine.setText(f"{len(PACK) - len(miss)} of {len(PACK)} meme sounds downloaded" if miss
                              else f"All {len(PACK)} meme sounds downloaded")
        self.b_pack.setText(f"Download meme sounds ({len(miss)})" if miss else "Re-download meme sounds")

    def _pack_clicked(self) -> None:
        from .soundpack import missing
        self.download_pack(only_missing=bool(missing()))

    def download_pack(self, only_missing: bool = True) -> None:
        if self._pack_worker is not None:
            return
        w = SoundPackWorker(only_missing)
        self._pack_worker = w
        self.b_pack.setEnabled(False)
        self.lbl_mine.setText("Downloading meme sounds…")

        def wire(worker):
            worker.progress.connect(lambda i, n, label: self.lbl_mine.setText(f"Downloading {label}… ({i}/{n})"))
            worker.done.connect(self._pack_done)

        mw = self.parent()
        while mw is not None and not hasattr(mw, "_run_in_thread"):
            mw = mw.parent()
        if mw is not None:
            mw._run_in_thread(w, wire)
        else:                            # no main window: run here
            wire(w)
            w.start_work()

    def _pack_done(self, result: dict) -> None:
        self._pack_worker = None
        self.b_pack.setEnabled(True)
        self.pack_result = result
        self._pack_status()
        bad = [k for k, v in result.items() if v not in ("ok", "skipped")]
        if bad:
            self.lbl_mine.setText(self.lbl_mine.text() + f" — {len(bad)} couldn't be downloaded (no internet?)")

    def done(self, r: int) -> None:  # noqa: D401 - QDialog override: stop any sound on close
        from .mlg import stop_sound
        stop_sound()
        super().done(r)

    def _preview(self) -> None:
        p = self.parent()
        p.preview({**p.values(), **self.values(), "mlg": True})


class EffectsDialog(QDialog):
    """Transitions, kill-cam effects, captions, intro/outro cards, watermark, audio and templates."""
    TRANSITIONS = [("Fade through black", "fade"), ("Hard cut", "cut"), ("Crossfade", "crossfade"),
                   ("Whip pan (slide)", "whip"), ("Zoom", "zoom")]
    KILLCAMS = [("None", "none"), ("Zoom in on each kill", "zoom"), ("Shake on headshots", "shake"),
                ("Zoom + shake", "both")]

    def __init__(self, parent, values: dict, user_templates: dict):
        super().__init__(parent)
        from .reel import TEMPLATES
        self.setWindowTitle("Effects & branding")
        self.v = {**EFFECT_DEFAULTS, **values}
        self.user_templates = dict(user_templates)
        self.template_applied: Optional[str] = None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        trow = QHBoxLayout()
        self.cb_tpl = QComboBox()
        self.cb_tpl.addItem("— choose a look —", "")
        for n in TEMPLATES:
            self.cb_tpl.addItem(n, n)
        for n in self.user_templates:
            self.cb_tpl.addItem(f"{n} (yours)", "user:" + n)
        b_apply = QPushButton("Apply")
        b_apply.setToolTip("Sets transitions, effects, call-out style, extras and (for TikTok) the vertical export")
        b_apply.clicked.connect(self._apply_template)
        b_save = QPushButton("Save current as template…")
        b_save.clicked.connect(self._save_template)
        trow.addWidget(self.cb_tpl, 1)
        trow.addWidget(b_apply)
        trow.addWidget(b_save)
        form.addRow("<b>Template</b>", trow)
        self.cb_tr = QComboBox()
        self.cb_tr.addItems([n for n, _ in self.TRANSITIONS])
        self.sp_tr = QDoubleSpinBox()
        self.sp_tr.setRange(0.1, 2.0)
        self.sp_tr.setSingleStep(0.05)
        self.sp_tr.setSuffix(" s")
        tr_row = QHBoxLayout()
        tr_row.addWidget(self.cb_tr, 1)
        tr_row.addWidget(self.sp_tr)
        form.addRow("Transitions", tr_row)
        self.cb_kc = QComboBox()
        self.cb_kc.addItems([n for n, _ in self.KILLCAMS])
        form.addRow("Kill-cam effect", self.cb_kc)
        self.cb_cap = QCheckBox("Caption each kill (HEADSHOT · RIFLE, C4…)")
        form.addRow("", self.cb_cap)
        self.ed = {}
        for key, label, ph in (("intro_text", "Intro card", "e.g. your gamertag (empty = no intro)"),
                               ("intro_sub", "", "small line, e.g. BF6 highlights"),
                               ("outro_text", "Outro card", "e.g. Thanks for watching (empty = none)"),
                               ("outro_sub", "", "e.g. Subscribe for more"),
                               ("watermark_text", "Watermark", "e.g. @yourname (or pick an image)")):
            e = QLineEdit()
            e.setPlaceholderText(ph)
            self.ed[key] = e
            form.addRow(label, e)
        wrow = QHBoxLayout()
        self.ed_wimg = QLineEdit()
        self.ed_wimg.setPlaceholderText("logo image (PNG with transparency)")
        b_w = QPushButton("Image…")
        b_w.clicked.connect(self._pick_logo)
        self.cb_wpos = QComboBox()
        self.cb_wpos.addItems(["Top left", "Top right", "Bottom left", "Bottom right"])
        self.sp_wop = QSpinBox()
        self.sp_wop.setRange(10, 100)
        self.sp_wop.setSuffix(" % opacity")
        wrow.addWidget(self.ed_wimg, 1)
        wrow.addWidget(b_w)
        wrow.addWidget(self.cb_wpos)
        wrow.addWidget(self.sp_wop)
        form.addRow("", wrow)
        self.sp_card = QDoubleSpinBox()
        self.sp_card.setRange(1, 8)
        self.sp_card.setSuffix(" s per card")
        form.addRow("", self.sp_card)
        self.sp_track = QSpinBox()
        self.sp_track.setRange(1, 8)
        self.sp_track.setPrefix("track ")
        self.sp_track.setToolTip("OBS can record game and mic on separate tracks; pick which one to use")
        form.addRow("Game audio", self.sp_track)
        self.cb_dn = QCheckBox("Reduce background noise / hiss")
        self.cb_ln = QCheckBox("Even out loudness (YouTube level)")
        self.cb_thumb = QCheckBox("Also save a YouTube thumbnail (.jpg next to the video)")
        form.addRow("", self.cb_dn)
        form.addRow("", self.cb_ln)
        form.addRow("Extras", self.cb_thumb)
        from .reel import GRADES
        lrow = QHBoxLayout()
        self.cb_grade = QComboBox()
        for g in GRADES:
            self.cb_grade.addItem("Colour: " + g, g)
        self.cb_lb = QCheckBox("Cinema bars")
        self.cb_vig = QCheckBox("Vignette")
        self.cb_grain = QCheckBox("Film grain")
        lrow.addWidget(self.cb_grade, 1)
        for w_ in (self.cb_lb, self.cb_vig, self.cb_grain):
            lrow.addWidget(w_)
        form.addRow("Look", lrow)
        mrow = QHBoxLayout()
        self.cb_mlg = QCheckBox("MLG mode — the 2014 montage parody look")
        self.cb_mlg.setToolTip("Hitmarkers, air horns, flying soda cans, meme text, colour strobe and "
                               "OH BABY A TRIPLE on 3 kills in a few seconds")
        b_mlg = QPushButton("MLG settings…")
        b_mlg.setToolTip("20+ switches, strobe, chaos level, sounds (with listen buttons)")
        b_mlg.clicked.connect(self._edit_mlg)
        mrow.addWidget(self.cb_mlg, 1)
        mrow.addWidget(b_mlg)
        form.addRow("<b>MLG mode</b>", mrow)
        prow = QHBoxLayout()
        self.cb_pv_clip = QCheckBox("Use the selected clip")
        self.cb_pv_clip.setToolTip("Otherwise a made-up demo clip with a kill, a headshot, a triple and a C4 double")
        _, clip = parent._current_clip() if hasattr(parent, "_current_clip") else (None, None)
        self.cb_pv_clip.setEnabled(clip is not None)
        self.cb_pv_clip.setChecked(clip is not None)
        self.b_preview = QPushButton("▶ Preview this look")
        self.b_preview.setToolTip("Renders a few seconds with these settings (small and fast) and plays it with sound")
        self.b_preview.clicked.connect(lambda: self.preview())
        self.lbl_pv = QLabel("")
        prow.addWidget(self.b_preview)
        prow.addWidget(self.cb_pv_clip)
        prow.addWidget(self.lbl_pv, 1)
        form.addRow("Preview", prow)
        self.last_preview = ""
        self._pv_worker = None
        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self._load(self.v)

    def _load(self, v: dict) -> None:
        self.cb_tr.setCurrentIndex(next((i for i, (_, k) in enumerate(self.TRANSITIONS) if k == v["transition"]), 0))
        self.sp_tr.setValue(float(v["transition_s"]))
        self.cb_kc.setCurrentIndex(next((i for i, (_, k) in enumerate(self.KILLCAMS) if k == v["killcam"]), 0))
        self.cb_cap.setChecked(bool(v["captions"]))
        for k, e in self.ed.items():
            e.setText(str(v.get(k, "")))
        self.ed_wimg.setText(v["watermark_image"])
        self.cb_wpos.setCurrentText(v["watermark_pos"])
        self.sp_wop.setValue(int(round(float(v["watermark_opacity"]) * 100)))
        self.sp_card.setValue(float(v["card_s"]))
        self.sp_track.setValue(int(v["audio_track"]) + 1)
        self.cb_dn.setChecked(bool(v["denoise"]))
        self.cb_ln.setChecked(bool(v["loudnorm"]))
        self.cb_thumb.setChecked(bool(v["thumbnail"]))
        self.cb_mlg.setChecked(bool(v["mlg"]))
        self.cb_grade.setCurrentIndex(max(0, self.cb_grade.findData(v["grade"])))
        self.cb_lb.setChecked(bool(v["letterbox"]))
        self.cb_vig.setChecked(bool(v["vignette"]))
        self.cb_grain.setChecked(bool(v["grain"]))

    def values(self) -> dict:
        v = dict(self.v)
        v.update(transition=self.TRANSITIONS[self.cb_tr.currentIndex()][1], transition_s=self.sp_tr.value(),
                 killcam=self.KILLCAMS[self.cb_kc.currentIndex()][1], captions=self.cb_cap.isChecked(),
                 watermark_image=self.ed_wimg.text(), watermark_pos=self.cb_wpos.currentText(),
                 watermark_opacity=self.sp_wop.value() / 100, card_s=self.sp_card.value(),
                 audio_track=self.sp_track.value() - 1, denoise=self.cb_dn.isChecked(),
                 loudnorm=self.cb_ln.isChecked(), thumbnail=self.cb_thumb.isChecked(),
                 mlg=self.cb_mlg.isChecked(), grade=self.cb_grade.currentData(), letterbox=self.cb_lb.isChecked(),
                 vignette=self.cb_vig.isChecked(), grain=self.cb_grain.isChecked())
        for k, e in self.ed.items():
            v[k] = e.text()
        return v

    def _edit_mlg(self) -> None:
        cur = self.values()
        dlg = MlgDialog(self, cur)
        if dlg.exec():
            self.v = {**cur, **dlg.values()}
            self._load(self.v)

    # -- look preview --------------------------------------------------------
    def preview_options(self, values: Optional[dict] = None) -> ReelOptions:
        """Main-window settings + this dialog's (unsaved) values, made small and fast."""
        from .reel import TEMPLATES, apply_template, preview_options
        mw = self.parent()
        o = mw.reel_options()
        if self.template_applied in TEMPLATES:
            o = apply_template(self.template_applied, o)     # call-out style, overlays, replay of the look
            o.overlays = o.overlays or TEMPLATES[self.template_applied].get("overlays", False)
        for k, val in (values or self.values()).items():
            if hasattr(o, k) and k != "template":
                setattr(o, k, val)
        return preview_options(o)

    def preview_segment(self) -> Segment:
        from .reel import caption_for, demo_segment
        mw = self.parent()
        v, c = mw._current_clip() if hasattr(mw, "_current_clip") else (None, None)
        if self.cb_pv_clip.isChecked() and v is not None and c is not None:
            a = max(0.0, c.start - 1.5)
            b = min(v.duration or c.end + 2, c.end + 2.0, a + 14.0)
            kills = [(k.time, k.count) for k in c.events if a <= k.time <= b]
            caps = [(k.time, caption_for(k.type_list(), k.weapons)) for k in c.events if a <= k.time <= b]
            return Segment(v.path, a, b, c.title, kills, c.title, caps)
        return demo_segment(os.path.join(tempfile.gettempdir(), "bf6kt_preview"))

    def preview(self, values: Optional[dict] = None) -> None:
        if self._pv_worker is not None:
            return
        try:
            seg = self.preview_segment()
            o = self.preview_options(values)
        except Exception as exc:
            self.lbl_pv.setText(f"Preview failed: {exc}")
            return
        d = os.path.join(tempfile.gettempdir(), "bf6kt_preview")
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, f"look_{int(time.time() * 1000) % 10_000_000}.mp4")   # new name: players lock files
        w = ReelWorker([seg], out, o)
        self._pv_worker = w
        self.b_preview.setEnabled(False)
        self.lbl_pv.setText("Rendering preview…")

        def wire(worker):
            worker.progress.connect(lambda f, m: self.lbl_pv.setText(f"Rendering preview… {int(f * 100)}%"))
            worker.done.connect(self._preview_done)
            worker.failed.connect(self._preview_failed)

        self.parent()._run_in_thread(w, wire)

    def _preview_done(self, path: str) -> None:
        self._pv_worker = None
        self.b_preview.setEnabled(True)
        self.last_preview = path
        self.lbl_pv.setText("Playing in your video player")
        if not os.environ.get("BF6KT_NO_OPEN"):
            open_with_system(path)

    def _preview_failed(self, msg: str) -> None:
        self._pv_worker = None
        self.b_preview.setEnabled(True)
        self.lbl_pv.setText("Preview failed: " + msg[-200:])

    def _apply_template(self) -> None:
        from .reel import TEMPLATES
        key = self.cb_tpl.currentData()
        if not key:
            return
        from .reel import template_values
        t = self.user_templates[key[5:]] if key.startswith("user:") else template_values(key)
        cur = self.values()
        for k in EFFECT_DEFAULTS:
            if k in t:
                cur[k] = t[k]
        self._load(cur)
        self.v = cur
        self.template_applied = key

    def _save_template(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Save template", "Template name:")
        if ok and name.strip():
            self.user_templates[name.strip()] = {k: v for k, v in self.values().items()
                                                 if k not in ("template", "thumbnail")}
            self.user_templates[name.strip()]["_main"] = True     # also store main-window extras on OK
            self.cb_tpl.addItem(f"{name.strip()} (yours)", "user:" + name.strip())
            self.cb_tpl.setCurrentIndex(self.cb_tpl.count() - 1)
            self.template_applied = "save:" + name.strip()

    def _pick_logo(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Watermark image", os.path.expanduser("~"),
                                           "Images (*.png *.jpg *.jpeg *.webp)")
        if f:
            self.ed_wimg.setText(f)


class ReviewDialog(QDialog):
    """Check every detection: ✓ correct, ✗ not a kill, fix the count, or add a missed kill/death.

    Click a row to see it in the preview player. Corrections are saved per video and
    applied to every future scan.
    """

    def __init__(self, parent, entry: "VideoEntry"):
        super().__init__(parent)
        from . import review
        self.entry = entry
        self.rv = review.load(entry.path)
        self.setWindowTitle(f"Review — {entry.name}")
        self.resize(1100, 640)
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        lay.addLayout(left, 1)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Event", "Detected by", "Count", "Your verdict"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.currentCellChanged.connect(lambda r, *_: self._show(r))
        left.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn, key in (("✓ Correct", self.mark_ok, "C"), ("✗ Not a kill", self.mark_bad, "X"),
                              ("Count −", lambda: self.change_count(-1), "-"),
                              ("Count +", lambda: self.change_count(1), "+")):
            b = QPushButton(text)
            b.setShortcut(QKeySequence(key))
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        left.addLayout(row)
        row2 = QHBoxLayout()
        b_add = QPushButton("+ Missed kill at preview time")
        b_add.clicked.connect(lambda: self.add_at_playhead("kill"))
        b_addd = QPushButton("+ Missed death at preview time")
        b_addd.clicked.connect(lambda: self.add_at_playhead("death"))
        self.lbl = QLabel()
        row2.addWidget(b_add)
        row2.addWidget(b_addd)
        row2.addStretch(1)
        row2.addWidget(self.lbl)
        left.addLayout(row2)
        right = QVBoxLayout()
        lay.addLayout(right)
        self.player = PreviewPlayer()
        right.addWidget(self.player)
        right.addWidget(QLabel("Shortcuts: C correct · X not a kill · +/− count · arrows move"))
        right.addStretch(1)
        b_close = QPushButton("Done")
        b_close.clicked.connect(self.accept)
        right.addWidget(b_close)
        self._raw = None
        self._load_rows()

    def _load_rows(self) -> None:
        from . import analyze
        try:   # the raw detections (without earlier corrections), from the remembered scan
            self._raw = analyze(self.entry.path, use_cache=True, apply_review=False)
        except Exception:
            self._raw = self.entry.result
        r = self._raw
        hints = []
        try:
            from .audio import intensity, missed_kill_hints
            hints = missed_kill_hints(intensity(self.entry.path), [k.time for k in r.kills])
        except Exception:
            pass
        rows = [(k.time, "kill", k) for k in r.kills] + [(d, "death", None) for d in r.deaths]
        rows += [(a["time"], "added kill", a) for a in self.rv.added]
        rows += [(d, "added death", None) for d in self.rv.added_deaths]
        rows += [(h, "hint", None) for h in hints if not any(abs(h - x[0]) < 2 for x in rows)]
        rows.sort(key=lambda x: x[0])
        self.rows = rows
        self.table.setRowCount(len(rows))
        for i, (t, kind, obj) in enumerate(rows):
            self._fill(i)
        self._summary()

    def _verdict(self, t: float, kind: str) -> str:
        from .review import _near
        if kind == "kill":
            if _near(t, self.rv.rejected):
                return "✗ not a kill"
            if _near(t, self.rv.confirmed):
                return "✓ correct"
        if kind == "death" and _near(t, self.rv.rejected_deaths):
            return "✗ not a death"
        if kind.startswith("added"):
            return "added by you"
        return ""

    def _fill(self, i: int) -> None:
        t, kind, obj = self.rows[i]
        if kind == "kill":
            n = self.rv.counts.get(f"{t:.3f}", obj.count)
            types = ", ".join(x for x in obj.type_list() if x != "unknown")
            ev, by, cnt = f"Kill ({types})" if types else "Kill", obj.evidence or "skull", str(n)
        elif kind == "death":
            ev, by, cnt = "Death", "kill feed / MAN DOWN", ""
        elif kind == "added kill":
            ev, by, cnt = "Kill", "you", str(obj.get("count", 1))
        elif kind == "added death":
            ev, by, cnt = "Death", "you", ""
        else:
            ev, by, cnt = "Possible missed kill? (gunfire, no kill detected)", "audio", ""
        for c, val in enumerate([fmt_time(t), ev, by, cnt, self._verdict(t, kind)]):
            it = QTableWidgetItem(val)
            if kind == "hint":
                it.setForeground(QColor("#888"))
            self.table.setItem(i, c, it)

    def _summary(self) -> None:
        from .review import accuracy
        if self._raw is not None and not self.rv.is_empty():
            a = accuracy(self._raw, self.rv)
            self.lbl.setText(f"detector vs your review: {a['false']} false, {a['missed']} missed")
        else:
            self.lbl.setText("No corrections yet")

    def _show(self, r: int) -> None:
        if 0 <= r < len(self.rows):
            t = self.rows[r][0]
            self.player.load(self.entry.path, max(0.0, t - 2.5), t + 2.0, autoplay=True)

    def _cur(self):
        r = self.table.currentRow()
        return (r, *self.rows[r]) if 0 <= r < len(self.rows) else (None, None, None, None)

    def _save(self, r: int) -> None:
        from . import review
        review.save(self.rv)
        self._fill(r)
        self._summary()
        if r + 1 < self.table.rowCount():
            self.table.setCurrentCell(r + 1, 0)

    def _drop(self, lst, t):
        lst[:] = [x for x in lst if abs(x - t) > 0.6]

    def mark_ok(self) -> None:
        r, t, kind, obj = self._cur()
        if r is None:
            return
        if kind == "kill":
            self._drop(self.rv.rejected, t)
            self._drop(self.rv.confirmed, t)
            self.rv.confirmed.append(round(t, 3))
        elif kind == "death":
            self._drop(self.rv.rejected_deaths, t)
        elif kind == "hint":
            self.rv.added.append({"time": round(t - 0.5, 3), "count": 1})
            self.rows[r] = (t - 0.5, "added kill", self.rv.added[-1])
        self._save(r)

    def mark_bad(self) -> None:
        r, t, kind, obj = self._cur()
        if r is None:
            return
        if kind == "kill":
            self._drop(self.rv.confirmed, t)
            self._drop(self.rv.rejected, t)
            self.rv.rejected.append(round(t, 3))
        elif kind == "death":
            self._drop(self.rv.rejected_deaths, t)
            self.rv.rejected_deaths.append(round(t, 3))
        elif kind == "added kill":
            self.rv.added = [a for a in self.rv.added if a is not obj]
        elif kind == "added death":
            self._drop(self.rv.added_deaths, t)
        self._save(r)

    def change_count(self, d: int) -> None:
        r, t, kind, obj = self._cur()
        if kind == "kill":
            n = max(1, self.rv.counts.get(f"{t:.3f}", obj.count) + d)
            if n == obj.count:
                self.rv.counts.pop(f"{t:.3f}", None)
            else:
                self.rv.counts[f"{t:.3f}"] = n
            self._save(r)
            self.table.setCurrentCell(r, 0)
        elif kind == "added kill":
            obj["count"] = max(1, int(obj.get("count", 1)) + d)
            self._save(r)
            self.table.setCurrentCell(r, 0)

    def add_at_playhead(self, what: str) -> None:
        if not self.player.path:
            return
        t = round(self.player.start + self.player.pos, 2)
        if what == "kill":
            self.rv.added.append({"time": t, "count": 1})
        else:
            self.rv.added_deaths.append(t)
        from . import review
        review.save(self.rv)
        self._load_rows()

    def done(self, r) -> None:  # noqa: D401 - Qt override
        self.player.shutdown()
        super().done(r)


class PlayersDialog(QDialog):
    """Opponents grouped by name across the loaded videos (names shown as they appeared in the kill feed)."""

    def __init__(self, parent, videos: List["VideoEntry"]):
        super().__init__(parent)
        from .names import group_players
        self.setWindowTitle("Players — who you kill most, who kills you")
        self.resize(760, 520)
        feed = []
        for v in videos:
            if v.result and v.result.feed_used:
                for e in v.result.feed:
                    feed.append((v, e))
        players = group_players([e for _, e in feed])
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Names are matched by how they look in the kill feed. Click a row's times to jump."))
        table = QTableWidget(len(players), 4)
        table.setHorizontalHeaderLabels(["Player", "You killed them", "They killed you", "When"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for r, p in enumerate(players):
            if p.image is not None and p.image.size:
                im = np.ascontiguousarray(p.image[:, :, ::-1])
                q = QImage(im.data, im.shape[1], im.shape[0], im.strides[0], QImage.Format_RGB888).copy()
                lbl = QLabel()
                lbl.setPixmap(QPixmap.fromImage(q))
                lbl.setStyleSheet("background:#181818; padding:2px;")
                table.setCellWidget(r, 0, lbl)
            for c, val in ((1, len(p.kills)), (2, len(p.deaths))):
                it = QTableWidgetItem()
                it.setData(Qt.DisplayRole, val)
                it.setTextAlignment(Qt.AlignCenter)
                table.setItem(r, c, it)
            table.setItem(r, 3, QTableWidgetItem(", ".join(fmt_time(t) for t in sorted(p.kills + p.deaths))))
            table.setRowHeight(r, max(30, (p.image.shape[0] + 6) if p.image is not None else 30))
        table.setColumnWidth(0, 260)
        lay.addWidget(table, 1)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        lay.addWidget(b, 0, Qt.AlignRight)
        self.table = table
        self.players = players


class UpdateWorker(Worker):
    found = Signal(object)

    def run(self) -> None:
        from .update import check_for_update
        rel = check_for_update()
        if rel:
            self.found.emit(rel)


class LibraryDialog(QDialog):
    """Every scanned session with totals: best streaks, kills per hour, headshot rate, K/D."""
    open_videos = Signal(list)
    COLS = ["Recorded", "Video", "Length", "Kills", "Deaths", "K/D", "Best streak", "Best life", "Kills/hour",
            "Headshots"]

    def __init__(self, parent, streak_gap: float, streak_min: int):
        super().__init__(parent)
        self.setWindowTitle("Library & stats")
        self.resize(1000, 560)
        self.sessions = library.sessions(streak_gap, streak_min)
        tot = library.totals(self.sessions)
        lay = QVBoxLayout(self)
        summary = QLabel(
            f"<b>{tot['videos']}</b> videos, <b>{fmt_time(tot['hours'] * 3600)}</b> of footage &nbsp;·&nbsp; "
            f"<b>{tot['kills']}</b> kills, <b>{tot['deaths']}</b> deaths, K/D <b>{tot['kd']:.2f}</b> &nbsp;·&nbsp; "
            f"<b>{tot['kills_per_hour']:.0f}</b> kills/hour &nbsp;·&nbsp; "
            f"headshots <b>{tot['headshot_rate'] * 100:.0f}%</b> &nbsp;·&nbsp; "
            f"best streak <b>{tot['best_streak']}</b>, most kills in one life <b>{tot['best_life']}</b>")
        summary.setWordWrap(True)
        lay.addWidget(summary)
        self.table = QTableWidget(len(self.sessions), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        best = max((s.best_streak for s in self.sessions), default=-1)
        for r, se in enumerate(reversed(self.sessions)):
            vals = [time.strftime("%Y-%m-%d %H:%M", time.localtime(se.recorded)), se.name, fmt_time(se.duration),
                    se.kills, se.deaths if se.feed_used else "–", f"{se.kd:.1f}" if se.feed_used else "–",
                    se.best_streak, se.best_life, f"{se.kills_per_hour:.0f}",
                    f"{se.headshot_rate * 100:.0f}%" if se.feed_used else "–"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem()
                it.setData(Qt.DisplayRole, val)
                if c == 1:
                    it.setToolTip(se.video)
                    it.setData(Qt.UserRole, se.video)
                if c == 6 and se.best_streak == best and best > 0:
                    it.setForeground(QColor("#e67e22"))
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        b_open = QPushButton("Add selected to the project")
        b_open.clicked.connect(self._open_selected)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        row.addWidget(QLabel("Stats come from every video you've scanned (remembered scans)."))
        row.addStretch(1)
        row.addWidget(b_open)
        row.addWidget(b_close)
        lay.addLayout(row)
        self.table.itemDoubleClicked.connect(lambda *_: self._open_selected())

    def _open_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        paths = [self.table.item(r, 1).data(Qt.UserRole) for r in sorted(rows)]
        if paths:
            self.open_videos.emit(paths)
            self.accept()


# ----------------------------------------------------------------------------
# main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    COLS = ["Video", "Length", "Status", "Kills", "Deaths", "K/D", "Streaks"]

    def __init__(self, settings: Optional[QSettings] = None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1320, 900)
        self.setAcceptDrops(True)
        self.settings = settings or QSettings("bf6killtracker", "gui")
        self.videos: List[VideoEntry] = []
        self._threads: list = []  # (QThread, Worker) pairs kept alive while running
        self._analyze_worker: Optional[AnalyzeWorker] = None
        self._reel_worker: Optional[ReelWorker] = None
        self._thumb_req = 0
        self._build_ui()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda *_: self._watch_timer.start())
        self._watch_timer = QTimer(self)        # debounce: recordings are written over several seconds
        self._watch_timer.setSingleShot(True)
        self._watch_timer.setInterval(3000)
        self._watch_timer.timeout.connect(self._check_watch_folder)
        self._watch_sizes: Dict[str, int] = {}
        self._watch_known: set = set()
        self._project_path: Optional[str] = None
        wd = self.settings.value("watch_dir", "")
        if wd and os.path.isdir(wd) and self.settings.value("watching", "false") == "true":
            self.start_watching(wd)
        if self.settings.value("check_updates", "true") == "true" and not os.environ.get("BF6KT_NO_UPDATE_CHECK"):
            QTimer.singleShot(2000, lambda: self._run_in_thread(
                UpdateWorker(), lambda w: w.found.connect(self._on_update)))
        self._refresh_buttons()

    # -- layout ------------------------------------------------------------
    def _build_ui(self) -> None:
        st = self.style()
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.act_add = QAction(st.standardIcon(QStyle.SP_FileIcon), "Add videos…", self)
        self.act_add.setShortcut(QKeySequence.Open)
        self.act_add.triggered.connect(self.add_files_dialog)
        self.act_add_dir = QAction(st.standardIcon(QStyle.SP_DirOpenIcon), "Add folder…", self)
        self.act_add_dir.triggered.connect(self.add_folder_dialog)
        self.act_remove = QAction(st.standardIcon(QStyle.SP_TrashIcon), "Remove", self)
        self.act_remove.setShortcut(QKeySequence.Delete)
        self.act_remove.triggered.connect(self.remove_selected)
        self.act_analyze = QAction(st.standardIcon(QStyle.SP_MediaPlay), "Find streaks", self)
        self.act_analyze.triggered.connect(self.start_analysis)
        self.act_stop = QAction(st.standardIcon(QStyle.SP_BrowserStop), "Stop", self)
        self.act_stop.triggered.connect(self.stop_work)
        self.act_open = QAction(st.standardIcon(QStyle.SP_DialogOpenButton), "Open project…", self)
        self.act_open.triggered.connect(self.open_project_dialog)
        self.act_save = QAction(st.standardIcon(QStyle.SP_DialogSaveButton), "Save project…", self)
        self.act_save.setShortcut(QKeySequence.Save)
        self.act_save.triggered.connect(self.save_project_dialog)
        self.act_watch = QAction(st.standardIcon(QStyle.SP_FileDialogContentsView), "Watch folder…", self)
        self.act_watch.setCheckable(True)
        self.act_watch.setToolTip("Automatically add and scan new recordings that appear in a folder")
        self.act_watch.triggered.connect(self.toggle_watch)
        self.act_markers = QAction(st.standardIcon(QStyle.SP_FileDialogListView), "Export markers…", self)
        self.act_markers.setToolTip("Write kill/streak markers for Premiere, Resolve, Final Cut, or YouTube chapters")
        self.act_markers.triggered.connect(self.export_markers_dialog)
        self.act_review = QAction(st.standardIcon(QStyle.SP_DialogApplyButton), "Review…", self)
        self.act_review.setToolTip("Check each detected kill/death for the selected video and fix mistakes")
        self.act_review.triggered.connect(self.review_selected)
        self.act_players = QAction(st.standardIcon(QStyle.SP_DirHomeIcon), "Players", self)
        self.act_players.setToolTip("Opponents you killed most / who killed you, from the kill feed")
        self.act_players.triggered.connect(lambda: PlayersDialog(self, self.videos).exec())
        self.act_library = QAction(st.standardIcon(QStyle.SP_FileDialogDetailedView), "Library && stats", self)
        self.act_library.triggered.connect(self.show_library)
        for a in (self.act_add, self.act_add_dir, self.act_remove):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_analyze)
        tb.addAction(self.act_stop)
        tb.addSeparator()
        for a in (self.act_open, self.act_save, self.act_markers, self.act_review, self.act_watch, self.act_players,
                  self.act_library):
            tb.addAction(a)

        # left: video list + settings
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_video_selected)
        self.empty_hint = QLabel("Drop gameplay videos here, or click “Add videos…”")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setStyleSheet("color: gray; padding: 24px;")

        opts = QGroupBox("Streak rules")
        form = QFormLayout(opts)
        self.sp_min = QSpinBox()
        self.sp_min.setRange(2, 50)
        self.sp_min.setValue(int(self.settings.value("streak_min", 3)))
        self.sp_min.setSuffix(" kills")
        self.sp_gap = QDoubleSpinBox()
        self.sp_gap.setRange(1.0, 60.0)
        self.sp_gap.setSingleStep(0.5)
        self.sp_gap.setValue(float(self.settings.value("streak_gap", 5.0)))
        self.sp_gap.setSuffix(" s")
        form.addRow("At least", self.sp_min)
        form.addRow("Max time between kills", self.sp_gap)
        self.cb_singles = QCheckBox("Show all single kills too")
        self.cb_singles.setToolTip("List kills that aren't part of a streak (e.g. a double kill on a tank) "
                                   "so you can tick them into the highlight video")
        self.cb_singles.setChecked(self.settings.value("show_singles", "false") == "true")
        self.cb_singles.toggled.connect(self._on_singles_toggled)
        form.addRow("", self.cb_singles)
        self.cb_lowprio = QCheckBox("Scan at low priority (OK to play while it scans)")
        self.cb_lowprio.setChecked(self.settings.value("low_priority", "true") == "true")
        self.cb_lowprio.toggled.connect(lambda on: self.settings.setValue("low_priority", "true" if on else "false"))
        form.addRow("", self.cb_lowprio)
        self.cb_types = QComboBox()
        self._type_filters = [("All kills", None), ("Headshots only", ["headshot"]),
                              ("Gun kills (incl. headshots)", ["gun", "headshot"]), ("Explosive kills", ["explosive"])]
        self.cb_types.addItems([n for n, _ in self._type_filters])
        self.cb_types.setCurrentIndex(int(self.settings.value("streak_types", 0)))
        self.cb_types.setToolTip("Which kills count toward a streak (kill types come from the kill feed). "
                                 "A death always ends a streak.")
        self.cb_types.currentIndexChanged.connect(self._regroup_all)
        form.addRow("Count", self.cb_types)
        self.sp_min.valueChanged.connect(self._regroup_all)
        self.sp_gap.valueChanged.connect(self._regroup_all)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>Videos</b>"))
        lv.addWidget(self.table, 1)
        lv.addWidget(self.empty_hint)
        lv.addWidget(opts)
        self._left_layout = lv

        # right: streak tree + thumbnails + reel
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Clip", "Kills", "Types", "Time", "Length", "Hype"])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4, 5):
            self.tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._on_tree_check)
        self.tree.currentItemChanged.connect(self._on_clip_selected)
        self.tree.itemDoubleClicked.connect(lambda *_: self.preview.toggle())

        sel_row = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._set_all_checks(True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._set_all_checks(False))
        self.btn_preview = QPushButton(self.style().standardIcon(QStyle.SP_DirLinkIcon), "Open in video player")
        self.btn_preview.setToolTip("Cut this clip at full quality (with sound) and open it in your video player")
        self.btn_preview.clicked.connect(self.preview_streak)
        sel_row.addWidget(b_all)
        sel_row.addWidget(b_none)
        self.btn_montage = QPushButton("Make montage…")
        self.btn_montage.setToolTip("Tick only the best clips to fill a length, ordered to build up to the best one")
        self.btn_montage.clicked.connect(self.make_montage_dialog)
        sel_row.addWidget(self.btn_montage)
        sel_row.addStretch(1)
        sel_row.addWidget(self.btn_preview)

        # in-app preview (thumbnail quality, no sound) + a frame from each kill
        self.preview = PreviewPlayer()
        self.thumb_box = QWidget()
        self.thumb_layout = QVBoxLayout(self.thumb_box)
        self.thumb_layout.setContentsMargins(4, 4, 4, 4)
        self.thumb_layout.addStretch(1)
        thumbs = QScrollArea()
        thumbs.setWidget(self.thumb_box)
        thumbs.setWidgetResizable(True)
        thumbs.setMinimumWidth(200)
        self.thumb_title = QLabel("Select a streak to preview it")
        prev_row = QHBoxLayout()
        pv = QVBoxLayout()
        pv.addWidget(self.preview)
        trim = QHBoxLayout()
        self.sp_trim_a = QDoubleSpinBox()
        self.sp_trim_b = QDoubleSpinBox()
        for sp in (self.sp_trim_a, self.sp_trim_b):
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setSuffix(" s")
            sp.setRange(0, 1e6)
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(self._on_trim_spin)
        b_in = QPushButton("[ Start here")
        b_in.setToolTip("Clip starts at the preview's current position")
        b_in.clicked.connect(lambda: self._trim_at_playhead(True))
        b_out = QPushButton("End here ]")
        b_out.setToolTip("Clip ends at the preview's current position")
        b_out.clicked.connect(lambda: self._trim_at_playhead(False))
        self.btn_untrim = QPushButton("Reset")
        self.btn_untrim.setToolTip("Use the lead-in/tail settings again")
        self.btn_untrim.clicked.connect(self._reset_trim)
        trim.addWidget(QLabel("Clip"))
        trim.addWidget(self.sp_trim_a)
        trim.addWidget(QLabel("to"))
        trim.addWidget(self.sp_trim_b)
        trim.addWidget(b_in)
        trim.addWidget(b_out)
        trim.addWidget(self.btn_untrim)
        self.trim_box = QWidget()
        self.trim_box.setLayout(trim)
        self.trim_box.setEnabled(False)
        pv.addWidget(self.trim_box)
        prev_row.addLayout(pv)
        prev_row.addWidget(thumbs, 1)

        # clip order for the highlight video (drag to reorder)
        self.order_list = QListWidget()
        self.order_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.order_list.model().rowsMoved.connect(self._on_order_moved)
        order_btns = QHBoxLayout()
        for text, fn in (("Move up", lambda: self._move_clip(-1)), ("Move down", lambda: self._move_clip(1)),
                         ("Time order", lambda: self._sort_order("time")),
                         ("Best streak last", lambda: self._sort_order("best_last"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            order_btns.addWidget(b)
        order_btns.addStretch(1)
        order_tab = QWidget()
        ol = QVBoxLayout(order_tab)
        ol.addWidget(QLabel("Order of the clips in the highlight video (drag to reorder):"))
        ol.addWidget(self.order_list, 1)
        ol.addLayout(order_btns)
        self.reel_order: List[tuple] = []     # (video path, clip key)

        reel = QGroupBox("Highlight reel")
        rf = QFormLayout(reel)
        self.sp_pre = QDoubleSpinBox()
        self.sp_pre.setRange(0, 30)
        self.sp_pre.setSingleStep(0.5)
        self.sp_pre.setValue(float(self.settings.value("pad_before", 3.0)))
        self.sp_pre.setSuffix(" s before first kill")
        self.sp_post = QDoubleSpinBox()
        self.sp_post.setRange(0, 30)
        self.sp_post.setSingleStep(0.5)
        self.sp_post.setValue(float(self.settings.value("pad_after", 2.0)))
        self.sp_post.setSuffix(" s after last kill")
        self.cb_preset = QComboBox()
        self.cb_preset.addItems(list(PRESETS))
        self.cb_preset.setCurrentText(str(self.settings.value("reel_preset", "Custom")))
        self.cb_res = QComboBox()
        self.cb_res.addItems(["Same as first video", "1080p (1920×1080)", "1440p (2560×1440)", "720p (1280×720)"])
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["Same as first video", "60 fps", "30 fps"])
        self.cb_preset.currentTextChanged.connect(self._on_preset)
        music_row = QHBoxLayout()
        self.ed_music = QLineEdit(str(self.settings.value("music", "")))
        self.ed_music.setPlaceholderText("No music")
        self.ed_music.setReadOnly(True)
        b_music = QPushButton("Choose…")
        b_music.clicked.connect(self._choose_music)
        b_nomusic = QPushButton("✕")
        b_nomusic.setFixedWidth(30)
        b_nomusic.setToolTip("No music")
        b_nomusic.clicked.connect(lambda: self.ed_music.setText(""))
        music_row.addWidget(self.ed_music, 1)
        music_row.addWidget(b_music)
        music_row.addWidget(b_nomusic)
        self.sp_music_vol = QSpinBox()
        self.sp_music_vol.setRange(0, 200)
        self.sp_music_vol.setValue(int(self.settings.value("music_volume", 80)))
        self.sp_music_vol.setSuffix(" % music volume")
        self.cb_duck = QCheckBox("Lower music when the game is loud")
        self.cb_duck.setChecked(self.settings.value("duck", "true") == "true")
        self.cb_beat = QCheckBox("Cut on the beat")
        self.cb_beat.setChecked(self.settings.value("beat_sync", "false") == "true")
        self.cb_overlays = QCheckBox("Kill call-outs (DOUBLE KILL, streak count)")
        self.cb_overlays.setChecked(self.settings.value("overlays", "false") == "true")
        self.callout_style = CalloutStyle.from_json(self.settings.value("callout_style", ""))
        self.btn_callout = QPushButton("Style…")
        self.btn_callout.setToolTip("Where the call-outs appear, font, colours and wording")
        self.btn_callout.clicked.connect(self.edit_callout_style)
        callout_row = QHBoxLayout()
        callout_row.addWidget(self.cb_overlays)
        callout_row.addWidget(self.btn_callout)
        self.btn_effects = QPushButton("Effects && branding…")
        self.btn_effects.setToolTip("Transitions, kill-cam zoom/shake, captions, intro/outro, watermark, audio, "
                                    "templates, thumbnail")
        self.btn_effects.clicked.connect(self.edit_effects)
        callout_row.addWidget(self.btn_effects)
        import json as _json
        try:
            self.effects = {**EFFECT_DEFAULTS, **_json.loads(self.settings.value("effects", "") or "{}")}
        except ValueError:
            self.effects = dict(EFFECT_DEFAULTS)
        callout_row.addStretch(1)
        self.cb_titles = QCheckBox("Video name at the start of each clip")
        self.cb_titles.setChecked(self.settings.value("titles", "false") == "true")
        self.cb_replay = QCheckBox("Slow-motion replay of the best kill in each clip")
        self.cb_replay.setChecked(self.settings.value("replay", "false") == "true")
        self.cb_encoder = QComboBox()
        self._encoders = [("Automatic (GPU when available)", "auto"), ("CPU (x264)", "libx264"),
                          ("NVIDIA (NVENC)", "h264_nvenc"), ("Intel (Quick Sync)", "h264_qsv"),
                          ("AMD (AMF)", "h264_amf")]
        self.cb_encoder.addItems([n for n, _ in self._encoders])
        self.cb_encoder.setCurrentIndex(int(self.settings.value("encoder", 0)))
        self.cb_open = QCheckBox("Open the video when it’s done")
        self.cb_open.setChecked(self.settings.value("open_after", "true") == "true")
        self.lbl_reel = QLabel("Nothing selected")
        self.lbl_reel.setWordWrap(True)
        self.btn_reel = QPushButton(self.style().standardIcon(QStyle.SP_DialogSaveButton), "Build highlight video…")
        self.btn_reel.setMinimumHeight(34)
        self.btn_reel.clicked.connect(self.build_reel_dialog)
        rf.addRow("Lead-in", self.sp_pre)
        rf.addRow("Tail", self.sp_post)
        rf.addRow("Export", self.cb_preset)
        rf.addRow("Resolution", self.cb_res)
        rf.addRow("Frame rate", self.cb_fps)
        rf.addRow("Music", music_row)
        rf.addRow("", self.sp_music_vol)
        rf.addRow("", self.cb_duck)
        rf.addRow("", self.cb_beat)
        rf.addRow("Extras", callout_row)
        rf.addRow("", self.cb_titles)
        rf.addRow("", self.cb_replay)
        rf.addRow("Encoder", self.cb_encoder)
        rf.addRow("", self.cb_open)
        self.sp_pre.valueChanged.connect(self._update_reel_summary)
        self.sp_post.valueChanged.connect(self._update_reel_summary)
        self._on_preset(self.cb_preset.currentText())

        self._left_layout.addWidget(reel)
        left_scroll = QScrollArea()
        left_scroll.setWidget(left)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.NoFrame)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("<b>Kill streaks</b>  (ticked clips go into the highlight video)"))
        rv.addWidget(self.tree, 1)
        rv.addLayout(sel_row)
        prev_tab = QWidget()
        ptl = QVBoxLayout(prev_tab)
        ptl.addWidget(self.thumb_title)
        ptl.addLayout(prev_row)
        self.timeline = Timeline()
        self.timeline.setToolTip("Drag a clip's edges to trim it · click to select/seek · wheel to zoom, "
                                 "shift+wheel or drag to scroll")
        self.timeline.clipSelected.connect(self._on_timeline_select)
        self.timeline.clipTrimmed.connect(self._on_timeline_trim)
        self.timeline.seekRequested.connect(self._on_timeline_seek)
        self._timeline_video: Optional[str] = None
        self._waves: Dict[str, np.ndarray] = {}
        ptl.addWidget(self.timeline)
        self._tl_timer = QTimer(self)
        self._tl_timer.setInterval(100)
        self._tl_timer.timeout.connect(self._tick_playhead)
        self._tl_timer.start()
        self.tabs = QTabWidget()
        self.tabs.addTab(prev_tab, "Preview && trim")
        self.tabs.addTab(order_tab, "Clip order")
        rv.addWidget(self.tabs)

        split = QSplitter()
        left_wrap = QWidget()
        lw = QVBoxLayout(left_wrap)
        lw.setContentsMargins(0, 0, 0, 0)
        lw.addWidget(left_scroll, 1)
        lw.addWidget(self.lbl_reel)
        lw.addWidget(self.btn_reel)
        split.addWidget(left_wrap)
        split.addWidget(right)
        split.setSizes([500, 820])
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.addWidget(split)
        self.setCentralWidget(central)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status = QLabel("Ready")
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(320)
        self.bar.setVisible(False)
        sb.addWidget(self.status, 1)
        sb.addPermanentWidget(self.bar)

    # -- adding / removing videos -----------------------------------------
    def add_files_dialog(self) -> None:
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        files, _ = QFileDialog.getOpenFileNames(self, "Add gameplay videos", start, VIDEO_FILTER)
        if files:
            self.settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_paths(files)

    def add_folder_dialog(self) -> None:
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        d = QFileDialog.getExistingDirectory(self, "Add a folder of videos", start)
        if d:
            self.settings.setValue("last_dir", d)
            self.add_paths([os.path.join(d, f) for f in sorted(os.listdir(d))
                            if os.path.splitext(f)[1].lower() in VIDEO_EXTS])

    def add_paths(self, paths: List[str]) -> int:
        known = {v.path for v in self.videos}
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p in known or not os.path.isfile(p):
                continue
            entry = VideoEntry(p)
            try:
                entry.duration = probe(p).duration
            except Exception as exc:
                entry.status, entry.error = "Can't open", str(exc)
            self.videos.append(entry)
            known.add(p)
            added += 1
        self._rebuild_table()
        self._refresh_buttons()
        if added:
            self.status.setText(f"Added {added} video(s). Click “Find streaks” to scan them.")
        return added

    def remove_selected(self) -> None:
        if self._analyze_worker:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.videos[r]
        self._rebuild_table()
        self._rebuild_tree()
        self._refresh_buttons()

    def dragEnterEvent(self, e):  # noqa: N802 (Qt naming)
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths: List[str] = []
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if os.path.isdir(p):
                paths += [os.path.join(p, f) for f in sorted(os.listdir(p))
                          if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
            elif os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                paths.append(p)
        self.add_paths(paths)

    # -- table / tree ------------------------------------------------------
    def _row_values(self, v: VideoEntry) -> List[str]:
        status = v.status
        if v.status == "Scanning":
            status = f"Scanning {v.progress * 100:.0f}%"
        r = v.result
        kills = str(r.total_kills) if r else ""
        deaths = (str(len(r.deaths)) if r.feed_used else "–") if r else ""
        kd = (f"{r.kd:.1f}" if r.feed_used else "–") if r else ""
        streaks = str(len(v.streaks)) if r else ""
        return [v.name, fmt_time(v.duration) if v.duration else "?", status, kills, deaths, kd, streaks]

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self.videos))
        for r, v in enumerate(self.videos):
            for c, text in enumerate(self._row_values(v)):
                it = QTableWidgetItem(text)
                if c == 0:
                    it.setToolTip(v.path if not v.error else f"{v.path}\n\n{v.error}")
                if c >= 3:
                    it.setTextAlignment(Qt.AlignCenter)
                if v.error and c == 2:
                    it.setForeground(QColor("#c0392b"))
                self.table.setItem(r, c, it)
        self.empty_hint.setVisible(not self.videos)

    def _update_row(self, path: str) -> None:
        for r, v in enumerate(self.videos):
            if v.path == path:
                for c, text in enumerate(self._row_values(v)):
                    item = self.table.item(r, c)
                    if item:
                        item.setText(text)
                        if c == 2:
                            item.setForeground(QColor("#c0392b") if v.error else self.palette().text().color())
                return

    def _regroup(self, v: VideoEntry) -> None:
        if v.result:
            only = self._type_filters[self.cb_types.currentIndex()][1]
            v.streaks = find_streaks(v.result.kills, self.sp_gap.value(), self.sp_min.value(),
                                     v.result.deaths, only)

    def _show_singles(self) -> bool:
        return self.cb_singles.isChecked()

    def _on_singles_toggled(self, on: bool) -> None:
        self.settings.setValue("show_singles", "true" if on else "false")
        self._rebuild_tree()

    def _regroup_all(self) -> None:
        self.settings.setValue("streak_min", self.sp_min.value())
        self.settings.setValue("streak_gap", self.sp_gap.value())
        self.settings.setValue("streak_types", self.cb_types.currentIndex())
        for v in self.videos:
            self._regroup(v)
        self._rebuild_table()
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        cur = self._current_clip()
        cur_key = (cur[0].path, cur[1].key) if cur[1] else None
        self.tree.blockSignals(True)
        self.tree.clear()
        restore = None
        for vi, v in enumerate(self.videos):
            if not v.result:
                continue
            clips = v.clips(self._show_singles())
            r = v.result
            summary = f"{len(r.deaths)} death(s), K/D {r.kd:.1f}, best life {r.best_life}" if r.feed_used else ""
            top = QTreeWidgetItem([v.name, str(sum(c.kills for c in clips)), summary, "", ""])
            top.setData(0, Qt.UserRole, (vi, None))
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            if not clips:
                top.setText(0, f"{v.name}  — no streaks ({v.result.total_kills} kills)")
            self.tree.addTopLevelItem(top)
            for c in clips:
                trimmed = "  ✂" if c.key in v.trims else ""
                child = QTreeWidgetItem([c.title + trimmed, str(c.kills), c.types_text(),
                                         fmt_time(c.start) if c.kind == "kill" else
                                         f"{fmt_time(c.start)} – {fmt_time(c.end)}", c.span])
                hs, why = self._hype(v, c)
                child.setText(5, f"{hs:.0f}  {'★' * (1 + min(4, int(hs // 25)))}")
                child.setToolTip(5, ", ".join(why))
                child.setData(0, Qt.UserRole, (vi, c.key))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if v.is_selected(c) else Qt.Unchecked)
                child.setTextAlignment(1, Qt.AlignCenter)
                if c.kind == "kill":
                    fi = child.font(0)
                    fi.setItalic(True)
                    child.setFont(0, fi)
                top.addChild(child)
                if cur_key == (v.path, c.key):
                    restore = child
            top.setExpanded(True)
        self.tree.blockSignals(False)
        if restore is not None:
            self.tree.setCurrentItem(restore)
        elif cur_key:
            self._on_clip_selected()
        self._update_reel_summary()

    def _on_tree_check(self, item: QTreeWidgetItem, col: int) -> None:
        data = item.data(0, Qt.UserRole)
        if not data or col != 0:
            return
        vi, key = data
        if key is not None:
            self.videos[vi].selected[key] = item.checkState(0) == Qt.Checked
            self._update_reel_summary()

    def _set_all_checks(self, on: bool) -> None:
        for v in self.videos:
            for c in v.clips(self._show_singles()):
                v.selected[c.key] = on
        self._rebuild_tree()

    def _on_video_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return
        r = rows.pop()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, Qt.UserRole)[0] == r:
                self.tree.setCurrentItem(top.child(0) if top.childCount() else top)
                self.tree.scrollToItem(top)

    def _current_clip(self):
        it = self.tree.currentItem()
        if not it or not it.data(0, Qt.UserRole):
            return None, None
        vi, key = it.data(0, Qt.UserRole)
        if key is None or vi >= len(self.videos):
            return None, None
        v = self.videos[vi]
        c = next((x for x in v.clips(self._show_singles()) if x.key == key), None)
        return (v, c) if c else (None, None)

    # -- thumbnails --------------------------------------------------------
    def _clear_thumbs(self) -> None:
        while self.thumb_layout.count() > 1:
            w = self.thumb_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _on_clip_selected(self, *_):
        v, c = self._current_clip()
        self._clear_thumbs()
        self._refresh_buttons()
        if not c:
            self.thumb_title.setText("Select a streak to preview it")
            self.preview.clear()
            self.trim_box.setEnabled(False)
            return
        span = f" in {c.span}" if c.kind == "streak" and c.span not in ("", "at once") else (
            " at once" if c.span == "at once" else "")
        what = f"{c.kills} kills" if c.kills > 1 else "1 kill"
        self.thumb_title.setText(f"{v.name} — {c.title.lower() if c.kind == 'kill' else c.title}: {what}{span}  "
                                 f"(preview has no sound; “Open in video player” for full quality)")
        seg = self._segments(only=(v, c))[0]
        self.preview.load(v.path, seg.start, seg.end, autoplay=True)
        self._show_trim(v, c)
        self._refresh_timeline(v, c.key)
        self.timeline.focus_on(seg.start, seg.end)
        self._thumb_req += 1
        self._thumb_slots: Dict[float, QLabel] = {}
        for k in c.events:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            img = QLabel("loading…")
            img.setFixedSize(192, 108)
            img.setAlignment(Qt.AlignCenter)
            img.setStyleSheet("background:#222; color:#888; border-radius:4px;")
            img.setCursor(Qt.PointingHandCursor)
            img.setToolTip("Click to jump the preview to this kill")
            img.mousePressEvent = (lambda _e, t=k.time - seg.start: self.preview.seek(max(0.0, t - 1.0)))
            cap = QLabel(f"{fmt_time(k.time)}" + (f"  ×{k.count}" if k.count > 1 else ""))
            cap.setAlignment(Qt.AlignCenter)
            bl.addWidget(img)
            bl.addWidget(cap)
            self.thumb_layout.insertWidget(self.thumb_layout.count() - 1, box)
            self._thumb_slots[k.time] = img
        self._run_in_thread(ThumbWorker(self._thumb_req, v.path, [k.time for k in c.events]),
                            lambda w: w.thumb.connect(self._on_thumb))

    @Slot(int, float, QImage)
    def _on_thumb(self, req: int, t: float, img: QImage) -> None:
        if req != self._thumb_req:
            return
        lbl = self._thumb_slots.get(t)
        if lbl is not None:
            lbl.setPixmap(QPixmap.fromImage(img).scaled(lbl.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # -- analysis ----------------------------------------------------------
    def start_analysis(self) -> None:
        todo = [v.path for v in self.videos if v.result is None and v.status not in ("Can't open",)]
        if not todo:
            self.status.setText("All videos are already scanned.")
            return
        for v in self.videos:
            if v.path in todo:
                v.status, v.error, v.progress = "Queued", "", 0.0
        self._rebuild_table()
        self._done_count, self._todo_count = 0, len(todo)
        if self.cb_lowprio.isChecked():
            from .video import set_low_priority
            set_low_priority(True)   # (can't be raised again on macOS/Linux without restarting the app)
        w = AnalyzeWorker(todo)
        self._analyze_worker = w

        def wire(worker):
            worker.progress.connect(self._on_progress)
            worker.finished_one.connect(self._on_result)
            worker.failed_one.connect(self._on_failed)
            worker.all_done.connect(self._on_analysis_done)

        self._run_in_thread(w, wire)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.status.setText(f"Scanning {len(todo)} video(s)…")
        self._refresh_buttons()

    def _entry(self, path: str) -> Optional[VideoEntry]:
        return next((v for v in self.videos if v.path == path), None)

    @Slot(str, float)
    def _on_progress(self, path: str, frac: float) -> None:
        v = self._entry(path)
        if v:
            v.status, v.progress = "Scanning", frac
            self._update_row(path)
        overall = (self._done_count + frac) / max(1, self._todo_count)
        self.bar.setValue(int(overall * 100))
        self.status.setText(f"Scanning {os.path.basename(path)} ({self._done_count + 1}/{self._todo_count})…")

    @Slot(str, object)
    def _on_result(self, path: str, res: AnalysisResult) -> None:
        self._done_count += 1
        v = self._entry(path)
        if not v:
            return
        v.result, v.status, v.duration = res, "Done", res.duration or v.duration
        self._regroup(v)
        self._update_row(path)
        self._rebuild_tree()

    @Slot(str, str)
    def _on_failed(self, path: str, msg: str) -> None:
        v = self._entry(path)
        if v:
            v.status = "Stopped" if msg == "Cancelled" else "Error"
            v.error = "" if msg == "Cancelled" else msg
            self._rebuild_table()

    @Slot()
    def _on_analysis_done(self) -> None:
        self._analyze_worker = None
        self.bar.setVisible(False)
        for v in self.videos:
            if v.status in ("Queued", "Scanning"):
                v.status = "Waiting"
        self._rebuild_table()
        n_streaks = sum(len(v.streaks) for v in self.videos)
        n_kills = sum(v.result.total_kills for v in self.videos if v.result)
        self.status.setText(f"Scan finished: {n_kills} kills, {n_streaks} streak(s).")
        self._refresh_buttons()

    def stop_work(self) -> None:
        if self._analyze_worker:
            self._analyze_worker.cancel()
        if self._reel_worker:
            self._reel_worker.cancel()
        self.status.setText("Stopping…")

    # -- preview & reel ----------------------------------------------------
    def _selected_clips(self):
        return [(v, c) for v in self.videos for c in v.clips(self._show_singles()) if v.is_selected(c)]

    def _item(self, v: VideoEntry, c: Clip):
        """plan_segments item for a clip; trimmed clips use their exact range."""
        pre, post = self.sp_pre.value(), self.sp_post.value()
        if c.key in v.trims:
            a, b = v.trims[c.key]
            start, end = a + pre, b - post        # plan_segments adds the padding back
        else:
            start, end = c.start, c.end
        kills = [(k.time, k.count) for k in c.events]
        title = os.path.splitext(v.name)[0]
        from .reel import caption_for
        caps = [(k.time, caption_for(k.type_list(), k.weapons)) for k in c.events]
        return (v.path, start, end, f"{v.name} {c.title.lower()} ({c.kills} kills)", kills, title, caps)

    def _ordered_selection(self):
        sel = {(v.path, c.key): (v, c) for v, c in self._selected_clips()}
        self._sync_order(sel)
        return [sel[k] for k in self.reel_order if k in sel]

    def _segments(self, only=None) -> List[Segment]:
        pairs = [only] if only is not None else self._ordered_selection()
        return plan_segments([self._item(v, c) for v, c in pairs], {v.path: v.duration for v in self.videos},
                             self.sp_pre.value(), self.sp_post.value(), keep_order=True)

    # -- hype & montage -------------------------------------------------------
    def _hype(self, v: VideoEntry, c: Clip):
        from .hype import score_clip
        return score_clip(c.events, v.result.events if v.result else [], c.start, c.end)

    def make_montage_dialog(self) -> None:
        if not any(v.result for v in self.videos):
            QMessageBox.information(self, APP_NAME, "Scan a video first.")
            return
        dlg = MontageDialog(self)
        if not dlg.exec():
            return
        extra = dlg.extra_versions()
        if dlg.build_requested or extra:
            self.build_reel_dialog()
            out = getattr(self, "_last_reel_target", None)
            if out and extra:
                self._pending_versions = [(n, dlg.spec(float(n)), dlg.cb_finale.isChecked()) for n in extra]

    def _candidates(self):
        from .hype import Scored
        out = []
        for v in self.videos:
            if not v.result:
                continue
            for c in v.clips(True):
                sc, why = self._hype(v, c)
                types = [t for k in c.events for t in k.type_list()]
                weapons = [w for k in c.events for w in k.weapons]
                s = Scored(v.path, c.key, c.start, c.end, c.kills, sc, why,
                           [(k.time, k.count) for k in c.events], "", types, weapons)
                out.append((v, c, s))
        return out

    def montage_picks(self, spec):
        """[(video entry, clip, Scored)] chosen by ``spec``."""
        from .hype import build_montage
        cands = self._candidates()
        by = {(s.video, s.key): (v, c) for v, c, s in cands}
        picks = build_montage([s for _, _, s in cands], spec, self.sp_pre.value(), self.sp_post.value())
        return [(*by[(s.video, s.key)], s) for s in picks]

    def apply_montage(self, picks, finale_replay: bool = True, beat: bool = False) -> None:
        if not self.cb_singles.isChecked():
            self.cb_singles.setChecked(True)
        chosen = {(v.path, c.key) for v, c, _ in picks}
        for v in self.videos:
            for c in v.clips(True):
                v.selected[c.key] = (v.path, c.key) in chosen
        self.reel_order = [(v.path, c.key) for v, c, _ in picks]
        self.cb_replay.setChecked(finale_replay)
        self.effects["replay_finale_only"] = finale_replay
        if beat and self.ed_music.text():
            self.cb_beat.setChecked(True)
        self._rebuild_tree()
        self.tabs.setCurrentIndex(1)
        total = sum(g.duration for g in self._segments())
        self.status.setText(f"Montage: {len(picks)} clips, {fmt_time(total)}. Press “Build highlight video…”.")

    def make_montage(self, seconds: float) -> None:
        """Quick montage: best clips for ``seconds``, build-up order (see the Montage builder for options)."""
        from .hype import MontageSpec
        self.apply_montage(self.montage_picks(MontageSpec(seconds)), True)
        return

    def _make_montage_old(self, seconds: float) -> None:
        """Tick the highest-hype clips that fit ``seconds`` and order them for build-up."""
        from .hype import Scored, pick_montage
        if not self.cb_singles.isChecked():
            self.cb_singles.setChecked(True)          # single kills can be montage material too
        cands, lookup = [], {}
        for v in self.videos:
            for c in v.clips(True):
                sc, why = self._hype(v, c)
                a, b = v.trims.get(c.key, (c.start, c.end))
                s = Scored(v.path, c.key, c.start, c.end, c.kills, sc, why)
                cands.append(s)
                lookup[(v.path, c.key)] = v
        picks = pick_montage(cands, seconds, self.sp_pre.value(), self.sp_post.value())
        chosen = {(p.video, p.key) for p in picks}
        for v in self.videos:
            for c in v.clips(True):
                v.selected[c.key] = (v.path, c.key) in chosen
        self.reel_order = [(p.video, p.key) for p in picks]
        self._rebuild_tree()
        self.tabs.setCurrentIndex(1)
        total = sum(g.duration for g in self._segments())
        self.status.setText(f"Montage: {len(picks)} clips, {fmt_time(total)} — best last. "
                            f"Add music with “Cut on the beat” for a synced edit.")

    # -- clip order ----------------------------------------------------------
    def _sync_order(self, sel=None) -> None:
        if sel is None:
            sel = {(v.path, c.key): (v, c) for v, c in self._selected_clips()}
        rank = {v.path: i for i, v in enumerate(self.videos)}
        def natural(k):
            return (rank.get(k[0], 0), sel[k][1].start)

        order = [k for k in self.reel_order if k in sel]
        # clips that (re)appear go to their time position, without disturbing a custom order
        for k in sorted((k for k in sel if k not in order), key=natural):
            i = next((n for n, o in enumerate(order) if natural(o) > natural(k)), len(order))
            order.insert(i, k)
        self.reel_order = order

    def _refresh_order_list(self) -> None:
        sel = {(v.path, c.key): (v, c) for v, c in self._selected_clips()}
        self._sync_order(sel)
        self.order_list.blockSignals(True)
        cur = self.order_list.currentRow()
        self.order_list.clear()
        for n, k in enumerate(self.reel_order):
            v, c = sel[k]
            it = QListWidgetItem(f"{n + 1}.  {v.name} — {c.title} ({c.kills} kills, {fmt_time(c.start)})")
            it.setData(Qt.UserRole, k)
            self.order_list.addItem(it)
        if 0 <= cur < self.order_list.count():
            self.order_list.setCurrentRow(cur)
        self.order_list.blockSignals(False)

    def _on_order_moved(self, *_):
        self.reel_order = [self.order_list.item(i).data(Qt.UserRole) for i in range(self.order_list.count())]
        self._refresh_order_list()
        self._update_reel_summary()

    def _move_clip(self, d: int) -> None:
        i = self.order_list.currentRow()
        j = i + d
        if i < 0 or not 0 <= j < len(self.reel_order):
            return
        self.reel_order[i], self.reel_order[j] = self.reel_order[j], self.reel_order[i]
        self._refresh_order_list()
        self.order_list.setCurrentRow(j)
        self._update_reel_summary()

    def _sort_order(self, how: str) -> None:
        sel = {(v.path, c.key): (v, c) for v, c in self._selected_clips()}
        rank = {v.path: i for i, v in enumerate(self.videos)}
        if how == "time":
            self.reel_order = sorted(sel, key=lambda k: (rank.get(k[0], 0), sel[k][1].start))
        else:   # build up to the best one: fewest kills first
            self.reel_order = sorted(sel, key=lambda k: (sel[k][1].kills, rank.get(k[0], 0), sel[k][1].start))
        self._refresh_order_list()
        self._update_reel_summary()

    # -- timeline -------------------------------------------------------------
    def _refresh_timeline(self, v: Optional[VideoEntry], current: Optional[str] = None) -> None:
        if v is None or v.result is None:
            self.timeline.set_clips([])
            return
        if self._timeline_video != v.path:
            if v.path not in self._waves:
                try:
                    self._waves[v.path] = waveform(v.path)
                except Exception:
                    self._waves[v.path] = np.zeros(0, np.float32)
            self.timeline.set_video(v.duration or v.result.duration, self._waves[v.path],
                                    [(k.time, k.count) for k in v.result.kills], v.result.deaths)
            self._timeline_video = v.path
        clips = []
        for c in v.clips(self._show_singles()):
            a, b = self._effective_range(v, c)
            clips.append(TimelineClip(c.key, a, b, [k.time for k in c.events], c.title, v.is_selected(c),
                                      c.start, min(v.duration or 1e9, c.end + 0.2)))
        self.timeline.set_clips(clips, current)

    def _on_timeline_select(self, key: str) -> None:
        v = next((x for x in self.videos if x.path == self._timeline_video), None)
        if v is None:
            return
        vi = self.videos.index(v)
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).data(0, Qt.UserRole) == (vi, key):
                    self.tree.setCurrentItem(top.child(j))
                    return

    def _on_timeline_trim(self, key: str, a: float, b: float) -> None:
        self._on_timeline_select(key)
        self._set_trim(a, b)

    def _on_timeline_seek(self, t: float) -> None:
        v, c = self._current_clip()
        if v is None or v.path != self._timeline_video:
            return
        a, b = self._effective_range(v, c)
        if a <= t <= b:
            self.preview.seek(t - a, play=True)

    def _tick_playhead(self) -> None:
        if self.preview.path and self.preview.path == self._timeline_video:
            self.timeline.set_playhead(self.preview.start + self.preview.pos)

    # -- trimming ------------------------------------------------------------
    def _effective_range(self, v: VideoEntry, c: Clip):
        seg = self._segments(only=(v, c))
        return (seg[0].start, seg[0].end) if seg else (c.start, c.end)

    def _show_trim(self, v: VideoEntry, c: Clip) -> None:
        a, b = self._effective_range(v, c)
        for sp, val in ((self.sp_trim_a, a), (self.sp_trim_b, b)):
            sp.blockSignals(True)
            sp.setRange(0, v.duration or 1e6)
            sp.setValue(val)
            sp.blockSignals(False)
        self.btn_untrim.setEnabled(c.key in v.trims)
        self.trim_box.setEnabled(True)

    def _set_trim(self, a: float, b: float) -> None:
        v, c = self._current_clip()
        if not c:
            return
        a = max(0.0, min(a, c.start))            # keep every kill of the clip inside it
        b = max(b, c.end + 0.2)
        if v.duration:
            b = min(b, v.duration)
        v.trims[c.key] = (round(a, 2), round(b, 2))
        self._show_trim(v, c)
        self._refresh_timeline(v, c.key)
        self.preview.load(v.path, a, b, autoplay=False)
        self._mark_tree_item(v, c)
        self._update_reel_summary()

    def _mark_tree_item(self, v: VideoEntry, c: Clip) -> None:
        it = self.tree.currentItem()
        if it is not None:
            self.tree.blockSignals(True)
            it.setText(0, c.title + ("  ✂" if c.key in v.trims else ""))
            self.tree.blockSignals(False)

    def _on_trim_spin(self, *_):
        self._set_trim(self.sp_trim_a.value(), self.sp_trim_b.value())

    def _trim_at_playhead(self, start: bool) -> None:
        v, c = self._current_clip()
        if not c or not self.preview.path:
            return
        t = self.preview.start + self.preview.pos
        a, b = self._effective_range(v, c)
        self._set_trim(t, b) if start else self._set_trim(a, t)

    def _reset_trim(self) -> None:
        v, c = self._current_clip()
        if c and v.trims.pop(c.key, None) is not None:
            self._show_trim(v, c)
            self._refresh_timeline(v, c.key)
            a, b = self._effective_range(v, c)
            self.preview.load(v.path, a, b, autoplay=False)
            self._mark_tree_item(v, c)
            self._update_reel_summary()

    def save_thumbnail(self, out: str) -> str:
        """Thumbnail from the best (highest-hype) ticked clip."""
        from .reel import make_thumbnail
        best = max(self._selected_clips(), key=lambda vc: self._hype(*vc)[0], default=None)
        if best is None:
            raise RuntimeError("nothing selected")
        v, c = best
        k = max(c.events, key=lambda e: (e.count, e.time)) if c.events else None
        t = (k.time if k else c.start) + 0.5
        text = f"{c.kills} KILL STREAK" if c.kind == "streak" else c.title
        why = self._hype(v, c)[1]
        special = next((w for w in why if w in ("last squad member alive", "squad wipe", "tank buster")), "")
        make_thumbnail(v.path, t, out, text, special, self.callout_style)
        self.status.setText(self.status.text() + f" · thumbnail {os.path.basename(out)}")
        return out

    def _user_templates(self) -> dict:
        import json as _json
        try:
            return _json.loads(self.settings.value("user_templates", "") or "{}")
        except ValueError:
            return {}

    def edit_effects(self) -> None:
        import json as _json
        dlg = EffectsDialog(self, self.effects, self._user_templates())
        if not dlg.exec():
            return
        self.effects = dlg.values()
        self.settings.setValue("effects", _json.dumps(self.effects))
        key = dlg.template_applied or ""
        ut = dict(dlg.user_templates)
        if key.startswith("save:"):           # remember the main-window extras with the template
            t = ut[key[5:]]
            t.update(overlays=self.cb_overlays.isChecked(), replay=self.cb_replay.isChecked(),
                     beat_sync=self.cb_beat.isChecked(), callout=_json.loads(self.callout_style.to_json()),
                     preset=self.cb_preset.currentText())
        self.settings.setValue("user_templates", _json.dumps(ut))
        if key and not key.startswith("save:"):
            self.apply_template(key)

    def apply_template(self, key: str) -> None:
        """Set the main-window extras (call-outs, replay, beat sync, call-out style, export) from a template."""
        from .reel import TEMPLATES
        from .reel import template_values
        if key.startswith("user:"):
            t = self._user_templates().get(key[5:], {})
        else:
            t = template_values(key) if key in TEMPLATES else {}
        for k, cb in (("overlays", self.cb_overlays), ("replay", self.cb_replay), ("beat_sync", self.cb_beat)):
            if k in t:
                cb.setChecked(bool(t[k]))
        if "callout" in t:
            st = CalloutStyle.from_json(self.callout_style.to_json())
            for k, val in t["callout"].items():
                setattr(st, k, tuple(val) if isinstance(val, list) else val)
            self.callout_style = st
            self.settings.setValue("callout_style", st.to_json())
        if t.get("vertical"):
            self.cb_preset.setCurrentText("Vertical 9:16 (TikTok / Shorts / Reels)")
        elif t.get("preset"):
            self.cb_preset.setCurrentText(t["preset"])
        for k in EFFECT_DEFAULTS:
            if k in t:
                self.effects[k] = t[k]
        self.effects["template"] = key
        import json as _json
        self.settings.setValue("effects", _json.dumps(self.effects))
        self.status.setText(f"Applied the “{key.replace('user:', '')}” look.")

    def edit_callout_style(self) -> None:
        frame = None
        v, c = self._current_clip()
        if v is not None and c is not None:
            try:
                import cv2
                img = grab_frame(v.path, c.events[-1].time + 0.6 if c.events else c.start)
                rgb = cv2.cvtColor(cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
                frame = QImage(rgb.data, 1280, 720, 1280 * 3, QImage.Format_RGB888).copy()
            except Exception:
                frame = None
        dlg = CalloutDialog(self, self.callout_style, frame)
        if dlg.exec():
            self.callout_style = dlg.result_style()
            self.settings.setValue("callout_style", self.callout_style.to_json())
            if not self.cb_overlays.isChecked():
                self.cb_overlays.setChecked(True)

    # -- reel options -------------------------------------------------------
    def _on_preset(self, name: str) -> None:
        custom = name == "Custom"
        self.cb_res.setEnabled(custom)
        self.cb_fps.setEnabled(custom)
        self.settings.setValue("reel_preset", name)

    def _choose_music(self) -> None:
        start = os.path.dirname(self.ed_music.text()) or os.path.expanduser("~")
        f, _ = QFileDialog.getOpenFileName(self, "Choose music", start,
                                           "Audio (*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus);;All files (*)")
        if f:
            self.ed_music.setText(f)

    def reel_options(self) -> ReelOptions:
        name = self.cb_preset.currentText()
        o = apply_preset(name)
        if name == "Custom":
            o.size, o.fps = self._reel_size(), self._reel_fps()
        o.music = self.ed_music.text() or None
        o.music_volume = self.sp_music_vol.value() / 100
        o.duck, o.beat_sync = self.cb_duck.isChecked(), self.cb_beat.isChecked()
        o.overlays, o.titles, o.replay = self.cb_overlays.isChecked(), self.cb_titles.isChecked(), \
            self.cb_replay.isChecked()
        o.encoder = self._encoders[self.cb_encoder.currentIndex()][1]
        o.callout = self.callout_style
        for k, val in self.effects.items():
            if hasattr(o, k):
                setattr(o, k, val)
        self.settings.setValue("callout_style", self.callout_style.to_json())
        for key, val in (("music", self.ed_music.text()), ("music_volume", self.sp_music_vol.value()),
                         ("duck", o.duck), ("beat_sync", o.beat_sync), ("overlays", o.overlays),
                         ("titles", o.titles), ("replay", o.replay), ("encoder", self.cb_encoder.currentIndex())):
            self.settings.setValue(key, ("true" if val else "false") if isinstance(val, bool) else val)
        return o

    def _update_reel_summary(self) -> None:
        self._refresh_order_list()
        self.settings.setValue("pad_before", self.sp_pre.value())
        self.settings.setValue("pad_after", self.sp_post.value())
        sel = self._selected_clips()
        n_streaks = sum(1 for _, c in sel if c.kind == "streak")
        n_singles = len(sel) - n_streaks
        kills = sum(c.kills for _, c in sel)
        src = sum(v.duration for v in self.videos if v.result)
        if not sel:
            self.lbl_reel.setText("Nothing selected")
        else:
            total = sum(g.duration for g in self._segments())
            parts = [f"{n_streaks} streak(s)"] + ([f"{n_singles} other kill clip(s)"] if n_singles else [])
            self.lbl_reel.setText(f"{', '.join(parts)}, {kills} kills → {fmt_time(total)} of video "
                                  f"(from {fmt_time(src)} of footage)")
        self._refresh_buttons()

    def preview_streak(self) -> None:
        """Full-quality preview (with sound) in the system video player."""
        v, c = self._current_clip()
        if not c:
            return
        if self.preview.playing:
            self.preview.toggle()   # pause the in-app preview while the player opens
        seg = self._segments(only=(v, c))[0]
        out = os.path.join(tempfile.gettempdir(),
                           f"bf6kt_preview_{abs(hash((v.path, seg.start, seg.end))) % 10**8}.mp4")
        self.status.setText("Cutting preview…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if not os.path.exists(out):
                cut_clip(v.path, seg.start, seg.end, out)
            open_with_system(out)
            self.status.setText(f"Opened {c.title.lower()} in your video player.")
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Couldn't make the preview:\n{exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _reel_size(self):
        return {1: (1920, 1080), 2: (2560, 1440), 3: (1280, 720)}.get(self.cb_res.currentIndex())

    def _reel_fps(self):
        return {1: 60.0, 2: 30.0}.get(self.cb_fps.currentIndex())

    def build_reel_dialog(self) -> None:
        segs = self._segments()
        if not segs:
            return
        start = os.path.join(self.settings.value("reel_dir", os.path.dirname(self.videos[0].path)),
                             "kill-streaks.mp4")
        gif = self.cb_preset.currentText().startswith("GIF")
        ext = ".gif" if gif else ".mp4"
        start = os.path.splitext(start)[0] + ext
        out, _ = QFileDialog.getSaveFileName(self, "Save highlight video", start,
                                             "GIF (*.gif)" if gif else "MP4 video (*.mp4)")
        if not out:
            return
        if not out.lower().endswith(ext):
            out += ext
        self.settings.setValue("reel_dir", os.path.dirname(out))
        self._last_reel_target = out
        self.start_reel(out)

    def start_reel(self, out: str) -> None:
        segs = self._segments()
        w = ReelWorker(segs, out, self.reel_options())
        self._reel_worker = w
        self._reel_started = time.time()

        def wire(worker):
            worker.progress.connect(self._on_reel_progress)
            worker.done.connect(self._on_reel_done)
            worker.failed.connect(self._on_reel_failed)

        self._run_in_thread(w, wire)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.status.setText(f"Building highlight video from {len(segs)} clip(s)…")
        self._refresh_buttons()

    @Slot(float, str)
    def _on_reel_progress(self, frac: float, msg: str) -> None:
        self.bar.setValue(int(frac * 100))
        self.status.setText(f"Building highlight video: {msg}")

    @Slot(str)
    def _on_reel_done(self, path: str) -> None:
        self._reel_worker = None
        self.bar.setVisible(False)
        self.status.setText(f"Saved {path} ({time.time() - self._reel_started:.0f}s)")
        self.settings.setValue("open_after", "true" if self.cb_open.isChecked() else "false")
        self._refresh_buttons()
        self.last_reel = path
        pend = getattr(self, "_pending_versions", None)
        if pend:
            n, spec, finale = pend.pop(0)
            if not pend:
                self._pending_versions = None
            self.apply_montage(self.montage_picks(spec), finale)
            QTimer.singleShot(200, lambda p=path, n=n: self.start_reel(os.path.splitext(p)[0] + f"_{n}s"
                                                                       + os.path.splitext(p)[1]))
        if self.effects.get("thumbnail") and not path.lower().endswith(".gif"):
            try:
                self.save_thumbnail(os.path.splitext(path)[0] + "_thumbnail.jpg")
            except Exception as exc:
                self.status.setText(self.status.text() + f" (thumbnail failed: {exc})")
        if self.cb_open.isChecked():
            open_with_system(path)

    @Slot(str)
    def _on_reel_failed(self, msg: str) -> None:
        self._reel_worker = None
        self.bar.setVisible(False)
        self._refresh_buttons()
        if msg == "Cancelled":
            self.status.setText("Highlight video cancelled.")
        else:
            self.status.setText("Highlight video failed.")
            QMessageBox.warning(self, APP_NAME, f"Building the video failed:\n{msg}")

    # -- watch folder --------------------------------------------------------
    def toggle_watch(self, on: bool) -> None:
        if not on:
            self.stop_watching()
            return
        start = self.settings.value("watch_dir", "") or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Watch this folder for new recordings", start)
        if d:
            self.start_watching(d)
        else:
            self.act_watch.setChecked(False)

    def start_watching(self, d: str) -> None:
        for old in self._watcher.directories():
            self._watcher.removePath(old)
        self._watcher.addPath(d)
        self.watch_dir = d
        # only recordings that appear from now on are added automatically
        self._watch_known = {os.path.join(d, f) for f in os.listdir(d)}
        self._watch_sizes = {}
        self.settings.setValue("watch_dir", d)
        self.settings.setValue("watching", "true")
        self.act_watch.setChecked(True)
        self.act_watch.setText(f"Watching {os.path.basename(d) or d}")
        self.status.setText(f"Watching {d} for new recordings.")

    def stop_watching(self) -> None:
        for old in self._watcher.directories():
            self._watcher.removePath(old)
        self.watch_dir = None
        self.settings.setValue("watching", "false")
        self.act_watch.setChecked(False)
        self.act_watch.setText("Watch folder…")

    def _check_watch_folder(self) -> None:
        d = getattr(self, "watch_dir", None)
        if not d or not os.path.isdir(d):
            return
        new, pending = [], False
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if p in self._watch_known or os.path.splitext(f)[1].lower() not in VIDEO_EXTS:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if self._watch_sizes.get(p) == size and size > 0:   # stopped growing: recording finished
                self._watch_known.add(p)
                new.append(p)
            else:
                self._watch_sizes[p] = size
                pending = True
        if pending:
            self._watch_timer.start()
        if new and self.add_paths(new) and not self._analyze_worker and not self._reel_worker:
            self.start_analysis()

    def _on_update(self, rel) -> None:
        self.update_available = rel
        btn = QPushButton(f"Update available: v{rel.version}")
        btn.setFlat(True)
        btn.setStyleSheet("color:#e67e22; font-weight:bold;")
        btn.clicked.connect(lambda: self._show_update(rel))
        self.statusBar().addPermanentWidget(btn)

    def _show_update(self, rel) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        msg = QMessageBox(self)
        msg.setWindowTitle(APP_NAME)
        msg.setText(f"Version {rel.version} is available (you have {__version__}).")
        msg.setInformativeText((rel.notes or "")[:800])
        open_btn = msg.addButton("Open download page", QMessageBox.AcceptRole)
        msg.addButton("Later", QMessageBox.RejectRole)
        msg.exec()
        if msg.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(rel.url))

    def review_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        cands = [self.videos[r] for r in rows if self.videos[r].result] or [v for v in self.videos if v.result]
        if not cands:
            QMessageBox.information(self, APP_NAME, "Scan a video first.")
            return
        v = cands[0]
        ReviewDialog(self, v).exec()
        from . import analyze
        v.result = analyze(v.path, use_cache=True)       # re-applies the corrections
        self._regroup(v)
        self._update_row(v.path)
        self._rebuild_tree()

    def export_markers_dialog(self) -> None:
        from .markers import FORMATS, export
        done = [v for v in self.videos if v.result]
        if not done:
            QMessageBox.information(self, APP_NAME, "Scan a video first.")
            return
        labels = {"DaVinci Resolve / Premiere (EDL)": "edl", "Final Cut Pro / Resolve (FCPXML)": "fcpxml",
                  "Premiere Pro (XML)": "premiere", "YouTube chapters (text)": "chapters", "Spreadsheet (CSV)": "csv"}
        from PySide6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(self, "Export markers", "Format:", list(labels), 0, False)
        if not ok:
            return
        fmt = labels[choice]
        d = QFileDialog.getExistingDirectory(self, "Save marker files to", os.path.dirname(done[0].path))
        if not d:
            return
        written = []
        for v in done:
            r = v.result
            r.streaks = v.streaks           # current streak rules
            info = probe(v.path)
            path = os.path.join(d, os.path.splitext(v.name)[0] + FORMATS[fmt])
            written.append(export(r, fmt, path, width=info.width, height=info.height))
        self.status.setText(f"Wrote {len(written)} marker file(s) to {d}")
        if fmt == "chapters" and len(written) == 1:
            with open(written[0], encoding="utf-8") as f:
                QApplication.clipboard().setText(f.read())
            self.status.setText(self.status.text() + " (chapters copied to the clipboard)")

    # -- library -------------------------------------------------------------
    def show_library(self) -> None:
        dlg = LibraryDialog(self, self.sp_gap.value(), self.sp_min.value())
        dlg.open_videos.connect(lambda paths: (self.add_paths(paths), self.start_analysis()))
        dlg.exec()

    # -- projects ------------------------------------------------------------
    def project_data(self, path: str) -> dict:
        vids = []
        for v in self.videos:
            vids.append(library.project_video_entry(
                v.path, path, selected=v.selected, trims={k: list(t) for k, t in v.trims.items()}))
        settings = {k: self.settings.value(k) for k in (
            "streak_min", "streak_gap", "streak_types", "show_singles", "pad_before", "pad_after", "reel_preset",
            "music", "music_volume", "duck", "beat_sync", "overlays", "titles", "replay", "encoder", "callout_style",
            "effects")}
        self.reel_options()   # make sure the latest UI values are in settings
        settings = {k: self.settings.value(k) for k in settings}
        return {"videos": vids, "order": [list(k) for k in self.reel_order], "settings": settings}

    def save_project_dialog(self) -> None:
        start = self._project_path or os.path.join(self.settings.value("reel_dir", os.path.expanduser("~")),
                                                   "highlights" + library.PROJECT_EXT)
        out, _ = QFileDialog.getSaveFileName(self, "Save project", start,
                                             f"BF6 Kill Tracker project (*{library.PROJECT_EXT})")
        if out:
            if not out.endswith(library.PROJECT_EXT):
                out += library.PROJECT_EXT
            self.save_project(out)

    def save_project(self, path: str) -> None:
        library.save_project(path, self.project_data(path))
        self._project_path = path
        self.setWindowTitle(f"{APP_NAME} {__version__} — {os.path.basename(path)}")
        self.status.setText(f"Saved project {path}")

    def open_project_dialog(self) -> None:
        start = os.path.dirname(self._project_path or "") or self.settings.value("reel_dir", os.path.expanduser("~"))
        f, _ = QFileDialog.getOpenFileName(self, "Open project", start,
                                           f"BF6 Kill Tracker project (*{library.PROJECT_EXT})")
        if f:
            try:
                self.open_project(f)
            except Exception as exc:
                QMessageBox.warning(self, APP_NAME, f"Couldn't open the project:\n{exc}")

    def open_project(self, path: str) -> None:
        data = library.load_project(path)
        if self._analyze_worker or self._reel_worker:
            raise RuntimeError("Wait for the current job to finish first")
        widgets = {"streak_min": self.sp_min, "streak_gap": self.sp_gap, "pad_before": self.sp_pre,
                   "pad_after": self.sp_post, "music_volume": self.sp_music_vol}
        st = data.get("settings", {})
        for k, w in widgets.items():
            if st.get(k) not in (None, ""):
                w.setValue(type(w.value())(float(st[k])))
        for k, cb in (("show_singles", self.cb_singles), ("duck", self.cb_duck), ("beat_sync", self.cb_beat),
                      ("overlays", self.cb_overlays), ("titles", self.cb_titles), ("replay", self.cb_replay)):
            if st.get(k) is not None:
                cb.setChecked(str(st[k]) == "true")
        if st.get("streak_types") is not None:
            self.cb_types.setCurrentIndex(int(st["streak_types"]))
        if st.get("encoder") is not None:
            self.cb_encoder.setCurrentIndex(int(st["encoder"]))
        if st.get("reel_preset"):
            self.cb_preset.setCurrentText(str(st["reel_preset"]))
        self.ed_music.setText(str(st.get("music") or ""))
        if st.get("callout_style"):
            self.callout_style = CalloutStyle.from_json(st["callout_style"])
        if st.get("effects"):
            import json as _json
            try:
                self.effects = {**EFFECT_DEFAULTS, **_json.loads(st["effects"])}
                self.settings.setValue("effects", st["effects"])
            except ValueError:
                pass
        self.videos = []
        missing = [v["path"] for v in data["videos"] if not os.path.isfile(v["path"])]
        self.add_paths([v["path"] for v in data["videos"] if os.path.isfile(v["path"])])
        by_path = {v.path: v for v in self.videos}
        for v in data["videos"]:
            e = by_path.get(os.path.abspath(v["path"]))
            if e:
                e.selected = {k: bool(x) for k, x in v.get("selected", {}).items()}
                e.trims = {k: tuple(t) for k, t in v.get("trims", {}).items()}
        self.reel_order = [tuple(k) for k in data.get("order", [])]
        self._project_path = path
        self.setWindowTitle(f"{APP_NAME} {__version__} — {os.path.basename(path)}")
        self._rebuild_tree()
        if missing:
            QMessageBox.information(self, APP_NAME, "These videos weren't found:\n" + "\n".join(missing))
        self.start_analysis()      # remembered scans load instantly

    # -- plumbing ----------------------------------------------------------
    def _run_in_thread(self, worker: "Worker", wire) -> None:
        th = QThread(self)
        worker.moveToThread(th)
        wire(worker)
        th.started.connect(worker.start_work)
        worker.finished.connect(th.quit)
        pair = (th, worker)
        self._threads.append(pair)
        th.finished.connect(lambda: self._threads.remove(pair) if pair in self._threads else None)
        th.start()

    def _refresh_buttons(self) -> None:
        busy = bool(self._analyze_worker or self._reel_worker)
        has_unscanned = any(v.result is None and v.status != "Can't open" for v in self.videos)
        self.act_analyze.setEnabled(not busy and has_unscanned)
        self.act_stop.setEnabled(busy)
        self.act_remove.setEnabled(not self._analyze_worker and bool(self.videos))
        _, c = self._current_clip()
        self.btn_preview.setEnabled(c is not None)
        self.btn_reel.setEnabled(not busy and bool(self._selected_clips()))

    def closeEvent(self, e):  # noqa: N802
        self.stop_work()
        self.preview.shutdown()
        for th, _ in list(self._threads):
            th.quit()
            th.wait(3000)
        super().closeEvent(e)


def main(argv: Optional[List[str]] = None) -> int:
    find_ffmpeg()  # fail early with a clear message if ffmpeg can't be found
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    w = MainWindow()
    if argv:
        w.add_paths(argv)
    w.show()
    if os.environ.get("BF6KT_GUI_SMOKE"):     # CI: open the window, then quit
        QTimer.singleShot(1500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
