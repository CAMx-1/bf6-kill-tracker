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
    types: List[str] = field(default_factory=list)     # one per kill: headshot / gun / explosive / other
    weapons: List[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start

    def main_weapon(self) -> str:
        ws = [w for w in self.weapons if w and w != "other"]
        return max(set(ws), key=ws.count) if ws else ""

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
                          [(k.time, k.count) for k in ks], os.path.splitext(os.path.basename(result.video))[0],
                          [t for k in ks for t in k.type_list()], [w for k in ks for w in k.weapons]))
    if include_singles:
        for k in result.kills:
            if k.streak_id < 0:
                sc, why = score_clip([k], result.events)
                out.append(Scored(result.video, f"k{k.time:.3f}", k.time, k.time, k.count, sc, why,
                                  [(k.time, k.count)], os.path.splitext(os.path.basename(result.video))[0],
                                  k.type_list(), list(k.weapons)))
    return out


# --------------------------------------------------------------------------- montage builder
THEMES = {
    "Everything": None,
    "Headshots": "headshot",
    "Explosives": "explosive",
    "Multi-kills": "multi",
    "Clutch moments": "clutch",          # last squad member alive / squad wipes / long streaks
}
ORDERS = {
    "Build-up (best last)": "buildup",
    "Best first": "best_first",
    "Chronological": "time",
    "Alternate big and small": "alternate",
    "Shuffle": "shuffle",
}


@dataclass
class MontageSpec:
    target_s: float = 60.0
    theme: str = "Everything"
    min_score: float = 0.0
    order: str = "Build-up (best last)"
    max_per_video: int = 0                # 0 = no limit
    no_repeat_weapon: bool = False        # don't put two clips with the same main weapon back to back
    min_kills: int = 1
    seed: int = 0                         # for Shuffle


def matches_theme(c: Scored, theme: str) -> bool:
    key = THEMES.get(theme)
    if key is None:
        return True
    if key in ("headshot", "explosive"):
        return key in c.types
    if key == "multi":
        return any(n > 1 for _, n in c.kill_times)
    if key == "clutch":
        return c.kills >= 5 or any(r in ("last squad member alive", "squad wipe") for r in c.reasons)
    return True


def _order(clips: List[Scored], how: str, seed: int) -> List[Scored]:
    key = ORDERS.get(how, how)
    if key == "best_first":
        return rank(clips)
    if key == "time":
        return sorted(clips, key=lambda c: (c.video, c.start))
    if key == "alternate":
        r = rank(clips)
        big, small = r[:(len(r) + 1) // 2], r[(len(r) + 1) // 2:][::-1]
        out = []
        for i in range(max(len(big), len(small))):
            out += ([big[i]] if i < len(big) else []) + ([small[i]] if i < len(small) else [])
        return out
    if key == "shuffle":
        import random
        out = list(clips)
        random.Random(seed).shuffle(out)
        return out
    return build_up_order(clips)


def _spread_weapons(clips: List[Scored]) -> List[Scored]:
    """Reorder so the same main weapon rarely appears twice in a row (keeps the last clip last)."""
    if len(clips) < 3:
        return list(clips)
    rest, last = list(clips[:-1]), clips[-1]
    out: List[Scored] = []
    while rest:
        prev = out[-1].main_weapon() if out else None
        pick = next((c for c in rest if not prev or c.main_weapon() != prev), rest[0])
        out.append(pick)
        rest.remove(pick)
    return out + [last]


def build_montage(candidates: List[Scored], spec: MontageSpec, pad_before: float, pad_after: float) -> List[Scored]:
    """Pick and order clips for a montage according to ``spec``."""
    pool = [c for c in candidates if matches_theme(c, spec.theme) and c.score >= spec.min_score
            and c.kills >= spec.min_kills]
    chosen: List[Scored] = []
    used = 0.0
    per_video: Dict[str, int] = {}
    for c in rank(pool):
        if spec.max_per_video and per_video.get(c.video, 0) >= spec.max_per_video:
            continue
        length = c.length + pad_before + pad_after
        if used and used + length > spec.target_s * 1.1:
            continue
        chosen.append(c)
        per_video[c.video] = per_video.get(c.video, 0) + 1
        used += length
        if used >= spec.target_s * 0.95:
            break
    out = _order(chosen, spec.order, spec.seed)
    if spec.no_repeat_weapon:
        out = _spread_weapons(out)
    return out


def music_length(path: str) -> float:
    from .video import probe
    try:
        return probe(path).duration
    except Exception:
        return 0.0
