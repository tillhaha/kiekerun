import base64
import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from tcoach.intervals_client import IntervalsClient
from tcoach.intervals_text import to_intervals_text
from tcoach.models import Workout

AUTH = {"Authorization": "Basic " + base64.b64encode(b"tito:secret").decode()}


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d = status, data
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeHttp:
    """Records calls; `routes` maps (METHOD, path-suffix) -> FakeResp or callable."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kw):
        parts = url.split("/athlete/", 1)[1].split("/", 1)
        path = "/" + parts[1] if len(parts) > 1 else ""
        self.calls.append((method, path, kw))
        r = self.routes.get((method, path))
        if r is None:
            return FakeResp(404, {"error": "nope"})
        return r(kw) if callable(r) else r


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")

    def _make(routes):
        http = FakeHttp(routes)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=http))
        return TestClient(appmod.app), http
    return _make


def test_api_fails_closed_without_login_config(monkeypatch):
    monkeypatch.delenv("KIEKERUN_USER", raising=False)
    monkeypatch.delenv("KIEKERUN_PASSWORD", raising=False)
    c = TestClient(appmod.app)
    assert c.get("/api/health").status_code == 503
    assert c.get("/api/health", headers=AUTH).status_code == 503


def test_api_requires_login(make):
    c, _ = make({})
    r = c.get("/api/health")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    bad = {"Authorization": "Basic " + base64.b64encode(b"tito:wrong").decode()}
    assert c.get("/api/health", headers=bad).status_code == 401


def test_page_public_until_login_configured_then_protected(make, monkeypatch):
    c, _ = make({})
    assert c.get("/").status_code == 401
    assert c.get("/", headers=AUTH).status_code == 200
    monkeypatch.delenv("KIEKERUN_USER")
    assert TestClient(appmod.app).get("/").status_code == 200


def test_health(make):
    c, http = make({("GET", ""): FakeResp(200, {"id": "i1", "name": "Tito"})})
    r = c.get("/api/health", headers=AUTH)
    assert r.json() == {"ok": True, "athlete_id": "i1", "name": "Tito"}
    assert http.calls[0][2]["auth"] == ("API_KEY", "k")


def test_health_reports_rejected_key_as_502_not_401(make):
    c, _ = make({("GET", ""): FakeResp(401, {})})
    r = c.get("/api/health", headers=AUTH)
    assert r.status_code == 502 and "rejected" in r.json()["detail"]  # 401 would trigger a browser login prompt


def test_planned_converts_tcoach_workouts_and_flags_others(make):
    w = Workout.load("examples/threshold_4x8.json")
    events = [
        {"id": 7, "name": w.name, "start_date_local": "2026-10-20T00:00:00", "description": to_intervals_text(w)},
        {"id": 8, "name": "Easy run", "start_date_local": "2026-10-20T00:00:00", "description": "- 45m 6:00/km Pace"},
    ]
    c, http = make({("GET", "/events"): FakeResp(200, events)})
    items = c.get("/api/planned?date=2026-10-20", headers=AUTH).json()["items"]
    assert items[0]["event_id"] == 7 and items[0]["workout"]["blocks"][0]["label"] == "Warm-up"
    assert items[1]["workout"] is None and items[1]["warnings"]
    assert http.calls[0][2]["params"] == {"oldest": "2026-10-20", "newest": "2026-10-20", "category": "WORKOUT"}


def test_planned_falls_back_to_events_json_on_404(make):
    c, http = make({("GET", "/events.json"): FakeResp(200, [])})
    assert c.get("/api/planned?date=2026-10-20", headers=AUTH).json()["items"] == []
    assert [x[1] for x in http.calls] == ["/events", "/events.json"]


def test_planned_rejects_bad_date(make):
    c, _ = make({})
    assert c.get("/api/planned?date=20.10.2026", headers=AUTH).status_code == 422


def test_plan_creates_then_updates_by_external_id(make):
    w = Workout.load("examples/threshold_4x8.json")
    body = {"workout": w.to_dict(), "date": "2026-10-20"}
    c, http = make({("GET", "/events"): FakeResp(200, []), ("POST", "/events"): FakeResp(200, {"id": 1})})
    assert c.post("/api/planned", json=body, headers=AUTH).json()["event"] == {"id": 1}
    sent = http.calls[-1][2]["json"]
    assert sent["external_id"].startswith("tcoach-2026-10-20-") and sent["category"] == "WORKOUT"

    existing = [{"id": 99, "external_id": sent["external_id"]}]
    c, http = make({("GET", "/events"): FakeResp(200, existing), ("PUT", "/events/99"): FakeResp(200, {"id": 99})})
    assert c.post("/api/planned", json=body, headers=AUTH).json()["event"] == {"id": 99}
    assert [x[0] for x in http.calls] == ["GET", "PUT"]  # no duplicate created


def test_plan_rejects_machine_limit_violation(make):
    bad = {"name": "x", "blocks": [{"label": "a", "duration": "1:00", "speed": 15, "incline": 30}]}
    c, _ = make({})
    assert c.post("/api/planned", json={"workout": bad, "date": "2026-10-20"}, headers=AUTH).status_code == 422


def test_upload_builds_fit_and_pairs_event(make):
    log = {"version": 1, "name": "Test", "started_at": "2026-10-20T12:00:00Z", "duration_s": 5,
           "events": [{"t": 0, "speed": 8, "incline": 2}], "hr": [[1, 120], [3, 130]]}
    seen = {}

    def up(kw):
        seen.update(kw)
        seen["bytes"] = kw["files"]["file"][1].read()
        return FakeResp(201, {"activities": [{"id": "i55"}]})

    c, _ = make({("POST", "/activities"): up})
    r = c.post("/api/upload", json={"log": log, "paired_event_id": 7}, headers=AUTH).json()
    assert r["ok"] and r["totals"]["duration_s"] == 5
    assert seen["params"]["device_name"] == "KiekeRun"
    assert seen["params"]["paired_event_id"] == 7 and seen["params"]["external_id"] == "kiekerun-20261020T120000"
    assert seen["bytes"][8:12] == b".FIT"


def test_upload_rejects_bad_log(make):
    c, _ = make({})
    assert c.post("/api/upload", json={"log": {"events": []}}, headers=AUTH).status_code == 422


def _log(**extra):
    base = {"version": 1, "name": "Test", "started_at": "2026-10-20T12:00:00Z", "duration_s": 60,
            "events": [{"t": 0, "speed": 5, "incline": 20}], "hr": [],
            "blocks": [{"label": "Hike", "t0": 0, "dur": 60}]}
    base.update(extra)
    return base


def _routes(put=None, weight=70, activities=({"id": "i77"},)):
    r = {("POST", "/activities"): FakeResp(201, {"activities": list(activities)}),
         ("PUT_ACT", ""): put}
    if weight:
        r[("GET", "")] = FakeResp(200, {"id": "i1", "icu_weight": weight})
    return r


class ActHttp(FakeHttp):
    """FakeHttp that also answers the non-athlete PUT /activity/{id} and records it."""
    def __init__(self, routes, put_status=200):
        super().__init__(routes)
        self.puts, self.put_status = [], put_status

    def request(self, method, url, **kw):
        if method == "PUT" and "/activity/" in url:
            self.puts.append((url, kw["json"]))
            return FakeResp(self.put_status, {"id": "i77"} if self.put_status < 400 else {"error": "boom"})
        return super().request(method, url, **kw)


@pytest.fixture
def make2(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")
    monkeypatch.delenv("BODY_WEIGHT_KG", raising=False)

    def _make(routes, put_status=200):
        http = ActHttp(routes, put_status)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=http))
        return TestClient(appmod.app), http
    return _make


def test_upload_applies_feedback_description_and_calories(make2):
    fb = {"rpe": 8, "feel": 2, "note": "Beine schwer", "carbs": [[10, 25], [40, 25]]}
    c, http = make2(_routes())
    r = c.post("/api/upload", json={"log": _log(feedback=fb), "paired_event_id": 3}, headers=AUTH).json()
    assert r["ok"] and r["calories"] and r["update_error"] is None
    assert r["updated"] == ["carbs_ingested", "description", "feel", "icu_rpe"]
    (url, fields), = http.puts
    assert url.endswith("/api/v1/activity/i77")
    assert (fields["icu_rpe"], fields["feel"], fields["carbs_ingested"]) == (8, 2, 50)
    assert fields["description"].startswith("Beine schwer") and "Hike" in fields["description"]
    assert f"Calories ~{r['calories']} kcal" in fields["description"]


def test_upload_with_nothing_to_add_does_not_call_put(make2):
    c, http = make2(_routes(weight=None))  # no feedback, no blocks, no weight -> nothing to write
    log = _log(); log.pop("blocks")
    r = c.post("/api/upload", json={"log": log}, headers=AUTH).json()
    assert r["ok"] and r["updated"] == [] and http.puts == []


def test_upload_with_weight_only_writes_calorie_estimate_description(make2):
    c, http = make2(_routes())
    log = _log(); log.pop("blocks")
    r = c.post("/api/upload", json={"log": log}, headers=AUTH).json()
    assert r["updated"] == ["description"]
    assert http.puts[0][1]["description"].startswith("Calories ~")


def test_upload_survives_failed_update_and_reports_it(make2):
    c, http = make2(_routes(), put_status=500)
    r = c.post("/api/upload", json={"log": _log(feedback={"rpe": 5})}, headers=AUTH).json()
    assert r["ok"] and "500" in r["update_error"]


def test_upload_without_weight_skips_calories(make2):
    c, _ = make2(_routes(weight=None))
    r = c.post("/api/upload", json={"log": _log(feedback={"rpe": 5})}, headers=AUTH).json()
    assert r["calories"] is None


def test_upload_weight_falls_back_to_env(make2, monkeypatch):
    monkeypatch.setenv("BODY_WEIGHT_KG", "80")
    c, _ = make2(_routes(weight=None))
    assert c.post("/api/upload", json={"log": _log()}, headers=AUTH).json()["calories"] > 0


def test_upload_duplicate_has_no_id_and_no_update(make2):
    c, http = make2(_routes(activities=()))
    r = c.post("/api/upload", json={"log": _log(feedback={"rpe": 5})}, headers=AUTH).json()
    assert r["ok"] and r["update_error"] is None and http.puts == []


def test_upload_rejects_bad_feedback(make2):
    c, _ = make2(_routes())
    assert c.post("/api/upload", json={"log": _log(feedback={"rpe": 11})}, headers=AUTH).status_code == 422
    assert c.post("/api/upload", json={"log": _log(blocks=[{"label": "x"}])}, headers=AUTH).status_code == 422
