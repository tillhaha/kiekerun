"""Session samples (1 Hz) from a workout or from a runner log, plus totals."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import Block, climb_rise_m


@dataclass
class Sample:
    t: int                 # seconds since start
    speed_kmh: float
    incline_pct: float
    hr: int | None = None


def samples_from_blocks(blocks: list[Block]) -> list[Sample]:
    out: list[Sample] = []
    t = 0
    for b in blocks:
        for _ in range(b.duration_s):
            out.append(Sample(t, b.speed_kmh, b.incline_pct))
            t += 1
    return out


def _g(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return float(d[k])
    raise ValueError(f"missing one of {keys} in {d}")


def samples_from_log(log: dict) -> list[Sample]:
    """Runner log: {"duration_s": N, "events": [{"t": 0, "speed": 5.0, "incline": 10}, ...]}.
    Each event sets speed/incline from second t until the next event.
    Optional "hr": [[t, bpm], ...] is attached as heart rate."""
    duration = int(log["duration_s"])
    events = sorted(log["events"], key=lambda e: e["t"])
    if not events:
        raise ValueError("log has no events")
    out: list[Sample] = []
    idx = 0
    cur = events[0]
    for t in range(duration):
        while idx + 1 < len(events) and events[idx + 1]["t"] <= t:
            idx += 1
            cur = events[idx]
        out.append(Sample(t, _g(cur, "speed", "speed_kmh"), _g(cur, "incline", "incline_pct")))
    if log.get("hr"):  # runner log carries [[t, bpm], ...] from the strap
        attach_hr(out, {int(t): int(b) for t, b in log["hr"]})
    return out


def load_log(path) -> dict:
    return json.loads(Path(path).read_text())


def attach_hr(samples: list[Sample], hr_by_t: dict[int, int]) -> None:
    """Fill hr from a {second: bpm} mapping (nearest earlier value is carried forward)."""
    last = None
    for s in samples:
        last = hr_by_t.get(s.t, last)
        s.hr = last


def load_hr_csv(path) -> dict[int, int]:
    """CSV with columns t,hr (seconds from start, bpm); header optional."""
    out: dict[int, int] = {}
    for line in Path(path).read_text().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].replace(".", "", 1).isdigit():
            continue
        out[int(float(parts[0]))] = int(float(parts[1]))
    return out


def altitude_profile(samples: list[Sample], start_altitude_m: float = 0.0) -> list[float]:
    """Altitude (m) at the end of each sample second."""
    alt = start_altitude_m
    out = []
    for s in samples:
        alt += climb_rise_m(s.speed_kmh / 3.6, s.incline_pct)
        out.append(alt)
    return out


def totals(samples: list[Sample]) -> dict:
    dist = up = down = 0.0
    for s in samples:
        step = s.speed_kmh / 3.6
        dist += step
        rise = climb_rise_m(step, s.incline_pct)
        if rise >= 0:
            up += rise
        else:
            down -= rise
    return {"duration_s": len(samples), "distance_m": dist, "ascent_m": up, "descent_m": down}
