import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from tcoach.coach_data import plan_vs_actual, resolve_range, summarize_activity, summarize_season, summarize_wellness
from tcoach.intervals_client import IntervalsClient

TOKEN = "t" * 24


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._d


class Http:
    """Answers by URL suffix; records (method, url, params)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params")))
        for suffix, r in self.routes.items():
            if url.endswith(suffix):
                return r
        return Resp(404, {"error": "nope"})


ACT = {"id": "i1", "start_date_local": "2026-10-14T07:30:00", "name": "Hike", "type": "VirtualRun", "device_name": "KiekeRun",
       "trainer": True, "moving_time": 3600, "distance": 6000, "total_elevation_gain": 420.4, "average_heartrate": 141.6,
       "icu_rpe": 7, "feel": 2, "carbs_ingested": 50, "icu_training_load": 62, "paired_event_id": 9,
       "description": "Beine schwer", "average_vertical_speed": 0.117}


@pytest.fixture
def mk(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", TOKEN)

    def _mk(routes):
        http = Http(routes)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=http))
        monkeypatch.setattr(appmod, "today_local", lambda: "2026-10-16")
        return TestClient(appmod.app), http
    return _mk


def rpc(c, method, params=None, id_=1, token=TOKEN):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": id_} if id_ is not None else {}), **({"params": params} if params else {})}
    return c.post(f"/mcp/{token}", json=msg)


def test_wrong_or_unset_token_looks_like_missing_route(mk, monkeypatch):
    c, _ = mk({})
    assert rpc(c, "ping", token="x" * 24).status_code == 404
    monkeypatch.delenv("MCP_TOKEN")
    assert rpc(c, "ping", token="").status_code in (404, 405)
    monkeypatch.setenv("MCP_TOKEN", "short")
    assert rpc(c, "ping", token="short").status_code == 404  # too short to be a secret


def test_initialize_list_ping_and_notification(mk):
    c, _ = mk({})
    r = rpc(c, "initialize", {"protocolVersion": "2025-03-26"}).json()["result"]
    assert r["protocolVersion"] == "2025-03-26" and r["capabilities"] == {"tools": {}}
    assert rpc(c, "initialize", {"protocolVersion": "1999"}).json()["result"]["protocolVersion"] == "2025-06-18"
    names = [t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"]]
    assert names == ["get_activities", "get_activity", "get_wellness", "get_zones", "get_planned_workout", "get_season_plan", "get_plan_vs_actual"]
    assert rpc(c, "ping").json()["result"] == {}
    assert rpc(c, "notifications/initialized", id_=None).status_code == 202
    assert rpc(c, "nope").json()["error"]["code"] == -32601
    assert c.get(f"/mcp/{TOKEN}").status_code == 405


def test_get_activities_is_compact_and_has_units(mk):
    c, http = mk({"/activities": Resp(200, [ACT])})
    res = rpc(c, "tools/call", {"name": "get_activities", "arguments": {"oldest": "2026-10-10"}}).json()["result"]
    data = json.loads(res["content"][0]["text"])
    a = data["activities"][0]
    assert (data["oldest"], data["newest"], data["count"]) == ("2026-10-10", "2026-10-16", 1)
    assert a["source"] == "KiekeRun" and a["indoor"] is True and a["pace_min_km"] == "10:00"
    assert a["distance_km"] == 6.0 and a["duration_min"] == 60.0 and a["elevation_gain_m"] == 420
    assert (a["rpe"], a["feel"], a["carbs_g"], a["vertical_speed_m_h"]) == (7, 2, 50, 421)
    params = http.calls[0][2]
    assert params["limit"] == 60 and "icu_training_load" in params["fields"] and "isError" not in res


def test_activities_falls_back_to_json_path_on_404(mk):
    c, http = mk({"/activities.json": Resp(200, [])})
    rpc(c, "tools/call", {"name": "get_activities", "arguments": {}})
    assert [u.rsplit("/", 1)[1] for _, u, _ in http.calls] == ["activities", "activities.json"]


def test_get_activity_detail_has_intervals(mk):
    d = dict(ACT, icu_intervals=[{"type": "WORK", "label": "1", "start_time": 0, "moving_time": 480, "distance": 1500,
                                  "average_heartrate": 168}], icu_hr_zone_times=[10, 20, 30])
    c, http = mk({"/activity/i1": Resp(200, d)})
    out = json.loads(rpc(c, "tools/call", {"name": "get_activity", "arguments": {"id": "i1"}}).json()["result"]["content"][0]["text"])
    assert out["intervals"][0]["pace_min_km"] == "5:20" and out["hr_zone_times_s"] == [10, 20, 30]
    assert http.calls[0][2] == {"intervals": "true"}


def test_get_wellness_computes_form(mk):
    c, _ = mk({"/wellness": Resp(200, [{"id": "2026-10-15", "ctl": 50.0, "atl": 58.5, "hrv": 62.2, "sleepSecs": 27000, "restingHR": 48}])})
    out = json.loads(rpc(c, "tools/call", {"name": "get_wellness", "arguments": {}}).json()["result"]["content"][0]["text"])
    d = out["days"][0]
    assert d["form_tsb"] == -8.5 and d["sleep_h"] == 7.5 and d["resting_hr"] == 48


def test_plan_vs_actual_status(mk):
    events = [{"id": 9, "category": "WORKOUT", "name": "Threshold", "start_date_local": "2026-10-14T00:00:00", "moving_time": 3600},
              {"id": 10, "category": "WORKOUT", "name": "Easy", "start_date_local": "2026-10-15T00:00:00"},
              {"id": 11, "category": "WORKOUT", "name": "Long", "start_date_local": "2026-10-17T00:00:00"},
              {"id": 12, "category": "RACE_A", "name": "Mallorca", "start_date_local": "2026-10-30T00:00:00"}]
    rows = {r["date"]: r for r in plan_vs_actual(events, [ACT], "2026-10-16")}
    assert rows["2026-10-14"]["planned"][0]["status"] == "done" and rows["2026-10-14"]["planned"][0]["activity_id"] == "i1"
    assert rows["2026-10-15"]["planned"][0]["status"] == "no paired activity"
    assert rows["2026-10-17"]["planned"][0]["status"] == "open"
    assert "status" not in rows["2026-10-30"]["planned"][0]


def test_tool_errors_are_returned_not_raised(mk):
    c, _ = mk({"/activities": Resp(401, {})})
    r = rpc(c, "tools/call", {"name": "get_activities", "arguments": {}}).json()["result"]
    assert r["isError"] and "rejected" in r["content"][0]["text"]
    for args in ({"oldest": "16.10.2026"}, {"oldest": "2026-10-16", "newest": "2026-10-01"}, {"oldest": "2020-01-01"}, {"limit": 0}):
        r = rpc(c, "tools/call", {"name": "get_activities", "arguments": args}).json()["result"]
        assert r["isError"], args
    assert rpc(c, "tools/call", {"name": "get_activity", "arguments": {"id": "../x"}}).json()["result"]["isError"]
    assert rpc(c, "tools/call", {"name": "delete_everything"}).json()["error"]["code"] == -32602


def test_batch_and_bad_json(mk):
    c, _ = mk({})
    r = c.post(f"/mcp/{TOKEN}", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert r.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert c.post(f"/mcp/{TOKEN}", content=b"{nope", headers={"content-type": "application/json"}).status_code == 400


def test_helpers():
    assert resolve_range({}, "2026-10-16") == ("2026-10-03", "2026-10-16")
    assert summarize_activity({"id": 1}) == {"id": 1}
    assert summarize_wellness({"id": "2026-10-16"}) == {"date": "2026-10-16"}


def test_zones_and_planned_workout_tools():
    import json as _j
    from server import mcp

    class CL:
        def athlete(self):
            return {"sportSettings": [{"types": ["Run", "VirtualRun"], "lthr": 157, "max_hr": 172,
                                       "hr_zones": [134, 148, 157, 164, 172], "junk": 1},
                                      {"types": ["Ride"], "ftp": 250}]}

        def get_event(self, eid):
            return {"id": eid, "name": "Easy", "category": "WORKOUT", "start_date_local": "2026-10-20T00:00:00",
                    "description": "- 10m Z1 HR", "workout_doc": {"duration": 600, "steps": [{"duration": 600, "hr": {"units": "hr_zone", "value": 1}}]}}

    z = _j.loads(mcp.call_tool("get_zones", {"sport": "Run"}, CL(), "2026-10-19")["content"][0]["text"])
    assert len(z["sport_settings"]) == 1 and z["sport_settings"][0]["hr_zones"] == [134, 148, 157, 164, 172]
    assert "junk" not in z["sport_settings"][0]
    w = _j.loads(mcp.call_tool("get_planned_workout", {"event_id": 5}, CL(), "2026-10-19")["content"][0]["text"])
    assert w["has_structure"] and w["steps"][0]["hr"]["value"] == 1
    with pytest.raises(mcp.ToolError):
        mcp.call_tool("get_planned_workout", {"event_id": "x"}, CL(), "2026-10-19")


SEASON = [
    {"id": 1, "category": "SEASON_START", "name": "UTMB", "start_date_local": "2026-11-23T00:00:00"},
    {"id": 2, "category": "PLAN", "name": "UTMB", "start_date_local": "2026-11-23T00:00:00", "description": "Base Phase: Aerobe Kapazit\u00e4t"},
    {"id": 3, "category": "TARGET", "name": "Weekly", "type": "Run", "start_date_local": "2026-11-23T00:00:00",
     "load_target": 300, "time_target": 18000, "distance_target": 50000},
    {"id": 4, "category": "TARGET", "name": "Weekly", "type": "Run", "start_date_local": "2026-11-30T00:00:00"},
    {"id": 5, "category": "NOTE", "name": "Erholungswoche", "start_date_local": "2026-11-30T00:00:00"},
    {"id": 6, "category": "PLAN", "name": "UTMB", "start_date_local": "2027-04-12T00:00:00", "description": "Build Phase: Schwelle"},
    {"id": 7, "category": "RACE_A", "name": "UTMB", "start_date_local": "2027-08-27T00:00:00", "distance": 170000, "moving_time": 100800},
]


def test_season_summary_phases_weeks_races():
    out = summarize_season(SEASON, "2026-10-25")
    assert out["season_start"]["date"] == "2026-11-23"
    assert [(p["description"].split(":")[0], p["start"], p["end"]) for p in out["phases"]] == [
        ("Base Phase", "2026-11-23", "2027-04-11"), ("Build Phase", "2027-04-12", "2027-08-27")]
    w0, w1 = out["weeks"]
    assert (w0["week_no"], w0["phase"], w0["target_load"], w0["target_hours"], w0["target_km"]) == (1, "Base Phase", 300, 5.0, 50.0)
    assert w0["weeks_to_race"] == 39 and "recovery_week" not in w0
    assert w1["recovery_week"] is True and "target_load" not in w1
    assert out["races"][0]["planned_km"] == 170.0 and out["races"][0]["planned_h"] == 28.0
    assert out["weeks_with_targets"] == 1 and "sample_target_fields" not in out


def test_season_summary_reports_fields_when_targets_are_empty():
    out = summarize_season([e for e in SEASON if e["id"] != 3], "2026-10-25")
    assert out["weeks_with_targets"] == 0 and out["note"]
    assert out["sample_target_fields"]["category"] == "TARGET"
    assert summarize_season([], "2026-10-25")["weeks"] == []


def test_plan_vs_actual_passes_week_targets():
    row = plan_vs_actual([SEASON[2]], [], "2026-10-25")[0]["planned"][0]
    assert row["target_load"] == 300 and row["target_hours"] == 5.0 and row["target_km"] == 50.0


def test_get_season_plan_tool_filters_and_defaults(mk):
    c, http = mk({"/events": Resp(200, SEASON + [{"id": 99, "category": "WORKOUT", "name": "x", "start_date_local": "2026-10-20T00:00:00"}])})
    out = json.loads(rpc(c, "tools/call", {"name": "get_season_plan", "arguments": {}}).json()["result"]["content"][0]["text"])
    assert out["oldest"] == "2026-08-17" and out["newest"] == "2027-09-11"
    assert len(out["weeks"]) == 2 and len(out["phases"]) == 2
    assert http.calls[0][2]["oldest"] == "2026-08-17"
