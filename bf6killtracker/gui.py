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
        self.act_library = QAction(st.standardIcon(QStyle.SP_FileDialogDetailedView), "Library && stats", self)
        self.act_library.triggered.connect(self.show_library)
        for a in (self.act_add, self.act_add_dir, self.act_remove):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_analyze)
        tb.addAction(self.act_stop)
        tb.addSeparator()
        for a in (self.act_open, self.act_save, self.act_watch, self.act_library):
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
        self.tree.setHeaderLabels(["Clip", "Kills", "Types", "Time", "Length"])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
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
        return (v.path, start, end, f"{v.name} {c.title.lower()} ({c.kills} kills)", kills, title)

    def _ordered_selection(self):
        sel = {(v.path, c.key): (v, c) for v, c in self._selected_clips()}
        self._sync_order(sel)
        return [sel[k] for k in self.reel_order if k in sel]

    def _segments(self, only=None) -> List[Segment]:
        pairs = [only] if only is not None else self._ordered_selection()
        return plan_segments([self._item(v, c) for v, c in pairs], {v.path: v.duration for v in self.videos},
                             self.sp_pre.value(), self.sp_post.value(), keep_order=True)

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
            a, b = self._effective_range(v, c)
            self.preview.load(v.path, a, b, autoplay=False)
            self._mark_tree_item(v, c)
            self._update_reel_summary()

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
            "music", "music_volume", "duck", "beat_sync", "overlays", "titles", "replay", "encoder", "callout_style")}
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
