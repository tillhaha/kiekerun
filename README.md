# kiekerun

Treadmill workout runner (Web Bluetooth / FTMS) plus a small backend that bridges it to intervals.icu.

- `index.html` – the runner page (workout preview, treadmill + heart-rate strap, summary)
- `server/app.py` – FastAPI backend: serves the page and the `/api` routes
- `tcoach/` – toolkit: workout model, intervals.icu client, FIT writer with elevation
- `tests/`, `examples/` – `python -m pytest`

## Coolify setup
Build pack **Dockerfile**, port **8000**. Environment variables:

| Variable | Meaning |
|---|---|
| `INTERVALS_API_KEY` | intervals.icu -> Settings -> Developer Settings |
| `INTERVALS_ATHLETE_ID` | e.g. `i123456` |
| `KIEKERUN_USER`, `KIEKERUN_PASSWORD` | login for page and API (the API stays disabled without them) |
| `TZ_NAME` | optional, default `Europe/Berlin` (what "today" means) |
| `BODY_WEIGHT_KG` | optional fallback for the calorie estimate; normally the weight comes from the intervals.icu profile |
| `MCP_TOKEN` | optional, at least 16 random characters. Enables the read-only coach connector at `/mcp/<MCP_TOKEN>` |
| `TRUSTED_PROXY_HOPS` | optional, default `1`: reverse proxies in front of the app (Coolify's Traefik = 1; Cloudflare in front of it = 2). Decides which `X-Forwarded-For` entry counts as the client address |
| `RATE_FAILS`, `RATE_WINDOW_S`, `RATE_LOCKOUT_S` | optional, default 10 / 900 / 900: after 10 wrong logins or wrong connector tokens within 15 min an address is locked out for 15 min |
| `MCP_RATE_PER_MIN` | optional, default `60`: valid connector requests per address per minute |
| `MCP_ALLOW_WRITE` | optional, `1` lets the connector plan and delete calendar workouts. Default off: read-only, write tools are not even listed |

## API (HTTP Basic login)
| Route | Purpose |
|---|---|
| `GET /api/health` | connection test: reaches intervals.icu with the configured key |
| `GET /api/planned?date=YYYY-MM-DD` | planned workouts of a day as kiekerun JSON (default today) |
| `POST /api/planned` `{workout, date}` | create/update a planned workout (matched by `external_id`) |
| `POST /api/upload` `{log, paired_event_id?}` | runner log -> FIT (elevation, HR, calorie estimate) -> intervals.icu activity; then RPE, feel, carbs and a description (note + vertical speed per block) are set on it |

Endpoints were checked against https://intervals.icu/api/v1/docs.

## Coach connector (read-only MCP)

Lets Claude (the coach) read everything in intervals.icu: KiekeRun uploads, Garmin runs, wellness, plan.
Set `MCP_TOKEN` in Coolify (for example the output of `openssl rand -hex 24`), redeploy, then add a custom
connector in claude.ai (Settings > Connectors) with the URL `https://<your-domain>/mcp/<MCP_TOKEN>`.

The URL is the secret (claude.ai connectors cannot send the Basic login), so treat it like a password and
change `MCP_TOKEN` if it leaks. Without `MCP_TOKEN` the route answers 404.

| Tool | Returns |
|---|---|
| `get_activities(oldest?, newest?, limit?)` | activities of all sources, newest first: distance, duration, pace, HR, elevation, load, RPE, feel, carbs, notes, source device |
| `get_activity(id)` | one activity with intervals/laps and HR zone times |
| `get_wellness(oldest?, newest?)` | CTL, ATL, form (TSB), ramp rate, HRV, resting HR, sleep, readiness, weight |
| `get_plan_vs_actual(oldest?, newest?)` | per day: planned entries next to done activities; planned workouts marked done / open / no paired activity |

Dates are `YYYY-MM-DD`, default is the last 14 days, maximum range 400 days. These four tools only read.

### Writing (optional, `MCP_ALLOW_WRITE=1`)

| Tool | Does |
|---|---|
| `save_treadmill_workout(date, workout)` | plans a KiekeRun workout (blocks with speed/incline, checked against the treadmill limits); same name on the same day updates it |
| `save_planned_workout(date, name, type?, description?, duration_min?, load?, carbs_per_h?)` | plans any other workout; `description` is intervals.icu workout text (`- 15m Z2 Pace`); same name on the same day updates it |
| `delete_planned_workout(event_id)` | deletes a planned workout, only one created here or by KiekeRun (`coach-` / `tcoach-` external id), only today or later |

The write tools only touch planned `WORKOUT` entries. They cannot change activities, wellness data, races or
notes. Dates must be between yesterday and 400 days ahead, and all writes together are capped at 30 per hour.
Because the URL is the only secret, anyone who has it can plan workouts once writing is on: keep `MCP_TOKEN` private.
Your intervals.icu API key needs no extra scope setting; it already allows calendar writes (the same call KiekeRun uses).

### Rate limiting

Wrong Basic-Auth logins and wrong connector tokens count per client address (shared counter). Too many
and the address gets `429` with `Retry-After`, even with the right password, until the lockout ends. A
request without credentials (the browser's first try) is not counted. State is in memory, so a restart
clears it. If the client address looks wrong (every visitor appears as the same IP), check
`TRUSTED_PROXY_HOPS`; too low trusts a spoofable header, too high falls back to the proxy's own address.

## Workout text in intervals.icu

KiekeRun writes each block as one line:

    - <description> <time> @ <X.X> km/h (<MM:SS> min/km) @ <X>% incline
    - Steady Vertical 30 min @ 5.5 km/h (10:55 min/km) @ 15% incline

Repeat groups are a line `Main set 4x` followed by their steps. Speed always uses a decimal point and the
pace is calculated from it. intervals.icu does not read this line as a structured step (no step graph, no
automatic load), so duration and distance are set as separate fields on the calendar entry. KiekeRun reads
this format back, and the older `- Label incline 6pct 8m 5:43/km Pace` format as well.

## Workout-Feedback (log extension)
The page adds `feedback` and `blocks` to the runner log:
`feedback: {rpe 1-10, feel 1-5, note, carbs: [[seconds, grams], ...]}`, `blocks: [{label, t0, dur}]`.
intervals.icu fields: `icu_rpe`, `feel` (1 Strong ... 5 Weak, verify once in the UI), `carbs_ingested`, `description`, `calories`.
Calories are estimated with the ACSM walking/running equations from speed, incline and body weight
(`tcoach/calories.py`); vertical speed per block is computed from speed and incline, not measured by the treadmill.
