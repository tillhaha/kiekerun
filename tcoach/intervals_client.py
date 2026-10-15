"""Thin intervals.icu API client (planned workouts + activity upload).

Auth: HTTP Basic with username 'API_KEY' and your API key as password
(intervals.icu -> Settings -> Developer Settings). Athlete id '0' means 'me'.

Endpoints checked against the public OpenAPI spec (https://intervals.icu/api/v1/docs):
  GET  /athlete/{id}                    athlete profile (connection test)
  GET  /athlete/{id}/events{format}     calendar events (oldest/newest/category)
  POST /athlete/{id}/events             create one event
  PUT  /athlete/{id}/events/{eventId}   update one event
  POST /athlete/{id}/activities         upload fit/tcx/gpx (multipart 'file')

Idempotency: the bulk 'upsert' flag only matches events created by the same
OAuth application, which an API key is not, so push_planned() looks the events
up itself (by external_id) and then updates or creates them one by one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import requests

from .intervals_text import to_intervals_text
from .models import Workout

BASE_URL = "https://intervals.icu/api/v1"


class IntervalsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "workout"


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str = "0", http=None, base_url: str = BASE_URL):
        if not api_key:
            raise IntervalsError("missing API key (set INTERVALS_API_KEY)")
        self.api_key = api_key
        self.athlete_id = athlete_id or "0"
        self.http = http or requests.Session()
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls, http=None) -> "IntervalsClient":
        return cls(os.environ.get("INTERVALS_API_KEY", ""), os.environ.get("INTERVALS_ATHLETE_ID", "0"), http=http)

    def _call(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/athlete/{self.athlete_id}{path}"
        try:
            resp = self.http.request(method, url, auth=("API_KEY", self.api_key), timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise IntervalsError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise IntervalsError(f"{method} /athlete{path or ''} -> {resp.status_code}: API key or athlete id rejected", resp.status_code)
        if resp.status_code >= 400:
            raise IntervalsError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}", resp.status_code)
        return resp.json() if resp.content else None

    # --- connection test --------------------------------------------------
    def athlete(self) -> dict:
        return self._call("GET", "")

    # --- planned workouts -------------------------------------------------
    @staticmethod
    def build_event(workout: Workout, date: str) -> dict:
        return {
            "category": "WORKOUT",
            "start_date_local": f"{date}T00:00:00",
            "type": "Run",
            "name": workout.name,
            "description": to_intervals_text(workout),
            "external_id": f"tcoach-{date}-{slug(workout.name)}",
            "moving_time": workout.duration_s,
        }

    def list_events(self, oldest: str, newest: str, category: str | None = "WORKOUT") -> list[dict]:
        params = {"oldest": oldest, "newest": newest}
        if category:
            params["category"] = category
        try:
            return self._call("GET", "/events", params=params) or []
        except IntervalsError as exc:
            if exc.status != 404:
                raise
            return self._call("GET", "/events.json", params=params) or []  # spec path is /events{format}

    list_planned = list_events

    def push_planned(self, items: list[tuple[Workout, str]], dry_run: bool = False) -> list[dict]:
        """Create or update planned workouts (items = [(workout, 'YYYY-MM-DD'), ...]).

        Matching is by external_id, so re-sending a workout updates it in place."""
        events = [self.build_event(w, d) for w, d in items]
        if dry_run:
            return events
        dates = sorted(e["start_date_local"][:10] for e in events)
        existing = {e.get("external_id"): e for e in self.list_events(dates[0], dates[-1], category=None) if e.get("external_id")}
        out = []
        for ev in events:
            old = existing.get(ev["external_id"])
            if old:
                out.append(self._call("PUT", f"/events/{old['id']}", json=ev))
            else:
                out.append(self._call("POST", "/events", json=ev))
        return out

    # --- activities -------------------------------------------------------
    def upload_activity(self, fit_path, name: str | None = None, description: str | None = None,
                        external_id: str | None = None, paired_event_id: int | None = None,
                        device_name: str | None = None, dry_run: bool = False) -> dict:
        path = Path(fit_path)
        params = {k: v for k, v in (("name", name), ("description", description), ("external_id", external_id),
                                    ("paired_event_id", paired_event_id), ("device_name", device_name)) if v}
        if dry_run:
            return {"would_upload": path.name, "bytes": path.stat().st_size, "params": params}
        with path.open("rb") as fh:
            return self._call("POST", "/activities", params=params, files={"file": (path.name, fh)})
