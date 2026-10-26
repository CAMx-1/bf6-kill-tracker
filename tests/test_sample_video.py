"""End-to-end test on a real recording.

Set BF6KT_SAMPLE to the path of the sample clip (quick-19-killstreak.mp4) to
run it; it is skipped otherwise. Expected values were checked by hand,
frame by frame, against the on-screen skulls and the kill feed.
"""
import os

import pytest

from bf6killtracker import analyze

SAMPLE = os.environ.get("BF6KT_SAMPLE", os.path.expanduser("~/Downloads/quick-19-killstreak.mp4"))

# (approx. time the kill notification pops, number of kills in it)
GROUND_TRUTH = [
    (8.07, 1), (13.65, 1), (16.78, 1), (19.42, 1), (30.28, 1), (39.6, 1),
    (44.35, 2), (52.03, 3), (61.82, 1), (62.55, 1), (64.0, 1), (71.9, 1),
    (77.95, 2), (83.3, 1),
]

pytestmark = pytest.mark.skipif(not os.path.isfile(SAMPLE), reason="sample video not available")


def match(events, tol=0.8):
    used, pairs = set(), []
    for e in events:
        cands = [j for j, (gt, _) in enumerate(GROUND_TRUTH) if j not in used and abs(gt - e.time) <= tol]
        if cands:
            used.add(cands[0])
            pairs.append((e, GROUND_TRUTH[cands[0]]))
    return pairs


@pytest.mark.parametrize("fps", [None, 30.0])
def test_sample_video(fps):
    r = analyze(SAMPLE, sample_fps=fps)
    pairs = match(r.kills)
    assert r.total_kills == sum(c for _, c in GROUND_TRUTH) == 18
    assert len(pairs) == len(GROUND_TRUTH) == len(r.kills)       # every event lines up with a real kill
    assert all(e.count == c for e, (_, c) in pairs)               # multi-kills counted right
    assert [s.kills for s in r.streaks] == [3, 3, 3, 3]
    assert r.best_window_kills == 5
