"""Workout model: blocks with speed + incline, optional repeat groups.

JSON schema (all keys short on purpose, so a coach/LLM can emit it easily):

{
  "name": "Uphill threshold 4x8",
  "mode": "run",                      # "run" | "climb" (T-800 modes), default for all blocks
  "date": "2026-10-20",               # optional
  "notes": "free text",
  "fuel": [                           # optional nutrition plan, times in workout time (pauses don't count)
    {"at": "20:00", "kind": "gel", "flask": 1, "note": "V4-Mix"},       # amount = fraction of one FULL flask
    {"every": "20:00", "kind": "drink", "flask": 0.5, "note": "V3-Mix"}  # repeating: from 'from' (default = every) until 'until'
  ],                                  # kind gel: 1 flask = 110 g powder / 150 ml, drink: 1 flask = 37.5 g / 500 ml
                                      # (principles/nutrition.md); carbs_g / fluid_ml are derived, or set explicitly
  "blocks": [
    {"label": "Warm-up", "duration": "10:00", "speed": 8.0, "incline": 2},
    {"repeat": 4, "label": "Main set", "blocks": [
        {"label": "Hard", "duration": "8:00", "speed": 10.5, "incline": 6},
        {"label": "Easy", "duration": "3:00", "speed": 6.0, "incline": 2}]},
    {"label": "Cool-down", "duration": "8:00", "speed": 6.0, "incline": 0}
  ]
}

duration: seconds (int) or "mm:ss", "h:mm:ss", "10m", "1m30", "45s".
speed: km/h.  incline: percent (negative = decline).
A block may carry its own "mode" to override the workout default.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

MODES = ("run", "climb")


def parse_duration(value) -> int:
    """Parse seconds or '10:00' / '1:05:00' / '10m' / '1m30' / '45s' into seconds."""
    if isinstance(value, bool):
        raise ValueError(f"bad duration: {value!r}")
    if isinstance(value, (int, float)):
        secs = int(round(value))
    else:
        s = str(value).strip().lower()
        if not s:
            raise ValueError("empty duration")
        if ":" in s:
            parts = [int(p) for p in s.split(":")]
            if len(parts) == 2:
                secs = parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
            else:
                raise ValueError(f"bad duration: {value!r}")
        else:
            m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", s)
            if not m or not any(m.groups()):
                raise ValueError(f"bad duration: {value!r}")
            h, mi, se = (int(g) if g else 0 for g in m.groups())
            secs = h * 3600 + mi * 60 + se
    if secs <= 0:
        raise ValueError(f"duration must be > 0: {value!r}")
    return secs


def fmt_duration(secs: int) -> str:
    """mm:ss or h:mm:ss for display."""
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _num(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return float(d[k])
    if default is None:
        raise ValueError(f"missing one of {keys} in {d}")
    return default


@dataclass
class Block:
    label: str
    duration_s: int
    speed_kmh: float
    incline_pct: float
    mode: str | None = None  # None = workout default

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        mode = d.get("mode")
        if mode is not None and mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        dur = d.get("duration", d.get("duration_s"))
        if dur is None:
            raise ValueError(f"block without duration: {d}")
        return cls(
            label=str(d.get("label", "Step")),
            duration_s=parse_duration(dur),
            speed_kmh=_num(d, "speed", "speed_kmh"),
            incline_pct=_num(d, "incline", "incline_pct", default=0.0),
            mode=mode,
        )

    def to_dict(self) -> dict:
        d = {
            "label": self.label,
            "duration": fmt_duration(self.duration_s),
            "speed": self.speed_kmh,
            "incline": self.incline_pct,
        }
        if self.mode:
            d["mode"] = self.mode
        return d


@dataclass
class Repeat:
    count: int
    blocks: list[Block]
    label: str = "Repeat"

    @classmethod
    def from_dict(cls, d: dict) -> "Repeat":
        count = int(d["repeat"])
        if count < 1:
            raise ValueError("repeat must be >= 1")
        inner = [Block.from_dict(b) for b in d["blocks"]]
        if not inner:
            raise ValueError("repeat group without blocks")
        return cls(count=count, blocks=inner, label=str(d.get("label", "Repeat")))

    def to_dict(self) -> dict:
        return {"repeat": self.count, "label": self.label, "blocks": [b.to_dict() for b in self.blocks]}


Step = Union[Block, Repeat]


def climb_rise_m(distance_m: float, incline_pct: float) -> float:
    """Vertical metres for belt distance at an incline (belt distance * sin(atan(grade)))."""
    return distance_m * math.sin(math.atan(incline_pct / 100.0))


# One FULL flask, from Tito's nutrition principles: gel = 110 g powder (+~90 ml water) = 150 ml,
# drink = 37.5 g powder + 500 ml water. Powder is ~97-98 % carbs, so g powder ~ g carbs.
FLASK = {"gel": {"carbs_g": 110, "fluid_ml": 150}, "drink": {"carbs_g": 37.5, "fluid_ml": 500}}


@dataclass
class Fuel:
    at_s: int
    carbs_g: int = 0
    fluid_ml: int = 0
    note: str = ""
    kind: str = ""      # "gel" | "drink" | "" (free entry)
    flask: float = 0.0  # fraction of one full flask

    def to_dict(self) -> dict:
        d = {"at": fmt_clock(self.at_s)}
        if self.kind:
            d["kind"] = self.kind
            d["flask"] = self.flask
        if self.carbs_g:
            d["carbs_g"] = self.carbs_g
        if self.fluid_ml:
            d["fluid_ml"] = self.fluid_ml
        if self.note:
            d["note"] = self.note
        return d


def fmt_clock(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _amount(d: dict, key: str, hi: int) -> int:
    v = d.get(key, 0) or 0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= hi:
        raise ValueError(f"fuel {key} must be a number from 0 to {hi}")
    return int(round(v))


def parse_fuel(items, total_s: int) -> list[Fuel]:
    """Expand the 'fuel' list ({at,...} or {every, from?, until?,...}) into sorted single entries."""
    if items in (None, []):
        return []
    if not isinstance(items, list):
        raise ValueError("fuel must be a list")
    out: list[Fuel] = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("fuel entries must be objects")
        carbs, fluid = _amount(it, "carbs_g", 300), _amount(it, "fluid_ml", 2000)
        note = str(it.get("note", "")).strip()[:80]
        kind, flask = str(it.get("kind", "") or "").strip().lower(), 0.0
        if kind:
            if kind not in FLASK:
                raise ValueError(f"fuel kind must be one of: {', '.join(FLASK)}")
            flask = it.get("flask", 1)
            if isinstance(flask, bool) or not isinstance(flask, (int, float)) or not 0 < flask <= 4:
                raise ValueError("fuel flask must be a number above 0 up to 4 (1 = one full flask)")
            flask = round(float(flask), 2)
            carbs = carbs or int(round(flask * FLASK[kind]["carbs_g"]))
            fluid = fluid or int(round(flask * FLASK[kind]["fluid_ml"]))
        if not (carbs or fluid or note):
            raise ValueError("fuel entry needs carbs_g, fluid_ml or note")
        if "every" in it:
            step = parse_duration(it["every"])
            if step < 60:
                raise ValueError("fuel 'every' must be at least 1 minute")
            t = parse_duration(it["from"]) if "from" in it else step
            end = parse_duration(it["until"]) if "until" in it else total_s
            while t <= min(end, total_s):
                out.append(Fuel(t, carbs, fluid, note, kind, flask))
                t += step
        else:
            t = parse_duration(it["at"]) if "at" in it else None
            if t is None:
                raise ValueError("fuel entry needs 'at' or 'every'")
            if t > total_s:
                raise ValueError(f"fuel at {fmt_clock(t)} is after the end of the workout ({fmt_clock(total_s)})")
            out.append(Fuel(t, carbs, fluid, note, kind, flask))
    if len(out) > 60:
        raise ValueError("at most 60 fuel entries")
    return sorted(out, key=lambda f: f.at_s)


@dataclass
class Workout:
    name: str
    steps: list[Step]
    mode: str = "run"
    notes: str = ""
    date: str | None = None
    fuel: list[Fuel] = field(default_factory=list)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")

    # --- construction -----------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "Workout":
        steps: list[Step] = []
        for item in d.get("blocks", []):
            steps.append(Repeat.from_dict(item) if "repeat" in item else Block.from_dict(item))
        if not steps:
            raise ValueError("workout has no blocks")
        w = cls(
            name=str(d.get("name", "Treadmill workout")),
            steps=steps,
            mode=d.get("mode", "run"),
            notes=str(d.get("notes", "")),
            date=d.get("date"),
        )
        w.fuel = parse_fuel(d.get("fuel"), w.duration_s)
        return w

    @classmethod
    def load(cls, path) -> "Workout":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def to_dict(self) -> dict:
        d = {"name": self.name, "mode": self.mode, "blocks": [s.to_dict() for s in self.steps]}
        if self.notes:
            d["notes"] = self.notes
        if self.date:
            d["date"] = self.date
        if self.fuel:
            d["fuel"] = [f.to_dict() for f in self.fuel]
        return d

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    # --- derived data -----------------------------------------------------
    def flat(self) -> list[Block]:
        """Executable block list: repeats expanded, mode filled in on every block."""
        out: list[Block] = []
        for s in self.steps:
            blocks = s.blocks * s.count if isinstance(s, Repeat) else [s]
            for b in blocks:
                out.append(Block(b.label, b.duration_s, b.speed_kmh, b.incline_pct, b.mode or self.mode))
        return out

    @property
    def duration_s(self) -> int:
        return sum(b.duration_s for b in self.flat())

    @property
    def distance_km(self) -> float:
        return sum(b.speed_kmh * b.duration_s / 3600.0 for b in self.flat())

    def ascent_descent_m(self) -> tuple[float, float]:
        up = down = 0.0
        for b in self.flat():
            rise = climb_rise_m(b.speed_kmh / 3.6 * b.duration_s, b.incline_pct)
            if rise >= 0:
                up += rise
            else:
                down += -rise
        return up, down

    @property
    def ascent_m(self) -> float:
        return self.ascent_descent_m()[0]
