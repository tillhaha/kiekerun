"""Write a treadmill session (1 Hz samples) as a FIT activity file with an altitude stream."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity, Event, EventType, FileType, Manufacturer, Sport, SubSport,
)

from .session import Sample, altitude_profile, totals


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def write_fit(samples: list[Sample], path, start: datetime | None = None,
              start_altitude_m: float = 0.0) -> dict:
    """Write samples to `path`; returns the totals dict (distance, ascent, ...)."""
    if not samples:
        raise ValueError("no samples")
    start = start or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    t0 = _ms(start)
    tot = totals(samples)
    alts = altitude_profile(samples, start_altitude_m)
    end_ms = t0 + len(samples) * 1000

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.serial_number = 0x7C0AC4
    fid.time_created = t0
    builder.add(fid)

    ev = EventMessage()
    ev.event = Event.TIMER
    ev.event_type = EventType.START
    ev.timestamp = t0
    builder.add(ev)

    dist = 0.0
    for s, alt in zip(samples, alts):
        v = s.speed_kmh / 3.6
        dist += v
        rec = RecordMessage()
        rec.timestamp = t0 + (s.t + 1) * 1000
        rec.distance = dist
        rec.enhanced_speed = v
        rec.enhanced_altitude = alt
        rec.grade = s.incline_pct
        if s.hr is not None:
            rec.heart_rate = int(s.hr)
        builder.add(rec)

    ev = EventMessage()
    ev.event = Event.TIMER
    ev.event_type = EventType.STOP_ALL
    ev.timestamp = end_ms
    builder.add(ev)

    hrs = [s.hr for s in samples if s.hr is not None]
    speeds = [s.speed_kmh / 3.6 for s in samples]

    def fill_summary(m):
        m.timestamp = end_ms
        m.start_time = t0
        m.total_elapsed_time = float(len(samples))
        m.total_timer_time = float(len(samples))
        m.total_distance = tot["distance_m"]
        m.total_ascent = int(round(tot["ascent_m"]))
        m.total_descent = int(round(tot["descent_m"]))
        m.sport = Sport.RUNNING
        m.sub_sport = SubSport.TREADMILL
        m.avg_speed = tot["distance_m"] / len(samples)
        m.max_speed = max(speeds)
        if hrs:
            m.avg_heart_rate = int(round(sum(hrs) / len(hrs)))
            m.max_heart_rate = max(hrs)

    lap = LapMessage()
    fill_summary(lap)
    lap.message_index = 0
    builder.add(lap)

    ses = SessionMessage()
    fill_summary(ses)
    ses.message_index = 0
    ses.first_lap_index = 0
    ses.num_laps = 1
    builder.add(ses)

    act = ActivityMessage()
    act.timestamp = end_ms
    act.total_timer_time = float(len(samples))
    act.num_sessions = 1
    act.type = Activity.MANUAL
    builder.add(act)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    builder.build().to_file(str(path))
    return tot
