"""Convert workouts to/from intervals.icu workout text.

Step line (what to_intervals_text writes, one per block):
  - <description> <time> @ <X.X> km/h (<MM:SS> min/km) @ <X>% incline
  e.g.  - Steady Vertical 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline

A line like 'Main set 5x' before a block of steps makes a repeat group. Speed always uses a
decimal point, pace is derived from the speed (so it can never disagree with it).

intervals.icu does NOT understand this line as a structured step (verified live: no duration,
distance or load is derived from it). That is the price of a readable, treadmill-oriented text,
so the calendar entry carries duration and distance as separate fields (see build_event).
The older format  '- <label> incline 10pct 10m 6:00/km Pace'  is still read by parse_intervals_text()
(intervals.icu does parse that one), but is no longer written.
"""
from __future__ import annotations

import re

from .models import Block, Fuel, Repeat, Workout, fmt_clock, parse_duration

MODE_LINE = re.compile(r"^treadmill mode:\s*(run|climb)\b", re.I)
STEP_RE = re.compile(  # legacy format
    r"^-\s*(?P<label>.*?)\s*(?:mode\s+(?P<mode>run|climb)\s+)?"
    r"incline\s+(?P<inc>-?\d+(?:\.\d+)?)pct\s+(?P<dur>\S+)\s+(?P<pace>\d+:\d{2})/km\s+Pace\s*$",
    re.I,
)
NUM = r"\d+(?:[.,]\d+)?"
STEP_NEW_RE = re.compile(
    rf"^-\s*(?P<label>.*?)\s+(?P<dur>\d+(?::\d{{2}})?)\s*(?P<unit>min|s)\s*@\s*(?P<speed>{NUM})\s*km/h\s*"
    rf"(?:\(\s*\d+:\d{{2}}\s*min/km\s*\)\s*)?@\s*(?P<inc>-?{NUM})\s*%\s*incline"
    rf"(?:\s+mode\s+(?P<mode>run|climb))?\s*$",
    re.I,
)
FUEL_HEAD = re.compile(r"^fuel plan:?$", re.I)
FUEL_RE = re.compile(
    r"^@?\s*(?P<at>\d+(?::\d{2}){1,2})\s*[·|,]?\s*(?:(?P<fl>\d+(?:\.\d+)?)\s*(?P<kind>gel|drink)-?flasks?)?\s*[·|,]?\s*(?:(?P<c>\d+)\s*g(?:\s*carbs?)?)?\s*[·|,]?\s*"
    r"(?:(?P<f>\d+)\s*ml)?\s*[·|,]?\s*(?P<note>.*)$", re.I)
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


def minutes_str(secs: int) -> str:
    """'30 min', '1:30 min' (90 s), '45 s'."""
    if secs < 60:
        return f"{secs} s"
    if secs % 60 == 0:
        return f"{secs // 60} min"
    return f"{secs // 60}:{secs % 60:02d} min"


def _step_line(b: Block) -> str:
    mode = f" mode {b.mode}" if b.mode else ""
    return (f"- {b.label} {minutes_str(b.duration_s)} @ {b.speed_kmh:.1f} km/h ({pace_str(b.speed_kmh)} min/km) "
            f"@ {_pct(b.incline_pct)}% incline{mode}")


def _new_duration(dur: str, unit: str) -> int:
    if unit.lower() == "s":
        return int(dur)
    if ":" in dur:
        m, s = dur.split(":")
        return int(m) * 60 + int(s)
    return int(dur) * 60


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
    if workout.fuel:
        lines.append("Fuel plan")
        for f in workout.fuel:
            parts = [f"@ {fmt_clock(f.at_s)}"]
            if f.kind:
                parts.append(f"{f.flask:g} {f.kind.capitalize()}-Flask")
            if f.carbs_g:
                parts.append(f"{f.carbs_g} g carbs")
            if f.fluid_ml:
                parts.append(f"{f.fluid_ml} ml")
            if f.note:
                parts.append(f.note)
            lines.append(" · ".join(parts))
    return "\n".join(lines).rstrip() + "\n"


def parse_intervals_text(text: str, name: str = "Treadmill workout", mode: str = "run"):
    """Parse text produced by to_intervals_text() (new or legacy step format). Returns (Workout, warnings)."""
    warnings: list[str] = []
    steps = []
    current_repeat: Repeat | None = None
    fuel: list[Fuel] = []
    in_fuel = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            current_repeat = None
            in_fuel = False
            continue
        if FUEL_HEAD.match(line):
            in_fuel = True
            continue
        if in_fuel:
            fm = FUEL_RE.match(line)
            if fm and (fm.group("c") or fm.group("f") or fm.group("note") or fm.group("kind")):
                kind = (fm.group("kind") or "").lower()
                fuel.append(Fuel(parse_duration(fm.group("at")), int(fm.group("c") or 0), int(fm.group("f") or 0),
                                 fm.group("note").strip(), kind, float(fm.group("fl")) if kind else 0.0))
            else:
                warnings.append(f"unparsed fuel line: {line}")
            continue
        mm = MODE_LINE.match(line)
        if mm:
            mode = mm.group(1).lower()
            continue
        if line.startswith("-"):
            m = STEP_NEW_RE.match(line)
            if m:
                b = Block(
                    label=m.group("label") or "Step",
                    duration_s=_new_duration(m.group("dur"), m.group("unit")),
                    speed_kmh=float(m.group("speed").replace(",", ".")),
                    incline_pct=float(m.group("inc").replace(",", ".")),
                    mode=(m.group("mode") or "").lower() or None,
                )
            else:
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
    return Workout(name=name, steps=steps, mode=mode, fuel=sorted(fuel, key=lambda f: f.at_s)), warnings
