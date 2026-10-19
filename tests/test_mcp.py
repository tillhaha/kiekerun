import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from tcoach.coach_data import plan_vs_actual, resolve_range, summarize_activity, summarize_wellness
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
    assert names == ["get_activities", "get_activity", "get_wellness", "get_zones", "get_planned_workout", "get_plan_vs_actual"]
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
