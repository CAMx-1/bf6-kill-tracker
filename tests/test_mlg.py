"""MLG mode: generated assets, event planning and a real render."""
import random
import subprocess
import wave

import numpy as np
import pytest

from bf6killtracker import mlg
from bf6killtracker.reel import ReelOptions, Segment, TEMPLATES, apply_template, build_reel
from bf6killtracker.video import find_ffmpeg, probe


def _alpha_cover(img):
    return float((np.asarray(img)[..., 3] > 0).mean())


def test_drawings_are_transparent_stickers():
    for img in (mlg.soda_can(200), mlg.air_horn(300), mlg.hitmarker(60), mlg.dollar_bill(80),
                mlg.illuminati(200), mlg.pixel_shades(240), mlg.meme_text("OH BABY A TRIPLE", 90),
                mlg.meme_text("o my god", 70, "pink", "comic")):
        assert img.mode == "RGBA" and img.width > 10 and img.height > 10
        assert 0.02 < _alpha_cover(img) <= 1.0         # something drawn
    can = np.asarray(mlg.soda_can(200))
    body = can[can[..., 3] > 0][:, :3].astype(int)
    assert (body[:, 1] > body[:, 0] + 40).mean() > 0.3   # a green can
    tile = mlg.oh_screen(640, 360)
    assert tile.size == (int(640 * 1.6), 360) and 0.2 < _alpha_cover(tile) < 0.95


def test_sounds(tmp_path):
    h = mlg.airhorn_wave()
    assert 1.5 < len(h) / mlg.SR < 2.2 and 0.5 < np.abs(h).max() <= 1.0
    # four blasts: loud, short gap, ... (the BWAAA-BWA-BWA-BWAAA rhythm)
    env = np.array([np.abs(h[i:i + 480]).max() for i in range(0, len(h) - 480, 480)])
    gaps = np.flatnonzero(np.diff((env > 0.2).astype(int)) == 1)
    assert len(gaps) >= 3
    p = mlg._write_wav(str(tmp_path / "h.wav"), h)
    with wave.open(p) as w:
        assert w.getnchannels() == 2 and w.getframerate() == mlg.SR


def test_find_triples():
    assert mlg.find_triples([(1.0, 1), (2.0, 1), (3.0, 1)]) == [3.0]
    assert mlg.find_triples([(5.0, 3)]) == [5.0]                   # a x3 multi-kill
    assert mlg.find_triples([(1.0, 1), (6.0, 1), (11.0, 1)]) == []  # too spread out
    assert mlg.find_triples([(1, 1), (2, 1), (3, 1), (4, 1), (9, 1), (9.5, 2)]) == [3, 9.5]


def test_plan_respects_switches(tmp_path):
    a = mlg.Assets(str(tmp_path), 640, 360, triple_sound=str(tmp_path / "missing.wav"))
    kills = [(1.0, 1), (2.0, 1), (3.0, 1)]
    o = ReelOptions(mlg=True)
    p = mlg.plan(kills, 6.0, 640, 360, o, a, random.Random(1))
    assert p.triples == [3.0]
    pngs = {s.png for s in p.stickers}
    assert a.horn() in pngs and a.can() in pngs and a.hit() in pngs and a.tile() in pngs
    assert any(f == a.airhorn_wav() for _, f, _ in p.sfx) and "hue=" in p.vf and "eq=" in p.vf
    assert [s.layer for s in p.stickers] == sorted(s.layer for s in p.stickers)
    # same seed → same layout
    q = mlg.plan(kills, 6.0, 640, 360, o, a, random.Random(1))
    assert [(s.x, s.y) for s in q.stickers] == [(s.x, s.y) for s in p.stickers]
    off = ReelOptions(mlg=True, mlg_airhorn=False, mlg_cans=False, mlg_triple=False, mlg_strobe="off")
    p2 = mlg.plan(kills, 6.0, 640, 360, off, a, random.Random(1))
    pngs = {s.png for s in p2.stickers}
    assert a.horn() not in pngs and a.can() not in pngs and a.tile() not in pngs and not p2.triples
    assert p2.vf == "" and all(f != a.airhorn_wav() for _, f, _ in p2.sfx)
    assert "negate" in mlg.plan(kills, 6.0, 640, 360, ReelOptions(mlg_strobe="max"), a, random.Random(1)).vf
    assert "negate" not in mlg.plan(kills, 6.0, 640, 360, ReelOptions(mlg_strobe="light"), a,
                                    random.Random(1)).vf


def test_user_sounds_are_used(tmp_path):
    own = mlg._write_wav(str(tmp_path / "mine.wav"), mlg.hit_wave())
    a = mlg.Assets(str(tmp_path / "x"), 640, 360, airhorn_sound=own, triple_sound=own)
    assert a.airhorn_wav() == own and a.triple_wav() == own


def test_template():
    o = apply_template("MLG")
    assert o.mlg and o.killcam == "both" and o.overlays
    assert not apply_template("Clean", o).mlg          # switching look turns it off again
    assert "MLG" in TEMPLATES


def _video(path, seconds=6):
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=640x360:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=300:duration={seconds}", "-af", "volume=0.05", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path], check=True)


def _pcm(path):
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-i", path, "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _frame(path, t):
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(360, 640, 3).astype(int)


@pytest.mark.parametrize("strobe", ["normal", "max"])
def test_render_with_mlg(tmp_path, strobe):
    src = str(tmp_path / "src.mp4")
    _video(src)
    own = mlg._write_wav(str(tmp_path / "triple.wav"), mlg.hit_wave())     # avoid TTS differences in CI
    seg = Segment(src, 0.0, 6.0, "x", kills=[(1.0, 1), (2.0, 1), (3.0, 1)])
    plain, out = str(tmp_path / "plain.mp4"), str(tmp_path / "mlg.mp4")
    base = ReelOptions(fade=0, transition="cut")
    build_reel([seg], plain, options=base)
    o = ReelOptions(fade=0, transition="cut", mlg=True, mlg_strobe=strobe, mlg_triple_sound=own)
    build_reel([seg], out, options=o)
    assert abs(probe(out).duration - probe(plain).duration) < 0.15
    a, b = _pcm(plain), _pcm(out)
    win = lambda x, t: float(np.sqrt(np.mean(x[int(t * 8000):int((t + 0.3) * 8000)] ** 2)))  # noqa: E731
    assert win(b, 1.0) > 4 * win(a, 1.0)          # air horn at the kill
    assert abs(win(b, 0.2) - win(a, 0.2)) < 0.02  # untouched before it
    assert np.abs(_frame(out, 3.6) - _frame(plain, 3.6)).mean() > 25    # the triple chaos
    assert np.abs(_frame(out, 0.3) - _frame(plain, 0.3)).mean() < 6     # nothing before the first kill


def test_built_in_voice(tmp_path):
    import sys
    out = mlg.speak("Oh baby, a triple!", str(tmp_path / "v"))
    if sys.platform in ("win32", "darwin"):        # SAPI / `say` ship with the OS
        assert out and len(_pcm(out)) > 4000
    else:
        assert out is None or len(_pcm(out)) > 4000
