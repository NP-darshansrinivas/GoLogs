"""GoLogs FastAPI application entrypoint (PRD §14).

Owns process-level lifecycle: DB initialization, launching the MCP server
subprocess once at startup and keeping one `ClientSession` alive for the
app's lifetime (per `docs/context_transfer.md` §9's design note — a fresh
subprocess per request would be needlessly slow and defeats the point of a
long-lived stdio connection), and constructing the `OllamaClient`.

Bound to `127.0.0.1` only (PRD §14, §20 Spoofing mitigation: "N/A — single
local user, no auth boundary crossed... Bind API to 127.0.0.1 only"). No
CORS wildcard, no external network exposure.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

from app.config import get_settings
from app.db.session import init_db, make_engine, make_session_factory
from app.orchestrator.ollama_client import OllamaClient
from app.state import AppState

log = structlog.get_logger(__name__)

BACKEND_ROOT = Path(__file__).parent.parent


def _mcp_server_params() -> StdioServerParameters:
    settings = get_settings()
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
        cwd=str(BACKEND_ROOT),
        env={
            "DB_URL": settings.db_url,
            "CASE_STORAGE_DIR": str(settings.case_storage_dir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = make_engine(settings.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = Path(settings.case_storage_dir)
    case_storage_dir.mkdir(parents=True, exist_ok=True)

    ollama_client = OllamaClient(host=settings.ollama_host, model=settings.ollama_model_name)

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(_mcp_server_params()))
        mcp_session = await stack.enter_async_context(ClientSession(read, write))
        await mcp_session.initialize()
        log.info("mcp_subprocess_ready")

        app.state.gologs = AppState(
            settings=settings,
            session_factory=session_factory,
            case_storage_dir=case_storage_dir,
            mcp_session=mcp_session,
            ollama_client=ollama_client,
        )
        try:
            yield
        finally:
            await ollama_client.aclose()


def create_app(lifespan_override: Any = None) -> FastAPI:
    """Build the FastAPI app.

    `lifespan_override` lets tests substitute a fake lifespan that sets
    `app.state.gologs` directly (a test-double `AppState`, no real MCP
    subprocess or Ollama connection) instead of the real one above —
    standard FastAPI test pattern for a lifespan that does real I/O.
    """
    settings = get_settings()
    app = FastAPI(
        title=settings.product_name,
        description=settings.product_tagline,
        version="0.1.0",
        lifespan=lifespan_override or lifespan,
    )

    from app.api.routes_cases import router as cases_router
    from app.api.routes_chat import router as chat_router
    from app.api.ws_chat import router as ws_router

    app.include_router(cases_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(ws_router)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """PRD §22: reports Ollama reachability, DB connectivity, last-parse status."""
        state: AppState = app.state.gologs
        health_status: dict[str, Any] = {"status": "ok", "checks": {}}

        try:
            with state.session_factory() as session:
                session.execute(text("SELECT 1"))
            health_status["checks"]["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            health_status["checks"]["database"] = f"error: {exc}"
            health_status["status"] = "degraded"

        try:
            chunks_seen = False
            async for _ in state.ollama_client.chat(
                messages=[{"role": "user", "content": "ping"}], stream=True
            ):
                chunks_seen = True
                break
            health_status["checks"]["ollama"] = "ok" if chunks_seen else "no response"
        except Exception as exc:  # noqa: BLE001
            health_status["checks"]["ollama"] = f"unreachable: {exc}"
            health_status["status"] = "degraded"

        return health_status

    return app


app = create_app()
