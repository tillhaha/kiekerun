from tcoach.intervals_client import IntervalsClient, IntervalsError
from tcoach.intervals_text import dur_str, pace_str, parse_intervals_text, speed_from_pace, to_intervals_text
from tcoach.models import Workout

import pytest

W = {
    "name": "Mixed", "blocks": [
        {"label": "Warm-up", "duration": "8:00", "speed": 5.0, "incline": 10},
        {"repeat": 3, "label": "Main set", "blocks": [
            {"label": "Steep", "duration": "12:00", "speed": 5.0, "incline": 30},
            {"label": "Recover", "duration": "3:30", "speed": 4.0, "incline": 15}]},
        {"label": "Cool-down", "duration": "5:00", "speed": 5.5, "incline": -2},
    ]}


def test_pace_helpers():
    assert pace_str(6.0) == "10:00"
    assert pace_str(10.5) == "5:43"
    assert speed_from_pace("10:00") == 6.0
    assert speed_from_pace("5:43") == 10.5
    assert dur_str(600) == "10m" and dur_str(210) == "3m30" and dur_str(45) == "45s"


def test_text_contains_expected_syntax():
    text = to_intervals_text(Workout.from_dict(W))
    assert "Main set 3x" in text
    assert "- Steep incline 30pct 12m 12:00/km Pace" in text
    assert "- Warm-up incline 10pct 8m 12:00/km Pace" in text
    assert "mode" not in text.lower()
    assert "%" not in text  # would be read as a % of threshold target by intervals.icu


def test_round_trip():
    w = Workout.from_dict(W)
    w2, warns = parse_intervals_text(to_intervals_text(w), name=w.name)
    assert not warns
    a, b = w.flat(), w2.flat()
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert (x.label, x.duration_s, x.incline_pct) == (y.label, y.duration_s, y.incline_pct)
        assert x.speed_kmh == pytest.approx(y.speed_kmh, abs=0.05)


def test_parse_reports_unparsable_steps():
    text = "- Hard 10m 5:00/km Pace\n- Easy incline 2pct 5m 6:00/km Pace\n"
    w, warns = parse_intervals_text(text)
    assert len(w.flat()) == 1 and any("unparsed" in x for x in warns)


class FakeResp:
    def __init__(self, data=None, status=200):
        self.status_code, self._d = status, data
        self.content = b"x" if data is not None else b""
        self.text = str(data)

    def json(self):
        return self._d


class FakeHttp:
    def __init__(self, resp):
        self.calls, self.resp = [], resp

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.resp


def test_build_event_payload_is_idempotent_key():
    w = Workout.from_dict(W)
    ev = IntervalsClient.build_event(w, "2026-10-20")
    assert ev["category"] == "WORKOUT" and ev["start_date_local"] == "2026-10-20T00:00:00"
    assert ev["external_id"] == "tcoach-2026-10-20-mixed"
    assert ev["moving_time"] == w.duration_s and "Main set 3x" in ev["description"]


def test_push_dry_run_makes_no_call_and_real_push_looks_up_then_creates():
    class Seq(FakeHttp):
        def request(self, method, url, **kw):
            self.calls.append((method, url, kw))
            return FakeResp([]) if method == "GET" else FakeResp({"id": 1})

    http = Seq(None)
    c = IntervalsClient("k", "i123", http=http)
    w = Workout.from_dict(W)
    assert c.push_planned([(w, "2026-10-20")], dry_run=True)[0]["name"] == "Mixed"
    assert http.calls == []
    c.push_planned([(w, "2026-10-20")])
    (m1, u1, k1), (m2, u2, k2) = http.calls
    assert m1 == "GET" and u1.endswith("/athlete/i123/events") and k1["auth"] == ("API_KEY", "k")
    assert m2 == "POST" and u2.endswith("/athlete/i123/events") and k2["json"]["external_id"] == "tcoach-2026-10-20-mixed"


def test_errors_and_missing_key():
    with pytest.raises(IntervalsError):
        IntervalsClient("")
    c = IntervalsClient("k", http=FakeHttp(FakeResp("nope", status=401)))
    with pytest.raises(IntervalsError):
        c.list_planned("2026-10-01", "2026-10-08")
