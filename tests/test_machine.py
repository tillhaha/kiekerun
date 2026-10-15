from tcoach.machine import has_errors, validate
from tcoach.models import Workout


def mk(blocks, mode="run"):
    return Workout.from_dict({"name": "t", "mode": mode, "blocks": blocks})


def test_speed_and_incline_limits():
    assert not validate(mk([{"duration": "1:00", "speed": 22, "incline": 20}]))
    bad = mk([{"duration": "1:00", "speed": 23, "incline": 41}])
    assert len([i for i in validate(bad) if i.severity == "error"]) == 3  # speed, incline, steep-speed cap


def test_steep_speed_cap():
    assert not has_errors(validate(mk([{"duration": "1:00", "speed": 9, "incline": 40}])))
    assert has_errors(validate(mk([{"duration": "1:00", "speed": 9.5, "incline": 30}])))
    assert not has_errors(validate(mk([{"duration": "1:00", "speed": 12, "incline": 20}])))


def test_normal_interval_steps_do_not_warn():
    w = mk([{"duration": "1:00", "speed": 10.5, "incline": 6}, {"duration": "1:00", "speed": 6, "incline": 2}])
    assert not validate(w)
