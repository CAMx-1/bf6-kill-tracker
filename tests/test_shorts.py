"""Shorts batch export."""
import os

from bf6killtracker.hype import Scored
from bf6killtracker.shorts import hook_title, plan_shorts, short_options


def S(start, end, kills, score, types=None, weapons=None, video="a.mp4"):
    kt = kills if isinstance(kills, list) else [(start, kills)]
    n = sum(c for _, c in kt)
    return Scored(video, f"s{start}", start, end, n, score, [], kt, "a", types or ["gun"] * n,
                  weapons or ["gun_rifle"] * n)


def test_hook_titles():
    assert hook_title(S(10, 10, 3, 90, ["explosive"] * 3, ["explosive_c4"] * 3)) == "TRIPLE C4!"
    assert hook_title(S(10, 12, [(10, 1), (11, 1), (12.2, 1)], 80)) == "3 KILLS IN 2 SECONDS"
    assert hook_title(S(10, 10, 2, 50)) == "DOUBLE RIFLE"
    assert hook_title(S(10, 30, [(10, 1), (20, 1), (30, 1)], 40)) == "3 KILL STREAK"
    assert hook_title(S(10, 10, 1, 20, ["headshot"])) == "CLEAN HEADSHOT"


def test_plan_shorts_lengths_and_no_overlap():
    cands = [S(50, 52, [(50, 1), (51, 1), (52, 1)], 90), S(51, 51, 1, 80),       # overlaps the first
             S(5, 6, [(5, 1), (6, 1)], 70), S(100, 160, [(100, 1), (130, 1), (160, 1)], 60)]
    picks = plan_shorts(cands, {"a.mp4": 170}, count=5, min_s=15, max_s=30)
    segs = [g for _, g in picks]
    assert len(segs) == 3
    for g in segs:
        assert 15 - 1e-6 <= g.end - g.start <= 30 + 1e-6 and 0 <= g.start and g.end <= 170
    first = segs[0]
    assert first.start < 50 - 4 and first.end > 52 and first.title == "3 KILLS IN 2 SECONDS"
    long_ = [g for g in segs if g.end > 150][0]
    assert long_.end >= 160 and long_.end - long_.start == 30          # trimmed from the start
    early = [g for g in segs if g.start == 0][0]
    assert early.end - early.start >= 15                               # pushed forward at the video start


def test_short_options():
    o = short_options("MLG TikTok")
    assert o.vertical and o.size == (1080, 1920) and o.title_top and o.mlg and not o.intro_text


