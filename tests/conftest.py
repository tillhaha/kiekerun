import pytest

from server import app as appmod
from server import mcp as mcpmod


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("KIEKERUN_DB", str(tmp_path / "runs" / "kiekerun.db"))
    appmod._RUNS.clear()
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
    mcpmod.WRITE_RATE.reset()
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("MCP_ALLOW_WRITE", raising=False)
    yield
    appmod.FAILS.reset()
    appmod.MCP_RATE.reset()
