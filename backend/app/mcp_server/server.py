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

import json
from pathlib import Path
from typing import Any, cast

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
    tools: list[types.Tool] = []
    tool_cls = cast(Any, types.Tool)
    for schema in schemas.values():
        try:
            t: types.Tool = tool_cls(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            )
        except TypeError:
            t = tool_cls(
                name=schema["name"],
                description=schema["description"],
                input_schema=schema["inputSchema"],
            )
        tools.append(t)
    return tools


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
    server_any = cast(Any, server)

    if hasattr(server, "list_tools") and callable(server.list_tools):

        @server_any.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return await list_tools_impl(schemas)
    else:

        async def handle_list_tools_req(*_args: Any) -> types.ServerResult:
            tools = await list_tools_impl(schemas)
            return types.ListToolsResult(tools=tools)

        if hasattr(server_any, "add_request_handler") and callable(server_any.add_request_handler):
            list_tools_method = types.ListToolsRequest.model_fields["method"].default
            paginated_params = getattr(types, "PaginatedRequestParams", types.RequestParams)
            server_any.add_request_handler(list_tools_method, paginated_params, handle_list_tools_req)
        else:
            request_handlers = getattr(server_any, "request_handlers", None)
            if request_handlers is None:
                request_handlers = server_any._request_handlers
            request_handlers[types.ListToolsRequest] = handle_list_tools_req
            request_handlers[types.ListToolsRequest.model_fields["method"].default] = handle_list_tools_req

    if hasattr(server, "call_tool") and callable(server.call_tool):

        @server_any.call_tool()
        async def handle_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            # The SDK auto-serializes a returned dict to JSON text content; see
            # mcp.server.lowlevel.server.Server.call_tool's docstring.
            return await call_tool_impl(name, arguments, ctx)
    else:

        async def handle_call_tool_req(*_args: Any) -> types.ServerResult:
            req_or_params = _args[-1]
            tool_name = req_or_params.name
            arguments = req_or_params.arguments or {}
            res_dict = await call_tool_impl(tool_name, arguments, ctx)
            sc = res_dict if isinstance(res_dict, dict) else None
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(res_dict, indent=2))],
                structured_content=sc,
                is_error=False,
            )

        if hasattr(server_any, "add_request_handler") and callable(server_any.add_request_handler):
            call_tool_method = types.CallToolRequest.model_fields["method"].default
            server_any.add_request_handler(call_tool_method, types.CallToolRequestParams, handle_call_tool_req)
        else:
            request_handlers = getattr(server_any, "request_handlers", None)
            if request_handlers is None:
                request_handlers = server_any._request_handlers
            request_handlers[types.CallToolRequest] = handle_call_tool_req
            request_handlers[types.CallToolRequest.model_fields["method"].default] = handle_call_tool_req

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
