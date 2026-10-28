"""The MLG meme sound pack: downloaded on demand into your sounds folder (not shipped).

The clips are well-known meme sounds hosted on myinstants.com. They aren't in the
repository because most are copyrighted (e.g. Darude - Sandstorm); the app fetches
them for you, checks each file against a SHA-256, and stores them in
:func:`bf6killtracker.mlg.my_sounds_dir`, where MLG mode picks them up by name.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from typing import Callable, Dict, List, NamedTuple, Optional


class Clip(NamedTuple):
    slot: str          # file name in the sounds folder (without .mp3)
    url: str
    sha256: str
    label: str


_BASE = "https://www.myinstants.com/media/sounds/"
PACK: List[Clip] = [
    Clip("triple", _BASE + "oh-baby-a-triple.mp3",
         "5ed9c3169405f34cc748a0bdfdaf826a617c9510b8171b3c5e41faf0475ecb4a", "Oh baby a triple"),
    Clip("airhorn", _BASE + "mlg-airhorn.mp3",
         "95beb8827452fab91d38535bbfd359b5e59547e2f98a937cb01032d3dced930c", "MLG air horn"),
    Clip("hit", _BASE + "mlg-hit-sound.mp3",
         "4ed53f74df3811a4905381822376df9e6d48515bb6a98e10efa4d1c5c993bd2c", "MLG hitmarker"),
    Clip("explosion", _BASE + "explosion-sound-capcut-version.mp3",
         "091f6277c175ab25f09be98e2f40a7d1a60497059154755b851bd52840d7a5d7", "Explosion"),
    Clip("fbi", _BASE + "fbi-open-up_dwLhIFf.mp3",
         "cd7d9f2fa02b785d3eff81affdd56ea1ce16a3192a702ebd98c3265d8b024453", "FBI OPEN UP"),
    Clip("wow", _BASE + "anime-wow-sound-effect.mp3",
         "5118c619f0ab62e181f32d9eacd490441b49aaa7bb8e1be1b6b51e7a3c25debc", "Anime wow"),
    Clip("what", _BASE + "what-bottom-text-meme-sanctuary-guardian-sound-effect-hd.mp3",
         "b77604ffbbddf636e166969f69f4c1a092f9c9094010ad08b952725929c90d12", "WHAT?!"),
    Clip("money", _BASE + "money-soundfx.mp3",
         "6f0a8d1e8406ee2e3afc354d17500fa524925ec971d9abf821960f72a14453a7", "Money"),
    Clip("omgwow", _BASE + "omg-wow.mp3",
         "371c618f9b355311f8386e0785084c2f4ba59512c446339405ae8874b87f9591", "OMG WOW"),
    Clip("dotatriple", _BASE + "announcer_kill_triple_01.mp3",
         "74665407c62c7a434039175430dfd33955bbc9474435000be219e36ed0505230", "Announcer TRIPLE KILL"),
    Clip("boompow", _BASE + "boom-bam-bada-bomp-boomp-pow_3xkcJl6.mp3",
         "6bba867d5c8a2e6e88ec89ef89ec65399eed5ad9f0b40aa46f4c3fc55b95f59e", "Boom bam bada bomp pow"),
    Clip("mtndew", _BASE + "im-talking-about-mountain-dew-baby.mp3",
         "6c8d66ba6b1cfb33eb80980eb95df4969d441db2894e4da9befc669f1524cdd3", "Mountain Dew baby"),
    Clip("pwnage", _BASE + "swaggityswagger.mp3",
         "bee0979925c0b8db103c67e58339fd18bc942a4593524dd0b5ec6c787252b33f", "MLG pwnage intro"),
    Clip("headshot", _BASE + "mlg-headshot_0Q5SaS8.mp3",
         "99c375fee7c3748c38b65c507097e7fb6d7eb50353d64a42ed8a26980f749cac", "Best ever headshot"),
    Clip("sandstorm", _BASE + "untitled_605.mp3",
         "409167e9f05f8ed40c710b9d2bb1706c792d82417e85aae9d8f1648c95767393", "Darude - Sandstorm"),
    Clip("sandstorm_flute", _BASE + "darude-sandstorm-flute-anime-version-mp3cut.mp3",
         "e2f6af4c929160bc58efb6c0365d488c2cfd4ff5e905a7ea360f45d32b1e98ce", "Sandstorm (flute)"),
]

_UA = "Mozilla/5.0 (BF6KillTracker; +https://github.com/CAMx-1/bf6-kill-tracker)"


def _folder(folder: Optional[str]) -> str:
    if folder:
        return folder
    from .mlg import my_sounds_dir
    return my_sounds_dir()


def missing(folder: Optional[str] = None) -> List[Clip]:
    """Pack clips not in the sounds folder yet (any audio type with that name counts)."""
    from .mlg import SOUND_EXTS
    d = _folder(folder)
    return [c for c in PACK if not any(os.path.isfile(os.path.join(d, c.slot + e)) for e in SOUND_EXTS)]


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (fixed https URLs)
        return r.read(5_000_000)


def download(folder: Optional[str] = None, *, only_missing: bool = True, timeout: float = 20.0,
             progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, str]:
    """Download the pack. Returns {slot: "ok" | "skipped" | error text}. Never raises for a single clip."""
    d = _folder(folder)
    os.makedirs(d, exist_ok=True)
    todo = missing(d) if only_missing else list(PACK)
    out: Dict[str, str] = {c.slot: "skipped" for c in PACK if c not in todo}
    offline = False
    for i, c in enumerate(todo):
        if progress:
            progress(i, len(todo), c.label)
        if offline:
            out[c.slot] = "skipped (no connection)"
            continue
        try:
            data = _fetch(c.url, timeout)
            if hashlib.sha256(data).hexdigest() != c.sha256:
                out[c.slot] = "file on the website changed (checksum mismatch); not saved"
                continue
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(d, c.slot + ".mp3"))
            out[c.slot] = "ok"
        except Exception as exc:          # offline, blocked, timeout...
            out[c.slot] = f"{type(exc).__name__}: {exc}"
            import urllib.error
            if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)) and \
                    not isinstance(exc, urllib.error.HTTPError):
                offline = True            # don't wait on every clip when there's no internet
    if progress:
        progress(len(todo), len(todo), "done")
    return out


_tried = False


def ensure(timeout: float = 8.0) -> None:
    """Fetch missing clips once per run (used when building an MLG video); silent if offline."""
    global _tried
    if _tried or os.environ.get("BF6KT_NO_SOUND_DOWNLOAD"):
        return
    _tried = True
    if missing():
        download(timeout=timeout)
