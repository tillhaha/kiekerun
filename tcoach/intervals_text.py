"""Convert workouts to/from intervals.icu workout text.

Format (syntax verified against the intervals.icu forum guides):
  - steps start with '- ', duration like 10m / 1m30 / 45s, absolute pace like 6:00/km Pace
  - a line like 'Main set 5x' before a block of steps makes a repeat group
  - all text before the duration becomes the step text

intervals.icu has no incline target, so incline is written into the step text as
'incline 10pct' (the '%' sign is avoided on purpose: it would be read as a % of
threshold target). This text format is round-trippable by parse_intervals_text().
"""
from __future__ import annotations

import re

from .models import Block, Repeat, Workout, parse_duration

MODE_LINE = re.compile(r"^treadmill mode:\s*(run|climb)\b", re.I)
STEP_RE = re.compile(
    r"^-\s*(?P<label>.*?)\s*(?:mode\s+(?P<mode>run|climb)\s+)?"
    r"incline\s+(?P<inc>-?\d+(?:\.\d+)?)pct\s+(?P<dur>\S+)\s+(?P<pace>\d+:\d{2})/km\s+Pace\s*$",
    re.I,
)
REPEAT_RE = re.compile(r"^(?P<label>.*?)\s*(?P<n>\d+)x\s*$", re.I)


def pace_str(speed_kmh: float) -> str:
    sec = round(3600.0 / speed_kmh)
    return f"{sec // 60}:{sec % 60:02d}"


def speed_from_pace(pace: str) -> float:
    m, s = pace.split(":")
    return round(3600.0 / (int(m) * 60 + int(s)), 1)


def dur_str(secs: int) -> str:
    if secs % 60 == 0:
        return f"{secs // 60}m"
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}"


def _pct(v: float) -> str:
    return f"{v:g}"


def _step_line(b: Block) -> str:
    return f"- {b.label} incline {_pct(b.incline_pct)}pct {dur_str(b.duration_s)} {pace_str(b.speed_kmh)}/km Pace"


def to_intervals_text(workout: Workout) -> str:
    lines = []
    if workout.notes:
        lines.append(workout.notes)
    lines.append("")
    for step in workout.steps:
        if isinstance(step, Repeat):
            lines.append(f"{step.label} {step.count}x")
            lines.extend(_step_line(b) for b in step.blocks)
        else:
            lines.append(_step_line(step))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_intervals_text(text: str, name: str = "Treadmill workout", mode: str = "run"):
    """Parse text produced by to_intervals_text(). Returns (Workout, warnings)."""
    warnings: list[str] = []
    steps = []
    current_repeat: Repeat | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            current_repeat = None
            continue
        mm = MODE_LINE.match(line)
        if mm:
            mode = mm.group(1).lower()
            continue
        if line.startswith("-"):
            m = STEP_RE.match(line)
            if not m:
                warnings.append(f"unparsed step: {line}")
                continue
            b = Block(
                label=m.group("label") or "Step",
                duration_s=parse_duration(m.group("dur")),
                speed_kmh=speed_from_pace(m.group("pace")),
                incline_pct=float(m.group("inc")),
                mode=(m.group("mode") or "").lower() or None,
            )
            if current_repeat is not None:
                current_repeat.blocks.append(b)
            else:
                steps.append(b)
            continue
        rm = REPEAT_RE.match(line)
        if rm:
            current_repeat = Repeat(count=int(rm.group("n")), blocks=[], label=rm.group("label") or "Repeat")
            steps.append(current_repeat)
        else:
            warnings.append(f"ignored line: {line}")
    steps = [s for s in steps if not isinstance(s, Repeat) or s.blocks]
    if not steps:
        raise ValueError("no parsable steps found")
    return Workout(name=name, steps=steps, mode=mode), warnings
