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

## API (HTTP Basic login)
| Route | Purpose |
|---|---|
| `GET /api/health` | connection test: reaches intervals.icu with the configured key |
| `GET /api/planned?date=YYYY-MM-DD` | planned workouts of a day as kiekerun JSON (default today) |
| `POST /api/planned` `{workout, date}` | create/update a planned workout (matched by `external_id`) |
| `POST /api/upload` `{log, paired_event_id?}` | runner log -> FIT (elevation + HR) -> intervals.icu activity |

Endpoints were checked against https://intervals.icu/api/v1/docs.
