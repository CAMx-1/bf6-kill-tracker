"""MLG mode: generated assets, event planning and a real render."""
import os
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


def test_templates():
    o = apply_template("MLG")
    assert o.mlg and o.killcam == "both" and o.overlays
    assert not apply_template("Clean", o).mlg          # switching look turns it off again
    assert len(TEMPLATES) >= 18 and sum(k.startswith("MLG") for k in TEMPLATES) >= 5
    mx = apply_template("MLG MAX")
    assert mx.mlg_intensity == "max" and mx.mlg_voices and mx.mlg_deepfry == "always"
    lite = apply_template("MLG lite", mx)               # MAX extras don't leak into lite
    assert lite.mlg_intensity == "mild" and not lite.mlg_voices and lite.mlg_strobe == "light"
    c = apply_template("Cinematic", lite)
    assert c.letterbox and c.grade == "Cinematic" and not c.mlg
    assert not apply_template("Clean", c).letterbox
    from bf6killtracker.reel import GRADES
    for name, t in TEMPLATES.items():
        assert t.get("grade", "None") in GRADES, name


def test_every_template_renders(tmp_path):
    """Each look's filter graph runs (tiny, fast)."""
    from bf6killtracker.reel import demo_segment, preview_options
    seg = demo_segment(str(tmp_path))
    seg = Segment(seg.video, 1.5, 3.6, "d", [k for k in seg.kills if 1.5 <= k[0] <= 3.6],
                  captions=[c for c in seg.captions if 1.5 <= c[0] <= 3.6])
    for name in TEMPLATES:
        o = preview_options(apply_template(name))
        o.size = (320, 180) if not o.vertical else (180, 320)
        o.replay = False
        o.mlg_voices = False
        out = str(tmp_path / f"{len(os.listdir(tmp_path))}.mp4")
        build_reel([seg], out, options=o)
        info = probe(out)
        assert info.width == o.size[0] and abs(info.duration - 2.1) < 0.3, name


def test_all_toggles_and_new_elements(tmp_path):
    a = mlg.Assets(str(tmp_path), 640, 360)
    kills = [(1.0, 1), (2.0, 1), (3.0, 1), (5.0, 2)]
    caps = [(2.0, "HEADSHOT · RIFLE"), (5.0, "C4")]
    full = ReelOptions(mlg=True, mlg_smoke=True, mlg_siren=True)
    p = mlg.plan(kills, 7.0, 640, 360, full, a, random.Random(3), captions=caps)
    pngs = {s.png for s in p.stickers}
    assert a.scope() in pngs and a.boom() in pngs                 # headshot scope, C4 explosion
    files = {f for _, f, _ in p.sfx}
    assert a.boom_wav() in files and a.wub_wav() in files and a.siren_wav() in files
    assert "rotate=" in p.vf_post and "crop=" in p.vf_post and "bass=" in p.af
    for k in mlg.TOGGLES:
        setattr(full, k, False)
    full.mlg_strobe, full.mlg_deepfry = "off", "off"
    q = mlg.plan(kills, 7.0, 640, 360, full, a, random.Random(3), captions=caps)
    assert not q.sfx and not q.vf and not q.vf_post and not q.af and not q.triples
    loud = ReelOptions(mlg=True, mlg_sfx_volume=2.0)
    r = mlg.plan(kills, 7.0, 640, 360, loud, a, random.Random(3))
    base = mlg.plan(kills, 7.0, 640, 360, ReelOptions(mlg=True), a, random.Random(3))
    assert max(g for _, _, g in r.sfx) == 2 * max(g for _, _, g in base.sfx)
    none = mlg.plan([(t, 1) for t in (1, 6, 11, 16)], 20, 640, 360, ReelOptions(mlg_airhorn_chance=0.0), a,
                    random.Random(3))
    assert a.horn() not in {s.png for s in none.stickers}
    only_text = ReelOptions(mlg_words="GG,EZ", **{k: k == "mlg_text" for k in mlg.TOGGLES})
    w = mlg.plan(kills, 7.0, 640, 360, only_text, a, random.Random(3))
    words = {s.png for s in w.stickers} & {a.text(x, st, h, k) for x in ("GG", "EZ") for st in mlg.TEXT_STYLES
                                             for h in (0.09, 0.11, 0.14) for k in ("impact", "comic")}
    assert words


def test_new_drawings_and_sounds():
    for img in (mlg.explosion(120), mlg.nacho_chip(80), mlg.rainbow_smoke(100)):
        assert img.mode == "RGBA" and 0.05 < _alpha_cover(img) <= 1.0
    sc = np.asarray(mlg.scope(320, 180))
    assert sc[60, 120, 3] < 50 and sc[5, 5, 3] == 255          # see-through window, black edge
    for fn in (mlg.wub_wave, mlg.boom_wave, mlg.siren_wave):
        x = fn()
        assert len(x) > mlg.SR * 0.5 and 0.3 < np.abs(x).max() <= 1.0


def test_look_filters_render(tmp_path):
    from bf6killtracker.reel import GRADES, demo_segment
    seg = demo_segment(str(tmp_path))
    seg = Segment(seg.video, 0.0, 1.0)
    for g in GRADES:
        out = str(tmp_path / f"g{len(os.listdir(tmp_path))}.mp4")
        build_reel([seg], out, options=ReelOptions(size=(320, 180), fps=15, grade=g, letterbox=True, vignette=True,
                                                   grain=True, preset="ultrafast"))
        assert probe(out).width == 320, g
    f = _frame_at(out, 0.5, 320, 180)
    assert f[2].mean() < 8 and f[90].mean() > 8                  # black bar on top, picture in the middle


def _video(path, seconds=6):
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=640x360:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=300:duration={seconds}", "-af", "volume=0.05", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path], check=True)


def _pcm(path):
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-i", path, "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _frame_at(path, t, w, h):
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(int)


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


def test_stalled_ffmpeg_is_killed(monkeypatch):
    import sys
    import time
    from bf6killtracker import reel
    monkeypatch.setenv("BF6KT_STALL_S", "1")
    t = time.time()
    with pytest.raises(RuntimeError, match="stopped responding"):
        reel._run([sys.executable, "-c", "import time; time.sleep(30)"], 1.0, lambda x: None, None, "test")
    assert time.time() - t < 10


def test_frozen_ffmpeg_that_keeps_printing_is_killed(monkeypatch):
    import sys
    import time
    from bf6killtracker import reel
    monkeypatch.setenv("BF6KT_STALL_S", "1")
    fake = "import time\nwhile True:\n    print('out_time_us=500000', flush=True)\n    time.sleep(0.2)"
    t = time.time()
    with pytest.raises(RuntimeError, match="stopped responding"):
        reel._run([sys.executable, "-c", fake], 1.0, lambda x: None, None, "test")
    assert time.time() - t < 10


def test_bundled_real_assets(tmp_path):
    """The real air horn recording and Mtn Dew logo ship in templates/mlg and are used by default."""
    horn, logo = mlg.bundled("airhorn.wav"), mlg.bundled("mtn_dew_logo.png")
    assert horn and logo and mlg.bundled("SOURCES.txt")
    a = mlg.Assets(str(tmp_path), 640, 360)
    assert a.airhorn_wav() == horn and a.logo()
    with wave.open(horn) as w:
        assert 1.5 < w.getnframes() / w.getframerate() < 2.5
    can = np.asarray(mlg.soda_can(300)).astype(int)
    red = (can[..., 0] > 180) & (can[..., 1] < 80) & (can[..., 3] > 0)
    assert red.mean() > 0.02                       # the red DEW of the logo is on the can
    own = mlg._write_wav(str(tmp_path / "mine.wav"), mlg.hit_wave())
    assert mlg.Assets(str(tmp_path / "b"), 640, 360, airhorn_sound=own).airhorn_wav() == own
