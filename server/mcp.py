"""Read-only MCP server (Streamable HTTP, stateless JSON) that lets the coach read intervals.icu.

Read tools:  get_activities, get_activity, get_wellness, get_plan_vs_actual.
Write tools (only when MCP_ALLOW_WRITE=1): save_treadmill_workout, save_planned_workout,
save_note, delete_planned_workout, delete_note. They only touch planned WORKOUT entries and
NOTE markers in the calendar; they cannot change activities, wellness data or races, and
delete only works on entries this app created.

Auth is a secret in the URL path (env MCP_TOKEN), because claude.ai's custom connectors take a URL
and cannot send our Basic-Auth login. Anyone with the URL can read your training data (and plan
workouts if writing is on), so treat it like a password and rotate MCP_TOKEN if it leaks.
"""
from __future__ import annotations

import json
import math
import re
from datetime import timedelta
from typing import Callable

from server.ratelimit import RateLimiter
from tcoach.coach_data import (ACTIVITY_FIELDS, SEASON_CATEGORIES, parse_day, plan_vs_actual, resolve_range, summarize_activity,
                               summarize_activity_detail, summarize_season, summarize_wellness, summarize_workout_doc, summarize_zones)
from tcoach.intervals_client import IntervalsClient, IntervalsError, slug
from tcoach.intervals_text import to_intervals_text
from tcoach.machine import has_errors, validate
from tcoach.models import Workout

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LIMIT = 200

_RANGE = {
    "oldest": {"type": "string", "description": "First day, YYYY-MM-DD (default: 14 days before newest)"},
    "newest": {"type": "string", "description": "Last day, YYYY-MM-DD (default: today)"},
}
TOOLS = [
    {"name": "get_activities",
     "description": "Completed activities from intervals.icu (all sources: KiekeRun treadmill uploads, Garmin, anything else), "
                    "newest first. Distance, duration, pace, HR, elevation, load, RPE, feel, carbs, notes, source device.",
     "inputSchema": {"type": "object", "properties": {**_RANGE, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                     "description": "Max activities (default 60)"}}}},
    {"name": "get_activity",
     "description": "One activity in detail incl. intervals/laps and HR zone times. Use the id from get_activities.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "get_wellness",
     "description": "Daily fitness and wellness: CTL, ATL, form (TSB), ramp rate, HRV, resting HR, sleep, readiness, weight, steps.",
     "inputSchema": {"type": "object", "properties": _RANGE}},
    {"name": "get_zones",
     "description": "Training zones as set in intervals.icu per sport: HR zones (upper limits in bpm, last = max HR), LTHR, "
                    "threshold pace, pace and power zones. Use these instead of guessing when writing zone-based workouts.",
     "inputSchema": {"type": "object", "properties": {"sport": {"type": "string", "description": "e.g. Run, Ride (default: all)"}}}},
    {"name": "get_planned_workout",
     "description": "One planned calendar workout as intervals.icu understood it: description plus the structured steps with their "
                    "targets (hr / pace / power / %). Use it after saving to check that a step carries an HR or pace target and "
                    "not power, because that is what the Garmin receives. event_id from get_plan_vs_actual or a save result.",
     "inputSchema": {"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]}},
    {"name": "get_season_plan",
     "description": "Annual training plan (Saisonplanung) from the intervals.icu calendar: season start, phases (PLAN), weekly "
                    "targets (TARGET: load / hours / km when set), recovery weeks, races (RACE_A/B/C), with week number, phase and "
                    "weeks to the A race per week. Default window: 60 days back to 330 days ahead.",
     "inputSchema": {"type": "object", "properties": _RANGE}},
    {"name": "get_plan_vs_actual",
     "description": "Per day: planned calendar entries (workouts, races, notes) next to the activities actually done. "
                    "Planned workouts get status done / open / no paired activity.",
     "inputSchema": {"type": "object", "properties": _RANGE}},
]


READ_ONLY = {"readOnlyHint": True}
for _t in TOOLS:
    _t["annotations"] = READ_ONLY

WORKOUT_TYPES = ("Run", "TrailRun", "VirtualRun", "Walk", "Hike", "Ride", "VirtualRide", "Swim", "WeightTraining",
                 "Workout", "Yoga", "Other")
OWN_PREFIXES = ("tcoach-", "coach-")  # delete_planned_workout only touches entries created here
OWN_NOTE_PREFIX = "coach-note-"  # delete_note only touches notes created here
MAX_AHEAD_DAYS = 400
WRITE_RATE = RateLimiter(limit=30, window_s=3600)  # all writes together, per hour

_DATE = {"type": "string", "description": "Day of the workout, YYYY-MM-DD (yesterday up to 400 days ahead)"}
WRITE_TOOLS = [
    {"name": "save_treadmill_workout",
     "description": "Plan a treadmill workout for KiekeRun in the intervals.icu calendar. Blocks have speed (km/h) and incline (%) "
                    "and are checked against the treadmill limits. Saving the same name on the same day updates it in place. "
                    "KiekeRun loads it with the 'Today's workout' button. The block label is only the description "
                    "(e.g. 'Steady Vertical'): time, km/h, min/km and incline are written into the calendar text automatically as "
                    "'<label> 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline', so do not put them in the label. "
                    "Zone info belongs in notes. A fueling plan goes into 'fuel' (shown in KiekeRun as an overview and as reminders "
                    "during the run, times are workout time). Amounts are fractions of one FULL flask per Tito's nutrition principles: "
                    "kind 'gel' (1 flask = 110 g powder, 150 ml) or 'drink' (1 flask = 37.5 g powder, 500 ml); carbs_g and fluid_ml are "
                    "derived. 'note' is free text for the sugar mix version (e.g. 'V4-Mix', 'V3-Mix'). "
                    "[{at:'20:00', kind:'gel', flask:1, note:'V4-Mix'}] or repeating "
                    "[{every:'20:00', from?:'10:00', until?:'2:00:00', kind:'drink', flask:0.5, note:'V3-Mix'}]. "
                    "kind/flask are optional; without them give carbs_g / fluid_ml directly.",
     "inputSchema": {"type": "object", "required": ["date", "workout"], "properties": {
         "date": _DATE,
         "workout": {"type": "object", "required": ["name", "blocks"], "description":
                     "{name, notes?, blocks:[{label, duration:'mm:ss' or 'h:mm:ss', speed:km/h, incline:%} or "
                     "{repeat:N, label?, blocks:[...]}], fuel?:[{at|every, kind?:'gel'|'drink', flask?:0-4, carbs_g?, fluid_ml?, note?}]}", "properties": {
                         "name": {"type": "string"}, "notes": {"type": "string"}, "blocks": {"type": "array"},
                         "fuel": {"type": "array", "maxItems": 60}}}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "save_planned_workout",
     "description": "Plan any other workout (outdoor run, strength, mobility, rest-day note as a workout) in the intervals.icu calendar. "
                    "Description is intervals.icu workout text: one step per line starting with '- ' (e.g. '- 15m Z2 Pace' or '- 15m Z2 HR'), "
                    "ALWAYS name the target type after a zone (Pace or HR): a bare '- 15m Z2' is exported to the Garmin as a POWER target "
                    "and makes the watch alarm about watts all run long. "
                    "free text above it is shown as notes. Saving the same name on the same day updates it in place.",
     "inputSchema": {"type": "object", "required": ["date", "name"], "properties": {
         "date": _DATE, "name": {"type": "string", "maxLength": 120},
         "type": {"type": "string", "enum": list(WORKOUT_TYPES), "description": "Default Run"},
         "description": {"type": "string", "maxLength": 5000},
         "duration_min": {"type": "number", "minimum": 1, "maximum": 1440},
         "load": {"type": "number", "minimum": 0, "maximum": 1000, "description": "Planned training load"},
         "carbs_per_h": {"type": "number", "minimum": 0, "maximum": 200, "description": "Planned carbs in g per hour"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "save_note",
     "description": "Add a plain NOTE to the intervals.icu calendar: title + description, no duration, no load, "
                    "no compliance math. Use this for rest days, reminders, race-week markers or anything that is "
                    "not a workout. Saving the same name on the same day updates it in place.",
     "inputSchema": {"type": "object", "required": ["date", "name"], "properties": {
         "date": _DATE, "name": {"type": "string", "maxLength": 120},
         "description": {"type": "string", "maxLength": 5000}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}},
    {"name": "delete_planned_workout",
     "description": "Delete a planned workout that was created through this connector or KiekeRun (not races, notes or other calendar "
                    "entries) and that is today or later. Use event_id from get_plan_vs_actual or a save result.",
     "inputSchema": {"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "integer"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}},
    {"name": "delete_note",
     "description": "Delete a calendar NOTE that was created through this connector (save_note) and that is today or later. "
                    "Use event_id from get_plan_vs_actual or a save result.",
     "inputSchema": {"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "integer"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}},
]
WRITE_NAMES = {t["name"] for t in WRITE_TOOLS}


def tools_for(allow_write: bool) -> list[dict]:
    return TOOLS + WRITE_TOOLS if allow_write else TOOLS


class ToolError(Exception):
    pass


ZONE_STEP = re.compile(r"^\s*-.*\bZ[1-7]\b", re.I)
TARGET_TYPE = re.compile(r"\b(pace|hr|lthr|power|w|watts?)\b|%", re.I)
_LABEL_TAIL = re.compile(r"\s+\d+(?::\d{2})?\s*(?:min|s)\s*@\s*\d+(?:[.,]\d+)?\s*km/h\b.*$", re.I)


def clean_labels(node):
    """The server writes '<label> <time> @ <speed> km/h (<pace> min/km) @ <incline>% incline' itself. If the
    caller already put that into the label, keep only the description so the numbers are not written twice."""
    if isinstance(node, dict):
        out = {k: clean_labels(v) if k == "blocks" else v for k, v in node.items()}
        if isinstance(out.get("label"), str):
            out["label"] = _LABEL_TAIL.sub("", out["label"]).strip() or out["label"]
        return out
    if isinstance(node, list):
        return [clean_labels(x) for x in node]
    return node


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}]}


def _check_date(v, today: str) -> str:
    d, t = parse_day(v, "date"), parse_day(today, "today")
    if d < t - timedelta(days=1) or d > t + timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError("date must be between yesterday and 400 days ahead")
    return d.isoformat()


def _write(name: str, args: dict, cl: IntervalsClient, today: str) -> dict:
    wait = WRITE_RATE.hit("all")
    if wait:
        raise ToolError(f"too many calendar changes, try again in {wait} s")
    if name == "save_treadmill_workout":
        day = _check_date(args.get("date"), today)
        raw = args.get("workout")
        if not isinstance(raw, dict):
            raise ValueError("workout must be an object")
        w = Workout.from_dict(clean_labels(raw))
        issues = validate(w)
        if has_errors(issues):
            raise ValueError("; ".join(str(i) for i in issues))
        ev = cl.push_planned([(w, day)])[0]
        return _text({"saved": True, "event_id": ev.get("id"), "date": day, "name": w.name,
                      "duration_min": round(w.duration_s / 60.0, 1), "description": to_intervals_text(w)})
    if name == "save_planned_workout":
        day = _check_date(args.get("date"), today)
        title = str(args.get("name") or "").strip()
        if not title or len(title) > 120:
            raise ValueError("name is required (max 120 characters)")
        typ = args.get("type", "Run")
        if typ not in WORKOUT_TYPES:
            raise ValueError("type must be one of: " + ", ".join(WORKOUT_TYPES))
        desc = args.get("description") or ""
        if not isinstance(desc, str) or len(desc) > 5000:
            raise ValueError("description must be text of at most 5000 characters")
        ev = {"category": "WORKOUT", "start_date_local": f"{day}T00:00:00", "type": typ, "name": title,
              "description": desc, "external_id": f"coach-{day}-{slug(title)}"}
        for key, field, lo, hi, mult in (("duration_min", "moving_time", 1, 1440, 60), ("load", "icu_training_load", 0, 1000, 1),
                                         ("carbs_per_h", "carbs_per_hour", 0, 200, 1)):
            if args.get(key) is not None:
                v = args[key]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
                    raise ValueError(f"{key} must be a number {lo}-{hi}")
                ev[field] = round(v * mult) if mult != 1 else v
        res = cl.upsert_event(ev)
        out = {"saved": True, "updated_existing": bool(res.get("_updated")), "event_id": res.get("id"),
               "date": day, "name": title}
        bare = [ln.strip() for ln in desc.splitlines() if ZONE_STEP.search(ln) and not TARGET_TYPE.search(ln)]
        if bare:
            out["warning"] = ("these steps have a zone but no target type; intervals.icu sends them to Garmin as power targets "
                              "(watt alarms). Add 'Pace' or 'HR' and save again: " + " | ".join(bare[:5]))
        return _text(out)
    if name == "save_note":
        day = _check_date(args.get("date"), today)
        title = str(args.get("name") or "").strip()
        if not title or len(title) > 120:
            raise ValueError("name is required (max 120 characters)")
        desc = args.get("description") or ""
        if not isinstance(desc, str) or len(desc) > 5000:
            raise ValueError("description must be text of at most 5000 characters")
        ev = {"category": "NOTE", "start_date_local": f"{day}T00:00:00", "name": title,
              "description": desc, "external_id": f"coach-note-{day}-{slug(title)}"}
        res = cl.upsert_event(ev)
        return _text({"saved": True, "updated_existing": bool(res.get("_updated")), "event_id": res.get("id"),
                      "date": day, "name": title})
    if name == "delete_planned_workout":
        eid = args.get("event_id")
        if isinstance(eid, bool) or not isinstance(eid, int) or eid <= 0:
            raise ValueError("event_id must be a positive integer")
        ev = cl.get_event(eid)
        ext = str(ev.get("external_id") or "")
        if ev.get("category") != "WORKOUT" or not ext.startswith(OWN_PREFIXES):
            raise ValueError("only workouts created through this connector or KiekeRun can be deleted")
        if (ev.get("start_date_local") or "")[:10] < today:
            raise ValueError("past workouts are not deleted")
        cl.delete_event(eid)
        return _text({"deleted": True, "event_id": eid, "name": ev.get("name"), "date": (ev.get("start_date_local") or "")[:10]})
    if name == "delete_note":
        eid = args.get("event_id")
        if isinstance(eid, bool) or not isinstance(eid, int) or eid <= 0:
            raise ValueError("event_id must be a positive integer")
        ev = cl.get_event(eid)
        ext = str(ev.get("external_id") or "")
        if ev.get("category") != "NOTE" or not ext.startswith(OWN_NOTE_PREFIX):
            raise ValueError("only notes created through this connector can be deleted")
        if (ev.get("start_date_local") or "")[:10] < today:
            raise ValueError("past notes are not deleted")
        cl.delete_event(eid)
        return _text({"deleted": True, "event_id": eid, "name": ev.get("name"), "date": (ev.get("start_date_local") or "")[:10]})
    raise ToolError(f"unknown tool: {name}")


def call_tool(name: str, args: dict, cl: IntervalsClient, today: str, allow_write: bool = False) -> dict:
    try:
        if name in WRITE_NAMES:
            if not allow_write:
                raise ToolError("writing is disabled on this server (set MCP_ALLOW_WRITE=1)")
            return _write(name, args, cl, today)
        if name == "get_activities":
            oldest, newest = resolve_range(args, today)
            limit = args.get("limit", 60)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
                raise ValueError(f"limit must be 1-{MAX_LIMIT}")
            acts = cl.list_activities(oldest, newest, limit=limit, fields=ACTIVITY_FIELDS)
            return _text({"oldest": oldest, "newest": newest, "count": len(acts), "activities": [summarize_activity(a) for a in acts]})
        if name == "get_activity":
            aid = str(args.get("id") or "").strip()
            if not aid or "/" in aid or len(aid) > 40:
                raise ValueError("id is required")
            return _text(summarize_activity_detail(cl.get_activity(aid)))
        if name == "get_wellness":
            oldest, newest = resolve_range(args, today)
            rows = [summarize_wellness(w) for w in cl.list_wellness(oldest, newest)]
            return _text({"oldest": oldest, "newest": newest, "days": rows})
        if name == "get_zones":
            sport = args.get("sport")
            if sport is not None and not isinstance(sport, str):
                raise ValueError("sport must be text")
            return _text(summarize_zones(cl.athlete(), sport))
        if name == "get_planned_workout":
            eid = args.get("event_id")
            if isinstance(eid, bool) or not isinstance(eid, int) or eid <= 0:
                raise ValueError("event_id must be a positive integer")
            ev = cl.get_event(eid)
            if ev.get("category") != "WORKOUT":
                raise ValueError("event is not a planned workout")
            return _text(summarize_workout_doc(ev))
        if name == "get_season_plan":
            t = parse_day(today, "today")
            a = dict(args)
            if not a.get("oldest") and not a.get("newest"):
                a["oldest"], a["newest"] = (t - timedelta(days=60)).isoformat(), (t + timedelta(days=330)).isoformat()
            elif not a.get("newest"):
                a["newest"] = (parse_day(a["oldest"], "oldest") + timedelta(days=MAX_RANGE_DAYS)).isoformat()
            elif not a.get("oldest"):
                a["oldest"] = (parse_day(a["newest"], "newest") - timedelta(days=MAX_RANGE_DAYS)).isoformat()
            oldest, newest = resolve_range(a, today)
            events = [e for e in cl.list_events(oldest, newest, category=None) if e.get("category") in SEASON_CATEGORIES]
            return _text({"oldest": oldest, "newest": newest, **summarize_season(events, today)})
        if name == "get_plan_vs_actual":
            oldest, newest = resolve_range(args, today)
            events = cl.list_events(oldest, newest, category=None)
            acts = cl.list_activities(oldest, newest, fields=ACTIVITY_FIELDS)
            return _text({"oldest": oldest, "newest": newest, "days": plan_vs_actual(events, acts, today)})
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError(str(exc))
    except IntervalsError as exc:
        raise ToolError(f"intervals.icu error: {exc}")
    raise ToolError(f"unknown tool: {name}")


def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(msg, get_client: Callable[[], IntervalsClient], today: Callable[[], str], allow_write: bool = False):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request")
    method, id_ = msg.get("method"), msg.get("id")
    if id_ is None:  # notification (initialized, cancelled, ...): nothing to answer
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return _ok(id_, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "kiekerun-coach", "version": "1.0"},
                         "instructions": "Tito's intervals.icu data: activities from all sources, wellness/fitness, planned "
                                         "workouts. Dates are local, YYYY-MM-DD."
                                         + (" Calendar writes are enabled for planned workouts and notes." if allow_write else " Read-only.")})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": tools_for(allow_write)})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in {t["name"] for t in tools_for(True)}:
            return _err(id_, -32602, f"unknown tool: {name}")
        try:
            return _ok(id_, call_tool(name, args, get_client(), today(), allow_write))
        except ToolError as exc:
            return _ok(id_, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return _err(id_, -32601, f"method not found: {method}")
