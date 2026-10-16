"""kiekerun backend: serves the runner page and bridges it to intervals.icu.

Environment (set in Coolify, never in the repo or the page):
  INTERVALS_API_KEY, INTERVALS_ATHLETE_ID   intervals.icu access
  KIEKERUN_USER, KIEKERUN_PASSWORD          HTTP Basic login for page + API
  TZ_NAME                                   default Europe/Berlin (what "today" means)
  MCP_TOKEN                                 secret path of the read-only coach connector (/mcp/<token>)
  TRUSTED_PROXY_HOPS                        reverse proxies in front (default 1: Coolify/Traefik), for the client IP
  RATE_FAILS / RATE_WINDOW_S / RATE_LOCKOUT_S   failed-login lockout (default 10 fails in 900 s -> 900 s lockout)
  MCP_ALLOW_WRITE                           1 lets the connector plan/delete calendar workouts (default: read-only)
  MCP_RATE_PER_MIN                          valid connector requests per IP per minute (default 60)

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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from server import mcp as mcpmod
from server.ratelimit import FailLimiter, RateLimiter
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


def _env_num(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


FAILS = FailLimiter(int(_env_num("RATE_FAILS", 10)), _env_num("RATE_WINDOW_S", 900), _env_num("RATE_LOCKOUT_S", 900))
MCP_RATE = RateLimiter(int(_env_num("MCP_RATE_PER_MIN", 60)), 60)


def client_ip(request: Request) -> str:
    """Client address as seen by the first trusted proxy. X-Forwarded-For is appended to by each proxy,
    so only the entry TRUSTED_PROXY_HOPS from the right can be trusted; anything left of it is client-supplied."""
    hops = int(_env_num("TRUSTED_PROXY_HOPS", 1))
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) >= hops:
        return parts[-hops]
    return request.client.host if request.client else "unknown"


def _too_many(wait: int) -> HTTPException:
    return HTTPException(429, "too many attempts, try again later", headers={"Retry-After": str(wait)})


def authenticate(request: Request, c: HTTPBasicCredentials | None) -> bool:
    """True = logged in. Counts wrong credentials per IP and locks the IP out after too many.
    A request without credentials is the browser's normal first try and is not counted."""
    ip = client_ip(request)
    wait = FAILS.retry_after(ip)
    if wait:
        raise _too_many(wait)
    if c is None:
        return False
    if _check(c):
        FAILS.ok(ip)
        return True
    FAILS.fail(ip)
    return False
app = FastAPI(title="kiekerun", docs_url=None, redoc_url=None, openapi_url=None)


def _creds() -> tuple[str, str]:
    return os.environ.get("KIEKERUN_USER", ""), os.environ.get("KIEKERUN_PASSWORD", "")


def _check(c: HTTPBasicCredentials | None) -> bool:
    user, pw = _creds()
    return bool(c and user and pw
                and secrets.compare_digest(c.username.encode(), user.encode())
                and secrets.compare_digest(c.password.encode(), pw.encode()))


def require_login(request: Request, c: HTTPBasicCredentials | None = Depends(basic)):
    """Protects /api. Fails closed when no login is configured."""
    user, pw = _creds()
    if not (user and pw):
        raise HTTPException(503, "API disabled: set KIEKERUN_USER and KIEKERUN_PASSWORD")
    if not authenticate(request, c):
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
    if user and pw and not authenticate(request, c):  # protect the page too once a login is configured
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
    """Planned workouts of a day as kiekerun JSON. Only workouts with '... @ X% incline' steps
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


# ---------------------------------------------------------------- coach connector (MCP, read-only)
def mcp_write_enabled() -> bool:
    return os.environ.get("MCP_ALLOW_WRITE", "").strip().lower() in ("1", "true", "yes")


@app.api_route("/mcp/{token}", methods=["GET", "POST", "DELETE"])
async def mcp_endpoint(token: str, request: Request):
    """Streamable-HTTP MCP endpoint for claude.ai. The URL itself is the secret (MCP_TOKEN);
    wrong token or unset MCP_TOKEN look like a missing route."""
    ip = client_ip(request)
    wait = FAILS.retry_after(ip)
    if wait:
        raise _too_many(wait)
    expected = os.environ.get("MCP_TOKEN", "")
    if len(expected) < 16 or not secrets.compare_digest(token.encode(), expected.encode()):
        FAILS.fail(ip)  # scanners guessing the path lock themselves out
        raise HTTPException(404, "not found")
    wait = MCP_RATE.hit(ip)
    if wait:
        raise _too_many(wait)
    if request.method != "POST":  # no server-initiated stream, no sessions
        return Response(status_code=405, headers={"Allow": "POST"})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(mcpmod._err(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):
        out = [r for r in (mcpmod.handle(m, client, today_local, mcp_write_enabled()) for m in body) if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    out = mcpmod.handle(body, client, today_local, mcp_write_enabled())
    return JSONResponse(out) if out is not None else Response(status_code=202)
