"""Compact, unit-friendly views of intervals.icu data for the coach (read-only).

intervals.icu returns hundreds of fields per activity; the coach needs a few dozen.
Everything here is pure (no network) so it can be tested with plain dicts.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

MAX_RANGE_DAYS = 400
MAX_DESC = 600

# fields requested from /activities (keeps the payload small)
ACTIVITY_FIELDS = ",".join([
    "id", "start_date_local", "name", "type", "device_name", "source", "trainer", "race", "sub_type", "tags",
    "moving_time", "elapsed_time", "distance", "total_elevation_gain", "total_elevation_loss", "average_speed",
    "average_heartrate", "max_heartrate", "calories", "icu_training_load", "hr_load", "trimp", "icu_intensity",
    "icu_rpe", "feel", "carbs_ingested", "description", "paired_event_id", "decoupling", "icu_efficiency_factor",
    "average_cadence", "icu_ctl", "icu_atl", "interval_summary", "gap", "average_vertical_speed", "kg_lifted"])


def parse_day(v, name: str) -> date:
    try:
        return datetime.strptime(str(v), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD")


def resolve_range(args: dict, today: str, default_days: int = 14) -> tuple[str, str]:
    t = parse_day(today, "today")
    newest = parse_day(args["newest"], "newest") if args.get("newest") else t
    oldest = parse_day(args["oldest"], "oldest") if args.get("oldest") else newest - timedelta(days=default_days - 1)
    if oldest > newest:
        raise ValueError("oldest must not be after newest")
    if (newest - oldest).days > MAX_RANGE_DAYS:
        raise ValueError(f"range too long (max {MAX_RANGE_DAYS} days)")
    return oldest.isoformat(), newest.isoformat()


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _pace(distance_m, time_s) -> str | None:
    if not distance_m or not time_s or distance_m < 50:
        return None
    sec = round(time_s / (distance_m / 1000.0))
    return f"{sec // 60}:{sec % 60:02d}"


def _r(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) else None


def summarize_activity(a: dict) -> dict:
    dist, mt = a.get("distance"), a.get("moving_time")
    desc = (a.get("description") or "").strip()
    return _clean({
        "id": a.get("id"),
        "date": (a.get("start_date_local") or "")[:16].replace("T", " "),
        "name": a.get("name"),
        "type": a.get("type"),
        "indoor": True if a.get("trainer") else None,
        "race": True if a.get("race") else None,
        "source": a.get("device_name") or a.get("source"),
        "duration_min": _r(mt / 60.0) if mt else None,
        "elapsed_min": _r(a["elapsed_time"] / 60.0) if a.get("elapsed_time") else None,
        "distance_km": _r(dist / 1000.0, 2) if dist else None,
        "pace_min_km": _pace(dist, mt),
        "elevation_gain_m": _r(a.get("total_elevation_gain"), 0),
        "elevation_loss_m": _r(a.get("total_elevation_loss"), 0),
        "avg_hr": _r(a.get("average_heartrate"), 0),
        "max_hr": _r(a.get("max_heartrate"), 0),
        "calories": a.get("calories"),
        "training_load": a.get("icu_training_load"),
        "hr_load": a.get("hr_load"),
        "intensity": _r(a.get("icu_intensity")),
        "decoupling_pct": _r(a.get("decoupling")),
        "cadence": _r(a.get("average_cadence"), 0),
        "vertical_speed_m_h": _r(a["average_vertical_speed"] * 3600, 0) if a.get("average_vertical_speed") else None,
        "ctl_after": _r(a.get("icu_ctl")),
        "atl_after": _r(a.get("icu_atl")),
        "rpe": a.get("icu_rpe"),
        "feel": a.get("feel"),
        "carbs_g": a.get("carbs_ingested"),
        "kg_lifted": a.get("kg_lifted"),
        "tags": a.get("tags"),
        "paired_event_id": a.get("paired_event_id"),
        "description": desc[:MAX_DESC] + ("..." if len(desc) > MAX_DESC else "") or None,
    })


def summarize_interval(i: dict) -> dict:
    mt = i.get("moving_time") or i.get("elapsed_time")
    dist = i.get("distance")
    return _clean({
        "type": i.get("type"), "label": i.get("label"),
        "start_s": i.get("start_time"), "duration_s": mt, "distance_m": _r(dist, 0),
        "pace_min_km": _pace(dist, mt),
        "avg_hr": _r(i.get("average_heartrate"), 0), "max_hr": _r(i.get("max_heartrate"), 0),
        "elevation_gain_m": _r(i.get("total_elevation_gain"), 0),
    })


def summarize_activity_detail(a: dict) -> dict:
    out = summarize_activity(a)
    ivs = a.get("icu_intervals") or []
    if ivs:
        out["intervals"] = [summarize_interval(i) for i in ivs][:80]
    if a.get("interval_summary"):
        out["interval_summary"] = a["interval_summary"]
    zt = a.get("icu_hr_zone_times")
    if zt:
        out["hr_zone_times_s"] = zt
    return out


def summarize_wellness(w: dict) -> dict:
    ctl, atl = w.get("ctl"), w.get("atl")
    sleep = w.get("sleepSecs")
    return _clean({
        "date": w.get("id"),
        "ctl": _r(ctl), "atl": _r(atl),
        "form_tsb": _r(ctl - atl) if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)) else None,
        "ramp_rate": _r(w.get("rampRate")),
        "hrv": _r(w.get("hrv")), "resting_hr": w.get("restingHR"),
        "sleep_h": _r(sleep / 3600.0) if sleep else None, "sleep_score": w.get("sleepScore"),
        "readiness": w.get("readiness"), "vo2max": _r(w.get("vo2max")), "weight_kg": _r(w.get("weight")),
        "steps": w.get("steps"), "stress": w.get("stress"),
        "soreness": w.get("soreness"), "fatigue": w.get("fatigue"), "mood": w.get("mood"),
        "motivation": w.get("motivation"), "injury": w.get("injury"), "comments": w.get("comments"),
    })


def plan_vs_actual(events: list[dict], activities: list[dict], today: str) -> list[dict]:
    """One row per day: what was planned and what was done. 'paired' means intervals.icu
    linked the activity to the planned event; an unpaired activity on the same day may
    still be that workout (e.g. a Garmin run that was not paired automatically)."""
    days: dict[str, dict] = {}

    def day(d):
        return days.setdefault(d, {"date": d, "planned": [], "activities": []})

    acts_by_id = {}
    for a in activities:
        s = summarize_activity(a)
        d = (a.get("start_date_local") or "")[:10]
        if d:
            day(d)["activities"].append(s)
            acts_by_id[a.get("id")] = s
    paired = {a.get("paired_event_id"): a.get("id") for a in activities if a.get("paired_event_id")}
    for e in events:
        d = (e.get("start_date_local") or "")[:10]
        if not d:
            continue
        desc = (e.get("description") or "").strip()
        row = _clean({
            "event_id": e.get("id"), "category": e.get("category"), "name": e.get("name"), "type": e.get("type"),
            "planned_min": _r(e["moving_time"] / 60.0) if e.get("moving_time") else None,
            "planned_km": _r(e["distance"] / 1000.0, 1) if e.get("distance") else None,
            "planned_load": e.get("icu_training_load"),
            "description": desc[:MAX_DESC] + ("..." if len(desc) > MAX_DESC else "") or None,
        })
        if e.get("category") == "WORKOUT":
            if e.get("id") in paired:
                row["status"] = "done"
                row["activity_id"] = paired[e.get("id")]
            elif d >= today:
                row["status"] = "open"
            else:
                row["status"] = "no paired activity"
        day(d)["planned"].append(row)
    return [_clean(days[k]) | {"date": k} for k in sorted(days)]
