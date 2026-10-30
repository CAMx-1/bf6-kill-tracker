"""Check GitHub releases for a newer version.

Public repository: no login needed (a token from ``BF6KT_GITHUB_TOKEN`` /
``GITHUB_TOKEN`` / ``gh auth token`` is used if present, for higher rate limits).
Offline, the check quietly reports nothing. The installed Windows app can update
itself: it downloads the release's installer and runs it silently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from . import __version__
from .video import NO_WINDOW

REPO = "CAMx-1/bf6-kill-tracker"


@dataclass
class Release:
    version: str
    url: str          # release page
    notes: str = ""
    exe_url: str = ""  # direct download of the Windows build, if attached
    setup_url: str = ""  # the Windows installer (one-click update), if attached


def parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - min(3, len(nums)))


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def _token() -> Optional[str]:
    for k in ("BF6KT_GITHUB_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    gh = shutil.which("gh")
    if gh:
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=5, **NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def latest_release(timeout: float = 5.0) -> Optional[Release]:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"bf6killtracker/{__version__}"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (fixed https URL)
            data = json.load(resp)
    except Exception:
        return None
    assets = data.get("assets", [])
    exe = next((a.get("browser_download_url", "") for a in assets
                if a.get("name", "").lower().endswith((".zip", ".exe"))), "")
    setup = next((a.get("browser_download_url", "") for a in assets
                  if a.get("name", "").lower().endswith("setup.exe")), "")
    return Release(data.get("tag_name", "").lstrip("v"), data.get("html_url", ""), data.get("body") or "", exe,
                   setup)


def check_for_update() -> Optional[Release]:
    """The latest release if it's newer than this version, else None."""
    rel = latest_release()
    return rel if rel and rel.version and is_newer(rel.version) else None


def can_self_update() -> bool:
    """True for the installed Windows app (the exe), which can run a newer installer over itself."""
    import sys
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def download_installer(rel: Release, folder: Optional[str] = None, progress=None, timeout: float = 30.0) -> str:
    """Download the release's installer; returns its path. Raises on failure."""
    import tempfile
    if not rel.setup_url:
        raise RuntimeError("this release has no installer attached")
    if not rel.setup_url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise RuntimeError("unexpected download address")          # only ever run our own release files
    folder = folder or tempfile.mkdtemp(prefix="bf6kt_update_")
    out = os.path.join(folder, f"BF6KillTracker-setup-{rel.version}.exe")
    req = urllib.request.Request(rel.setup_url, headers={"User-Agent": f"bf6killtracker/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(out + ".part", "wb") as f:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    os.replace(out + ".part", out)
    if os.path.getsize(out) < 1_000_000:
        raise RuntimeError("the download looks incomplete")
    return out


def installer_command(setup: str) -> list:
    """Silent install over the current version; the installer restarts the app when it's done."""
    return [setup, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]


def run_installer(setup: str) -> None:
    """Start the installer detached (the app should quit right after so its files can be replaced)."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(installer_command(setup), close_fds=True, creationflags=flags)
