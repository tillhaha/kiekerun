"""Outdoor workouts (intervals.icu native text, HR targets) -> treadmill workouts.

An outdoor step line looks like intervals.icu's own syntax:
  - Easy 60m Z2 HR            (HR zone 2)
  - 10m Z1                    (a bare zone is read as HR zone)
  - Tempo 20m 85-92% LTHR     (percent of lactate-threshold HR)
Repeat groups use the same '5x' header line as the treadmill format.

The treadmill speed for a target HR comes from the athlete's own recent runs: HR and speed of flat
samples are smoothed and fitted with a straight line (speed = a + b * HR), then evaluated at the
middle of the target band. Without enough recent data the caller gets None and must warn.
"""
from __future__ import annotations

import re
import statistics
from datetime import date, timedelta

from .models import Block, Repeat, Workout

DEFAULT_INCLINE = 1.0     # % on the treadmill to roughly match outdoor effort at flat speed
MIN_SAMPLES = 300         # seconds of usable data
MIN_HR_SPREAD = 8         # bpm between 10th and 90th percentile, otherwise the slope is meaningless
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}

DUR = r"(?P<dur>(?:\d+h)?(?:\d+m)?(?:\d+s)?)"
STEP_RE = re.compile(
    rf"^-\s*(?:(?P<label>.*?)\s+)?{DUR}\s+(?:(?:Z(?P<zone>[1-7])(?:\s*HR)?)|(?:(?P<lo>\d{{2,3}})(?:\s*-\s*(?P<hi>\d{{2,3}}))?\s*%\s*LTHR))"
    r"\s*$",
    re.I,
)
REPEAT_RE = re.compile(r"^(?P<label>.*?)\s*(?P<n>\d+)x\s*$", re.I)


def is_outdoor_text(desc: str) -> bool:
    return any(STEP_RE.match(l.strip()) for l in (desc or "").splitlines() if l.strip().startswith("-"))


def _secs(tok: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", tok or "")
    if not m or not any(m.groups()):
        raise ValueError(f"bad duration {tok!r}")
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def zone_band(zone: int, hr_zones: list[float]) -> tuple[float, float]:
    """(low, high) bpm of a zone; hr_zones = upper limits of zones 1..5 (+ more)."""
    zone = min(zone, len(hr_zones))
    hi = hr_zones[zone - 1]
    lo = hr_zones[zone - 2] + 1 if zone > 1 else max(60, round(hi * 0.65))
    return float(lo), float(hi)


def parse_outdoor(text: str, hr_zones: list[float], lthr: float | None):
    """Parse outdoor step lines. Returns (steps, warnings); a step is
    {'label','duration_s','lo','hi','zone'} or {'repeat','label','blocks'}."""
    steps: list[dict] = []
    warnings: list[str] = []
    rep: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            rep = None
            continue
        if line.startswith("-"):
            m = STEP_RE.match(line)
            if not m:
                warnings.append(f"unparsed step: {line}")
                continue
            if m.group("zone"):
                z = int(m.group("zone"))
                lo, hi = zone_band(z, hr_zones)
                label = (m.group("label") or f"Z{z}").strip()
                if f"z{z}" not in label.lower():
                    label = f"{label} Z{z}"
            else:
                if not lthr:
                    warnings.append(f"no LTHR configured, cannot convert: {line}")
                    continue
                lo = float(m.group("lo")) / 100 * lthr
                hi = float(m.group("hi") or m.group("lo")) / 100 * lthr
                z = None
                label = (m.group("label") or f"{m.group('lo')}-{m.group('hi') or m.group('lo')}% LTHR").strip()
            st = {"label": label, "duration_s": _secs(m.group("dur")), "lo": lo, "hi": hi, "zone": z}
            (rep["blocks"] if rep else steps).append(st)
            continue
        rm = REPEAT_RE.match(line)
        if rm:
            rep = {"repeat": int(rm.group("n")), "label": rm.group("label") or "Repeat", "blocks": []}
            steps.append(rep)
        # other text lines (notes) are ignored
    return [s for s in steps if "repeat" not in s or s["blocks"]], warnings


# --- HR -> speed model -------------------------------------------------------------------------

def _roll(xs: list[float], k: int) -> list[float]:
    out, acc = [], 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= k:
            acc -= xs[i - k]
        out.append(acc / min(i + 1, k))
    return out


def samples_from_streams(streams, warmup_s: int = 300, max_grade: float = 1.5, win: int = 45) -> list[tuple[float, float]]:
    """(hr, speed_kmh) pairs of flat, moving, post-warm-up seconds, HR and speed smoothed over win seconds."""
    by = {s.get("type"): s.get("data") or [] for s in (streams or []) if isinstance(s, dict)}
    hr, v, g = by.get("heartrate") or [], by.get("velocity_smooth") or [], by.get("grade_smooth") or []
    n = min(len(hr), len(v))
    if n < warmup_s + win:
        return []
    ok = [isinstance(x, (int, float)) and x > 0 for x in hr[:n]]
    hrs = _roll([float(x) if o else 0.0 for x, o in zip(hr[:n], ok)], win)
    vs = _roll([float(x) * 3.6 if isinstance(x, (int, float)) else 0.0 for x in v[:n]], win)
    pts = []
    for i in range(warmup_s, n):
        gr = g[i] if i < len(g) and isinstance(g[i], (int, float)) else 0.0
        if ok[i] and abs(gr) <= max_grade and vs[i] >= 4.0 and hrs[i] > 0:
            pts.append((hrs[i], vs[i]))
    return pts


def fit(points: list[tuple[float, float]]):
    """Least squares speed = a + b*hr. Returns dict or None when the data cannot support it."""
    if len(points) < MIN_SAMPLES:
        return None
    xs = sorted(p[0] for p in points)
    spread = xs[int(len(xs) * 0.9)] - xs[int(len(xs) * 0.1)]
    if spread < MIN_HR_SPREAD:
        return None
    mx = statistics.fmean(p[0] for p in points)
    my = statistics.fmean(p[1] for p in points)
    sxx = sum((p[0] - mx) ** 2 for p in points)
    b = sum((p[0] - mx) * (p[1] - my) for p in points) / sxx
    if b <= 0:
        return None
    return {"a": my - b * mx, "b": b, "n": len(points), "hr_range": [xs[0], xs[-1]]}


def build_model(client, today: str, days: int = 42, max_activities: int = 10):
    """Fit the HR->speed line from the most recent runs; None if not enough usable data."""
    oldest = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    acts = client.list_activities(oldest, today)
    runs = [a for a in acts if a.get("type") in RUN_TYPES and (a.get("distance") or 0) >= 3000]
    runs.sort(key=lambda a: a.get("start_date_local") or "", reverse=True)
    pts: list[tuple[float, float]] = []
    used = 0
    for a in runs[:max_activities]:
        try:
            streams = client.get_streams(str(a["id"]), "time,heartrate,velocity_smooth,grade_smooth")
        except Exception:  # one broken activity must not hide the others
            continue
        p = samples_from_streams(streams)
        if p:
            pts += p
            used += 1
    m = fit(pts)
    if m:
        m["activities"] = used
    return m


def speed_for_hr(model: dict, hr: float) -> float:
    lo, hi = model["hr_range"]
    hr = min(max(hr, lo - 12), hi + 12)  # do not extrapolate far outside the HR range seen in recent runs
    return round(max(3.0, model["a"] + model["b"] * hr), 1)


def to_treadmill(name: str, steps: list[dict], model: dict, incline: float = DEFAULT_INCLINE) -> Workout:
    def blk(s):
        mid = (s["lo"] + s["hi"]) / 2
        return Block(label=s["label"], duration_s=s["duration_s"], speed_kmh=speed_for_hr(model, mid), incline_pct=incline)

    out = []
    for s in steps:
        out.append(Repeat(count=s["repeat"], blocks=[blk(b) for b in s["blocks"]], label=s["label"]) if "repeat" in s else blk(s))
    return Workout(name=name, steps=out, notes="Outdoor workout converted to treadmill speeds from recent runs (estimate, adjust by feel).")
