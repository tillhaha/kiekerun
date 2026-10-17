import pytest

from tcoach.intervals_text import parse_intervals_text, to_intervals_text
from tcoach.models import Workout

BASE = {"name": "Fuel test", "blocks": [{"label": "Steady", "duration": "1:00:00", "speed": 8, "incline": 2}]}


def wk(fuel):
    return Workout.from_dict({**BASE, "fuel": fuel})


def test_single_and_repeating_entries_expand_and_sort():
    w = wk([{"every": "20:00", "carbs_g": 30, "note": "Gel"}, {"at": "5:00", "fluid_ml": 150}])
    assert [f.at_s for f in w.fuel] == [300, 1200, 2400, 3600]
    assert w.fuel[1].carbs_g == 30 and w.fuel[0].fluid_ml == 150


def test_from_until():
    w = wk([{"every": "10:00", "from": "15:00", "until": "35:00", "carbs_g": 20}])
    assert [f.at_s for f in w.fuel] == [900, 1500, 2100]


@pytest.mark.parametrize("bad", [
    [{"at": "70:00", "carbs_g": 10}],       # after end
    [{"carbs_g": 10}],                      # no time
    [{"at": "5:00"}],                       # nothing to take
    [{"every": "0:30", "carbs_g": 5}],      # too frequent
    [{"at": "5:00", "carbs_g": -1}],
    [{"at": "5:00", "carbs_g": True}],
    "gel",
])
def test_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        wk(bad)


def test_json_roundtrip_and_text_roundtrip():
    w = wk([{"at": "20:00", "carbs_g": 30, "fluid_ml": 150, "note": "Gel"}, {"at": "45:30", "note": "Salt"}])
    d = w.to_dict()
    assert d["fuel"][0] == {"at": "20:00", "carbs_g": 30, "fluid_ml": 150, "note": "Gel"}
    text = to_intervals_text(w)
    assert "Fuel plan\n@ 20:00 · 30 g carbs · 150 ml · Gel\n@ 45:30 · Salt" in text
    w2, warns = parse_intervals_text(text)
    assert warns == []
    assert w2.to_dict()["fuel"] == d["fuel"]
    assert len(w2.flat()) == 1


def test_no_fuel_leaves_text_unchanged():
    assert "Fuel plan" not in to_intervals_text(Workout.from_dict(BASE))
    assert "fuel" not in Workout.from_dict(BASE).to_dict()


def test_flask_amounts_are_derived_from_the_principles():
    w = wk([{"at": "10:00", "kind": "gel", "flask": 0.5, "note": "V4-Mix"},
            {"at": "20:00", "kind": "drink", "note": "V3-Mix"},
            {"at": "30:00", "kind": "gel", "flask": 1, "carbs_g": 100}])
    g, d, o = w.fuel
    assert (g.carbs_g, g.fluid_ml, g.flask) == (55, 75, 0.5)
    assert (d.carbs_g, d.fluid_ml, d.flask) == (38, 500, 1.0)   # 37.5 g rounds to 38
    assert o.carbs_g == 100 and o.fluid_ml == 150               # explicit carbs win, ml still derived


@pytest.mark.parametrize("bad", [
    [{"at": "5:00", "kind": "bar", "flask": 1}],
    [{"at": "5:00", "kind": "gel", "flask": 0}],
    [{"at": "5:00", "kind": "gel", "flask": 9}],
    [{"at": "5:00", "kind": "gel", "flask": "x"}],
])
def test_rejects_bad_flask(bad):
    with pytest.raises(ValueError):
        wk(bad)


def test_flask_text_roundtrip():
    w = wk([{"at": "10:00", "kind": "gel", "flask": 0.5, "note": "V4-Mix"}, {"at": "20:00", "kind": "drink", "flask": 1}])
    text = to_intervals_text(w)
    assert "@ 10:00 · 0.5 Gel-Flask · 55 g carbs · 75 ml · V4-Mix" in text
    assert "@ 20:00 · 1 Drink-Flask · 38 g carbs · 500 ml" in text
    w2, warns = parse_intervals_text(text)
    assert warns == [] and w2.to_dict()["fuel"] == w.to_dict()["fuel"]
