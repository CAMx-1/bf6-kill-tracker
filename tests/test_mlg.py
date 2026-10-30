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


@pytest.fixture(autouse=True)
def no_personal_sounds(tmp_path_factory, monkeypatch):
    """Keep the developer's own sounds folder out of the tests."""
    d = str(tmp_path_factory.mktemp("mysounds"))
    monkeypatch.setattr(mlg, "my_sounds_dir", lambda: d)
    monkeypatch.setenv("BF6KT_STICKERS_DIR", os.path.join(d, "stickers"))
    return d


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


def test_my_sounds_folder_and_fbi(no_personal_sounds, tmp_path):
    import shutil
    d = no_personal_sounds
    for n in mlg.MY_SOUNDS:
        mlg._write_wav(os.path.join(d, n + ".wav"), np.concatenate([np.zeros(4800), mlg.hit_wave()]))
    a = mlg.Assets(str(tmp_path / "a"), 640, 360)
    assert a.triple_wav() == os.path.join(d, "triple.wav") and a.airhorn_wav() == os.path.join(d, "airhorn.wav")
    assert a.boom_wav() == os.path.join(d, "explosion.wav") and a.fbi_wav() == os.path.join(d, "fbi.wav")
    chosen = mlg._write_wav(str(tmp_path / "chosen.wav"), mlg.hit_wave())
    assert mlg.Assets(str(tmp_path / "b"), 640, 360, triple_sound=chosen).triple_wav() == chosen   # a pick wins
    four = [(1.0, 1), (2.0, 1), (3.0, 1), (4.0, 1)]
    p = mlg.plan(four, 8.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1))
    assert any(f == a.fbi_wav() for _, f, _ in p.sfx)
    hs = mlg.plan([(1.0, 1)], 3.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1),
                  captions=[(1.0, "HEADSHOT · RIFLE")])
    assert any(f == os.path.join(d, "wow.wav") for _, f, _ in hs.sfx)          # anime wow on headshots
    mine = lambda n: os.path.join(d, n + ".wav")  # noqa: E731
    dbl = mlg.plan([(1.0, 1), (2.5, 1), (8.0, 2)], 10.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1))
    files = [f for _, f, _ in dbl.sfx]
    assert mine("what") in files and mine("omgwow") in files and mine("money") in files
    assert dbl.caps[mine("what")] == mlg.MY_SOUND_CAP["what"]
    assert mine("boompow") in files                                       # 4 kills in the clip: finisher
    tri = mlg.plan([(1.0, 1), (1.5, 1), (2.0, 1)], 8.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1))
    at = {os.path.basename(f): t for t, f, _ in tri.sfx}
    assert at["dotatriple.wav"] < at["triple.wav"]                           # announcer, then oh baby
    assert "boompow.wav" not in at                                           # not over the triple
    hs = mlg.plan([(1.0, 1)], 6.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1),
                  captions=[(1.0, "HEADSHOT · RIFLE")], first=True)
    at = {os.path.basename(f): t for t, f, _ in hs.sfx}
    assert at["pwnage.wav"] == 0.0 and at["headshot.wav"] < at["wow.wav"] and "hit.wav" in at
    p3 = mlg.plan(four[:3], 8.0, 640, 360, ReelOptions(mlg=True), a, random.Random(1))
    assert not any(f == a.fbi_wav() for _, f, _ in p3.sfx)          # a triple isn't enough
    shutil.rmtree(d)
    os.makedirs(d)
    assert mlg.Assets(str(tmp_path / "c"), 640, 360).fbi_wav() is None   # no file: no FBI


def test_leading_silence_is_trimmed(no_personal_sounds, tmp_path):
    """A downloaded clip with silence at the start still hits on the kill."""
    src = str(tmp_path / "src.mp4")
    _video(src, 4)
    late = mlg._write_wav(os.path.join(no_personal_sounds, "airhorn.wav"),
                          np.concatenate([np.zeros(int(0.8 * mlg.SR)), 0.9 * np.sign(np.sin(np.arange(mlg.SR) / 8))]))
    assert late
    out = str(tmp_path / "o.mp4")
    o = ReelOptions(fade=0, transition="cut", mlg=True, mlg_strobe="off",
                    **{k: k == "mlg_airhorn" for k in mlg.TOGGLES})
    build_reel([Segment(src, 0.0, 4.0, "x", kills=[(1.0, 1)])], out, options=o)
    x = _pcm(out)
    rms = lambda t: float(np.sqrt(np.mean(x[int(t * 8000):int((t + 0.2) * 8000)] ** 2)))  # noqa: E731
    assert rms(1.05) > 5 * rms(0.5)


def test_sandstorm_music(no_personal_sounds, tmp_path):
    src = str(tmp_path / "src.mp4")
    _video(src, 3)
    mlg._write_wav(os.path.join(no_personal_sounds, "sandstorm.wav"),
                   0.5 * np.sin(2 * np.pi * 220 * np.arange(mlg.SR) / mlg.SR))   # 1 s: must loop
    seg = Segment(src, 0.0, 3.0, "x")
    plain, out = str(tmp_path / "p.mp4"), str(tmp_path / "m.mp4")
    build_reel([seg], plain, options=ReelOptions(fade=0, mlg=True, mlg_strobe="off"))
    build_reel([seg], out, options=ReelOptions(fade=0, mlg=True, mlg_strobe="off", mlg_music="sandstorm",
                                               duck=False))
    a, b = _pcm(plain), _pcm(out)
    w = lambda x, t: float(np.sqrt(np.mean(x[int(t * 8000):int((t + 0.3) * 8000)] ** 2)))  # noqa: E731
    assert w(b, 1.5) > 3 * w(a, 1.5)                 # still playing after the 1 s file ended


def test_suggestions_follow_the_clips():
    one = mlg.ClipFacts([(2.0, 1)], [(2.0, "RIFLE")], 8.0)
    v, why = mlg.suggest([one])
    assert v["mlg_intensity"] == "mild" and not v["mlg_triple"] and not v["mlg_fbi"] and v["mlg_strobe"] == "light"
    assert not v["mlg_scope"] and v["mlg_music"] == "sandstorm_flute" and why
    big = mlg.ClipFacts([(1, 1), (2, 1), (3, 1), (4, 2)], [(2, "HEADSHOT · RIFLE"), (4, "C4")], 12.0)
    v, why = mlg.suggest([one, big, big], has_music=True)
    assert v["mlg_triple"] and v["mlg_fbi"] and v["mlg_scope"] and v["mlg_explosions"] and v["mlg_what"]
    assert v["mlg_auto"] and v["mlg_music"] == "off" and v["mlg_strobe"] == "normal"
    assert any("FBI" in w for w in why) and any("headshot" in w for w in why)
    assert mlg.suggest([])[0] == {"mlg": True}
    assert [mlg.clip_level(k) for k in ([(1, 1)], [(1, 1), (5, 1)], [(1, 1), (2, 1), (3, 1)])] == \
        ["mild", "normal", "max"]


def test_sounds_start_early_by_their_attack(tmp_path):
    slow = np.concatenate([np.linspace(0, 0.9, int(0.2 * mlg.SR)), 0.9 * np.ones(int(0.3 * mlg.SR))])
    p = mlg._write_wav(str(tmp_path / "slow.wav"), slow * np.sign(np.sin(np.arange(len(slow)) / 5)))
    on = mlg.sound_onset(p)
    assert 0.08 < on < 0.15                              # half-level after ~0.11 s of ramp
    a = mlg.Assets(str(tmp_path / "a"), 640, 360, airhorn_sound=p)
    o = ReelOptions(mlg=True, **{k: k == "mlg_airhorn" for k in mlg.TOGGLES})
    pl = mlg.plan([(2.0, 1)], 5.0, 640, 360, o, a, random.Random(1))
    assert [round(t, 2) for t, f, _ in pl.sfx if f == p] == [round(2.0 - on, 2)]


def test_reel_audio_stays_in_sync_over_many_clips(tmp_path):
    """Joined clips must not drift: a click at the flash stays on the flash in clip 1 and clip 12."""
    src = str(tmp_path / "s.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=160x90:r=30:d=4,drawbox=x=0:y=0:w=160:h=90:c=white:t=fill:"
                    "enable='between(t,2,2.034)'", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t\\,2\\,2.01)\\,0.9*sin(2*PI*1000*t)\\,0)':s=48000:d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", src],
                   check=True)
    out = str(tmp_path / "r.mp4")
    build_reel([Segment(src, 0.5, 3.5) for _ in range(12)], out, options=ReelOptions(fade=0, transition="cut"))
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-i", out, "-vf", "scale=4:4,format=gray", "-f", "rawvideo",
                          "-"], capture_output=True, check=True).stdout
    v = np.frombuffer(raw, np.uint8).reshape(-1, 16).mean(1)
    flashes = [i / 30 for i in range(1, len(v)) if v[i] > 128 >= v[i - 1]]
    x = np.abs(_pcm(out))
    clicks, last = [], -1.0
    for i in np.flatnonzero(x > 0.3):
        if i / 8000 - last > 0.5:
            clicks.append(i / 8000)
        last = i / 8000
    assert len(flashes) == len(clicks) == 12
    assert max(abs(c - f) for f, c in zip(flashes, clicks)) < 0.04


def test_voices_never_overlap_and_game_ducks(no_personal_sounds, tmp_path):
    d = no_personal_sounds
    for n in mlg.MY_SOUNDS:
        mlg._write_wav(os.path.join(d, n + ".wav"), 0.8 * np.sign(np.sin(np.arange(int(1.5 * mlg.SR)) / 9)))
    a = mlg.Assets(str(tmp_path / "a"), 640, 360)
    kills = [(1.0, 1), (1.4, 1), (1.8, 1), (2.2, 1), (6.0, 2)]
    caps = [(1.0, "HEADSHOT · RIFLE"), (6.0, "C4")]
    pl = mlg.plan(kills, 10.0, 640, 360, ReelOptions(mlg=True), a, random.Random(2), captions=caps)
    vs = sorted((t, mlg.voice_length(f)) for t, f, _ in pl.sfx if mlg.is_voice(f))
    assert len(vs) >= 3
    assert all(t1 + ln1 <= t2 + 1e-6 for (t1, ln1), (t2, _) in zip(vs, vs[1:]))       # one at a time
    assert len(pl.duck) == len(vs)
    rp = mlg.plan(kills, 10.0, 640, 360, ReelOptions(mlg=True), a, random.Random(2), captions=caps, replay=True)
    assert not any(mlg.is_voice(f) for _, f, _ in rp.sfx) and any(f == a.airhorn_wav() for _, f, _ in rp.sfx)


def test_schedule_voices_shifts_or_drops():
    import tempfile
    d = tempfile.mkdtemp()
    w = mlg._write_wav(os.path.join(d, "what.wav"), 0.8 * np.ones(mlg.SR))       # 1 s line (hold 1.2 s cap)
    t = mlg._write_wav(os.path.join(d, "triple.wav"), 0.8 * np.ones(mlg.SR))
    out, win = mlg.schedule_voices([(1.0, w, 1, 3), (1.2, t, 1, 0), (5.0, w, 1, 3)], 10.0)
    starts = sorted((round(x, 2), mlg._slot(f)) for x, f, _, _ in out)
    assert starts[0] == (1.2, "triple")                  # the important line keeps its moment
    assert (2.38, "what") in [(round(x, 2), s) for x, s in starts] or len(starts) == 2   # shifted after, or dropped
    assert (5.0, "what") in starts and len(win) == len(out)


def test_auto_variety_and_build_up(no_personal_sounds, tmp_path):
    from bf6killtracker import reel
    d = no_personal_sounds
    for n in mlg.MY_SOUNDS:
        mlg._write_wav(os.path.join(d, n + ".wav"), 0.5 * np.ones(int(0.5 * mlg.SR)))
    a = mlg.Assets(str(tmp_path / "a"), 640, 360)
    o = ReelOptions(mlg=True, mlg_auto=True)
    dbl = [(2.0, 2)]
    p1 = mlg.plan(dbl, 6.0, 640, 360, o, a, random.Random(1))
    p2 = mlg.plan(dbl, 6.0, 640, 360, o, a, random.Random(1))
    slots = lambda p: {mlg._slot(f) for _, f, _ in p.sfx}  # noqa: E731
    assert "what" in slots(p1) and "what" not in slots(p2)            # same gag not twice in a row
    seg = Segment("x.mp4", 0, 6, "x", [(2.0, 1)])
    lvl = lambda **k: reel._mlg_plan(reel._Part(seg, 0, 6), o, 640, 360,   # noqa: E731
                                     mlg.Assets(str(tmp_path / "b"), 640, 360), **k)
    assert len(lvl(last=True).stickers) > len(lvl(pos=0.0).stickers)   # the finale gets more
    assert not [f for _, f, _ in mlg.plan([(1, 1), (2, 1), (3, 1)], 6, 640, 360, o, a, random.Random(1)).sfx
                if mlg._slot(f) == "boompow"]                              # finisher only on the last clip


def test_suggest_uses_game_loudness():
    clip = mlg.ClipFacts([(2.0, 1)], [], 8.0)
    assert mlg.suggest([clip], game_db=-12)[0]["mlg_sfx_volume"] == 1.3
    assert mlg.suggest([clip], game_db=-35)[0]["mlg_sfx_volume"] == 0.8
    assert "mlg_sfx_volume" not in mlg.suggest([clip])[0]


def _gif(path, bg, seconds=1.0, size="160x120"):
    """A test 'meme': a moving red square on a flat background (or green screen)."""
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={bg}:s={size}:r=10:d={seconds}",
                    "-vf", "drawbox=x=40:y=30:w=50:h=50:c=red:t=fill", path], check=True)
    return path


def test_detect_key_and_cut_out(no_personal_sounds, tmp_path):
    st = os.path.join(no_personal_sounds, "stickers")
    os.makedirs(st)
    green = _gif(os.path.join(st, "gs.webm"), "0x00FF00")
    black = _gif(os.path.join(st, "dark.gif"), "black")
    busy = os.path.join(st, "busy.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x120:r=10:d=1", busy],
                   check=True)
    assert mlg.detect_key(green).startswith("0x00F") and mlg.detect_key(black).startswith("0x0")
    assert mlg.detect_key(busy) == ""
    assert sorted(map(mlg._slot, mlg.my_stickers())) == ["busy", "dark", "gs"]
    a = mlg.Assets(str(tmp_path / "a"), 640, 360)
    clip = a.meme(green, 0.4, 1.5)
    assert clip and a.size_of(clip)[1] == 144
    raw = subprocess.run([find_ffmpeg(), "-v", "error", "-ss", "0.5", "-i", clip, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout
    w, h = a.size_of(clip)
    px = np.frombuffer(raw, np.uint8).reshape(h, w, 4)
    assert px[2, 2, 3] < 30 and px[:, :, 3].max() == 255            # green gone, red square kept
    framed = a.meme(busy, 0.4, 1.0)
    fw, fh = a.size_of(framed)
    assert fh > 144                                                 # white frame added round a pop-up


def test_video_memes_on_triples(no_personal_sounds, tmp_path):
    st = os.path.join(no_personal_sounds, "stickers")
    os.makedirs(st)
    for n in ("frog", "nyan", "snoop"):
        _gif(os.path.join(st, n + ".gif"), "black")
    a = mlg.Assets(str(tmp_path / "a"), 640, 360)
    o = ReelOptions(mlg=True)
    p = mlg.plan([(1.0, 1), (1.5, 1), (2.0, 1)], 6.0, 640, 360, o, a, random.Random(1))
    vids = [s_ for s_ in p.stickers if s_.video]
    assert len(vids) == 2 and len({s_.png for s_ in vids}) == 2          # normal: 2 different memes
    off = mlg.plan([(1.0, 1), (1.5, 1), (2.0, 1)], 6.0, 640, 360, ReelOptions(mlg=True, mlg_videomemes=False), a,
                   random.Random(1))
    assert not [s_ for s_ in off.stickers if s_.video]
    src = str(tmp_path / "src.mp4")
    _video(src, 4)
    out = str(tmp_path / "o.mp4")
    build_reel([Segment(src, 0, 4, "x", [(1.0, 1), (1.5, 1), (2.0, 1)])], out,
               options=ReelOptions(fade=0, mlg=True, mlg_strobe="off",
                                   **{k: k in ("mlg_videomemes", "mlg_triple") for k in mlg.TOGGLES}))
    assert abs(probe(out).duration - 4.0) < 0.2


def test_video_pack_download(tmp_path, monkeypatch):
    import hashlib
    from bf6killtracker import soundpack
    data = {c.url: c.slot.encode() * 30 for c in soundpack.VIDEO_PACK}
    pack = [c._replace(sha256=hashlib.sha256(data[c.url]).hexdigest()) for c in soundpack.VIDEO_PACK]
    monkeypatch.setattr(soundpack, "VIDEO_PACK", pack)
    monkeypatch.setattr(soundpack, "_fetch", lambda url, timeout: data[url])
    r = soundpack.download(str(tmp_path), video=True)
    assert set(r.values()) == {"ok"}
    assert (tmp_path / "frog.gif").exists() and (tmp_path / "explosion_gs.webm").exists()
    assert not soundpack.missing(str(tmp_path), video=True)


def _song(path, bpm=120.0, seconds=20.0, drop=10.0, sr=22050):
    """Quiet clicks on the beat, then loud bass + clicks from ``drop`` on."""
    t = np.arange(int(seconds * sr)) / sr
    per = 60 / bpm
    click = np.exp(-((t % per) / 0.01)) * np.sin(2 * np.pi * 2000 * t)
    bass = np.sin(2 * np.pi * 55 * t) * (t >= drop)
    y = 0.15 * click + 0.6 * bass + 0.4 * click * (t >= drop)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def test_drop_and_beat_grid(tmp_path):
    from bf6killtracker.music import detect_beats, find_drop, load_mono
    y = load_mono(_song(str(tmp_path / "s.wav")))
    bpm, beats = detect_beats(y)
    assert abs(bpm - 120) < 3
    assert abs(find_drop(y) - 10.0) < 0.4
    flat = load_mono(_song(str(tmp_path / "f.wav"), drop=99))
    assert find_drop(flat) is None
    ph, per = mlg.beat_grid([0.25, 0.75, 1.25, 1.76, 2.25])
    assert abs(per - 0.5) < 0.02 and abs(ph - 0.25) < 0.03
    assert mlg._snap(0.8, (0.25, 0.5), 0.12) == 0.75 and mlg._snap(1.0, (0.25, 0.5), 0.12) == 1.0
    assert "zoompan" in mlg.beat_pump(640, 360, (0.25, 0.5), 30)


def test_big_moment_lands_on_the_drop(tmp_path):
    from bf6killtracker import reel
    song = _song(str(tmp_path / "s.wav"), drop=10.0)
    segs = [Segment("a.mp4", 0, 8, "a", [(3.0, 1)]), Segment("b.mp4", 0, 8, "b", [(2.0, 1), (3.0, 1), (4.0, 1)])]
    parts = [reel._Part(s_, s_.start, s_.duration) for s_ in segs]
    o = ReelOptions(mlg=True, music=song, transition="cut")
    reel._mlg_beat_grid(parts, o)
    big = 8.0 + 4.0                                       # the triple's 3rd kill, in the reel
    assert abs(parts[1].drop - 4.0) < 0.5                 # the drop hits the triple
    assert parts[0].beats and all(0 <= b < 8 for b in parts[1].beats)
    # drop earlier than the big moment: a later loop of the song is used
    parts2 = [reel._Part(s_, s_.start, s_.duration) for s_ in segs * 2]
    reel._mlg_beat_grid(parts2, o)
    assert any(p.drop is not None and abs(p.drop - 4.0) < 0.5 for p in parts2)


def test_beat_render(tmp_path):
    src = str(tmp_path / "src.mp4")
    _video(src, 4)
    out = str(tmp_path / "o.mp4")
    build_reel([Segment(src, 0, 4, "x", [(1.0, 1), (1.5, 1), (2.0, 1)])], out,
               options=ReelOptions(fade=0, mlg=True, music=_song(str(tmp_path / "s.wav"), seconds=8, drop=3.0),
                                   mlg_videomemes=False))
    assert abs(probe(out).duration - 4.0) < 0.2
