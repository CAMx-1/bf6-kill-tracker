"""Meme sound pack download (network faked) and the Listen player."""
import hashlib
import os
import urllib.error

from bf6killtracker import mlg, soundpack


def _fake(pack_bytes):
    def fetch(url, timeout):
        if url not in pack_bytes:
            raise urllib.error.URLError("offline")
        return pack_bytes[url]
    return fetch


def test_download_checks_and_saves(tmp_path, monkeypatch):
    data = {c.url: c.slot.encode() * 50 for c in soundpack.PACK}
    pack = [c._replace(sha256=hashlib.sha256(data[c.url]).hexdigest()) for c in soundpack.PACK]
    pack[1] = pack[1]._replace(sha256="0" * 64)                      # pretend the website changed one file
    monkeypatch.setattr(soundpack, "PACK", pack)
    monkeypatch.setattr(soundpack, "_fetch", _fake(data))
    r = soundpack.download(str(tmp_path))
    assert r[pack[0].slot] == "ok" and "checksum" in r[pack[1].slot]
    assert (tmp_path / (pack[0].slot + ".mp3")).read_bytes() == data[pack[0].url]
    assert not (tmp_path / (pack[1].slot + ".mp3")).exists()
    assert [c.slot for c in soundpack.missing(str(tmp_path))] == [pack[1].slot]
    assert soundpack.download(str(tmp_path))[pack[0].slot] == "skipped"     # only the missing one again
    assert not list(tmp_path.glob("*.part"))


def test_offline_gives_up_quickly(tmp_path, monkeypatch):
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        raise urllib.error.URLError("no internet")
    monkeypatch.setattr(soundpack, "_fetch", fetch)
    r = soundpack.download(str(tmp_path))
    assert len(calls) == 1 and all(v != "ok" for v in r.values())


def test_pack_slots_are_used_by_mlg():
    assert {c.slot for c in soundpack.PACK} <= set(mlg.MY_SOUNDS)
    assert len({c.url for c in soundpack.PACK}) == len(soundpack.PACK)
    assert all(c.url.startswith("https://") and len(c.sha256) == 64 for c in soundpack.PACK)


def test_ensure_respects_opt_out(monkeypatch):
    monkeypatch.setattr(soundpack, "_tried", False)
    monkeypatch.setattr(soundpack, "download", lambda **k: (_ for _ in ()).throw(AssertionError("downloaded")))
    soundpack.ensure()                        # BF6KT_NO_SOUND_DOWNLOAD is set by conftest


def test_wav_conversion_for_players(tmp_path):
    import subprocess
    from bf6killtracker.video import find_ffmpeg
    mp3 = str(tmp_path / "x.mp3")
    subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=d=0.3", mp3], check=True)
    wav = mlg._as_wav(mp3)
    assert wav.endswith(".wav") and os.path.getsize(wav) > 10000
    assert mlg._as_wav(mp3) == wav                      # cached
    assert mlg.play_sound(str(tmp_path / "missing.mp3")) == ""
