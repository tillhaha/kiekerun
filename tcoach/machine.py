"""Machine limits and workout validation.

Limits for the adidas T-800 High Incline are taken from retailer listings
(up to 22 km/h, -6..40 % incline, 9 km/h cap above 20 %).
They are NOT verified against the manual - check and adjust LIMITS if needed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Workout

# Reported over Bluetooth (FTMS ranges): speed 1-22 km/h, incline -6..40 %.
# Above 20 % incline the retailer lists a 9 km/h speed cap. The machine itself does not
# distinguish run and climb modes, so limits depend on the incline only.
LIMITS = {"speed": (1.0, 22.0), "incline": (-6.0, 40.0), "steep_above_pct": 20.0, "steep_max_speed": 9.0}

MAX_SPEED_JUMP_KMH = 6.0     # warn above this change between consecutive blocks
MAX_INCLINE_JUMP_PCT = 15.0  # same for incline (normal interval steps stay below)


@dataclass
class Issue:
    severity: str  # "error" | "warn"
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.message}"


def validate(workout: Workout, limits: dict | None = None) -> list[Issue]:
    lim = limits or LIMITS
    issues: list[Issue] = []
    blocks = workout.flat()
    prev = None
    elapsed = 0
    for i, b in enumerate(blocks, start=1):
        where = f"block {i} '{b.label}' (t={elapsed}s)"
        lo, hi = lim["speed"]
        if not lo <= b.speed_kmh <= hi:
            issues.append(Issue("error", f"{where}: speed {b.speed_kmh} km/h outside {lo}-{hi}"))
        lo, hi = lim["incline"]
        if not lo <= b.incline_pct <= hi:
            issues.append(Issue("error", f"{where}: incline {b.incline_pct}% outside {lo}-{hi}"))
        if b.incline_pct > lim["steep_above_pct"] and b.speed_kmh > lim["steep_max_speed"]:
            issues.append(Issue("error", f"{where}: above {lim['steep_above_pct']:g}% incline speed is limited to {lim['steep_max_speed']:g} km/h"))
        if prev is not None:
            if abs(b.speed_kmh - prev.speed_kmh) > MAX_SPEED_JUMP_KMH:
                issues.append(Issue("warn", f"{where}: speed jump {prev.speed_kmh} -> {b.speed_kmh} km/h"))
            if abs(b.incline_pct - prev.incline_pct) > MAX_INCLINE_JUMP_PCT:
                issues.append(Issue("warn", f"{where}: incline jump {prev.incline_pct} -> {b.incline_pct}%"))
        elapsed += b.duration_s
        prev = b
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
