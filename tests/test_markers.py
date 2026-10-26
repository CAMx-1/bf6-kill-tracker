import xml.dom.minidom

from bf6killtracker.analyzer import AnalysisResult
from bf6killtracker.events import KillEvent, find_streaks
from bf6killtracker.markers import export, markers_for, to_chapters


def result(tmp_path):
    kills = [KillEvent(5, types=["gun"]), KillEvent(30, types=["headshot"]), KillEvent(32, count=2),
             KillEvent(34), KillEvent(80), KillEvent(82), KillEvent(84)]
    r = AnalysisResult(str(tmp_path / "my clip.mp4"), 120.0, 60.0, kills, [], 0, 0.0, 10.0, deaths=[50.0],
                       feed_used=True)
    r.streaks = find_streaks(kills, 5, 3, r.deaths)
    return r


def test_marker_list(tmp_path):
    ms = markers_for(result(tmp_path))
    assert [m.name for m in ms if m.color == "red"] == ["Streak 1: 4 kills", "Streak 2: 3 kills"]
    assert any(m.name == "Double kill" for m in ms) and any(m.name == "Death" for m in ms)
    assert ms[1].duration == 4.0


def test_all_formats(tmp_path):
    r = result(tmp_path)
    for fmt in ("edl", "fcpxml", "premiere", "chapters", "csv"):
        p = export(r, fmt, str(tmp_path / f"out.{fmt}"))
        text = open(p, encoding="utf-8").read()
        assert "Streak 1" in text
        if fmt in ("fcpxml", "premiere"):
            xml.dom.minidom.parseString(text)          # well-formed
    edl = open(tmp_path / "out.edl").read()
    assert "00:00:30:00 00:00:34:00" in edl and "|C:ResolveColorRed" in edl
    fc = open(tmp_path / "out.fcpxml").read()
    assert 'start="30s" duration="4s"' in fc and "my%20clip.mp4" in fc


def test_youtube_chapters_rules(tmp_path):
    ch = to_chapters(markers_for(result(tmp_path)), 120.0).strip().splitlines()
    assert ch[0] == "0:00 Start"
    assert ch[1:] == ["0:27 Streak 1: 4 kills", "1:17 Streak 2: 3 kills"]
