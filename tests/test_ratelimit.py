import base64

import pytest
from fastapi.testclient import TestClient

from server import app as appmod
from server.ratelimit import FailLimiter, RateLimiter

GOOD = {"Authorization": "Basic " + base64.b64encode(b"tito:secret").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"tito:wrong").decode()}
TOKEN = "t" * 24


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_fail_limiter_locks_after_n_fails_and_unlocks_after_lockout():
    clk = Clock()
    f = FailLimiter(max_fails=3, window_s=60, lockout_s=120, clock=clk)
    for _ in range(2):
        f.fail("a")
    assert f.retry_after("a") == 0
    f.fail("a")
    assert 0 < f.retry_after("a") <= 121 and f.retry_after("b") == 0
    clk.t += 121
    assert f.retry_after("a") == 0
    f.fail("a")
    assert f.retry_after("a") == 0  # counter restarted after the lockout


def test_fail_limiter_window_expires_old_fails_and_ok_resets():
    clk = Clock()
    f = FailLimiter(max_fails=3, window_s=60, lockout_s=120, clock=clk)
    f.fail("a"); f.fail("a")
    clk.t += 61
    f.fail("a")
    assert f.retry_after("a") == 0  # the first two aged out
    f.fail("a"); f.ok("a"); f.fail("a"); f.fail("a")
    assert f.retry_after("a") == 0


def test_fail_limiter_memory_is_bounded(monkeypatch):
    import server.ratelimit as rl
    monkeypatch.setattr(rl, "MAX_KEYS", 100)
    f = FailLimiter(clock=Clock())
    for i in range(1000):
        f.fail(f"ip{i}")
    assert len(f._fails) <= 100


def test_rate_limiter_window():
    clk = Clock()
    r = RateLimiter(limit=3, window_s=60, clock=clk)
    assert [r.hit("a") for _ in range(3)] == [0, 0, 0]
    assert 0 < r.hit("a") <= 60 and r.hit("b") == 0
    clk.t += 61
    assert r.hit("a") == 0


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("KIEKERUN_USER", "tito")
    monkeypatch.setenv("KIEKERUN_PASSWORD", "secret")
    monkeypatch.setenv("MCP_TOKEN", TOKEN)
    return TestClient(appmod.app)


def ip(n):
    return {"X-Forwarded-For": f"10.0.0.{n}"}


def test_login_lockout_blocks_even_the_right_password(c):
    for _ in range(10):
        assert c.get("/api/health", headers={**BAD, **ip(1)}).status_code == 401
    r = c.get("/api/health", headers={**GOOD, **ip(1)})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert c.get("/", headers={**GOOD, **ip(1)}).status_code == 429  # page too
    assert c.get("/", headers={**GOOD, **ip(2)}).status_code == 200  # other address unaffected


def test_requests_without_credentials_are_not_counted(c):
    for _ in range(30):
        assert c.get("/", headers=ip(3)).status_code == 401  # browser asks without credentials first
    assert c.get("/", headers={**GOOD, **ip(3)}).status_code == 200


def test_successful_login_resets_the_counter(c):
    for _ in range(9):
        c.get("/", headers={**BAD, **ip(4)})
    assert c.get("/", headers={**GOOD, **ip(4)}).status_code == 200
    for _ in range(9):
        c.get("/", headers={**BAD, **ip(4)})
    assert c.get("/", headers={**GOOD, **ip(4)}).status_code == 200


def test_client_cannot_spoof_its_address_via_forwarded_for(c):
    # one trusted proxy appends the real peer last; the client-supplied left part must not matter
    for i in range(10):
        c.get("/", headers={**BAD, "X-Forwarded-For": f"1.1.1.{i}, 10.0.0.5"})
    assert c.get("/", headers={**GOOD, "X-Forwarded-For": "9.9.9.9, 10.0.0.5"}).status_code == 429


def test_mcp_wrong_token_locks_out_and_shares_the_counter_with_login(c):
    for _ in range(5):
        assert c.post(f"/mcp/{'x' * 24}", json={}, headers=ip(6)).status_code == 404
    for _ in range(5):
        c.get("/", headers={**BAD, **ip(6)})
    r = c.post(f"/mcp/{TOKEN}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=ip(6))
    assert r.status_code == 429


def test_mcp_valid_requests_are_rate_limited(c, monkeypatch):
    monkeypatch.setattr(appmod, "MCP_RATE", RateLimiter(limit=3, window_s=60))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert [c.post(f"/mcp/{TOKEN}", json=ping, headers=ip(7)).status_code for _ in range(3)] == [200, 200, 200]
    r = c.post(f"/mcp/{TOKEN}", json=ping, headers=ip(7))
    assert r.status_code == 429 and "retry-after" in r.headers
    assert c.post(f"/mcp/{TOKEN}", json=ping, headers=ip(8)).status_code == 200


def test_hops_config_and_fallback_to_peer(c, monkeypatch):
    class R:
        def __init__(self, xff, host="1.2.3.4"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": host})()
    assert appmod.client_ip(R("")) == "1.2.3.4"
    assert appmod.client_ip(R("5.5.5.5, 6.6.6.6")) == "6.6.6.6"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert appmod.client_ip(R("5.5.5.5, 6.6.6.6")) == "5.5.5.5"
    assert appmod.client_ip(R("6.6.6.6")) == "1.2.3.4"  # fewer hops than expected: do not trust the header
