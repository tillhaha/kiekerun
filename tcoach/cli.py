"""tcoach command line.

  tcoach validate WORKOUT.json
  tcoach text WORKOUT.json                       # intervals.icu workout text
  tcoach push WORKOUT.json --date 2026-10-20 [--dry-run]
  tcoach pull [--days 7] [--out DIR]             # planned workouts back to JSON
  tcoach simulate WORKOUT.json --out session.fit # run on the simulator, write FIT
  tcoach fit LOG.json --out session.fit [--hr-csv hr.csv] [--start ISO]
  tcoach upload session.fit [--name N] [--dry-run]

Environment: INTERVALS_API_KEY, INTERVALS_ATHLETE_ID (default 0 = me).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fit_writer import write_fit
from .intervals_client import IntervalsClient, slug
from .intervals_text import parse_intervals_text, to_intervals_text
from .machine import has_errors, validate
from .models import Workout, fmt_duration
from .session import attach_hr, load_hr_csv, load_log, samples_from_log
from .treadmill import FakeClock, SimTreadmill, run_workout


def _summary(w: Workout) -> str:
    up, down = w.ascent_descent_m()
    return (f"{w.name}: {fmt_duration(w.duration_s)}, {w.distance_km:.2f} km, "
            f"+{up:.0f} m / -{down:.0f} m, {len(w.flat())} blocks")


def cmd_validate(a) -> int:
    w = Workout.load(a.workout)
    print(_summary(w))
    issues = validate(w)
    for i in issues:
        print(i)
    if not issues:
        print("OK: no issues")
    return 1 if has_errors(issues) else 0


def cmd_text(a) -> int:
    print(to_intervals_text(Workout.load(a.workout)), end="")
    return 0


def cmd_push(a) -> int:
    w = Workout.load(a.workout)
    date = a.date or w.date
    if not date:
        print("need --date or a 'date' in the workout", file=sys.stderr)
        return 2
    issues = validate(w)
    for i in issues:
        print(i)
    if has_errors(issues):
        return 1
    client = IntervalsClient.from_env() if not a.dry_run else IntervalsClient("dry-run")
    result = client.push_planned([(w, date)], dry_run=a.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_pull(a) -> int:
    client = IntervalsClient.from_env()
    today = datetime.now().date()
    events = client.list_planned(today.isoformat(), (today + timedelta(days=a.days)).isoformat())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for e in events:
        desc = e.get("description") or ""
        if "incline" not in desc:
            continue  # not one of ours
        date = (e.get("start_date_local") or "")[:10]
        try:
            w, warns = parse_intervals_text(desc, name=e.get("name", "workout"))
        except ValueError as exc:
            print(f"skip {e.get('name')}: {exc}")
            continue
        w.date = date
        for msg in warns:
            print(f"  warn ({e.get('name')}): {msg}")
        path = out / f"{date}-{slug(w.name)}.json"
        w.save(path)
        print(f"wrote {path}  ({_summary(w)})")
        n += 1
    print(f"{n} workout(s) pulled")
    return 0


def cmd_simulate(a) -> int:
    w = Workout.load(a.workout)
    samples = run_workout(SimTreadmill(), w, clock=FakeClock())
    start = datetime.now(timezone.utc) - timedelta(seconds=len(samples))
    tot = write_fit(samples, a.out, start=start)
    print(f"wrote {a.out}: {fmt_duration(tot['duration_s'])}, {tot['distance_m']/1000:.2f} km, +{tot['ascent_m']:.0f} m")
    return 0


def cmd_fit(a) -> int:
    log = load_log(a.log)
    samples = samples_from_log(log)
    if a.hr_csv:
        attach_hr(samples, load_hr_csv(a.hr_csv))
    start = datetime.fromisoformat(a.start or log["started_at"].replace("Z", "+00:00"))
    tot = write_fit(samples, a.out, start=start)
    print(f"wrote {a.out}: {fmt_duration(tot['duration_s'])}, {tot['distance_m']/1000:.2f} km, +{tot['ascent_m']:.0f} m")
    return 0


def cmd_upload(a) -> int:
    client = IntervalsClient.from_env() if not a.dry_run else IntervalsClient("dry-run")
    print(json.dumps(client.upload_activity(a.file, name=a.name, dry_run=a.dry_run), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tcoach", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("validate"); s.add_argument("workout"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("text"); s.add_argument("workout"); s.set_defaults(fn=cmd_text)
    s = sub.add_parser("push"); s.add_argument("workout"); s.add_argument("--date"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_push)
    s = sub.add_parser("pull"); s.add_argument("--days", type=int, default=7); s.add_argument("--out", default="planned"); s.set_defaults(fn=cmd_pull)
    s = sub.add_parser("simulate"); s.add_argument("workout"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_simulate)
    s = sub.add_parser("fit"); s.add_argument("log"); s.add_argument("--out", required=True); s.add_argument("--hr-csv"); s.add_argument("--start"); s.set_defaults(fn=cmd_fit)
    s = sub.add_parser("upload"); s.add_argument("file"); s.add_argument("--name"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_upload)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:  # surface a clean message instead of a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
