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

from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStatusBar, QStyle, QTableWidget, QTableWidgetItem, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .analyzer import AnalysisResult, Cancelled, analyze
from .cli import VIDEO_EXTS, fmt_time
from .events import Streak, find_streaks
from .reel import ReelCancelled, Segment, build_reel, plan_segments
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
class VideoEntry:
    path: str
    duration: float = 0.0
    status: str = "Waiting"
    progress: float = 0.0
    result: Optional[AnalysisResult] = None
    streaks: List[Streak] = field(default_factory=list)
    error: str = ""
    # streak index -> included in the reel
    selected: Dict[int, bool] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


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

                res = analyze(path, progress=cb)
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

    def __init__(self, segments: List[Segment], out_path: str, size, fps):
        super().__init__()
        self.segments, self.out_path, self.size, self.fps = segments, out_path, size, fps
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            build_reel(self.segments, self.out_path, size=self.size, fps=self.fps,
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


# ----------------------------------------------------------------------------
# main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    COLS = ["Video", "Length", "Status", "Kills", "Streaks"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self.settings = QSettings("bf6killtracker", "gui")
        self.videos: List[VideoEntry] = []
        self._threads: list = []  # (QThread, Worker) pairs kept alive while running
        self._analyze_worker: Optional[AnalyzeWorker] = None
        self._reel_worker: Optional[ReelWorker] = None
        self._thumb_req = 0
        self._build_ui()
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
        for a in (self.act_add, self.act_add_dir, self.act_remove):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_analyze)
        tb.addAction(self.act_stop)

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
        self.sp_min.valueChanged.connect(self._regroup_all)
        self.sp_gap.valueChanged.connect(self._regroup_all)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("<b>Videos</b>"))
        lv.addWidget(self.table, 1)
        lv.addWidget(self.empty_hint)
        lv.addWidget(opts)

        # right: streak tree + thumbnails + reel
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Streak", "Kills", "Time", "Length"])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._on_tree_check)
        self.tree.currentItemChanged.connect(self._on_streak_selected)
        self.tree.itemDoubleClicked.connect(lambda *_: self.preview_streak())

        sel_row = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._set_all_checks(True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._set_all_checks(False))
        self.btn_preview = QPushButton(self.style().standardIcon(QStyle.SP_MediaPlay), "Preview streak")
        self.btn_preview.clicked.connect(self.preview_streak)
        sel_row.addWidget(b_all)
        sel_row.addWidget(b_none)
        sel_row.addStretch(1)
        sel_row.addWidget(self.btn_preview)

        self.thumb_box = QWidget()
        self.thumb_layout = QHBoxLayout(self.thumb_box)
        self.thumb_layout.setContentsMargins(4, 4, 4, 4)
        self.thumb_layout.addStretch(1)
        thumbs = QScrollArea()
        thumbs.setWidget(self.thumb_box)
        thumbs.setWidgetResizable(True)
        thumbs.setFixedHeight(230)
        self.thumb_title = QLabel("Select a streak to see its kills")

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
        self.cb_res = QComboBox()
        self.cb_res.addItems(["Same as first video", "1080p (1920×1080)", "1440p (2560×1440)", "720p (1280×720)"])
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["Same as first video", "60 fps", "30 fps"])
        self.cb_open = QCheckBox("Open the video when it’s done")
        self.cb_open.setChecked(self.settings.value("open_after", "true") == "true")
        self.lbl_reel = QLabel("No streaks selected")
        self.btn_reel = QPushButton(self.style().standardIcon(QStyle.SP_DialogSaveButton), "Build highlight video…")
        self.btn_reel.setMinimumHeight(34)
        self.btn_reel.clicked.connect(self.build_reel_dialog)
        rf.addRow("Lead-in", self.sp_pre)
        rf.addRow("Tail", self.sp_post)
        rf.addRow("Resolution", self.cb_res)
        rf.addRow("Frame rate", self.cb_fps)
        rf.addRow("", self.cb_open)
        rf.addRow(self.lbl_reel)
        rf.addRow(self.btn_reel)
        self.sp_pre.valueChanged.connect(self._update_reel_summary)
        self.sp_post.valueChanged.connect(self._update_reel_summary)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("<b>Kill streaks</b>  (ticked streaks go into the highlight video)"))
        rv.addWidget(self.tree, 1)
        rv.addLayout(sel_row)
        rv.addWidget(self.thumb_title)
        rv.addWidget(thumbs)
        rv.addWidget(reel)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([480, 800])
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
        kills = str(v.result.total_kills) if v.result else ""
        streaks = str(len(v.streaks)) if v.result else ""
        return [v.name, fmt_time(v.duration) if v.duration else "?", status, kills, streaks]

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
        if not v.result:
            return
        old = {(s.start, s.end): v.selected.get(s.id, True) for s in v.streaks}
        v.streaks = find_streaks(v.result.kills, self.sp_gap.value(), self.sp_min.value())
        v.selected = {s.id: old.get((s.start, s.end), True) for s in v.streaks}

    def _regroup_all(self) -> None:
        self.settings.setValue("streak_min", self.sp_min.value())
        self.settings.setValue("streak_gap", self.sp_gap.value())
        for v in self.videos:
            self._regroup(v)
        self._rebuild_table()
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for vi, v in enumerate(self.videos):
            if not v.result:
                continue
            top = QTreeWidgetItem([v.name, str(sum(s.kills for s in v.streaks)), "", ""])
            top.setData(0, Qt.UserRole, (vi, -1))
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            top.setFirstColumnSpanned(False)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            if not v.streaks:
                top.setText(0, f"{v.name}  — no streaks ({v.result.total_kills} kills)")
            self.tree.addTopLevelItem(top)
            for s in v.streaks:
                child = QTreeWidgetItem([f"Streak {s.id + 1}", str(s.kills),
                                         f"{fmt_time(s.start)} – {fmt_time(s.end)}",
                                         f"{s.duration:.1f}s" if s.duration >= 0.5 else "at once"])
                child.setData(0, Qt.UserRole, (vi, s.id))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if v.selected.get(s.id, True) else Qt.Unchecked)
                child.setTextAlignment(1, Qt.AlignCenter)
                top.addChild(child)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._update_reel_summary()

    def _on_tree_check(self, item: QTreeWidgetItem, col: int) -> None:
        data = item.data(0, Qt.UserRole)
        if not data or col != 0:
            return
        vi, sid = data
        if sid >= 0:
            self.videos[vi].selected[sid] = item.checkState(0) == Qt.Checked
            self._update_reel_summary()

    def _set_all_checks(self, on: bool) -> None:
        for v in self.videos:
            for s in v.streaks:
                v.selected[s.id] = on
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

    def _current_streak(self):
        it = self.tree.currentItem()
        if not it or not it.data(0, Qt.UserRole):
            return None, None
        vi, sid = it.data(0, Qt.UserRole)
        if sid < 0:
            return None, None
        v = self.videos[vi]
        s = next((x for x in v.streaks if x.id == sid), None)
        return v, s

    # -- thumbnails --------------------------------------------------------
    def _clear_thumbs(self) -> None:
        while self.thumb_layout.count() > 1:
            w = self.thumb_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def _on_streak_selected(self, *_):
        v, s = self._current_streak()
        self._clear_thumbs()
        self._refresh_buttons()
        if not s:
            self.thumb_title.setText("Select a streak to see its kills")
            return
        kills = [k for k in v.result.kills if s.start <= k.time <= s.end]
        span = f"in {s.duration:.1f}s" if s.duration >= 0.5 else "at once"
        self.thumb_title.setText(f"{v.name} — streak {s.id + 1}: {s.kills} kills {span} "
                                 f"(double-click or Preview to watch)")
        self._thumb_req += 1
        self._thumb_slots: Dict[float, QLabel] = {}
        for k in kills:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            img = QLabel("loading…")
            img.setFixedSize(320, 180)
            img.setAlignment(Qt.AlignCenter)
            img.setStyleSheet("background:#222; color:#888; border-radius:4px;")
            cap = QLabel(f"{fmt_time(k.time)}" + (f"  ×{k.count}" if k.count > 1 else ""))
            cap.setAlignment(Qt.AlignCenter)
            bl.addWidget(img)
            bl.addWidget(cap)
            self.thumb_layout.insertWidget(self.thumb_layout.count() - 1, box)
            self._thumb_slots[k.time] = img
        self._run_in_thread(ThumbWorker(self._thumb_req, v.path, [k.time for k in kills]),
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
    def _segments(self, only=None) -> List[Segment]:
        items = []
        for v in self.videos:
            for s in v.streaks:
                if only is not None:
                    if (v, s) != only:
                        continue
                elif not v.selected.get(s.id, True):
                    continue
                items.append((v.path, s.start, s.end, f"{v.name} streak {s.id + 1} ({s.kills} kills)"))
        return plan_segments(items, {v.path: v.duration for v in self.videos},
                             self.sp_pre.value(), self.sp_post.value(), order=[v.path for v in self.videos])

    def _update_reel_summary(self) -> None:
        self.settings.setValue("pad_before", self.sp_pre.value())
        self.settings.setValue("pad_after", self.sp_post.value())
        segs = self._segments()
        n = sum(1 for v in self.videos for s in v.streaks if v.selected.get(s.id, True))
        kills = sum(s.kills for v in self.videos for s in v.streaks if v.selected.get(s.id, True))
        src = sum(v.duration for v in self.videos if v.result)
        if not n:
            self.lbl_reel.setText("No streaks selected")
        else:
            total = sum(s.duration for s in segs)
            self.lbl_reel.setText(f"{n} streak(s), {kills} kills → {fmt_time(total)} of video "
                                  f"(from {fmt_time(src)} of footage)")
        self._refresh_buttons()

    def preview_streak(self) -> None:
        v, s = self._current_streak()
        if not s:
            return
        seg = self._segments(only=(v, s))[0]
        out = os.path.join(tempfile.gettempdir(), f"bf6kt_preview_{abs(hash((v.path, s.start))) % 10**8}.mp4")
        self.status.setText("Cutting preview…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if not os.path.exists(out):
                cut_clip(v.path, seg.start, seg.end, out)
            open_with_system(out)
            self.status.setText(f"Opened preview of streak {s.id + 1}.")
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
        out, _ = QFileDialog.getSaveFileName(self, "Save highlight video", start, "MP4 video (*.mp4)")
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        self.settings.setValue("reel_dir", os.path.dirname(out))
        self.start_reel(out)

    def start_reel(self, out: str) -> None:
        segs = self._segments()
        w = ReelWorker(segs, out, self._reel_size(), self._reel_fps())
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
        _, s = self._current_streak()
        self.btn_preview.setEnabled(s is not None)
        any_sel = any(v.selected.get(s.id, True) for v in self.videos for s in v.streaks)
        self.btn_reel.setEnabled(not busy and any_sel)

    def closeEvent(self, e):  # noqa: N802
        self.stop_work()
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
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
