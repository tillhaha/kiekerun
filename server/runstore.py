"""Run archive: every treadmill run is written to SQLite while it is recorded, so a crashed tab or a
forgotten upload does not lose it. One row per run, keyed by the same id as the intervals.icu
external_id (kiekerun-<UTC start>), so saving the same run twice updates it.

Status only moves forward: recording -> finished -> uploaded. A late autosave never overwrites a
finished or uploaded run.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

RANK = {"recording": 0, "finished": 1, "uploaded": 2}
ID_RE = re.compile(r"^kiekerun-\d{8}T\d{6}$")
MAX_LOG_BYTES = 5_000_000


def run_id(start: datetime) -> str:
    return f"kiekerun-{start.astimezone(timezone.utc):%Y%m%dT%H%M%S}"


class RunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, name TEXT, started_at TEXT, duration_s INTEGER, status TEXT NOT NULL,
                log TEXT NOT NULL, intervals_id TEXT, updated_at TEXT NOT NULL)""")

    @classmethod
    def from_env(cls) -> "RunStore | None":
        """None when the database file cannot be created (the app then runs without an archive)."""
        try:
            return cls(os.environ.get("KIEKERUN_DB", "/data/kiekerun.db"))
        except (OSError, sqlite3.Error):
            return None

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return closing_commit(db)

    @property
    def persistent(self) -> bool:
        """True when the database directory is a mount point, i.e. a volume that survives a redeploy."""
        return os.path.ismount(self.path.parent)

    def save(self, rid: str, log: dict, status: str, intervals_id: str | None = None) -> str:
        """Insert or update a run; returns the status that is stored."""
        if status not in RANK or not ID_RE.match(rid):
            raise ValueError("bad run id or status")
        body = json.dumps(log, separators=(",", ":"))
        if len(body) > MAX_LOG_BYTES:
            raise ValueError("run log too large")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            old = db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
            if old and status == "recording" and RANK[old["status"]] > 0:
                return old["status"]  # late autosave of a run that is already finished
            keep = old["status"] if old and RANK[old["status"]] > RANK[status] else status
            db.execute(
                """INSERT INTO runs(id,name,started_at,duration_s,status,log,intervals_id,updated_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, started_at=excluded.started_at,
                     duration_s=excluded.duration_s, status=?, log=excluded.log,
                     intervals_id=COALESCE(excluded.intervals_id, runs.intervals_id), updated_at=excluded.updated_at""",
                (rid, log.get("name"), log.get("started_at"), int(log.get("duration_s") or 0), keep, body, intervals_id, now, keep))
            return keep

    def get(self, rid: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return None if r is None else {**_meta(r), "log": json.loads(r["log"])}

    def list(self, limit: int = 30) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id,name,started_at,duration_s,status,intervals_id,updated_at FROM runs "
                              "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [_meta(r) for r in rows]

    def delete(self, rid: str) -> bool:
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM runs WHERE id=?", (rid,)).rowcount > 0


def _meta(r) -> dict:
    return {k: r[k] for k in ("id", "name", "started_at", "duration_s", "status", "intervals_id", "updated_at")}


class closing_commit:
    """Connection as a context manager that commits and always closes (sqlite3's own `with` does not close)."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, et, ev, tb):
        try:
            self.db.commit() if et is None else self.db.rollback()
        finally:
            self.db.close()
