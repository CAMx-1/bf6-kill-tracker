"""Unit tests that need no video file (run on every OS)."""
import os

import cv2
import numpy as np
import pytest

from bf6killtracker.detector import TEMPLATE_DIR, FrameObservation, HudLayout, SkullDetector
from bf6killtracker.events import KillEvent, KillTracker, TrackerConfig, find_streaks, max_kills_in_window

FPS = 60.0


# --------------------------------------------------------------------------
# synthetic observation timelines for the tracker
# --------------------------------------------------------------------------
class Timeline:
    """Build per-frame observations: slot scores, pop score."""

    def __init__(self, seconds: float, fps: float = FPS, slots: int = 5):
        self.n = int(seconds * fps)
        self.fps = fps
        self.s = np.full((self.n, slots), 0.3, np.float32)
        self.p = np.zeros(self.n, np.float32)

    def _r(self, a, b):
        return slice(int(a * self.fps), int(b * self.fps))

    def skull(self, slot, a, b, score=0.95):
        self.s[self._r(a, b), slot] = score
        return self

    def pop(self, a, b=None, score=0.8):
        self.p[self._r(a, b if b is not None else a + 0.3)] = score
        return self

    def run(self, cfg=None):
        tr = KillTracker(self.fps, cfg)
        for i in range(self.n):
            tr.update(i / self.fps, FrameObservation(self.s[i], float(self.p[i])))
        return tr.finish()


def kills_total(events):
    return sum(e.count for e in events)


def test_single_kill_with_pop():
    ev = Timeline(6).pop(1.0).skull(0, 1.3, 4.0).run()
    assert [(round(e.time, 1), e.count, e.evidence) for e in ev] == [(1.0, 1, "pop")]


def test_no_icons_no_kills():
    assert Timeline(5).run() == []


def test_pop_without_skull_is_not_a_kill():
    # e.g. "enemy spotted" / assist notifications pop a non-skull icon
    assert Timeline(5).pop(1.0).run() == []


def test_skull_without_pop_after_empty_hud_counts():
    ev = Timeline(6).skull(0, 2.0, 4.5).run()
    assert kills_total(ev) == 1 and ev[0].evidence == "appear"


def test_flicker_and_occlusion_are_not_new_kills():
    tl = Timeline(8).pop(1.0).skull(0, 1.3, 2.5).skull(0, 2.8, 4.0)  # 0.3 s hidden (smoke/flash)
    tl.skull(0, 4.1, 6.0)  # blink
    assert kills_total(tl.run()) == 1


def test_brief_false_match_is_ignored():
    assert Timeline(5).skull(0, 2.0, 2.1).run() == []


def test_kills_appending_skulls():
    tl = Timeline(10)
    tl.pop(1.0).skull(0, 1.3, 2.0)
    # second kill: pop in slot 0, first skull moves to slot 1
    tl.pop(2.0).skull(1, 2.0, 5.0).skull(0, 2.3, 5.5)
    # third kill
    tl.pop(3.0).skull(2, 3.0, 5.0).skull(1, 3.0, 5.0)
    tl.s[int(3.0 * FPS):int(3.3 * FPS), 0] = 0.3
    ev = tl.run()
    assert [e.count for e in ev] == [1, 1, 1]
    assert [round(e.time, 1) for e in ev] == [1.0, 2.0, 3.0]


def test_multikill_from_one_pop():
    # one notification that shows three skulls at once (e.g. explosive)
    ev = Timeline(6).pop(1.0).skull(0, 1.3, 4).skull(1, 1.35, 4).skull(2, 1.4, 4).run()
    assert [(e.count, e.evidence) for e in ev] == [(3, "pop")]


def test_replacement_counts_one():
    # new kill replaces the fading previous skull in slot 0 (no extra skull)
    tl = Timeline(8).pop(1.0).skull(0, 1.3, 3.0)
    tl.pop(3.0).skull(0, 3.3, 6.0)
    assert [e.count for e in tl.run()] == [1, 1]


@pytest.mark.parametrize("fps", [30.0, 60.0])
def test_frame_rate_independent(fps):
    tl = Timeline(10, fps=fps).pop(1.0).skull(0, 1.3, 3.0).pop(5.0).skull(0, 5.3, 8.0)
    assert kills_total(tl.run()) == 2


# --------------------------------------------------------------------------
# streaks
# --------------------------------------------------------------------------
def test_streak_grouping():
    kills = [KillEvent(t) for t in (1, 3, 6, 20, 21)] + [KillEvent(40, count=3)]
    streaks = find_streaks(kills, max_gap=5, min_kills=3)
    assert [(s.start, s.end, s.kills) for s in streaks] == [(1, 6, 3), (40, 40, 3)]
    assert [k.streak_id for k in sorted(kills, key=lambda k: k.time)] == [0, 0, 0, -1, -1, 1]


def test_max_kills_in_window():
    kills = [KillEvent(t) for t in (0, 10, 11, 12, 30)] + [KillEvent(13, count=2)]
    assert max_kills_in_window(kills, 5.0) == (5, 10)
    assert max_kills_in_window([], 5.0) == (0, 0.0)


# --------------------------------------------------------------------------
# detector on synthetic frames
# --------------------------------------------------------------------------
def _frame_with_icons(n_skulls: int, headshot: bool = False, pop: bool = False, bg_seed: int = 0):
    L = HudLayout()
    rng = np.random.default_rng(bg_seed)
    frame = cv2.GaussianBlur(rng.integers(20, 120, (L.ref_size[1], L.ref_size[0], 3), dtype=np.uint8), (9, 9), 0)
    sk = cv2.imread(os.path.join(TEMPLATE_DIR, "skull_headshot.png" if headshot else "skull.png"))
    tw, th = L.icon_size
    for k in range(n_skulls):
        x = L.slot_xs()[k] + 2
        frame[L.icon_y + 1:L.icon_y + 1 + th, x:x + tw] = sk
    if pop:
        px, py, pw, ph = L.pop_box
        frame[py:py + ph, px:px + pw] = (frame[py:py + ph, px:px + pw] * 0.4 + 150).astype(np.uint8)
    cv2.putText(frame, "200", (L.score_box[0] + 5, L.score_box[1] + 40), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (255, 255, 255), 3)
    return frame


def _observe(frame):
    det = SkullDetector()
    x, y, w, h = det.roi
    return det.observe(frame[y:y + h, x:x + w])


@pytest.mark.parametrize("n", [0, 1, 3])
def test_detector_counts_skulls(n):
    obs = _observe(_frame_with_icons(n))
    on = TrackerConfig().on_threshold
    assert [bool(s >= on) for s in obs.slot_scores] == [k < n for k in range(5)]


def test_detector_headshot_skull():
    obs = _observe(_frame_with_icons(1, headshot=True))
    assert obs.slot_scores[0] >= TrackerConfig().on_threshold


def test_detector_pop_square():
    assert _observe(_frame_with_icons(0, pop=True)).pop_score >= TrackerConfig().pop_threshold
    assert _observe(_frame_with_icons(0)).pop_score < TrackerConfig().pop_threshold


def test_detector_rejects_yellow_icon():
    frame = _frame_with_icons(1)
    L = HudLayout()
    x = L.slot_xs()[0] + 2
    region = frame[L.icon_y + 1:L.icon_y + 1 + L.icon_size[1], x:x + L.icon_size[0]]
    bright = region[:, :, 2] > 150
    region[bright] = (40, 210, 230)  # BGR yellow
    assert _observe(frame).slot_scores[0] < TrackerConfig().on_threshold


def test_layout_roi_contains_everything():
    L = HudLayout()
    x, y, w, h = L.roi()
    for sx in L.slot_xs():
        assert x <= sx - L.tol and sx + L.icon_size[0] + L.tol <= x + w
    assert y <= L.icon_y - L.tol and L.icon_y + L.icon_size[1] + L.tol <= y + h
    s = L.scaled(0.9)
    assert s.slot_pitch < L.slot_pitch


def test_detector_rejects_assist_icon():
    # the damage-assist icon (small skull over a bone) is not a kill
    frame = _frame_with_icons(0)
    L = HudLayout()
    x = L.slot_xs()[0] + 2
    frame[L.icon_y + 1:L.icon_y + 1 + L.icon_size[1], x:x + L.icon_size[0]] = \
        cv2.imread(os.path.join(TEMPLATE_DIR, "assist.png"))
    assert _observe(frame).slot_scores[0] < TrackerConfig().on_threshold
