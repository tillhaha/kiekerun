"""Strava client: OAuth (authorize / code exchange / refresh) and FIT upload.

Strava API (https://developers.strava.com/docs/):
  GET  https://www.strava.com/oauth/authorize   user consent, returns ?code=...&scope=...
  POST https://www.strava.com/oauth/token       code -> tokens, refresh_token -> new tokens
  POST https://www.strava.com/api/v3/uploads    multipart file + data_type=fit, name, description, trainer, external_id
  GET  https://www.strava.com/api/v3/uploads/{id}   poll until activity_id (or error) is there

Access tokens live 6 hours; refreshing may hand out a NEW refresh token, which replaces the old one,
so the latest tokens are kept in a small JSON file (STRAVA_TOKEN_FILE, put it on a persistent volume).
STRAVA_REFRESH_TOKEN seeds the store when the file does not exist.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
NEED_SCOPE = "activity:write"
STATE_TTL_S = 600


class StravaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TokenStore:
    """Tokens in a JSON file; falls back to memory when the file cannot be written (then `persistent` is False)."""

    def __init__(self, path: str | Path, seed_refresh_token: str = ""):
        self.path = Path(path)
        self.seed = seed_refresh_token.strip()
        self.persistent = True
        self._mem: dict | None = None
        self._lock = threading.Lock()

    def load(self) -> dict:
        if self._mem is not None:
            return dict(self._mem)
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        if self.seed:
            return {"refresh_token": self.seed, "expires_at": 0}
        return {}

    def save(self, data: dict) -> None:
        with self._lock:
            self._mem = dict(data)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                os.chmod(tmp, 0o600)
                tmp.replace(self.path)
                self.persistent = True
            except OSError:
                self.persistent = False

    def clear(self) -> None:
        with self._lock:
            self._mem = {}
            try:
                self.path.unlink()
            except OSError:
                pass


class StravaClient:
    def __init__(self, client_id: str, client_secret: str, store: TokenStore, http=None, now=time.time, sleep=time.sleep):
        self.client_id, self.client_secret, self.store = str(client_id).strip(), client_secret.strip(), store
        self.http = http or requests.Session()
        self.now, self.sleep = now, sleep
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, http=None) -> "StravaClient | None":
        cid, sec = os.environ.get("STRAVA_CLIENT_ID", ""), os.environ.get("STRAVA_CLIENT_SECRET", "")
        if not (cid.strip() and sec.strip()):
            return None
        store = _STORE.get(os.environ.get("STRAVA_TOKEN_FILE", "/data/strava.json"))
        if not store.seed:
            store.seed = os.environ.get("STRAVA_REFRESH_TOKEN", "").strip()
        return cls(cid, sec, store, http=http)

    # --- OAuth ------------------------------------------------------------
    def sign_state(self, secret: str) -> str:
        body = f"{int(self.now())}.{secrets.token_urlsafe(12)}"
        return f"{body}.{_mac(secret, body)}"

    def check_state(self, state: str, secret: str) -> bool:
        try:
            ts, nonce, mac = state.split(".")
            body = f"{ts}.{nonce}"
            fresh = 0 <= self.now() - int(ts) <= STATE_TTL_S
        except ValueError:
            return False
        return fresh and hmac.compare_digest(mac, _mac(secret, body))

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        q = {"client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "approval_prompt": "force", "scope": f"read,{NEED_SCOPE}", "state": state}
        return f"{AUTH_URL}?{urlencode(q)}"

    def _token_call(self, data: dict) -> dict:
        try:
            r = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret, **data}, timeout=30)
        except requests.RequestException as exc:
            raise StravaError(f"token request failed: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise StravaError(f"token request -> {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()

    def exchange_code(self, code: str, scope: str = "") -> dict:
        """Code from the callback -> stored tokens. Refuses a grant without activity:write."""
        if NEED_SCOPE not in scope.split(","):  # an unticked box or a missing scope parameter both end up here
            raise StravaError(f"Strava access was granted without '{NEED_SCOPE}' (got '{scope or 'no scope'}'); connect again and keep the upload permission ticked")
        d = self._token_call({"code": code, "grant_type": "authorization_code"})
        ath = d.get("athlete") or {}
        name = " ".join(x for x in (ath.get("firstname"), ath.get("lastname")) if x)
        self.store.save({"access_token": d["access_token"], "refresh_token": d["refresh_token"], "expires_at": d["expires_at"],
                         "athlete_id": ath.get("id"), "athlete": name, "scope": scope})
        return self.status()

    def access_token(self) -> str:
        with self._lock:
            t = self.store.load()
            if not t.get("refresh_token"):
                raise StravaError("Strava is not connected yet")
            if t.get("access_token") and t.get("expires_at", 0) - 120 > self.now():
                return t["access_token"]
            d = self._token_call({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            t.update(access_token=d["access_token"], refresh_token=d.get("refresh_token") or t["refresh_token"],
                     expires_at=d["expires_at"])
            self.store.save(t)
            return t["access_token"]

    def status(self) -> dict:
        t = self.store.load()
        return {"configured": True, "connected": bool(t.get("refresh_token")), "athlete": t.get("athlete"), "scope": t.get("scope"),
                "persistent": self.store.persistent}

    def disconnect(self) -> None:
        self.store.clear()

    # --- upload -----------------------------------------------------------
    def _api(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self.access_token()}"}, timeout=60, **kw)
        except requests.RequestException as exc:
            raise StravaError(f"{method} {path} failed: {exc.__class__.__name__}") from exc
        if r.status_code == 429:
            raise StravaError("Strava rate limit reached, try again in a few minutes", 429)
        if r.status_code in (401, 403):
            raise StravaError(f"{method} {path} -> {r.status_code}: Strava rejected the request ({r.text[:300]}); granted scope: "
                              f"'{self.store.load().get('scope') or 'unknown'}'. Connect again at /api/strava/connect", r.status_code)
        if r.status_code >= 400:
            raise StravaError(f"{method} {path} -> {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json()

    def upload_fit(self, fit_path, name: str | None = None, description: str | None = None,
                   external_id: str | None = None, wait_s: float = 25.0) -> dict:
        """Upload a FIT file and wait for Strava to finish processing.

        Returns {"activity_id", "status": "uploaded"|"duplicate"|"processing", "upload_id"}."""
        path = Path(fit_path)
        data = {k: v for k, v in (("data_type", "fit"), ("name", name), ("description", description),
                                   ("external_id", external_id), ("trainer", "1")) if v}
        with path.open("rb") as fh:
            up = self._api("POST", "/uploads", data=data, files={"file": (path.name, fh)})
        deadline = self.now() + wait_s
        while True:
            err = up.get("error")
            if err:
                dup = re.search(r"duplicate of activity (\d+)", err)
                if dup:
                    return {"activity_id": int(dup.group(1)), "status": "duplicate", "upload_id": up.get("id")}
                raise StravaError(f"Strava refused the file: {err}")
            if up.get("activity_id"):
                return {"activity_id": up["activity_id"], "status": "uploaded", "upload_id": up.get("id")}
            if self.now() >= deadline:
                return {"activity_id": None, "status": "processing", "upload_id": up.get("id")}
            self.sleep(1.5)
            up = self._api("GET", f"/uploads/{up['id']}")


def _mac(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]


class _Stores:
    """One TokenStore per file path, so all requests share the in-memory copy."""

    def __init__(self):
        self._d: dict[str, TokenStore] = {}

    def get(self, path: str) -> TokenStore:
        return self._d.setdefault(path, TokenStore(path))

    def reset(self):
        self._d.clear()


_STORE = _Stores()
