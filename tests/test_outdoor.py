import random

import pytest

from tcoach import outdoor as o
from tcoach.intervals_client import IntervalsClient
from tests.test_server import AUTH, FakeHttp, FakeResp, make  # noqa: F401  (fixture re-used)
from server import app as appmod

ZONES = [137, 148, 157, 164, 180]


def test_zone_band():
    assert o.zone_band(2, ZONES) == (138.0, 148.0)
    assert o.zone_band(3, ZONES) == (149.0, 157.0)
    assert o.zone_band(1, ZONES)[1] == 137.0


def test_parse_zone_and_lthr_steps_with_repeat():
    text = "Intro\n\n- Warm-up 10m Z1 HR\nMain 3x\n- Tempo 5m 85-92% LTHR\n- Easy 3m Z2\n\n- Cool 1h5m Z1"
    steps, warns = o.parse_outdoor(text, ZONES, 163)
    assert not warns
    assert steps[0]["duration_s"] == 600 and steps[0]["zone"] == 1 and steps[0]["label"] == "Warm-up Z1"
    rep = steps[1]
    assert rep["repeat"] == 3 and [b["duration_s"] for b in rep["blocks"]] == [300, 180]
    assert rep["blocks"][0]["lo"] == pytest.approx(0.85 * 163) and rep["blocks"][0]["hi"] == pytest.approx(0.92 * 163)
    assert steps[2]["duration_s"] == 3900


def test_lthr_step_without_lthr_warns():
    steps, warns = o.parse_outdoor("- Tempo 5m 90% LTHR", ZONES, None)
    assert steps == [] and warns


def test_is_outdoor_text_ignores_treadmill_format():
    assert o.is_outdoor_text("- Easy 45m Z2 HR")
    assert not o.is_outdoor_text("- 45m 6:00/km Pace")
    assert not o.is_outdoor_text("- Steady 30 min @ 9.0 km/h (6:40 min/km) @ 5% incline")


def synth_streams(seed=1, n=2400):
    """A run whose speed follows HR (speed = 0.1*HR - 5) on flat ground, HR drifting 125..155."""
    rnd = random.Random(seed)
    hr = [125 + 30 * (i / n) + 6 * __import__("math").sin(i / 90) + rnd.gauss(0, .5) for i in range(n)]
    v = [(0.1 * h - 5) / 3.6 for h in hr]
    return [{"type": "time", "data": list(range(n))}, {"type": "heartrate", "data": hr},
            {"type": "velocity_smooth", "data": v}, {"type": "grade_smooth", "data": [0.2] * n}]


def test_fit_recovers_the_relationship():
    pts = o.samples_from_streams(synth_streams())
    m = o.fit(pts)
    assert m and m["b"] == pytest.approx(0.1, rel=0.1)
    assert o.speed_for_hr(m, 143) == pytest.approx(9.3, abs=0.4)


def test_fit_rejects_thin_or_flat_data():
    assert o.fit([]) is None
    assert o.fit([(140.0, 9.0)] * 1000) is None  # no HR spread


def test_inclined_samples_are_excluded():
    st = synth_streams()
    st[3]["data"] = [8.0] * len(st[3]["data"])  # steep: not flat running
    assert o.samples_from_streams(st) == []


def test_to_treadmill_uses_model_and_tags():
    m = {"a": -5.0, "b": 0.1, "n": 1000, "hr_range": [120, 160]}
    steps, _ = o.parse_outdoor("- Easy 60m Z2", ZONES, 163)
    w = o.to_treadmill("Easy", steps, m)
    b = w.steps[0]
    assert b.speed_kmh == 9.3 and b.incline_pct == o.DEFAULT_INCLINE and b.duration_s == 3600


class ActHttp(FakeHttp):
    """/activity/{id}/... lives outside /athlete/{id}/ in the real API."""
    def request(self, method, url, **kw):
        if "/athlete/" not in url:
            path = "/activity/" + url.split("/activity/", 1)[1]
            self.calls.append((method, path, kw))
            return self.routes.get((method, path), FakeResp(404, {}))
        return super().request(method, url, **kw)


@pytest.fixture
def make_act(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")

    def _make(routes):
        from fastapi.testclient import TestClient
        http = ActHttp(routes)
        monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=http))
        return TestClient(appmod.app), http
    return _make


def _routes(desc):
    ev = [{"id": 5, "name": "Easy Z2", "start_date_local": "2026-10-20T00:00:00", "description": desc}]
    acts = [{"id": "a1", "type": "Run", "distance": 9000, "start_date_local": "2026-10-18T07:00:00"}]
    athlete = {"sportSettings": [{"types": ["Run"], "hr_zones": ZONES, "lthr": 163}]}
    return {("GET", "/events"): FakeResp(200, ev), ("GET", "/activities"): FakeResp(200, acts),
            ("GET", ""): FakeResp(200, athlete), ("GET", "/activity/a1/streams"): FakeResp(200, synth_streams())}


def test_api_planned_converts_outdoor_workout(make_act):
    appmod._MODEL_CACHE.clear()
    c, _ = make_act(_routes("- Easy 60m Z2 HR"))
    it = c.get("/api/planned?date=2026-10-20", headers=AUTH).json()["items"][0]
    assert it["where"] == "outdoor" and it["workout"]["where"] == "outdoor"
    assert it["workout"]["blocks"][0]["speed"] == pytest.approx(9.3, abs=0.4)
    assert it["speed_model"]["activities"] == 1


def test_api_planned_outdoor_without_recent_runs_warns(make_act):
    appmod._MODEL_CACHE.clear()
    r = _routes("- Easy 60m Z2 HR")
    r[("GET", "/activities")] = FakeResp(200, [])
    c, _ = make_act(r)
    it = c.get("/api/planned?date=2026-10-20", headers=AUTH).json()["items"][0]
    assert it["workout"] is None and any("recent runs" in w for w in it["warnings"])
