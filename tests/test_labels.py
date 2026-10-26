import os

import cv2
import numpy as np

from bf6killtracker.labels import LABEL_DIR, LabelReader, LabelTracker, group_of, summarize


def crops_with(reader, placements):
    """placements: [(template file, group, x offset, y offset)] -> {group: BGR crop}"""
    rng = np.random.default_rng(0)
    out = {}
    for g, (x, y, w, h) in reader.rois.items():
        out[g] = cv2.GaussianBlur(rng.integers(30, 90, (h, w, 3), dtype=np.uint8), (5, 5), 0)
    for f, g, dx, dy in placements:
        t = cv2.imread(os.path.join(LABEL_DIR, f), cv2.IMREAD_GRAYSCALE)
        out[g][dy:dy + t.shape[0], dx:dx + t.shape[1]] = cv2.cvtColor(t, cv2.COLOR_GRAY2BGR)
    return out


def test_reader_and_tracker():
    lr = LabelReader()
    gs = group_of(lr)
    tr = LabelTracker()
    seq = [[("action__enemy_spot.png", "action", 300, 40)]] * 4 + [[]] * 8 + \
          [[("action__enemy_spot.png", "action", 300, 40), ("medal__tank_buster.png", "medal", 100, 20)]] * 3 + \
          [[("action__enemy_spot_x3.png", "action", 280, 40), ("banner__last_squad_member_alive.png", "banner", 150, 30)]] * 3
    for i, pl in enumerate(seq):
        tr.update(i * 0.2, lr.read(crops_with(lr, pl)), gs)
    ev = tr.finish()
    assert [(e.kind, e.count) for e in ev] == [("enemy_spot", 1), ("enemy_spot", 3), ("tank_buster", 1),
                                               ("last_squad_member_alive", 1)]
    assert summarize(ev)["enemy_spot"] == 4
    assert ev[-1].group == "banner" and ev[-1].label == "LAST SQUAD MEMBER ALIVE"


def test_empty_frames_find_nothing():
    lr = LabelReader()
    assert lr.read(crops_with(lr, [])) == {}
