import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server import mcp as mcpmod
from tcoach.intervals_client import IntervalsClient

TOKEN = "t" * 24
GOOD_WORKOUT = {"name": "Uphill 2x", "blocks": [{"label": "Warm-up", "duration": "10:00", "speed": 6, "incline": 5},
                                                  {"repeat": 2, "blocks": [{"label": "Up", "duration": "5:00", "speed": 5.5, "incline": 15},
                                                                           {"label": "Easy", "duration": "2:00", "speed": 5, "incline": 3}]}]}


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._d


class Cal:
    """Tiny fake calendar behind the intervals.icu API; records every call."""
    def __init__(self, events=None):
        self.events = {e["id"]: dict(e) for e in (events or [])}
        self.calls, self.next_id = [], 100

    def request(self, method, url, **kw):
        path = url.split("/athlete/i1", 1)[1] if "/athlete/i1" in url else url
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/events/") and not path.endswith(".json"):
            e = self.events.get(int(path.rsplit("/", 1)[1]))
            return Resp(200, e) if e else Resp(404, {})
        if method == "GET" and path == "/events":
            p = kw["params"]
            return Resp(200, [e for e in self.events.values() if p["oldest"] <= e["start_date_local"][:10] <= p["newest"]])
        if method == "POST" and path == "/events":
            self.next_id += 1
            self.events[self.next_id] = {**kw["json"], "id": self.next_id}
            return Resp(200, self.events[self.next_id])
        if method == "PUT" and path.startswith("/events/"):
            eid = int(path.rsplit("/", 1)[1])
            self.events[eid] = {**self.events[eid], **kw["json"]}
            return Resp(200, self.events[eid])
        if method == "DELETE" and path.startswith("/events/"):
            self.events.pop(int(path.rsplit("/", 1)[1]), None)
            return Resp(200, None)
        return Resp(404, {})


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", TOKEN)

    def _mk(events=None, write=True):
        if write:
            monkeypatch.setenv("MCP_ALLOW_WRITE", "1")
        cal = Cal(events)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=cal))
        monkeypatch.setattr(appmod, "today_local", lambda: "2026-10-16")
        return TestClient(appmod.app), cal
    return _mk


def call(c, name, args):
    r = c.post(f"/mcp/{TOKEN}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).json()
    if "error" in r:
        return r
    res = r["result"]
    txt = res["content"][0]["text"]
    return {"isError": bool(res.get("isError")), "data": json.loads(txt) if not res.get("isError") else txt}


def test_write_tools_hidden_and_refused_by_default(mk):
    c, cal = mk(write=False)
    tools = c.post(f"/mcp/{TOKEN}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["get_activities", "get_activity", "get_wellness", "get_zones", "get_planned_workout", "get_plan_vs_actual"]
    assert all(t["annotations"] == {"readOnlyHint": True} for t in tools)
    r = call(c, "save_planned_workout", {"date": "2026-10-17", "name": "x"})
    assert r["isError"] and "disabled" in r["data"] and not [x for x in cal.calls if x[0] != "GET"]
    r = call(c, "save_note", {"date": "2026-10-17", "name": "x"})
    assert r["isError"] and "disabled" in r["data"] and not [x for x in cal.calls if x[0] != "GET"]


def test_write_tools_listed_with_annotations_when_enabled(mk):
    c, _ = mk()
    tools = {t["name"]: t for t in c.post(f"/mcp/{TOKEN}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    assert set(tools) >= {"save_treadmill_workout", "save_planned_workout", "save_note",
                           "delete_planned_workout", "delete_note"}
    assert tools["delete_planned_workout"]["annotations"]["destructiveHint"] is True
    assert tools["delete_note"]["annotations"]["destructiveHint"] is True
    assert tools["save_planned_workout"]["annotations"]["readOnlyHint"] is False
    assert tools["save_note"]["annotations"] == {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}


def test_save_treadmill_workout_creates_then_updates(mk):
    c, cal = mk()
    r = call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": GOOD_WORKOUT})
    assert not r["isError"] and r["data"]["saved"] and r["data"]["duration_min"] == 24.0
    ev = cal.events[r["data"]["event_id"]]
    assert ev["external_id"].startswith("tcoach-2026-10-17-") and "- Up 5 min @ 5.5 km/h (10:55 min/km) @ 15% incline" in ev["description"]
    assert ev["moving_time"] == 24 * 60 and ev["distance"] > 0
    r2 = call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": GOOD_WORKOUT})
    assert r2["data"]["event_id"] == r["data"]["event_id"] and len(cal.events) == 1


def test_save_treadmill_workout_rejects_machine_limit_and_bad_input(mk):
    c, cal = mk()
    bad = {"name": "x", "blocks": [{"label": "a", "duration": "1:00", "speed": 15, "incline": 30}]}
    assert call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": bad})["isError"]
    assert call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": "nope"})["isError"]
    assert call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": {"name": "x", "blocks": [{"label": "a"}]}})["isError"]
    assert not cal.events


def test_save_planned_workout_idempotent_and_fields(mk):
    c, cal = mk()
    args = {"date": "2026-10-20", "name": "Fuel Rehearsal", "type": "VirtualRun", "description": "- 60m Z2 Pace",
            "duration_min": 60, "load": 70, "carbs_per_h": 75}
    r = call(c, "save_planned_workout", args)
    ev = cal.events[r["data"]["event_id"]]
    assert r["data"]["updated_existing"] is False
    assert (ev["category"], ev["type"], ev["moving_time"], ev["icu_training_load"], ev["carbs_per_hour"]) == ("WORKOUT", "VirtualRun", 3600, 70, 75)
    assert ev["external_id"] == "coach-2026-10-20-fuel-rehearsal" and ev["start_date_local"] == "2026-10-20T00:00:00"
    r2 = call(c, "save_planned_workout", {**args, "description": "- 45m Z2 Pace"})
    assert r2["data"]["updated_existing"] is True and r2["data"]["event_id"] == r["data"]["event_id"]
    assert len(cal.events) == 1 and cal.events[r["data"]["event_id"]]["description"] == "- 45m Z2 Pace"


@pytest.mark.parametrize("args", [
    {"date": "2026-10-14", "name": "x"}, {"date": "2028-01-01", "name": "x"}, {"date": "17.10.2026", "name": "x"},
    {"date": "2026-10-17", "name": ""}, {"date": "2026-10-17", "name": "x" * 121}, {"date": "2026-10-17", "name": "x", "type": "Rocket"},
    {"date": "2026-10-17", "name": "x", "duration_min": 0}, {"date": "2026-10-17", "name": "x", "duration_min": "60"},
    {"date": "2026-10-17", "name": "x", "load": 1e9}, {"date": "2026-10-17", "name": "x", "carbs_per_h": 500},
    {"date": "2026-10-17", "name": "x", "description": "y" * 5001},
])
def test_save_planned_workout_validation(mk, args):
    c, cal = mk()
    assert call(c, "save_planned_workout", args)["isError"]
    assert not cal.events


def test_save_note_creates_then_updates(mk):
    c, cal = mk()
    r = call(c, "save_note", {"date": "2026-10-20", "name": "Rest Day", "description": "Walk 20 min + Mobility"})
    assert not r["isError"] and r["data"]["saved"] and r["data"]["updated_existing"] is False
    ev = cal.events[r["data"]["event_id"]]
    assert ev["category"] == "NOTE" and ev["name"] == "Rest Day"
    assert ev["description"] == "Walk 20 min + Mobility"
    assert ev["external_id"] == "coach-note-2026-10-20-rest-day"
    assert ev["start_date_local"] == "2026-10-20T00:00:00"
    # NOTE must not carry workout-only fields
    assert "moving_time" not in ev and "icu_training_load" not in ev and "type" not in ev
    # Re-save with same name on same day updates in place
    r2 = call(c, "save_note", {"date": "2026-10-20", "name": "Rest Day", "description": "Updated: full rest"})
    assert r2["data"]["updated_existing"] is True and r2["data"]["event_id"] == r["data"]["event_id"]
    assert len(cal.events) == 1
    assert cal.events[r["data"]["event_id"]]["description"] == "Updated: full rest"


@pytest.mark.parametrize("args", [
    {"date": "2026-10-14", "name": "x"}, {"date": "2028-01-01", "name": "x"}, {"date": "17.10.2026", "name": "x"},
    {"date": "2026-10-17", "name": ""}, {"date": "2026-10-17", "name": "x" * 121},
    {"date": "2026-10-17", "name": "x", "description": "y" * 5001},
    {"date": "2026-10-17", "name": "x", "description": 123},
])
def test_save_note_validation(mk, args):
    c, cal = mk()
    assert call(c, "save_note", args)["isError"]
    assert not cal.events


def test_delete_only_own_future_workouts(mk):
    events = [
        {"id": 1, "category": "WORKOUT", "external_id": "coach-2026-10-20-x", "name": "mine", "start_date_local": "2026-10-20T00:00:00"},
        {"id": 2, "category": "WORKOUT", "external_id": "tcoach-2026-10-21-y", "name": "kiekerun", "start_date_local": "2026-10-21T00:00:00"},
        {"id": 3, "category": "RACE_A", "external_id": "coach-2026-10-30-r", "name": "race", "start_date_local": "2026-10-30T00:00:00"},
        {"id": 4, "category": "WORKOUT", "external_id": None, "name": "manual", "start_date_local": "2026-10-22T00:00:00"},
        {"id": 5, "category": "WORKOUT", "external_id": "coach-2026-10-10-old", "name": "old", "start_date_local": "2026-10-10T00:00:00"},
    ]
    c, cal = mk(events)
    assert call(c, "delete_planned_workout", {"event_id": 1})["data"]["deleted"] is True
    assert call(c, "delete_planned_workout", {"event_id": 2})["data"]["deleted"] is True
    for eid in (3, 4, 5, 99):
        assert call(c, "delete_planned_workout", {"event_id": eid})["isError"], eid
    assert set(cal.events) == {3, 4, 5}
    for bad in ("1", True, -1, None):
        assert call(c, "delete_planned_workout", {"event_id": bad})["isError"]


def test_delete_only_own_future_notes(mk):
    events = [
        {"id": 1, "category": "NOTE", "external_id": "coach-note-2026-10-20-rest", "name": "mine",
         "start_date_local": "2026-10-20T00:00:00"},
        {"id": 2, "category": "NOTE", "external_id": "coach-2026-10-21-x", "name": "workout-prefix",
         "start_date_local": "2026-10-21T00:00:00"},  # NOTE but non-note prefix (workout tool prefix)
        {"id": 3, "category": "NOTE", "external_id": None, "name": "manual-ui",
         "start_date_local": "2026-10-22T00:00:00"},
        {"id": 4, "category": "WORKOUT", "external_id": "coach-note-2026-10-23-x", "name": "wrong-category",
         "start_date_local": "2026-10-23T00:00:00"},
        {"id": 5, "category": "NOTE", "external_id": "coach-note-2026-10-10-old", "name": "past",
         "start_date_local": "2026-10-10T00:00:00"},
    ]
    c, cal = mk(events)
    assert call(c, "delete_note", {"event_id": 1})["data"]["deleted"] is True
    for eid in (2, 3, 4, 5, 99):
        assert call(c, "delete_note", {"event_id": eid})["isError"], eid
    assert set(cal.events) == {2, 3, 4, 5}
    for bad in ("1", True, -1, None):
        assert call(c, "delete_note", {"event_id": bad})["isError"]


def test_delete_note_refuses_workout_entries(mk):
    events = [{"id": 10, "category": "WORKOUT", "external_id": "coach-2026-10-20-run", "name": "run",
               "start_date_local": "2026-10-20T00:00:00"}]
    c, cal = mk(events)
    r = call(c, "delete_note", {"event_id": 10})
    assert r["isError"] and "note" in r["data"].lower()
    assert set(cal.events) == {10}


def test_write_rate_cap(mk, monkeypatch):
    monkeypatch.setattr(mcpmod, "WRITE_RATE", mcpmod.RateLimiter(limit=2, window_s=3600))
    c, cal = mk()
    ok = [call(c, "save_planned_workout", {"date": "2026-10-2%d" % i, "name": "w"})["isError"] for i in range(3)]
    assert ok == [False, False, True] and len(cal.events) == 2


def test_label_that_already_contains_the_numbers_is_not_doubled(mk):
    c, cal = mk()
    w = {"name": "Doppel", "blocks": [
        {"label": "Steady Vertical 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline", "duration": "30:00", "speed": 5.5, "incline": 15},
        {"repeat": 2, "label": "Sets", "blocks": [{"label": "Up 5 min @ 5,0 km/h @ 10% incline", "duration": "5:00", "speed": 5, "incline": 10}]}]}
    r = call(c, "save_treadmill_workout", {"date": "2026-10-17", "workout": w})
    text = cal.events[r["data"]["event_id"]]["description"]
    assert "- Steady Vertical 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline\n" in text + "\n"
    assert "- Up 5 min @ 5.0 km/h (12:00 min/km) @ 10% incline" in text
    assert text.count("@ 5.5 km/h") == 1 and text.count("km/h") == 2


def test_bare_zone_step_gets_a_power_target_warning():
    import json as _j
    from server import mcp
    class CL:
        def upsert_event(self, ev): return {"id": 1}
    r = mcp.call_tool("save_planned_workout", {"date": "2026-10-25", "name": "x",
                      "description": "- 10m Z1 WU\n- 30m Z2 HR\n- 5m Z1 Pace"}, CL(), "2026-10-19", allow_write=True)
    out = _j.loads(r["content"][0]["text"])
    assert "10m Z1 WU" in out["warning"] and "30m" not in out["warning"] and "5m" not in out["warning"]
