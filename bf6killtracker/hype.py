"""Hype score for clips, and automatic montages.

Each clip (streak or single kill) gets a score from what happens in it:
kills, multi-kills, headshots, explosives, how fast the kills come, and
special moments (LAST SQUAD MEMBER ALIVE, squad wipes, medals). The montage
builder picks the best clips that fit a target length and orders them so the
video builds up to its best moment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# points per thing in a clip
WEIGHTS: Dict[str, float] = {
    "kill": 10.0,
    "multi_extra": 8.0,          # each extra kill landed at the same moment (double/triple...)
    "headshot": 5.0,
    "explosive": 3.0,
    "quick": 6.0,                # each kill within 1.5 s of the previous one
    "last_squad_member_alive": 15.0,
    "squad_wipe": 12.0,
    "medal": 4.0,                # any medal badge (tank buster, avenger, payback...)
    "vehicle_destroyed": 6.0,
}
MEDALS = {"double_kill", "triple_kill", "quad_kill", "tank_buster", "payback", "avenger", "gunslinger",
          "marauder", "headhunter"}
_MULTI_MEDALS = {"double_kill", "triple_kill", "quad_kill"}   # already counted via multi-kills


@dataclass
class Scored:
    video: str
    key: str
    start: float
    end: float
    kills: int
    score: float
    reasons: List[str] = field(default_factory=list)
    kill_times: List[Tuple[float, int]] = field(default_factory=list)
    title: str = ""

    @property
    def stars(self) -> str:
        n = 1 + min(4, int(self.score // 25))
        return "★" * n + "☆" * (5 - n)


def score_clip(kill_events: Sequence, hud_events: Sequence = (), start: Optional[float] = None,
               end: Optional[float] = None, window: float = 3.0) -> Tuple[float, List[str]]:
    """(score, reasons) for a clip with these kills; HUD events within ``window`` s of it count too."""
    ks = sorted(kill_events, key=lambda k: k.time)
    if not ks:
        return 0.0, []
    a = ks[0].time if start is None else start
    b = ks[-1].time if end is None else end
    w = WEIGHTS
    score, why = 0.0, []
    total = sum(k.count for k in ks)
    score += w["kill"] * total
    extra = sum(k.count - 1 for k in ks)
    if extra:
        score += w["multi_extra"] * extra
        best = max(k.count for k in ks)
        why.append({2: "double kill", 3: "triple kill", 4: "quad kill"}.get(best, f"{best}x multi-kill"))
    types = [t for k in ks for t in k.type_list()]
    hs, ex = types.count("headshot"), types.count("explosive")
    if hs:
        score += w["headshot"] * hs
        why.append(f"{hs} headshot{'s' if hs > 1 else ''}")
    if ex:
        score += w["explosive"] * ex
    quick = sum(1 for p, q in zip(ks, ks[1:]) if q.time - p.time <= 1.5)
    if quick:
        score += w["quick"] * quick
        why.append("rapid kills")
    seen = set()
    for e in hud_events:
        if not (a - window <= e.time <= b + window) or e.kind in seen:
            continue
        if e.kind == "last_squad_member_alive":
            score += w["last_squad_member_alive"]
            why.append("last squad member alive")
        elif e.kind == "squad_wipe":
            score += w["squad_wipe"] * e.count
            why.append("squad wipe")
        elif e.kind == "vehicle_destroyed":
            score += w["vehicle_destroyed"]
            why.append("vehicle destroyed")
        elif e.kind in MEDALS and e.kind not in _MULTI_MEDALS:
            score += w["medal"]
            why.append(e.kind.replace("_", " "))
        else:
            continue
        seen.add(e.kind)
    why.insert(0, f"{total} kill{'s' if total > 1 else ''}")
    return round(score, 1), why


def rank(scored: List[Scored]) -> List[Scored]:
    return sorted(scored, key=lambda c: (-c.score, c.start))


def build_up_order(clips: List[Scored]) -> List[Scored]:
    """Weakest first, best last, with the second-best opening the video as a hook."""
    if len(clips) <= 2:
        return sorted(clips, key=lambda c: c.score)
    by = sorted(clips, key=lambda c: c.score)
    best, second, rest = by[-1], by[-2], by[:-2]
    return [second] + rest + [best]


def pick_montage(candidates: List[Scored], target_s: float, pad_before: float, pad_after: float,
                 min_score: float = 0.0) -> List[Scored]:
    """Highest-scoring clips whose padded lengths add up to about ``target_s`` seconds."""
    chosen: List[Scored] = []
    used = 0.0
    for c in rank(candidates):
        if c.score < min_score:
            continue
        length = (c.end - c.start) + pad_before + pad_after
        if used and used + length > target_s * 1.1:
            continue          # try shorter clips that still fit
        chosen.append(c)
        used += length
        if used >= target_s * 0.95:
            break
    return build_up_order(chosen)


def score_result(result, streaks=None, include_singles: bool = True) -> List[Scored]:
    """Score every streak (and kill outside streaks) of an analysis result."""
    import os
    streaks = result.streaks if streaks is None else streaks
    out: List[Scored] = []
    for s in streaks:
        ks = [k for k in result.kills if k.streak_id == s.id]
        sc, why = score_clip(ks, result.events, s.start, s.end)
        out.append(Scored(result.video, f"s{s.start:.3f}", s.start, s.end, s.kills, sc, why,
                          [(k.time, k.count) for k in ks], os.path.splitext(os.path.basename(result.video))[0]))
    if include_singles:
        for k in result.kills:
            if k.streak_id < 0:
                sc, why = score_clip([k], result.events)
                out.append(Scored(result.video, f"k{k.time:.3f}", k.time, k.time, k.count, sc, why,
                                  [(k.time, k.count)], os.path.splitext(os.path.basename(result.video))[0]))
    return out
