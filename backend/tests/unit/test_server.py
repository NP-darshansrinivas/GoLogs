"""Unit tests for `mcp_server.server`'s protocol-independent handler logic.

`list_tools_impl` and `call_tool_impl` are plain async functions with no
stdio/subprocess dependency, extracted specifically so this logic can be
tested directly. Full wire-protocol behavior (the part that genuinely needs
a subprocess) is covered separately by
`tests/integration/test_mcp_server_protocol.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import Case
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.mcp_server.server import (
    build_default_context,
    build_server,
    call_tool_impl,
    list_tools_impl,
)
from app.mcp_server.tool_registry import ToolContext, load_tool_schemas


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = make_session_factory(engine)
    return ToolContext(session_factory=session_factory, case_storage_dir=tmp_path)


class TestListToolsImpl:
    @pytest.mark.asyncio
    async def test_returns_all_registered_schemas_as_tool_objects(self) -> None:
        schemas = load_tool_schemas()
        tools = await list_tools_impl(schemas)
        assert {t.name for t in tools} == set(schemas)

    @pytest.mark.asyncio
    async def test_tool_objects_carry_input_schema_through_unmodified(self) -> None:
        schemas = load_tool_schemas()
        tools = await list_tools_impl(schemas)
        by_name = {t.name: t for t in tools}
        tool = by_name["mem.run_plugin"]
        input_schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
        assert input_schema == schemas["mem.run_plugin"]["inputSchema"]


class TestCallToolImpl:
    @pytest.mark.asyncio
    async def test_unknown_tool_name_returns_structured_error_not_exception(self, ctx) -> None:
        result = await call_tool_impl("nonexistent.tool", {"case_id": "x"}, ctx)
        assert result["error"]["type"] == "unknown_tool"

    @pytest.mark.asyncio
    async def test_valid_tool_call_routes_to_correct_dispatch_function(self, ctx) -> None:
        with session_scope(ctx.session_factory) as s:
            case = Case(name="Direct Test")
            s.add(case)
            s.flush()
            case_id = case.id

        result = await call_tool_impl("case.get_metadata", {"case_id": case_id}, ctx)
        assert result["case_id"] == case_id
        assert result["name"] == "Direct Test"

    @pytest.mark.asyncio
    async def test_unexpected_exception_in_dispatch_is_caught_not_propagated(
        self, ctx, monkeypatch
    ) -> None:
        import app.mcp_server.server as server_module

        async def _boom(args, ctx):
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setitem(server_module.TOOL_DISPATCH, "case.get_metadata", _boom)

        result = await call_tool_impl("case.get_metadata", {"case_id": "x"}, ctx)
        assert result["error"]["type"] == "internal_error"
        assert "simulated unexpected failure" in result["error"]["message"]


class TestBuildServer:
    def test_build_server_registers_a_server_named_after_product(self, ctx) -> None:
        server = build_server(ctx)
        assert server.name == "gologs"

    def test_build_default_context_creates_case_storage_dir(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path / 'test.db'}")
        monkeypatch.setenv("CASE_STORAGE_DIR", str(tmp_path / "cases"))
        # Settings is a lazily-constructed singleton; clear it so the env
        # vars just set actually take effect for this test.
        import app.config as config_module

        config_module._settings = None
        try:
            built_ctx = build_default_context()
            assert built_ctx.case_storage_dir.exists()
            assert built_ctx.case_storage_dir == (tmp_path / "cases").resolve()
        finally:
            config_module._settings = None  # don't leak state into other tests
