"""GPU/CPU decoder choice and the speed test."""
import subprocess

from bf6killtracker import video
from bf6killtracker.video import benchmark_decode, find_ffmpeg, hwaccel_args, save_decoder, saved_decoder


def test_decoder_choice_order(tmp_path, monkeypatch):
    monkeypatch.setenv("BF6KT_CACHE_DIR", str(tmp_path / "scans"))
    monkeypatch.delenv("BF6KT_HWACCEL", raising=False)
    assert saved_decoder() is None
    save_decoder("cuda", [("cuda", 300.0), ("none", 120.0)])
    assert saved_decoder() == "cuda" and hwaccel_args() == ["-hwaccel", "cuda"]
    save_decoder("none")
    assert hwaccel_args() == []
    monkeypatch.setenv("BF6KT_HWACCEL", "qsv")                 # the environment always wins
    assert hwaccel_args() == ["-hwaccel", "qsv"]


def test_benchmark(tmp_path):
    v = str(tmp_path / "v.mp4")
    subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=3", "-pix_fmt",
                    "yuv420p", v], check=True)
    seen = []
    res = benchmark_decode(v, 2, progress=lambda i, n, m: seen.append(m))
    names = [n for n, _ in res]
    assert "none" in names and dict(res)["none"] and dict(res)["none"] > 10
    assert res == sorted(res, key=lambda r: -(r[1] or 0)) and seen[-1] == "done"
    assert set(names) - {"none"} <= set(video.HW_CANDIDATES[video._plat()])
