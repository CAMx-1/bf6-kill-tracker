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

needs_sample = pytest.mark.skipif(not os.path.isfile(SAMPLE), reason="sample video not available")


def match(events, tol=0.8):
    used, pairs = set(), []
    for e in events:
        cands = [j for j, (gt, _) in enumerate(GROUND_TRUTH) if j not in used and abs(gt - e.time) <= tol]
        if cands:
            used.add(cands[0])
            pairs.append((e, GROUND_TRUTH[cands[0]]))
    return pairs


@needs_sample
@pytest.mark.parametrize("fps", [None, 30.0])
def test_sample_video(fps):
    r = analyze(SAMPLE, sample_fps=fps)
    pairs = match(r.kills)
    assert r.total_kills == sum(c for _, c in GROUND_TRUTH) == 18
    assert len(pairs) == len(GROUND_TRUTH) == len(r.kills)       # every event lines up with a real kill
    assert all(e.count == c for e, (_, c) in pairs)               # multi-kills counted right
    assert [s.kills for s in r.streaks] == [3, 3, 3, 3]
    assert r.best_window_kills == 5
    if fps is None:
        assert [(b.score, b.kills, b.deaths, b.assists) for b in r.scoreboards] == [(14755, 66, 20, 31)]
        assert len(r.down) == 1 and r.down[0].duration > 5


# Further clips checked by hand against the skulls and the kill feed (CAMx rows).
DL = os.path.expanduser("~/Downloads")
EXTRA = {
    # indoor, includes a squad revive (green icon, not a kill) and a 4-kill burst ("KILL x4")
    "Battlefield™ 6 - 2026-06-14 11-33-50 PM.mp4": dict(total=7, streak_kills=[5], deaths=0,
                                                         events={"squad_revive", "last_squad_member_alive"}),
    # dirt bike + C4: tank kill whose vehicle-destroyed gear sits in the skull slot,
    # white skulls over yellow flowers, then a C4 double kill
    "bike-c4.mp4": dict(total=4, streak_kills=[4], deaths=1, events={"tank_buster", "vehicle_destroyed"},
                        board=(4015, 13, 6, 10)),
}


@pytest.mark.parametrize("name", sorted(EXTRA))
def test_extra_clips(name):
    path = os.path.join(DL, name)
    if not os.path.isfile(path):
        pytest.skip("clip not available")
    exp = EXTRA[name]
    r = analyze(path)
    assert r.total_kills == exp["total"]
    assert [s.kills for s in r.streaks] == exp["streak_kills"]
    assert len(r.deaths) == exp["deaths"]
    assert exp["events"] <= set(r.event_counts())
    if "board" in exp:      # the game's own scoreboard, read from the screen
        b = r.scoreboards[0]
        assert (b.score, b.kills, b.deaths, b.assists) == exp["board"]
