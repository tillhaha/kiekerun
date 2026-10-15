import math

import pytest

from tcoach.models import Workout, parse_duration


def test_parse_duration_variants():
    assert parse_duration(90) == 90
    assert parse_duration("10:00") == 600
    assert parse_duration("1:05:00") == 3900
    assert parse_duration("10m") == 600
    assert parse_duration("1m30") == 90
    assert parse_duration("45s") == 45
    assert parse_duration("1h") == 3600
    for bad in ("", "abc", 0, "0:00", True):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_repeat_expansion_and_totals():
    w = Workout.from_dict({
        "name": "t", "blocks": [
            {"label": "w", "duration": "1:00", "speed": 6, "incline": 0},
            {"repeat": 3, "blocks": [
                {"label": "h", "duration": "2:00", "speed": 10, "incline": 5},
                {"label": "e", "duration": "1:00", "speed": 6, "incline": 0}]},
        ]})
    flat = w.flat()
    assert len(flat) == 7
    assert w.duration_s == 60 + 3 * 180
    assert w.distance_km == pytest.approx(0.1 + 3 * (10 * 120 / 3600 + 6 * 60 / 3600))


def test_vertical_gain_formula():
    # 60 min at 5 km/h on 30 % -> 5 km belt distance * sin(atan(0.3))
    w = Workout.from_dict({"name": "t", "blocks": [
        {"label": "s", "duration": "60:00", "speed": 5, "incline": 30}]})
    expected = 5000 * math.sin(math.atan(0.3))
    assert w.ascent_m == pytest.approx(expected)
    assert 1430 < expected < 1440


def test_decline_counts_as_descent_not_ascent():
    w = Workout.from_dict({"name": "t", "blocks": [
        {"label": "d", "duration": "10:00", "speed": 8, "incline": -4}]})
    up, down = w.ascent_descent_m()
    assert up == 0 and down > 0


def test_json_round_trip_preserves_repeats():
    d = {"name": "x", "blocks": [
        {"repeat": 2, "label": "Main", "blocks": [
            {"label": "a", "duration": "1:00", "speed": 9.0, "incline": 4.0}]}]}
    w = Workout.from_dict(d)
    w2 = Workout.from_dict(w.to_dict())
    assert w2.duration_s == w.duration_s and len(w2.steps) == 1


def test_invalid_mode_and_empty():
    with pytest.raises(ValueError):
        Workout.from_dict({"name": "x", "mode": "fly", "blocks": [{"duration": "1:00", "speed": 5}]})
    with pytest.raises(ValueError):
        Workout.from_dict({"name": "x", "blocks": []})
