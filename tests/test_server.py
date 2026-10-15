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
