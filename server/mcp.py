"""Read-only MCP server (Streamable HTTP, stateless JSON) that lets the coach read intervals.icu.

Tools: get_activities, get_activity, get_wellness, get_plan_vs_actual.
Every tool only reads. Auth is a secret in the URL path (env MCP_TOKEN), because claude.ai's
custom connectors take a URL and cannot send our Basic-Auth login. Anyone with the URL can
read your training data, so treat it like a password and rotate MCP_TOKEN if it leaks.
"""
from __future__ import annotations

import json
from typing import Callable

from tcoach.coach_data import (ACTIVITY_FIELDS, plan_vs_actual, resolve_range, summarize_activity,
                               summarize_activity_detail, summarize_wellness)
from tcoach.intervals_client import IntervalsClient, IntervalsError

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
    {"name": "get_plan_vs_actual",
     "description": "Per day: planned calendar entries (workouts, races, notes) next to the activities actually done. "
                    "Planned workouts get status done / open / no paired activity.",
     "inputSchema": {"type": "object", "properties": _RANGE}},
]


class ToolError(Exception):
    pass


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}]}


def call_tool(name: str, args: dict, cl: IntervalsClient, today: str) -> dict:
    try:
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


def handle(msg, get_client: Callable[[], IntervalsClient], today: Callable[[], str]):
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
                         "instructions": "Read-only access to Tito's intervals.icu data (activities from all sources, "
                                         "wellness/fitness, planned workouts). Dates are local, YYYY-MM-DD."})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in {t["name"] for t in TOOLS}:
            return _err(id_, -32602, f"unknown tool: {name}")
        try:
            return _ok(id_, call_tool(name, args, get_client(), today()))
        except ToolError as exc:
            return _ok(id_, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return _err(id_, -32601, f"method not found: {method}")
