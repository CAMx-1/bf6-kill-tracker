"""Lightweight in-app video preview.

Decodes just the selected time range with ffmpeg at thumbnail resolution
(no audio) and paints the frames into a QLabel. Starting a preview takes a
fraction of a second, even for 1440p/4K HEVC recordings, and needs no
multimedia codecs or extra Qt modules.
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStyle, QVBoxLayout, QWidget

from .video import NO_WINDOW, find_ffmpeg

PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 512, 288, 30


def _fmt(t: float) -> str:
    m, s = divmod(max(0, int(t)), 60)
    return f"{m}:{s:02d}"


class _Decoder(QThread):
    frame = Signal(int, QImage, float)   # generation, image, position (s, relative to clip start)
    ended = Signal(int)

    def __init__(self, gen: int, path: str, start: float, duration: float, offset: float,
                 hold_first: bool = False, parent=None):
        super().__init__(parent)
        # (not "self.start": that would shadow QThread.start)
        self.gen, self.path, self.clip_start, self.duration, self.offset = gen, path, start, duration, offset
        self.hold_first = hold_first   # show one frame, then wait paused
        self._stop = False
        self._play = threading.Event()
        self._play.set()

    def stop(self) -> None:
        self._stop = True
        self._play.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._play.clear()
        else:
            self._play.set()

    def run(self) -> None:
        w, h, fps = PREVIEW_W, PREVIEW_H, PREVIEW_FPS
        remaining = max(0.05, self.duration - self.offset)
        cmd = [find_ffmpeg(), "-v", "error", "-nostdin", "-threads", "0",
               "-ss", f"{self.clip_start + self.offset:.3f}", "-t", f"{remaining:.3f}", "-i", self.path,
               "-an", "-sn",
               "-vf", f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        size = w * h * 3
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=size * 2, **NO_WINDOW)
        try:
            i = 0
            clock = time.monotonic()
            assert proc.stdout is not None
            while not self._stop:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                if not self._play.is_set():          # paused: hold the clock
                    paused_at = time.monotonic()
                    self._play.wait()
                    clock += time.monotonic() - paused_at
                    if self._stop:
                        break
                due = clock + i / fps
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                img = QImage(buf, w, h, w * 3, QImage.Format_RGB888).copy()
                self.frame.emit(self.gen, img, self.offset + i / fps)
                i += 1
                if self.hold_first:
                    self.hold_first = False
                    self._play.clear()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if not self._stop:
            self.ended.emit(self.gen)


class PreviewPlayer(QWidget):
    """Plays ``[start, end]`` of a video inside the window, at preview quality."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.screen_lbl = QLabel("Select a streak or kill to preview it here")
        self.screen_lbl.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.screen_lbl.setAlignment(Qt.AlignCenter)
        self.screen_lbl.setStyleSheet("background:#111; color:#777; border-radius:4px;")
        self.screen_lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        st = self.style()
        self._icon_play = st.standardIcon(QStyle.SP_MediaPlay)
        self._icon_pause = st.standardIcon(QStyle.SP_MediaPause)
        self.btn = QPushButton(self._icon_play, "")
        self.btn.setFixedWidth(36)
        self.btn.clicked.connect(self.toggle)
        self.btn_restart = QPushButton(st.standardIcon(QStyle.SP_MediaSkipBackward), "")
        self.btn_restart.setFixedWidth(36)
        self.btn_restart.setToolTip("Play from the start")
        self.btn_restart.clicked.connect(lambda: self.seek(0.0, play=True))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderReleased.connect(lambda: self.seek(self.slider.value() / 1000 * self.duration, play=True))
        self.time_lbl = QLabel("0:00 / 0:00")
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.btn_restart)
        controls.addWidget(self.btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_lbl)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.screen_lbl)
        lay.addLayout(controls)

        self.path: Optional[str] = None
        self.start = self.duration = self.pos = 0.0
        self.playing = False
        self._gen = 0
        self._dec: Optional[_Decoder] = None
        self._old: list = []
        self._set_enabled(False)

    # -- public API ----------------------------------------------------------
    def load(self, path: str, start: float, end: float, autoplay: bool = True) -> None:
        self.path, self.start, self.duration = path, max(0.0, start), max(0.1, end - start)
        self.pos = 0.0
        self._set_enabled(True)
        self.seek(0.0, play=autoplay)

    def clear(self, message: str = "Select a streak or kill to preview it here") -> None:
        self._stop_decoder()
        self.path = None
        self.playing = False
        self.screen_lbl.clear()
        self.screen_lbl.setText(message)
        self._set_enabled(False)
        self._update_time()

    def toggle(self) -> None:
        if not self.path:
            return
        if self.playing:
            self.playing = False
            if self._dec:
                self._dec.set_paused(True)
        else:
            if self._dec is None or self.pos >= self.duration - 0.05:
                self.seek(0.0 if self.pos >= self.duration - 0.05 else self.pos, play=True)
                return
            self.playing = True
            self._dec.set_paused(False)
        self._update_button()

    def seek(self, pos: float, play: bool = True) -> None:
        if not self.path:
            return
        self.pos = min(max(0.0, pos), self.duration)
        self._start_decoder(self.pos, hold_first=not play)
        self.playing = play
        self._update_button()
        self._update_time()

    def shutdown(self) -> None:
        self._stop_decoder()
        for d in self._old:
            d.wait(2000)

    # -- internals ---------------------------------------------------------
    def _set_enabled(self, on: bool) -> None:
        for w in (self.btn, self.btn_restart, self.slider):
            w.setEnabled(on)

    def _stop_decoder(self) -> None:
        if self._dec:
            self._dec.stop()
            self._old.append(self._dec)
            self._dec = None
        self._old = [d for d in self._old if d.isRunning()]

    def _start_decoder(self, offset: float, hold_first: bool = False) -> None:
        self._stop_decoder()
        self._gen += 1
        d = _Decoder(self._gen, self.path, self.start, self.duration, offset, hold_first, self)
        d.frame.connect(self._on_frame)
        d.ended.connect(self._on_ended)
        self._dec = d
        d.start()

    def _on_frame(self, gen: int, img: QImage, pos: float) -> None:
        if gen != self._gen:
            return
        self.screen_lbl.setPixmap(QPixmap.fromImage(img))
        self.pos = pos
        self._update_time()

    def _on_ended(self, gen: int) -> None:
        if gen != self._gen:
            return
        self.playing = False
        self.pos = self.duration
        self._dec = None
        self._update_button()
        self._update_time()

    def _update_button(self) -> None:
        self.btn.setIcon(self._icon_pause if self.playing else self._icon_play)

    def _update_time(self) -> None:
        self.time_lbl.setText(f"{_fmt(self.pos)} / {_fmt(self.duration)}")
        if not self.slider.isSliderDown():
            self.slider.setValue(int(1000 * self.pos / self.duration) if self.duration else 0)
