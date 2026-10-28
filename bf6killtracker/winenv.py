"""Windows packaged-app (MSIX/AppX) file virtualization workaround.

When this program runs with a package identity — Python from the Microsoft Store, or a
terminal started by a packaged app — Windows silently redirects every new file written
under ``AppData`` (including ``%TEMP%``) to a private ``...\\Packages\\<app>\\LocalCache``
copy. Python still sees those files, but other programs (ffmpeg!) and Explorer don't:
the folder seems never to be created and ffmpeg says "No such file or directory".

So in a packaged process we keep files that ffmpeg or the user must see outside AppData,
in ``%USERPROFILE%\\BF6KillTracker``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from functools import lru_cache

APPMODEL_ERROR_NO_PACKAGE = 15700


@lru_cache(maxsize=1)
def is_packaged() -> bool:
    """True if this Windows process has a package identity (so AppData writes are virtualized)."""
    if os.environ.get("BF6KT_ASSUME_PACKAGED"):          # for tests
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        n = wintypes.UINT(0)
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None)
        return rc != APPMODEL_ERROR_NO_PACKAGE
    except Exception:                      # very old Windows: no packages at all
        return "WindowsApps" in sys.executable


def visible_root() -> str:
    """A per-user folder that every program can see (outside AppData)."""
    return os.path.join(os.path.expanduser("~"), "BF6KillTracker")


_done = False


def fix_temp_dir() -> None:
    """In a packaged process, move temp files out of the virtualized %TEMP% (once)."""
    global _done
    if _done or not is_packaged():
        return
    _done = True
    d = os.path.join(visible_root(), "temp")
    try:
        os.makedirs(d, exist_ok=True)
        tempfile.tempdir = d
    except OSError:
        pass
