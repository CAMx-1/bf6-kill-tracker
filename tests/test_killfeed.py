"""Kill-feed reader and fusion tests on synthetic feed images."""
import os

import cv2
import numpy as np

from bf6killtracker.detector import TEMPLATE_DIR
from bf6killtracker.events import KillEvent, find_streaks, fuse_feed, split_lives
from bf6killtracker.killfeed import FEED_ROI, FeedEvent, FeedReader, FeedRow, FeedTracker

GREEN = (166, 222, 188)
RED = (70, 90, 235)
MARKER = cv2.imread(os.path.join(TEMPLATE_DIR, "feed_marker.png"))
RIFLE = cv2.imread(os.path.join(TEMPLATE_DIR, "weapons", "gun_rifle.png"))
C4 = cv2.imread(os.path.join(TEMPLATE_DIR, "weapons", "explosive_c4.png"))


def feed_image(rows):
    """rows: list of (kind, y, weapon_img, headshot). Returns an ROI-sized BGR image."""
    _, _, w, h = FEED_ROI
    img = np.full((h, w, 3), 30, np.uint8)
    for kind, y, weapon, hs in rows:
        if kind == "kill":
            x = 400
            img[y:y + MARKER.shape[0], x:x + MARKER.shape[1]] = MARKER
            cv2.putText(img, "CAMx", (x + 20, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
            wx = x + 85
            wh, ww = weapon.shape[:2]
            img[y - 6:y - 6 + wh, wx:wx + ww] = weapon
            vx = wx + ww + 12
            if hs:
                cv2.circle(img, (vx + 14, y + 6), 13, RED, -1)
                vx += 40
            cv2.putText(img, "Victim1", (vx, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2)
        else:
            cv2.putText(img, "Enemy", (380, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2)
            x = 560
            img[y:y + MARKER.shape[0], x:x + MARKER.shape[1]] = MARKER
            cv2.putText(img, "CAMx", (x + 20, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
    return img


def test_reader_classifies_rows():
    fr = FeedReader()
    rows = fr.read(feed_image([("kill", 100, RIFLE, False), ("kill", 140, C4, False),
                               ("kill", 180, RIFLE, True), ("death", 220, None, False)]))
    assert [r.kind for r in rows] == ["kill", "kill", "kill", "death"]
    assert rows[0].weapon == "gun_rifle" and not rows[0].headshot
    assert rows[1].weapon == "explosive_c4"
    assert rows[2].headshot


def test_reader_ignores_other_players_rows():
    img = feed_image([])
    cv2.putText(img, "Someone", (300, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2)
    cv2.putText(img, "Victim", (500, 114), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2)
    assert FeedReader().read(img) == []


def test_tracker_one_event_per_row_while_scrolling():
    ft = FeedTracker(0.1)
    t = 0.0
    for _ in range(10):          # row A at the bottom
        ft.update(t, [FeedRow(2200, 226, "kill", False, "gun_rifle")]); t += 0.1
    for _ in range(10):          # row B arrives, A scrolls up
        ft.update(t, [FeedRow(2200, 186, "kill", False, "gun_rifle"), FeedRow(2260, 226, "kill", True, "gun_rifle")])
        t += 0.1
    for _ in range(5):           # both hidden (smoke)
        ft.update(t, []); t += 0.1
    for _ in range(5):           # back
        ft.update(t, [FeedRow(2200, 186, "kill", False, "gun_rifle"), FeedRow(2260, 226, "kill", True, "gun_rifle")])
        t += 0.1
    ev = ft.finish()
    assert [(round(e.time, 1), e.category) for e in ev] == [(0.0, "gun"), (1.0, "headshot")]


def test_fuse_types_and_missed_kills():
    skulls = [KillEvent(10.0), KillEvent(20.0, count=2)]
    feed = [FeedEvent(9.9, "kill", True, "gun_rifle"), FeedEvent(20.1, "kill", False, "explosive_c4"),
            FeedEvent(20.2, "kill", False, "explosive_c4"), FeedEvent(30.0, "kill", False, "gun_smg")]
    out = fuse_feed(skulls, feed)
    assert [(k.time, k.count, k.type_list(), k.evidence) for k in out] == [
        (10.0, 1, ["headshot"], ""), (20.0, 2, ["explosive", "explosive"], ""), (30.0, 1, ["gun"], "feed")]


def test_streaks_break_on_death_and_filter_by_type():
    kills = [KillEvent(1, types=["headshot"]), KillEvent(2, types=["gun"]), KillEvent(3, types=["headshot"]),
             KillEvent(4, types=["headshot"]), KillEvent(5, types=["headshot"])]
    assert [s.kills for s in find_streaks(kills, 5, 3)] == [5]
    assert [s.kills for s in find_streaks(kills, 5, 3, deaths=[2.5])] == [3]   # 3,4,5 after respawn
    assert [s.kills for s in find_streaks(kills, 5, 3, only=["headshot"])] == [4]
    lives = split_lives(kills, [2.5], 10)
    assert [(lf.kills, lf.died) for lf in lives] == [(2, True), (3, False)]
