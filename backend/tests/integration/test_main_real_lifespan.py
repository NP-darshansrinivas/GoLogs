"""Tests the *real* `create_app()` lifespan (no override) -- the actual
production startup path that launches the real MCP server subprocess.
Every other API test uses `lifespan_override` for speed; this one exists
specifically to prove the real wiring (`main.lifespan` -> `stdio_client` ->
`ClientSession.initialize()` -> `app.state.gologs`) actually works, not
just the test-double path.

Ollama itself isn't running in this sandbox, so `/health`'s Ollama check is
expected to report "unreachable" here -- that's the correct, honest
behavior being verified, not a workaround.

Deliberately a single consolidated test (not split across multiple fixture
instantiations): spawning a real MCP subprocess via anyio TaskGroups inside
a pytest-asyncio fixture has a known task-scope-affinity conflict when the
fixture is invoked more than once per session. One thorough test proves the
same real-boot capability without fighting that interaction; the underlying
MCP-subprocess-wiring capability itself is already proven repeatedly and
robustly elsewhere (`tests/integration/test_orchestrator_real_mcp.py`,
`tests/integration/test_mcp_server_protocol.py`).
"""

from __future__ import annotations

import io

import httpx
import pytest

import app.config as config_module
from app.main import create_app


class TestRealLifespan:
    @pytest.mark.asyncio
    async def test_real_lifespan_boots_mcp_subprocess_and_serves_real_requests(
        self, tmp_path, monkeypatch
    ) -> None:
        db_path = tmp_path / "real_lifespan_test.db"
        monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("CASE_STORAGE_DIR", str(tmp_path / "cases"))
        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:19999")  # deliberately unreachable
        config_module._settings = None

        try:
            app = create_app()  # no override -- real lifespan, real MCP subprocess
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    health_response = await client.get("/health")
                    assert health_response.status_code == 200
                    health_body = health_response.json()
                    assert health_body["checks"]["database"] == "ok"
                    # Ollama genuinely isn't running in this sandbox --
                    # this must be reported honestly, not swallowed.
                    assert health_body["checks"]["ollama"] != "ok"
                    assert health_body["status"] == "degraded"

                    create_response = await client.post(
                        "/api/cases",
                        data={"name": "Real Boot Test"},
                        files={
                            "files": ("empty.evtx", io.BytesIO(b""), "application/octet-stream")
                        },
                    )
                    assert create_response.status_code == 200
                    case_id = create_response.json()["case_id"]

                    get_response = await client.get(f"/api/cases/{case_id}")
                    assert get_response.status_code == 200
                    assert get_response.json()["name"] == "Real Boot Test"
        finally:
            config_module._settings = None
