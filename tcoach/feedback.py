"""Workout feedback from the runner page + per-block statistics + activity description.

Log extension written by the runner page:
  "feedback": {"rpe": 1-10, "feel": 1-5, "note": "...", "carbs": [[t_seconds, grams], ...]}
  "blocks":   [{"label": "...", "t0": seconds, "dur": seconds}, ...]   (the planned blocks)

Per-block vertical speed is computed, not measured: belt distance * sin(atan(incline)) per
second, divided by the time in the block. It needs no treadmill data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import climb_rise_m, fmt_duration
from .session import Sample

MAX_NOTE = 2000
MAX_CARBS_ENTRY_G = 300


@dataclass
class Feedback:
    rpe: int | None = None
    feel: int | None = None
    note: str = ""
    carbs: list[tuple[int, int]] = field(default_factory=list)  # (t_seconds, grams)

    @property
    def carbs_total(self) -> int:
        return sum(g for _, g in self.carbs)

    def is_empty(self) -> bool:
        return self.rpe is None and self.feel is None and not self.note and not self.carbs


def _int_in(v, lo, hi, name):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not lo <= v <= hi:
        raise ValueError(f"{name} must be a whole number {lo}-{hi}")
    return int(v)


def parse_feedback(raw) -> Feedback:
    """Validate the log's 'feedback' object. Raises ValueError on bad input."""
    if raw in (None, {}):
        return Feedback()
    if not isinstance(raw, dict):
        raise ValueError("feedback must be an object")
    note = raw.get("note") or ""
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError(f"note must be text of at most {MAX_NOTE} characters")
    carbs = []
    for item in raw.get("carbs") or []:
        try:
            t, g = item
        except (TypeError, ValueError):
            raise ValueError("carbs entries must be [seconds, grams]")
        carbs.append((_int_in(t, 0, 10**6, "carbs time"), _int_in(g, 1, MAX_CARBS_ENTRY_G, "carbs grams")))
    return Feedback(rpe=_int_in(raw.get("rpe"), 1, 10, "rpe"), feel=_int_in(raw.get("feel"), 1, 5, "feel"),
                    note=note.strip(), carbs=carbs)


@dataclass
class BlockStat:
    label: str
    start_s: int
    dur_s: int
    avg_speed_kmh: float
    avg_incline_pct: float
    ascent_m: float
    vam_m_h: float
    avg_hr: int | None


def block_stats(samples: list[Sample], blocks: list[dict]) -> list[BlockStat]:
    """Statistics for the blocks that were actually run (blocks shorter than 5 s are skipped)."""
    out = []
    for b in blocks or []:
        t0, dur = int(b["t0"]), int(b["dur"])
        win = [s for s in samples if t0 <= s.t < t0 + dur]
        if len(win) < 5:
            continue
        ascent = sum(climb_rise_m(s.speed_kmh / 3.6, s.incline_pct) for s in win if s.incline_pct > 0)
        hrs = [s.hr for s in win if s.hr is not None]
        out.append(BlockStat(
            label=str(b.get("label") or "Block"), start_s=t0, dur_s=len(win),
            avg_speed_kmh=sum(s.speed_kmh for s in win) / len(win),
            avg_incline_pct=sum(s.incline_pct for s in win) / len(win),
            ascent_m=ascent, vam_m_h=ascent / (len(win) / 3600.0),
            avg_hr=int(round(sum(hrs) / len(hrs))) if hrs else None))
    return out


def build_description(fb: Feedback, stats: list[BlockStat], duration_s: int, kcal: int | None) -> str:
    """Text for the intervals.icu activity: the note first, then computed facts."""
    parts = []
    if fb.note:
        parts.append(fb.note)
    climbs = [b for b in stats if b.avg_incline_pct > 0.5 and b.ascent_m >= 1]
    if climbs:
        lines = ["Vertical speed per block"]
        for b in climbs:
            hr = f" · HR {b.avg_hr}" if b.avg_hr else ""
            lines.append(f"- {b.label} · {fmt_duration(b.dur_s)} · {b.avg_speed_kmh:.1f} km/h @ {b.avg_incline_pct:.0f}% "
                         f"-> {b.vam_m_h:.0f} m/h (+{b.ascent_m:.0f} m){hr}")
        parts.append("\n".join(lines))
    facts = []
    if fb.carbs_total:
        per_h = f" ({fb.carbs_total / (duration_s / 3600.0):.0f} g/h)" if duration_s >= 600 else ""
        facts.append(f"Carbs {fb.carbs_total} g{per_h}")
    if kcal:
        facts.append(f"Calories ~{kcal} kcal (estimate)")
    if facts:
        parts.append(" · ".join(facts))
    return "\n\n".join(parts)
