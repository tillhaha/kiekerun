import math

import pytest

from tcoach.calories import estimate_kcal, vo2_ml_kg_min
from tcoach.feedback import Feedback, block_stats, build_description, parse_feedback
from tcoach.session import Sample


def samples(n, speed, incline, t0=0, hr=None):
    return [Sample(t0 + i, speed, incline, hr) for i in range(n)]


def test_vo2_acsm_reference_points():
    assert vo2_ml_kg_min(10, 0) == pytest.approx(0.2 * 166.667 + 3.5, rel=1e-3)      # running
    assert vo2_ml_kg_min(5, 10) == pytest.approx(0.1 * 83.333 + 1.8 * 83.333 * .1 + 3.5, rel=1e-3)  # walking
    assert vo2_ml_kg_min(7, 0) == pytest.approx((vo2_ml_kg_min(6, 0) * 0 + (0.1 * 116.667 + 3.5 + 0.2 * 116.667 + 3.5) / 2), rel=1e-3)  # blend
    assert vo2_ml_kg_min(10, -5) == vo2_ml_kg_min(10, 0)  # decline treated as flat


def test_kcal_one_hour_flat_10kmh_70kg():
    assert estimate_kcal(samples(3600, 10, 0), 70) == pytest.approx(773.5, abs=1)


def test_kcal_rises_with_incline_and_weight_and_rejects_bad_weight():
    assert estimate_kcal(samples(600, 6, 15), 70) > estimate_kcal(samples(600, 6, 0), 70)
    assert estimate_kcal(samples(600, 8, 0), 80) > estimate_kcal(samples(600, 8, 0), 70)
    with pytest.raises(ValueError):
        estimate_kcal(samples(10, 8, 0), 0)


def test_parse_feedback_valid_and_empty():
    fb = parse_feedback({"rpe": 7, "feel": 2, "note": " gut \n", "carbs": [[600, 25], [1800, 40]]})
    assert (fb.rpe, fb.feel, fb.note, fb.carbs_total) == (7, 2, "gut", 65)
    assert parse_feedback(None).is_empty() and parse_feedback({}).is_empty()


@pytest.mark.parametrize("bad", [{"rpe": 0}, {"rpe": 11}, {"rpe": 7.5}, {"rpe": True}, {"feel": 6}, {"note": "x" * 2001},
                                 {"carbs": [[1, 0]]}, {"carbs": [[1, 301]]}, {"carbs": [5]}, "text"])
def test_parse_feedback_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_feedback(bad)


def test_block_stats_vertical_speed_is_computed_from_speed_and_incline():
    s = samples(20, 6, 10, 0, hr=150) + samples(20, 5, 0, 20)
    st = block_stats(s, [{"label": "A", "t0": 0, "dur": 20}, {"label": "B", "t0": 20, "dur": 20}])
    a, b = st
    expected_ascent = 20 * (6 / 3.6) * math.sin(math.atan(0.10))
    assert a.ascent_m == pytest.approx(expected_ascent) and a.vam_m_h == pytest.approx(expected_ascent * 180)
    assert a.avg_hr == 150 and b.avg_hr is None and b.ascent_m == 0


def test_block_stats_skips_unreached_and_tiny_blocks():
    s = samples(30, 6, 5)
    st = block_stats(s, [{"label": "A", "t0": 0, "dur": 20}, {"label": "partial", "t0": 20, "dur": 60},
                         {"label": "never", "t0": 90, "dur": 60}, {"label": "tiny", "t0": 28, "dur": 3}])
    assert [(b.label, b.dur_s) for b in st] == [("A", 20), ("partial", 10)]


def test_description_contains_note_vam_carbs_and_kcal():
    s = samples(1800, 5, 20, hr=140)
    st = block_stats(s, [{"label": "Climb 1/2 · Hike", "t0": 0, "dur": 1800}])
    d = build_description(Feedback(note="Beine schwer", carbs=[(600, 25), (1200, 25)]), st, 1800, 410)
    assert d.startswith("Beine schwer")
    assert "Climb 1/2 · Hike" in d and "5.0 km/h @ 20%" in d and "m/h" in d and "HR 140" in d
    assert "Carbs 50 g (100 g/h)" in d and "Calories ~410 kcal (estimate)" in d


def test_description_omits_flat_blocks_and_empty_parts():
    st = block_stats(samples(60, 8, 0), [{"label": "Flat", "t0": 0, "dur": 60}])
    assert build_description(Feedback(), st, 60, None) == ""
    assert "per block" not in build_description(Feedback(note="x"), st, 60, None)


def hr_samples(n, speed, incline, hr_fn):
    return [Sample(i, speed, incline, hr_fn(i)) for i in range(n)]


def test_drift_for_constant_load_block():
    s = hr_samples(1800, 5.5, 16, lambda i: 140 if i < 900 else 147)
    st = block_stats(s, [{"label": "AeT test", "t0": 0, "dur": 1800}])[0]
    assert (st.hr_first, st.hr_second) == (140, 147)
    assert st.drift_pct == pytest.approx(4.8, abs=0.01)
    d = build_description(Feedback(), st and [st], 1800, None)
    assert "HR drift" in d and "HR 140 -> 147 · drift +4.8%" in d


def test_no_drift_for_short_adjusted_or_hr_less_blocks():
    short = block_stats(hr_samples(900, 5.5, 16, lambda i: 140), [{"label": "x", "t0": 0, "dur": 900}])[0]
    assert short.drift_pct is None
    adjusted = [Sample(i, 5.5 if i < 900 else 6.5, 16, 140) for i in range(1800)]
    assert block_stats(adjusted, [{"label": "x", "t0": 0, "dur": 1800}])[0].drift_pct is None
    nohr = block_stats(hr_samples(1800, 5.5, 16, lambda i: None), [{"label": "x", "t0": 0, "dur": 1800}])[0]
    assert nohr.drift_pct is None
    assert "HR drift" not in build_description(Feedback(note="n"), [nohr], 1800, None)


def test_negative_drift_when_hr_falls():
    s = hr_samples(1800, 5.5, 16, lambda i: 145 if i < 900 else 142)
    assert block_stats(s, [{"label": "x", "t0": 0, "dur": 1800}])[0].drift_pct < 0
