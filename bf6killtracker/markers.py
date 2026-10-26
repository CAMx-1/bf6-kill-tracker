"""Export kills, deaths and streaks as markers for video editors and YouTube.

Formats:

* ``edl``      - CMX3600 EDL with marker comments; imports into DaVinci Resolve
                 (Timeline > Import > Timeline Markers from EDL) and Premiere.
* ``fcpxml``   - Final Cut Pro X / DaVinci Resolve: the video as a clip with markers.
* ``premiere`` - Premiere Pro marker CSV is not a thing; Premiere imports FCP7 XML,
                 so this writes an xmeml sequence with markers.
* ``chapters`` - YouTube chapter timestamps for the video description
                 (first chapter at 0:00, at least 10 s apart, as YouTube requires).
* ``csv``      - plain list of markers.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional
from xml.sax.saxutils import escape, quoteattr

FORMATS = {"edl": ".edl", "fcpxml": ".fcpxml", "premiere": ".xml", "chapters": ".txt", "csv": ".csv"}


@dataclass
class Marker:
    time: float
    name: str
    duration: float = 0.0
    color: str = "blue"      # "red" streaks, "yellow" multi-kills, "blue" kills, "purple" deaths
    note: str = ""


def markers_for(result, include_kills: bool = True, include_deaths: bool = True,
                include_streaks: bool = True) -> List[Marker]:
    """Markers for an :class:`AnalysisResult` (streaks as ranged markers)."""
    out: List[Marker] = []
    if include_streaks:
        for s in result.streaks:
            out.append(Marker(s.start, f"Streak {s.id + 1}: {s.kills} kills", max(0.0, s.end - s.start), "red",
                              f"{s.kills} kills in {s.end - s.start:.1f}s"))
    if include_kills:
        for k in result.kills:
            types = ", ".join(t for t in k.type_list() if t != "unknown")
            name = {1: "Kill", 2: "Double kill", 3: "Triple kill"}.get(k.count, f"{k.count}x multi-kill")
            out.append(Marker(k.time, name + (f" ({types})" if types else ""), 0.0,
                              "yellow" if k.count > 1 else "blue", ", ".join(w for w in k.weapons if w)))
    if include_deaths:
        for d in result.deaths:
            out.append(Marker(d, "Death", 0.0, "purple"))
    out.sort(key=lambda m: (m.time, m.color != "red"))
    return out


# --------------------------------------------------------------------------- helpers
def _tc(t: float, fps: int) -> str:
    f = int(round(max(0.0, t) * fps))
    h, f = divmod(f, 3600 * fps)
    m, f = divmod(f, 60 * fps)
    s, f = divmod(f, fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _hms(t: float) -> str:
    t = int(t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _rate(fps: float) -> int:
    return max(1, int(round(fps or 30)))


# --------------------------------------------------------------------------- writers
_EDL_COLORS = {"red": "ResolveColorRed", "yellow": "ResolveColorYellow", "blue": "ResolveColorBlue",
               "purple": "ResolveColorPurple"}


def to_edl(markers: List[Marker], title: str, fps: float, duration: float) -> str:
    r = _rate(fps)
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for i, m in enumerate(markers, 1):
        a = _tc(m.time, r)
        b = _tc(m.time + max(m.duration, 1.0 / r), r)
        lines.append(f"{i:03d}  001      V     C        {a} {b} {a} {b}  ")
        name = m.name.replace("|", "/")
        lines.append(f" |C:{_EDL_COLORS.get(m.color, 'ResolveColorBlue')} |M:{name} "
                     f"|D:{max(1, int(round(m.duration * r)))}")
        lines.append("")
    return "\n".join(lines)


def _rt(t: float, fps: int) -> str:
    """FCPXML rational time, frame-aligned."""
    fr = Fraction(int(round(max(0.0, t) * fps)), fps)
    return f"{fr.numerator}/{fr.denominator}s" if fr.denominator != 1 else f"{fr.numerator}s"


def to_fcpxml(markers: List[Marker], video: str, fps: float, duration: float, width: int = 1920,
              height: int = 1080) -> str:
    r = _rate(fps)
    src = "file://" + os.path.abspath(video).replace("\\", "/").replace(" ", "%20")
    if not src.startswith("file:///"):
        src = src.replace("file://", "file:///")
    name = os.path.splitext(os.path.basename(video))[0]
    dur = _rt(duration, r)
    fd = f"1/{r}s"
    ms = []
    for m in markers:
        attrs = f'start="{_rt(m.time, r)}" duration="{_rt(m.duration, r) if m.duration else fd}" value={quoteattr(m.name)}'
        if m.note:
            attrs += f" note={quoteattr(m.note)}"
        ms.append(f"                        <marker {attrs}/>")
    marker_xml = "\n".join(ms)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
    <resources>
        <format id="r1" frameDuration="{fd}" width="{width}" height="{height}"/>
        <asset id="r2" name={quoteattr(name)} start="0s" duration="{dur}" hasVideo="1" hasAudio="1" format="r1">
            <media-rep kind="original-media" src={quoteattr(src)}/>
        </asset>
    </resources>
    <library>
        <event name="BF6 Kill Tracker">
            <project name={quoteattr(name + " - kills")}>
                <sequence format="r1" duration="{dur}" tcStart="0s">
                    <spine>
                    <asset-clip ref="r2" name={quoteattr(name)} offset="0s" start="0s" duration="{dur}">
{marker_xml}
                    </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""


def to_premiere_xml(markers: List[Marker], video: str, fps: float, duration: float, width: int = 1920,
                    height: int = 1080) -> str:
    r = _rate(fps)
    name = os.path.splitext(os.path.basename(video))[0]
    frames = int(round(duration * r))
    url = "file://localhost/" + os.path.abspath(video).replace("\\", "/").lstrip("/").replace(" ", "%20")
    ms = []
    for m in markers:
        a = int(round(m.time * r))
        b = int(round((m.time + m.duration) * r)) if m.duration else -1
        ms.append(f"""      <marker><name>{escape(m.name)}</name><comment>{escape(m.note)}</comment>"""
                  f"""<in>{a}</in><out>{b}</out></marker>""")
    mx = "\n".join(ms)
    rate = f"<rate><timebase>{r}</timebase><ntsc>FALSE</ntsc></rate>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence>
    <name>{escape(name)} - kills</name>
    <duration>{frames}</duration>
    {rate}
{mx}
    <media>
      <video>
        <format><samplecharacteristics>{rate}<width>{width}</width><height>{height}</height></samplecharacteristics></format>
        <track>
          <clipitem id="clip-1">
            <name>{escape(name)}</name>
            <duration>{frames}</duration>
            {rate}
            <start>0</start><end>{frames}</end><in>0</in><out>{frames}</out>
            <file id="file-1">
              <name>{escape(os.path.basename(video))}</name>
              <pathurl>{escape(url)}</pathurl>
              {rate}
              <duration>{frames}</duration>
              <media><video><samplecharacteristics><width>{width}</width><height>{height}</height></samplecharacteristics></video><audio/></media>
            </file>
{mx}
          </clipitem>
        </track>
      </video>
    </media>
  </sequence>
</xmeml>
"""


def to_chapters(markers: List[Marker], duration: float, min_gap: float = 10.0) -> str:
    """YouTube chapters: '0:00 Intro' first, entries >= ``min_gap`` s apart (streaks preferred)."""
    streaks = [m for m in markers if m.color == "red"]
    picks = streaks or [m for m in markers if m.color in ("yellow", "blue")]
    lines = ["0:00 Start"]
    last = 0.0
    for m in sorted(picks, key=lambda m: m.time):
        t = max(0.0, m.time - 3.0)       # start the chapter a little before the action
        if t - last < min_gap or duration - t < min_gap:
            continue
        lines.append(f"{_hms(t)} {m.name}")
        last = t
    return "\n".join(lines) + "\n"


def to_csv(markers: List[Marker], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "timestamp", "duration_s", "name", "color", "note"])
        for m in markers:
            w.writerow([f"{m.time:.3f}", _hms(m.time), f"{m.duration:.3f}", m.name, m.color, m.note])


def export(result, fmt: str, out_path: Optional[str] = None, *, width: int = 1920, height: int = 1080,
           markers: Optional[List[Marker]] = None) -> str:
    """Write markers for ``result`` in ``fmt`` (see :data:`FORMATS`); returns the file path."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown marker format {fmt!r}; choose from {', '.join(FORMATS)}")
    ms = markers if markers is not None else markers_for(result)
    out_path = out_path or os.path.splitext(result.video)[0] + (".markers" if fmt == "csv" else "") + FORMATS[fmt]
    title = os.path.splitext(os.path.basename(result.video))[0]
    if fmt == "csv":
        to_csv(ms, out_path)
        return out_path
    if fmt == "edl":
        text = to_edl(ms, title, result.fps, result.duration)
    elif fmt == "fcpxml":
        text = to_fcpxml(ms, result.video, result.fps, result.duration, width, height)
    elif fmt == "premiere":
        text = to_premiere_xml(ms, result.video, result.fps, result.duration, width, height)
    else:
        text = to_chapters(ms, result.duration)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return out_path
