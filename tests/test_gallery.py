"""Template gallery thumbnails."""
import os
import time

from bf6killtracker.gallery import gallery_segment, render_all, render_thumb, thumb_paths
from bf6killtracker.reel import TEMPLATES, Segment


def test_render_and_cache(tmp_path):
    seg = gallery_segment()
    assert len(seg.kills) >= 3                                   # the demo's triple is inside
    got = render_all(["Clean", "MLG", "TikTok hype"], folder=str(tmp_path))
    assert set(got) == {"Clean", "MLG", "TikTok hype"}
    for jpg, gif in got.values():
        assert os.path.getsize(jpg) > 1000 and os.path.getsize(gif) > 10000
    t = time.time()
    assert render_thumb("MLG", seg, str(tmp_path)) == got["MLG"]      # cached: instant
    assert time.time() - t < 0.5
    assert thumb_paths("MLG", seg, str(tmp_path)) != thumb_paths("Clean", seg, str(tmp_path))


def test_segment_around_the_big_moment():
    seg = Segment("x.mp4", 10, 60, "x", [(20, 1), (40, 1), (41, 1), (42, 1)])
    g = gallery_segment(seg)
    assert g.start <= 40 and g.end >= 42 and g.end - g.start <= 5
    assert len(TEMPLATES) >= 20
