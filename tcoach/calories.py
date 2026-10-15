"""Calorie estimate from speed + incline + body weight (no treadmill data needed).

ACSM metabolic equations (VO2 in ml/kg/min, speed S in m/min, grade G as a fraction):
  walking: 0.1*S + 1.8*S*G + 3.5
  running: 0.2*S + 0.9*S*G + 3.5
The 3.5 is resting metabolism, so the result is gross energy (what a watch calls total
calories). Energy: 1 litre of O2 ~ 5 kcal.

Which equation applies depends on gait, which we cannot see. ACSM uses walking up to
~6 km/h and running from ~8 km/h; in between we blend linearly. Declines (negative
grade) are treated as flat, because the equations have no downhill term.
It is an estimate, typically within 10-15 %.
"""
from __future__ import annotations

from .session import Sample

WALK_MAX_KMH = 6.0
RUN_MIN_KMH = 8.0
KCAL_PER_LITRE_O2 = 5.0


def vo2_ml_kg_min(speed_kmh: float, incline_pct: float) -> float:
    s = speed_kmh * 1000.0 / 60.0
    g = max(incline_pct, 0.0) / 100.0
    walk = 0.1 * s + 1.8 * s * g + 3.5
    run = 0.2 * s + 0.9 * s * g + 3.5
    w = min(1.0, max(0.0, (speed_kmh - WALK_MAX_KMH) / (RUN_MIN_KMH - WALK_MAX_KMH)))
    return (1.0 - w) * walk + w * run


def estimate_kcal(samples: list[Sample], weight_kg: float) -> int:
    """Total gross kcal over 1 Hz samples."""
    if weight_kg <= 0:
        raise ValueError("weight must be > 0")
    kcal = 0.0
    for s in samples:
        litres_per_s = vo2_ml_kg_min(s.speed_kmh, s.incline_pct) * weight_kg / 1000.0 / 60.0
        kcal += litres_per_s * KCAL_PER_LITRE_O2
    return int(round(kcal))
