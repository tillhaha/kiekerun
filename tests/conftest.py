import pytest

from server import app as appmod
from server import mcp as mcpmod


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
    mcpmod.WRITE_RATE.reset()
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("MCP_ALLOW_WRITE", raising=False)
    yield
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
