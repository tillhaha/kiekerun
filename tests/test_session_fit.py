import json
from datetime import datetime, timezone

import pytest
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage

from tcoach.cli import main
from tcoach.fit_writer import write_fit
from tcoach.models import Workout
from tcoach.session import attach_hr, load_hr_csv, samples_from_blocks, samples_from_log, totals
from tcoach.treadmill import FakeClock, SafetyError, SimTreadmill, run_workout

START = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def workout():
    return Workout.from_dict({"name": "t", "blocks": [
        {"label": "a", "duration": "2:00", "speed": 5, "incline": 10},
        {"label": "b", "duration": "5:00", "speed": 5, "incline": 30}]})


def read_back(path):
    f = FitFile.from_file(str(path))
    msgs = [r.message for r in f.records]
    return ([m for m in msgs if isinstance(m, RecordMessage)],
            [m for m in msgs if isinstance(m, SessionMessage)][0])


def test_runner_on_simulator_matches_plan():
    w = workout()
    tm = SimTreadmill()
    blocks_seen = []
    samples = run_workout(tm, w, clock=FakeClock(), on_block=lambda i, b: blocks_seen.append(b.label))
    assert len(samples) == w.duration_s and blocks_seen == ["a", "b"]
    assert totals(samples)["ascent_m"] == pytest.approx(w.ascent_m)
    assert tm.speed == 0.0 and not tm.connected  # stopped and disconnected afterwards


def test_runner_refuses_workouts_outside_machine_limits():
    bad = Workout.from_dict({"name": "x", "blocks": [{"duration": "1:00", "speed": 12, "incline": 30}]})
    tm = SimTreadmill()
    with pytest.raises(SafetyError):
        run_workout(tm, bad, clock=FakeClock())
    assert tm.commands == []


def test_fit_round_trip_has_elevation(tmp_path):
    w = workout()
    samples = samples_from_blocks(w.flat())
    out = tmp_path / "s.fit"
    tot = write_fit(samples, out, start=START)
    records, session = read_back(out)
    assert len(records) == len(samples)
    assert records[-1].enhanced_altitude == pytest.approx(tot["ascent_m"], abs=0.2)
    assert session.total_ascent == round(w.ascent_m)
    assert session.total_distance == pytest.approx(tot["distance_m"], abs=0.1)
    assert records[-1].grade == 30


def test_fit_with_heart_rate(tmp_path):
    samples = samples_from_blocks(workout().flat())
    attach_hr(samples, {0: 100, 60: 140, 200: 165})
    out = tmp_path / "hr.fit"
    write_fit(samples, out, start=START)
    records, session = read_back(out)
    assert records[0].heart_rate == 100 and records[100].heart_rate == 140 and records[-1].heart_rate == 165
    assert session.max_heart_rate == 165


def test_log_to_samples_applies_events_in_order():
    log = {"duration_s": 10, "events": [{"t": 0, "speed": 5, "incline": 10},
                                        {"t": 4, "speed": 4.5, "incline": 12}]}
    s = samples_from_log(log)
    assert [x.incline_pct for x in s] == [10] * 4 + [12] * 6
    assert s[3].speed_kmh == 5 and s[4].speed_kmh == 4.5


def test_hr_csv(tmp_path):
    p = tmp_path / "hr.csv"
    p.write_text("t,hr\n0,90\n30,120\nbad,line\n")
    assert load_hr_csv(p) == {0: 90, 30: 120}


def test_cli_end_to_end_offline(tmp_path, capsys):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(workout().to_dict()))
    assert main(["validate", str(wf)]) == 0
    assert main(["push", str(wf), "--date", "2026-10-20", "--dry-run"]) == 0
    assert "tcoach-2026-10-20-t" in capsys.readouterr().out
    fit = tmp_path / "sim.fit"
    assert main(["simulate", str(wf), "--out", str(fit)]) == 0
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"started_at": "2026-10-14T12:00:00Z", "duration_s": 300,
                               "events": [{"t": 0, "speed": 5, "incline": 20}]}))
    out2 = tmp_path / "log.fit"
    assert main(["fit", str(log), "--out", str(out2)]) == 0
    _, session = read_back(out2)
    assert session.total_ascent == round(5 / 3.6 * 300 * 0.2 / (1 + 0.04) ** 0.5)
    assert main(["upload", str(out2), "--dry-run"]) == 0


def test_runner_log_hr_is_attached():
    from tcoach.session import samples_from_log
    log = {"duration_s": 6, "events": [{"t": 0, "speed": 5, "incline": 10}],
           "hr": [[2, 120], [3, 125], [5, 130]]}
    s = samples_from_log(log)
    assert [x.hr for x in s] == [None, None, 120, 125, 125, 130]
