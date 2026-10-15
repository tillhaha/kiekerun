"""kiekerun backend: serves the runner page and bridges it to intervals.icu.

Environment (set in Coolify, never in the repo or the page):
  INTERVALS_API_KEY, INTERVALS_ATHLETE_ID   intervals.icu access
  KIEKERUN_USER, KIEKERUN_PASSWORD          HTTP Basic login for page + API
  TZ_NAME                                   default Europe/Berlin (what "today" means)

The API fails closed: without KIEKERUN_USER/KIEKERUN_PASSWORD every /api route
answers 503, so nobody can write to the calendar through a public URL.
"""
from __future__ import annotations

import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from tcoach.calories import estimate_kcal
from tcoach.feedback import block_stats, build_description, parse_feedback
from tcoach.fit_writer import write_fit
from tcoach.intervals_client import IntervalsClient, IntervalsError
from tcoach.intervals_text import parse_intervals_text
from tcoach.machine import has_errors, validate
from tcoach.models import Workout
from tcoach.session import samples_from_log

ROOT = Path(__file__).resolve().parent.parent
basic = HTTPBasic(auto_error=False)
app = FastAPI(title="kiekerun", docs_url=None, redoc_url=None, openapi_url=None)


def _creds() -> tuple[str, str]:
    return os.environ.get("KIEKERUN_USER", ""), os.environ.get("KIEKERUN_PASSWORD", "")


def _check(c: HTTPBasicCredentials | None) -> bool:
    user, pw = _creds()
    return bool(c and user and pw
                and secrets.compare_digest(c.username.encode(), user.encode())
                and secrets.compare_digest(c.password.encode(), pw.encode()))


def require_login(c: HTTPBasicCredentials | None = Depends(basic)):
    """Protects /api. Fails closed when no login is configured."""
    user, pw = _creds()
    if not (user and pw):
        raise HTTPException(503, "API disabled: set KIEKERUN_USER and KIEKERUN_PASSWORD")
    if not _check(c):
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="kiekerun"'})


def client() -> IntervalsClient:
    try:
        return IntervalsClient.from_env()
    except IntervalsError as exc:
        raise HTTPException(503, str(exc))


def today_local() -> str:
    return datetime.now(ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))).date().isoformat()


def _intervals_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except IntervalsError as exc:
        # 401/403 from intervals.icu is a server config problem, not a login problem for the caller
        raise HTTPException(502, str(exc))


# ---------------------------------------------------------------- page
@app.get("/")
def page(request: Request, c: HTTPBasicCredentials | None = Depends(basic)):
    user, pw = _creds()
    if user and pw and not _check(c):  # protect the page too once a login is configured
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="kiekerun"'})
    return FileResponse(ROOT / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- api
@app.get("/api/health", dependencies=[Depends(require_login)])
def health():
    """Connection test: can the server reach intervals.icu with the configured key?"""
    a = _intervals_call(client().athlete)
    return {"ok": True, "athlete_id": a.get("id"), "name": a.get("name")}


@app.get("/api/planned", dependencies=[Depends(require_login)])
def planned(date: str | None = None):
    """Planned workouts of a day as kiekerun JSON. Only workouts with 'incline Npct' steps
    (written by tcoach) can be converted; others are listed with workout=null."""
    day = date or today_local()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    out = []
    for e in _intervals_call(client().list_events, day, day):
        item = {"event_id": e.get("id"), "name": e.get("name"), "date": (e.get("start_date_local") or "")[:10],
                "workout": None, "warnings": []}
        desc = e.get("description") or ""
        if "incline" not in desc:
            item["warnings"].append("no incline steps in description; not a treadmill workout")
        else:
            try:
                w, warns = parse_intervals_text(desc, name=e.get("name") or "Treadmill workout")
                w.date = item["date"]
                item["workout"], item["warnings"] = w.to_dict(), warns
            except ValueError as exc:
                item["warnings"].append(str(exc))
        out.append(item)
    return {"date": day, "items": out}


class PlanIn(BaseModel):
    workout: dict
    date: str


@app.post("/api/planned", dependencies=[Depends(require_login)])
def plan(body: PlanIn):
    """Create or update one planned workout in the intervals.icu calendar."""
    try:
        w = Workout.from_dict(body.workout)
        datetime.strptime(body.date, "%Y-%m-%d")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    issues = validate(w)
    if has_errors(issues):
        raise HTTPException(422, "; ".join(str(i) for i in issues))
    res = _intervals_call(client().push_planned, [(w, body.date)])
    return {"ok": True, "event": res[0]}


class UploadIn(BaseModel):
    log: dict
    paired_event_id: int | None = None


def body_weight_kg(cl: IntervalsClient) -> float | None:
    """Weight for the calorie estimate: intervals.icu profile first, BODY_WEIGHT_KG as fallback."""
    try:
        w = cl.athlete().get("icu_weight")
    except IntervalsError:
        w = None
    if not isinstance(w, (int, float)) or not 30 < w < 250:
        try:
            w = float(os.environ.get("BODY_WEIGHT_KG", ""))
        except ValueError:
            return None
    return float(w) if 30 < w < 250 else None


@app.post("/api/upload", dependencies=[Depends(require_login)])
def upload(body: UploadIn):
    """Runner log -> FIT with elevation, HR and calorie estimate -> intervals.icu activity,
    then RPE / feel / carbs / description (note + vertical speed per block) on the activity."""
    log = body.log
    try:
        samples = samples_from_log(log)
        start = datetime.fromisoformat(str(log["started_at"]).replace("Z", "+00:00"))
        fb = parse_feedback(log.get("feedback"))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"bad log: {exc}")
    blocks = log.get("blocks") if isinstance(log.get("blocks"), list) else []
    try:
        stats = block_stats(samples, blocks)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"bad blocks: {exc}")

    cl = client()
    weight = body_weight_kg(cl)
    kcal = estimate_kcal(samples, weight) if weight else None
    description = build_description(fb, stats, len(samples), kcal)

    with tempfile.TemporaryDirectory() as tmp:
        fit = Path(tmp) / "kiekerun.fit"
        tot = write_fit(samples, fit, start=start, calories=kcal)
        res = _intervals_call(
            cl.upload_activity, fit, name=log.get("name"),
            external_id=f"kiekerun-{start.astimezone(timezone.utc):%Y%m%dT%H%M%S}",
            paired_event_id=body.paired_event_id, device_name="KiekeRun")

    fields = {}
    if fb.rpe:
        fields["icu_rpe"] = fb.rpe
    if fb.feel:
        fields["feel"] = fb.feel
    if fb.carbs_total:
        fields["carbs_ingested"] = fb.carbs_total
    if description:
        fields["description"] = description
    acts = (res or {}).get("activities") or []
    update_error = None
    if acts and fields:  # no id = intervals.icu saw a duplicate file; nothing to update
        try:
            cl.update_activity(acts[0]["id"], fields)
        except IntervalsError as exc:  # the upload itself succeeded, so report instead of failing
            update_error = str(exc)
    return {"ok": True, "totals": tot, "calories": kcal, "updated": sorted(fields), "update_error": update_error,
            "intervals": res}
