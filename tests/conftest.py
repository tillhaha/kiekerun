import pytest

from server import app as appmod


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    yield
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
