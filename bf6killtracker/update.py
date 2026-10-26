"""Check GitHub releases for a newer version.

The repository is private, so the GitHub API needs a token: taken from
``BF6KT_GITHUB_TOKEN`` / ``GITHUB_TOKEN``, or from the GitHub CLI (``gh auth
token``) if it's installed. Without one (or offline) the check quietly
reports nothing. Never raises.
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
    exe = next((a.get("browser_download_url", "") for a in data.get("assets", [])
                if a.get("name", "").lower().endswith((".zip", ".exe"))), "")
    return Release(data.get("tag_name", "").lstrip("v"), data.get("html_url", ""), data.get("body") or "", exe)


def check_for_update() -> Optional[Release]:
    """The latest release if it's newer than this version, else None."""
    rel = latest_release()
    return rel if rel and rel.version and is_newer(rel.version) else None
