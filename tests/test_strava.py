import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from tcoach.strava_client import StravaClient, StravaError, TokenStore, _STORE

AUTH = {"Authorization": "Basic " + base64.b64encode(b"tito:secret").decode()}


class R:
    def __init__(self, status=200, data=None):
        self.status_code, self._d, self.text = status, data, json.dumps(data)
        self.content = b"x"

    def json(self):
        return self._d


class Http:
    """post() = token endpoint, request() = api; both scripted by queues."""
    def __init__(self, token=None, api=None):
        self.token, self.api, self.calls = list(token or []), list(api or []), []

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        return self.token.pop(0)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.api.pop(0)


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def mk(tmp_path, http, clock=None, seed=""):
    clock = clock or Clock()
    st = TokenStore(tmp_path / "s.json", seed)
    return StravaClient("42", "sec", st, http=http, now=clock, sleep=clock.sleep), st, clock


TOK = {"access_token": "A1", "refresh_token": "R1", "expires_at": 1_000_000 + 21600,
       "athlete": {"id": 7, "firstname": "Tito", "lastname": "H"}}


def test_exchange_stores_tokens_and_requires_write_scope(tmp_path):
    c, st, _ = mk(tmp_path, Http(token=[R(200, TOK)]))
    s = c.exchange_code("code", "read,activity:write")
    assert s["connected"] and s["athlete"] == "Tito H" and s["persistent"]
    assert json.loads((tmp_path / "s.json").read_text())["refresh_token"] == "R1"
    c2, _, _ = mk(tmp_path, Http())
    with pytest.raises(StravaError, match="activity:write"):
        c2.exchange_code("code", "read")
    with pytest.raises(StravaError, match="no scope"):
        c2.exchange_code("code", "")


def test_access_token_cached_then_refreshed_with_rotation(tmp_path):
    new = {"access_token": "A2", "refresh_token": "R2", "expires_at": 1_000_000 + 40000}
    http = Http(token=[R(200, TOK), R(200, new)])
    c, st, clock = mk(tmp_path, http)
    c.exchange_code("code", "activity:write")
    assert c.access_token() == "A1" and len(http.calls) == 1  # still valid, no refresh
    clock.t += 30000
    assert c.access_token() == "A2"
    assert http.calls[-1][2]["grant_type"] == "refresh_token" and http.calls[-1][2]["refresh_token"] == "R1"
    assert st.load()["refresh_token"] == "R2"  # newest refresh token wins


def test_seed_refresh_token_used_when_no_file(tmp_path):
    http = Http(token=[R(200, {"access_token": "A", "refresh_token": "R", "expires_at": 1_000_000 + 9999})])
    c, st, _ = mk(tmp_path, http, seed="SEED")
    assert c.access_token() == "A" and http.calls[0][2]["refresh_token"] == "SEED"


def test_not_connected_raises(tmp_path):
    c, _, _ = mk(tmp_path, Http())
    with pytest.raises(StravaError, match="not connected"):
        c.access_token()


def test_state_roundtrip_expiry_and_tamper(tmp_path):
    c, _, clock = mk(tmp_path, Http())
    s = c.sign_state("k")
    assert c.check_state(s, "k") and not c.check_state(s, "other")
    assert not c.check_state(s[:-1] + ("0" if s[-1] != "0" else "1"), "k")
    assert not c.check_state("junk", "k")
    clock.t += 601
    assert not c.check_state(s, "k")


def _connected(tmp_path, api):
    http = Http(token=[R(200, TOK)], api=api)
    c, st, clock = mk(tmp_path, http)
    c.exchange_code("code", "activity:write")
    fit = tmp_path / "a.fit"
    fit.write_bytes(b"FIT")
    return c, http, fit


def test_upload_polls_until_activity_id(tmp_path):
    c, http, fit = _connected(tmp_path, [R(201, {"id": 5, "status": "Your activity is still being processed.", "activity_id": None, "error": None}),
                                          R(200, {"id": 5, "activity_id": None, "error": None}),
                                          R(200, {"id": 5, "activity_id": 99, "error": None})])
    out = c.upload_fit(fit, name="Run", description="d", external_id="kiekerun-x")
    assert out == {"activity_id": 99, "status": "uploaded", "upload_id": 5}
    first = http.calls[1]
    assert first[0] == "POST" and first[1].endswith("/uploads")
    assert first[2]["data"]["data_type"] == "fit" and first[2]["data"]["trainer"] == "1" and first[2]["data"]["external_id"] == "kiekerun-x"
    assert first[2]["headers"]["Authorization"] == "Bearer A1"


def test_upload_duplicate_and_error_and_timeout(tmp_path):
    c, _, fit = _connected(tmp_path, [R(201, {"id": 1, "activity_id": None, "error": "a.fit duplicate of activity 123"})])
    assert c.upload_fit(fit)["status"] == "duplicate"
    c, _, fit = _connected(tmp_path, [R(201, {"id": 1, "activity_id": None, "error": "The file is empty"})])
    with pytest.raises(StravaError, match="refused"):
        c.upload_fit(fit)
    c, _, fit = _connected(tmp_path, [R(201, {"id": 1, "activity_id": None, "error": None})] + [R(200, {"id": 1, "activity_id": None, "error": None})] * 30)
    assert c.upload_fit(fit, wait_s=3)["status"] == "processing"


def test_upload_http_errors(tmp_path):
    c, _, fit = _connected(tmp_path, [R(401, {})])
    with pytest.raises(StravaError, match="granted scope: 'activity:write'"):
        c.upload_fit(fit)
    c, _, fit = _connected(tmp_path, [R(429, {})])
    with pytest.raises(StravaError, match="rate limit"):
        c.upload_fit(fit)


# ------------------------------------------------------------------ server
@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "42")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "sec")
    monkeypatch.setenv("STRAVA_TOKEN_FILE", str(tmp_path / "tok.json"))
    monkeypatch.delenv("STRAVA_REDIRECT_URI", raising=False)
    monkeypatch.delenv("STRAVA_REFRESH_TOKEN", raising=False)
    _STORE.reset()
    appmod._USED_STATES.clear()
    yield TestClient(appmod.app, follow_redirects=False)
    _STORE.reset()


def test_status_unconfigured(monkeypatch, app):
    monkeypatch.delenv("STRAVA_CLIENT_ID")
    assert app.get("/api/strava/status", headers=AUTH).json() == {"configured": False, "connected": False}
    assert app.get("/api/strava/status").status_code == 401


def test_connect_redirects_to_strava_with_state_and_scope(app):
    r = app.get("/api/strava/connect", headers={**AUTH, "x-forwarded-host": "run.example.com", "x-forwarded-proto": "https"})
    assert r.status_code == 307
    u = urlparse(r.headers["location"])
    q = parse_qs(u.query)
    assert u.netloc == "www.strava.com" and q["client_id"] == ["42"]
    assert q["redirect_uri"] == ["https://run.example.com/api/strava/callback"]
    assert "activity:write" in q["scope"][0] and q["state"][0]
    assert app.get("/api/strava/connect").status_code == 401


def test_callback_connects_once_and_rejects_bad_state(monkeypatch, app):
    state = parse_qs(urlparse(app.get("/api/strava/connect", headers=AUTH).headers["location"]).query)["state"][0]
    http = Http(token=[R(200, TOK)])
    monkeypatch.setattr(appmod, "strava_client", lambda: StravaClient.from_env(http=http))
    r = app.get("/api/strava/callback", params={"code": "c", "state": state, "scope": "read,activity:write"})
    assert r.status_code == 200 and "connected" in r.text
    assert app.get("/api/strava/status", headers=AUTH).json()["connected"] is True
    again = app.get("/api/strava/callback", params={"code": "c", "state": state, "scope": "read,activity:write"})
    assert again.status_code == 400  # state is single-use
    assert app.get("/api/strava/callback", params={"code": "c", "state": "forged"}).status_code == 400


def test_callback_without_write_scope_not_connected(monkeypatch, app):
    state = parse_qs(urlparse(app.get("/api/strava/connect", headers=AUTH).headers["location"]).query)["state"][0]
    monkeypatch.setattr(appmod, "strava_client", lambda: StravaClient.from_env(http=Http()))
    r = app.get("/api/strava/callback", params={"code": "c", "state": state, "scope": "read"})
    assert r.status_code == 502 and "activity:write" in r.text
    assert app.get("/api/strava/status", headers=AUTH).json()["connected"] is False


def test_disconnect(monkeypatch, app, tmp_path):
    (tmp_path / "tok.json").write_text(json.dumps({"refresh_token": "R"}))
    assert app.get("/api/strava/status", headers=AUTH).json()["connected"] is True
    assert app.post("/api/strava/disconnect", headers=AUTH).json() == {"ok": True}
    assert app.get("/api/strava/status", headers=AUTH).json()["connected"] is False
