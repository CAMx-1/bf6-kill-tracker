"""One-click update: finding the installer, downloading it safely, the silent install command."""
import io
import json

import pytest

from bf6killtracker import update
from bf6killtracker.update import Release


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_release_finds_installer(monkeypatch):
    body = {"tag_name": "v9.0.0", "html_url": "https://github.com/x", "body": "notes", "assets": [
        {"name": "BF6KillTracker-windows.zip", "browser_download_url": "https://github.com/a.zip"},
        {"name": "BF6KillTracker-setup.exe", "browser_download_url": "https://github.com/s.exe"}]}
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(json.dumps(body).encode()))
    monkeypatch.setattr(update, "_token", lambda: None)
    r = update.latest_release()
    assert r.version == "9.0.0" and r.exe_url.endswith(".zip") and r.setup_url.endswith("setup.exe")


def test_download_installer(tmp_path, monkeypatch):
    good = f"https://github.com/{update.REPO}/releases/download/v9.0.0/BF6KillTracker-setup.exe"
    data = b"MZ" + b"x" * 2_000_000
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(data, {"Content-Length": str(len(data))}))
    seen = []
    p = update.download_installer(Release("9.0.0", "", "", "", good), str(tmp_path), progress=seen.append)
    assert open(p, "rb").read() == data and seen[-1] == 1.0
    with pytest.raises(RuntimeError, match="unexpected"):           # never run files from elsewhere
        update.download_installer(Release("9.0.0", "", "", "", "https://evil.example/setup.exe"), str(tmp_path))
    with pytest.raises(RuntimeError, match="no installer"):
        update.download_installer(Release("9.0.0", "", "", "", ""), str(tmp_path))
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda req, timeout=0: _Resp(b"tiny"))
    with pytest.raises(RuntimeError, match="incomplete"):
        update.download_installer(Release("9.0.0", "", "", "", good), str(tmp_path))


def test_installer_command_is_silent():
    cmd = update.installer_command("C:/x/setup.exe")
    assert cmd[0] == "C:/x/setup.exe" and "/SILENT" in cmd and "/CLOSEAPPLICATIONS" in cmd
    assert update.can_self_update() is False                        # tests never run as the frozen exe
