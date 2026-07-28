"""GoLogs MCP server — stdio transport, local process only (PRD §9).

This process is launched as a subprocess by the orchestrator (or, for
testing/debugging, directly via `python -m app.mcp_server.server`) and
speaks MCP over stdin/stdout. It has no network listener and is not
reachable from outside the local machine.

Per PRD §10.2: this module owns *protocol* concerns only (tool listing,
request/response marshalling). All actual business logic lives in
`tool_registry.py`'s dispatch functions.

`list_tools_impl`/`call_tool_impl` are extracted as standalone, protocol-
independent async functions (rather than being defined only as closures
inside `build_server`) specifically so they're directly unit-testable
without a running stdio server — `build_server` below is a thin adapter
that registers them with the MCP SDK's `Server` object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import mcp.server.stdio
import mcp.types as types
import structlog
from mcp.server import Server
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions

from app.config import get_settings
from app.db.session import init_db, make_engine, make_session_factory
from app.mcp_server.tool_registry import TOOL_DISPATCH, ToolContext, load_tool_schemas

log = structlog.get_logger(__name__)


async def list_tools_impl(schemas: dict[str, Any]) -> list[types.Tool]:
    """Build the Tool list from loaded schemas. Pure function, no I/O."""
    return [
        types.Tool(
            name=schema["name"],
            description=schema["description"],
            inputSchema=schema["inputSchema"],
        )
        for schema in schemas.values()
    ]


async def call_tool_impl(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Route a tool call to its dispatch function.

    Never raises: every expected failure is already caught inside the
    dispatch functions and returned as a structured `{"error": ...}` dict
    (PRD §22); the try/except here is only a last-resort backstop so the
    server process itself can never be crashed by a single bad tool call —
    including one that hits a genuine bug in a dispatch function.
    """
    dispatch = TOOL_DISPATCH.get(name)
    if dispatch is None:
        return {"error": {"type": "unknown_tool", "message": f"No such tool: {name!r}"}}
    try:
        return await dispatch(arguments, ctx)
    except Exception as exc:  # noqa: BLE001 - last-resort guard, PRD §22
        log.error("tool_dispatch_unhandled_exception", tool=name, error=str(exc))
        return {
            "error": {
                "type": "internal_error",
                "message": f"Unhandled error executing {name!r}: {exc}",
            }
        }


def build_server(ctx: ToolContext) -> Server:
    """Construct the MCP Server with GoLogs' six tools registered against `ctx`.

    Kept separate from process/transport setup so tests can build a server
    against an in-memory SQLite `ToolContext` without touching stdio at all.
    """
    settings = get_settings()
    server: Server = Server(settings.product_name.lower())
    schemas = load_tool_schemas()

    # mcp SDK's list_tools decorator has no type annotations in the
    # installed version (`def list_tools(self):`) — third-party stub gap.
    @server.list_tools()  # type: ignore[no-untyped-call]
    async def handle_list_tools() -> list[types.Tool]:
        return await list_tools_impl(schemas)

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # The SDK auto-serializes a returned dict to JSON text content; see
        # mcp.server.lowlevel.server.Server.call_tool's docstring.
        return await call_tool_impl(name, arguments, ctx)

    return server


def build_default_context() -> ToolContext:
    """Build the ToolContext from real application settings (production use)."""
    settings = get_settings()
    engine = make_engine(settings.db_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = Path(settings.case_storage_dir)
    case_storage_dir.mkdir(parents=True, exist_ok=True)
    return ToolContext(session_factory=session_factory, case_storage_dir=case_storage_dir)


async def run() -> None:
    ctx = build_default_context()
    server = build_server(ctx)
    settings = get_settings()

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=settings.product_name.lower(),
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    import anyio

    anyio.run(run)


if __name__ == "__main__":
    main()
