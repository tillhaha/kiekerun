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

## API (HTTP Basic login)
| Route | Purpose |
|---|---|
| `GET /api/health` | connection test: reaches intervals.icu with the configured key |
| `GET /api/planned?date=YYYY-MM-DD` | planned workouts of a day as kiekerun JSON (default today) |
| `POST /api/planned` `{workout, date}` | create/update a planned workout (matched by `external_id`) |
| `POST /api/upload` `{log, paired_event_id?}` | runner log -> FIT (elevation, HR, calorie estimate) -> intervals.icu activity; then RPE, feel, carbs and a description (note + vertical speed per block) are set on it |

Endpoints were checked against https://intervals.icu/api/v1/docs.

## Workout-Feedback (log extension)
The page adds `feedback` and `blocks` to the runner log:
`feedback: {rpe 1-10, feel 1-5, note, carbs: [[seconds, grams], ...]}`, `blocks: [{label, t0, dur}]`.
intervals.icu fields: `icu_rpe`, `feel` (1 Strong ... 5 Weak, verify once in the UI), `carbs_ingested`, `description`, `calories`.
Calories are estimated with the ACSM walking/running equations from speed, incline and body weight
(`tcoach/calories.py`); vertical speed per block is computed from speed and incline, not measured by the treadmill.
