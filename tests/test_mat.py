import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from tcoach import mat
from tcoach.intervals_client import IntervalsClient
from tests.test_mcp_write import Cal, Resp, TOKEN, call
from tests.test_server import AUTH, ActHttp, FakeResp


def work(w):
    return [s for s in w["steps"] if s["t"] == "work"]


def test_me_wo1_has_four_exercises_and_doubled_times():
    w = mat.me_workout(1)
    names = {s["label"].split(" · ")[0] for s in work(w)}
    assert names == {"Split jump squat", "Squat jump", "Box step-up", "Front lunge"}
    assert len(work(w)) == 4 * 4 + 2 * 4          # 4 sets; step-up and lunge have right + left
    assert work(w)[0]["dur"] == 20 and work(w)[0]["reps"] == 10   # book: 10 s, doubled
    assert w["steps"][0]["t"] == "rest" and w["steps"][0]["dur"] == 10     # no warm-up: starts with the get-ready rest
    assert [s["t"] for s in w["steps"] if s["t"] in ("warm", "manual")] == []
    assert w["steps"][-1]["t"] == "cool" and w["steps"][-1]["dur"] == 600


def test_me_wo4_adds_goblet_and_swing_and_progression_rests():
    w = mat.me_workout(4)
    names = {s["label"].split(" · ")[0] for s in work(w)}
    assert {"Goblet squat", "2-hand kettlebell swing"} <= names and w["sets"] == 5
    rests = [s["dur"] for s in w["steps"] if s["t"] == "rest" and s["label"].startswith("Rest")]
    assert set(rests) == {60}
    assert {s["dur"] for s in mat.me_workout(13)["steps"] if s["label"].startswith("Switch")} == {10}


def test_me_validation():
    for bad in (0, 15, True, "3"):
        with pytest.raises(ValueError):
            mat.me_workout(bad)
    with pytest.raises(ValueError):
        mat.me_workout(3, sets=9)


def test_core15_structure():
    w = mat.core15_workout()
    assert [s["dur"] for s in w["steps"][:4]] == [30] * 4          # 2 min warm-up
    assert len(work(w)) == 15 and {s["dur"] for s in work(w)} == {40}
    assert w["steps"][-1]["t"] == "work"                            # no trailing change
    assert round(w["duration_s"] / 60) in (13, 14)
    h = mat.core15_workout(2, hard=True)
    assert len(work(h)) == 10 and {s["dur"] for s in work(h)} == {45}


def test_marker_roundtrip():
    assert mat.marker("me", wo=5, sets=6) == "KiekeRun: me wo=5 sets=6"
    assert mat.parse_marker("note\nKiekeRun: me wo=5 sets=6\n- x") == ("me", {"wo": 5, "sets": 6})
    assert mat.parse_marker("KiekeRun: core15 rounds=2 hard=1") == ("core15", {"rounds": 2, "hard": True})
    assert mat.parse_marker("- 30m Z2 HR") is None


@pytest.fixture
def make3(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")

    def _make(routes):
        http = ActHttp(routes)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=http))
        return TestClient(appmod.app), http
    return _make


def test_planned_returns_mat_workout(make3):
    ev = {"id": 9, "name": "ME WO 5", "start_date_local": "2026-11-30T00:00:00",
          "description": "KiekeRun: me wo=5 sets=6\nMuscular endurance WO 5"}
    c, _ = make3({("GET", "/events"): FakeResp(200, [ev])})
    it = c.get("/api/planned?date=2026-11-30", headers=AUTH).json()["items"][0]
    assert it["where"] == "mat" and it["mat"]["wo"] == 5 and it["event_id"] == 9 and it["workout"] is None


def test_mat_workout_endpoint_and_bad_input(make3):
    c, _ = make3({})
    r = c.get("/api/mat/workout?kind=core15&rounds=2", headers=AUTH).json()
    assert r["rounds"] == 2
    assert c.get("/api/mat/workout?kind=me&wo=99", headers=AUTH).status_code == 422
    assert c.get("/api/mat/workout?kind=yoga", headers=AUTH).status_code == 422


def test_mat_upload_creates_manual_activity_with_feedback(make3):
    seen = {}

    def manual(method, url, **kw):
        seen["json"] = kw["json"]
        return FakeResp(200, {"id": "i88"})
    c, http = make3({("POST", "/activities/manual"): lambda kw: manual("POST", "", **kw)})
    body = {"kind": "me", "wo": 5, "sets": 6, "started_at": "2026-11-30T17:00:00Z", "duration_s": 2900,
            "feedback": {"rpe": 6, "feel": 2, "note": "solid"}, "paired_event_id": 9}
    r = c.post("/api/mat/upload", json=body, headers=AUTH).json()
    assert r["ok"] and r["id"] == "i88" and r["updated"] == ["feel", "icu_rpe"]
    ev = seen["json"]
    assert ev["type"] == "WeightTraining" and ev["name"] == "ME WO 5" and ev["paired_event_id"] == 9
    assert ev["moving_time"] == 2900 and ev["start_date_local"] == "2026-11-30T18:00:00"
    assert ev["description"].startswith("KiekeRun: me wo=5 sets=6") and ev["description"].endswith("solid")
    assert http.puts[0][1] == {"icu_rpe": 6, "feel": 2}
    assert c.post("/api/mat/upload", json={**body, "duration_s": 5}, headers=AUTH).status_code == 422


def test_mat_image_endpoint(make3, tmp_path, monkeypatch):
    monkeypatch.setenv("KIEKERUN_IMG_DIR", str(tmp_path))
    (tmp_path / "dead-bug.png").write_bytes(b"png")
    c, _ = make3({})
    assert c.get("/api/mat/img/dead-bug", headers=AUTH).content == b"png"
    assert c.get("/api/mat/img/nope", headers=AUTH).status_code == 404
    assert c.get("/api/mat/img/..%2Fx", headers=AUTH).status_code == 404


def test_mat_page_served(make3):
    c, _ = make3({})
    r = c.get("/mat", headers=AUTH)
    assert r.status_code == 200 and "Mat workout" in r.text
    assert c.get("/mat").status_code == 401


class MatCal(Cal):
    def request(self, method, url, **kw):
        if method == "GET" and url.endswith("/activities"):
            return Resp(200, [{"id": "a1", "start_date_local": "2026-11-23T08:00:00", "description": "KiekeRun: me wo=3 sets=6"},
                              {"id": "a2", "start_date_local": "2026-11-20T08:00:00", "description": "KiekeRun: core15 rounds=3"}])
        return super().request(method, url, **kw)


def test_mcp_save_mat_workout_and_progress(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_ALLOW_WRITE", "1")
    cal = MatCal()
    monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=cal))
    monkeypatch.setattr(appmod, "today_local", lambda: "2026-11-26")
    c = TestClient(appmod.app)
    r = call(c, "save_mat_workout", {"date": "2026-11-30", "kind": "me", "wo": 4, "sets": 5})
    assert not r["isError"] and r["data"]["name"] == "ME WO 4"
    ev = next(iter(cal.events.values()))
    assert ev["type"] == "WeightTraining" and ev["description"].startswith("KiekeRun: me wo=4 sets=5")
    assert call(c, "save_mat_workout", {"date": "2026-11-30", "kind": "me"})["isError"]          # wo missing
    assert call(c, "save_mat_workout", {"date": "2026-11-30", "kind": "core15", "rounds": 9})["isError"]
    p = call(c, "get_mat_progress", {})["data"]
    assert p["me"]["last_done"]["wo"] == 3 and p["me"]["next_wo"] == 5          # planned WO 4 counts
    assert p["core15"]["last_done"]["rounds"] == 3
