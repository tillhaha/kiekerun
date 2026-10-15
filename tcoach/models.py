"""Workout model: blocks with speed + incline, optional repeat groups.

JSON schema (all keys short on purpose, so a coach/LLM can emit it easily):

{
  "name": "Uphill threshold 4x8",
  "mode": "run",                      # "run" | "climb" (T-800 modes), default for all blocks
  "date": "2026-10-20",               # optional
  "notes": "free text",
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


@dataclass
class Workout:
    name: str
    steps: list[Step]
    mode: str = "run"
    notes: str = ""
    date: str | None = None

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
        return cls(
            name=str(d.get("name", "Treadmill workout")),
            steps=steps,
            mode=d.get("mode", "run"),
            notes=str(d.get("notes", "")),
            date=d.get("date"),
        )

    @classmethod
    def load(cls, path) -> "Workout":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def to_dict(self) -> dict:
        d = {"name": self.name, "mode": self.mode, "blocks": [s.to_dict() for s in self.steps]}
        if self.notes:
            d["notes"] = self.notes
        if self.date:
            d["date"] = self.date
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
