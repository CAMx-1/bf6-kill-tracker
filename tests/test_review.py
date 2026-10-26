from bf6killtracker import review
from bf6killtracker.analyzer import AnalysisResult
from bf6killtracker.events import KillEvent


def make(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    kills = [KillEvent(1.0), KillEvent(2.0), KillEvent(3.0, count=2), KillEvent(20.0)]
    return AnalysisResult(str(v), 30.0, 30.0, kills, [], 0, 0.0, 10.0, deaths=[10.0])


def test_apply_and_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("BF6KT_CACHE_DIR", str(tmp_path / "c" / "scans"))
    r = make(tmp_path)
    rv = review.load(r.video)
    assert rv.is_empty()
    rv.rejected.append(2.05)                      # false detection
    rv.counts["3.000"] = 3                        # it was a triple
    rv.added.append({"time": 21.0, "count": 1, "type": "headshot"})
    rv.rejected_deaths.append(10.1)
    review.save(rv)
    out = review.apply(make(tmp_path), streak_gap=5, streak_min=3)
    assert [(k.time, k.count) for k in out.kills] == [(1.0, 1), (3.0, 3), (20.0, 1), (21.0, 1)]
    assert out.deaths == [] and out.reviewed
    assert [s.kills for s in out.streaks] == [4]
    acc = review.accuracy(make(tmp_path), review.load(r.video))
    assert (acc["false"], acc["missed"]) == (1, 1)
    assert review.export_dataset(str(tmp_path / "ds")) == 1
    rv2 = review.load(r.video)
    rv2.rejected, rv2.counts, rv2.added, rv2.rejected_deaths = [], {}, [], []
    review.save(rv2)                              # empty -> file removed
    assert review.load(r.video).is_empty()
