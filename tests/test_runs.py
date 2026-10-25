import base64
import json

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server.runstore import RunStore
from tcoach.intervals_client import IntervalsClient

AUTH = {"Authorization": "Basic " + base64.b64encode(b"tito:secret").decode()}
RID = "kiekerun-20261024T120211"


def log(dur=60, **kw):
    base = {"version": 1, "name": "Dress", "started_at": "2026-10-24T12:02:11.123Z", "duration_s": dur,
            "events": [{"t": 0, "speed": 5, "incline": 10}], "hr": [[1, 110]], "blocks": [{"label": "A", "t0": 0, "dur": 60}]}
    base.update(kw)
    return base


def test_store_save_list_get_roundtrip(tmp_path):
    st = RunStore(tmp_path / "d" / "r.db")
    assert st.save(RID, log(10), "recording") == "recording"
    assert st.save(RID, log(70), "finished") == "finished"
    (m,) = st.list()
    assert m["id"] == RID and m["duration_s"] == 70 and m["status"] == "finished"
    assert st.get(RID)["log"]["duration_s"] == 70 and st.get("kiekerun-20200101T000000") is None


def test_status_only_moves_forward(tmp_path):
    st = RunStore(tmp_path / "r.db")
    st.save(RID, log(70), "finished")
    assert st.save(RID, log(30), "recording") == "finished"      # late autosave is ignored
    assert st.get(RID)["log"]["duration_s"] == 70
    assert st.save(RID, log(70), "uploaded", "i99") == "uploaded"
    assert st.save(RID, log(70, feedback={"rpe": 5}), "finished") == "uploaded"  # feedback edit keeps the status
    r = st.get(RID)
    assert r["status"] == "uploaded" and r["intervals_id"] == "i99" and r["log"]["feedback"] == {"rpe": 5}


def test_store_rejects_bad_input(tmp_path):
    st = RunStore(tmp_path / "r.db")
    with pytest.raises(ValueError):
        st.save("../etc/passwd", log(), "finished")
    with pytest.raises(ValueError):
        st.save(RID, log(), "weird")
    with pytest.raises(ValueError):
        st.save(RID, log(blob="x" * 5_100_000), "finished")


def test_store_unavailable_when_path_not_creatable(monkeypatch):
    monkeypatch.setenv("KIEKERUN_DB", "/proc/nope/kiekerun.db")
    assert RunStore.from_env() is None


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")
    return TestClient(appmod.app)


def test_runs_api_needs_login_and_roundtrips(c):
    assert c.get("/api/runs").status_code == 401
    r = c.post("/api/runs", json={"log": log(10), "status": "recording"}, headers=AUTH).json()
    assert r == {"id": RID, "status": "recording"}
    c.post("/api/runs", json={"log": log(75), "status": "finished"}, headers=AUTH)
    d = c.get("/api/runs", headers=AUTH).json()
    assert d["enabled"] is True and d["runs"][0]["duration_s"] == 75 and "persistent" in d
    assert c.get(f"/api/runs/{RID}", headers=AUTH).json()["log"]["duration_s"] == 75
    assert c.get("/api/runs/kiekerun-20200101T000000", headers=AUTH).status_code == 404
    assert c.get("/api/runs/..%2Fx", headers=AUTH).status_code == 404
    assert c.delete(f"/api/runs/{RID}", headers=AUTH).json() == {"ok": True}
    assert c.get("/api/runs", headers=AUTH).json()["runs"] == []


def test_runs_api_validation(c):
    bad = lambda body: c.post("/api/runs", json=body, headers=AUTH).status_code
    assert bad({"log": log(), "status": "uploaded"}) == 422        # only the page's own states
    assert bad({"log": {"started_at": "nope", "events": []}}) == 422
    assert bad({"log": {"started_at": "2026-10-24T12:00:00Z", "events": "x"}}) == 422


def test_runs_disabled_without_database(c, monkeypatch):
    monkeypatch.setenv("KIEKERUN_DB", "/proc/nope/kiekerun.db")
    assert c.get("/api/runs", headers=AUTH).json() == {"enabled": False, "runs": []}
    assert c.post("/api/runs", json={"log": log()}, headers=AUTH).status_code == 503


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._d, self.content, self.text = status, data, b"x", json.dumps(data)

    def json(self):
        return self._d


class Http:
    def __init__(self, upload):
        self.upload = upload

    def request(self, method, url, **kw):
        if method == "POST" and url.endswith("/activities"):
            return self.upload
        if method == "GET":
            return Resp(200, {"icu_weight": 72})
        return Resp(200, {})


def test_upload_archives_run_as_uploaded_and_survives_store_failure(c, monkeypatch):
    monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=Http(Resp(201, {"activities": [{"id": "i55"}]}))))
    r = c.post("/api/upload", json={"log": log(60)}, headers=AUTH).json()
    assert r["ok"] and r["store_error"] is None
    run = c.get(f"/api/runs/{RID}", headers=AUTH).json()
    assert run["status"] == "uploaded" and run["intervals_id"] == "i55"
    # duplicate on intervals.icu (no activities) still counts as uploaded
    monkeypatch.setattr(appmod, "client", lambda: IntervalsClient("k", "i1", http=Http(Resp(201, {"activities": []}))))
    assert c.post("/api/upload", json={"log": log(60)}, headers=AUTH).json()["ok"]
    assert c.get(f"/api/runs/{RID}", headers=AUTH).json()["intervals_id"] == "i55"  # id kept
    # a broken archive never fails the upload
    monkeypatch.setattr(appmod, "runs", lambda: type("B", (), {"save": lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))})())
    r = c.post("/api/upload", json={"log": log(60)}, headers=AUTH).json()
    assert r["ok"] and "disk full" in r["store_error"]
