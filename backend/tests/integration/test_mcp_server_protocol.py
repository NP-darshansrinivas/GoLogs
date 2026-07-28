"""Integration test: a *real* MCP client talking to a *real* stdio server subprocess.

This is the M2 milestone deliverable ("Tools callable via a raw MCP client
script") made permanent: no mocking of the MCP protocol layer at all. We
spawn `python -m app.mcp_server.server` as a subprocess, speak real MCP
initialize/list_tools/call_tool messages to it over stdio, and assert on the
real responses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, Event
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope

BACKEND_ROOT = Path(__file__).parent.parent.parent
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@pytest.fixture(scope="module")
def parsed_security_records():
    records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
    return records[:10]


@pytest.fixture
def seeded_case(tmp_path, parsed_security_records) -> tuple[str, str, Path]:
    """Seed a real SQLite file (shared with the subprocess) with a case + events."""
    db_path = tmp_path / "gologs_test.db"
    db_url = f"sqlite:///{db_path}"
    case_storage_dir = tmp_path / "cases"
    case_storage_dir.mkdir()

    engine = make_engine(db_url)
    init_db(engine)
    sf = make_session_factory(engine)

    normalized = [normalize_record(r.xml) for r in parsed_security_records]

    with session_scope(sf) as s:
        case = Case(name="Protocol Test Case", status="parsed")
        s.add(case)
        s.flush()
        case_id = case.id
        for n in normalized:
            s.add(
                Event(
                    case_id=case_id,
                    channel=n.channel,
                    event_id=n.event_id,
                    time_created=n.time_created,
                    computer=n.computer,
                    user_sid=n.user_sid,
                    raw_xml=n.raw_xml,
                    normalized_fields=n.normalized_fields,
                )
            )

    return case_id, db_url, case_storage_dir


def _server_params(db_url: str, case_storage_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
        cwd=str(BACKEND_ROOT),
        env={
            "DB_URL": db_url,
            "CASE_STORAGE_DIR": str(case_storage_dir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )


class TestRealMcpProtocol:
    @pytest.mark.asyncio
    async def test_list_tools_returns_all_six_with_correct_schemas(self, seeded_case) -> None:
        _case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()

        names = {tool.name for tool in result.tools}
        assert names == {
            "case.get_metadata",
            "evtx.query_events",
            "evtx.get_event_detail",
            "mem.list_processes",
            "mem.run_plugin",
            "report.append_finding",
        }
        query_tool = next(t for t in result.tools if t.name == "evtx.query_events")
        assert query_tool.inputSchema["properties"]["limit"]["maximum"] == 200

    @pytest.mark.asyncio
    async def test_case_get_metadata_over_real_protocol(self, seeded_case) -> None:
        case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("case.get_metadata", {"case_id": case_id})

        assert result.isError is False
        assert result.structuredContent["event_count"] == 10
        assert result.structuredContent["name"] == "Protocol Test Case"

    @pytest.mark.asyncio
    async def test_evtx_query_events_over_real_protocol(self, seeded_case) -> None:
        case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "evtx.query_events", {"case_id": case_id, "event_id": 4608}
                )

        assert result.isError is False
        events = result.structuredContent["events"]
        assert all(e["event_id"] == 4608 for e in events)

    @pytest.mark.asyncio
    async def test_unknown_case_returns_structured_error_not_protocol_error(
        self, seeded_case
    ) -> None:
        _case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "case.get_metadata", {"case_id": "totally-made-up"}
                )

        # A missing case is a normal tool-level result, not an MCP protocol
        # error — the LLM needs to see this as a message it can reason
        # about, not a hard failure.
        assert result.isError is False
        assert result.structuredContent["error"]["type"] == "case_not_found"

    @pytest.mark.asyncio
    async def test_schema_validation_rejects_malformed_arguments(self, seeded_case) -> None:
        _case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # mem.run_plugin requires "plugin" to be one of the enum
                # values — the SDK validates this against inputSchema
                # before our dispatch code ever runs.
                result = await session.call_tool(
                    "mem.run_plugin", {"case_id": "x", "plugin": "rm -rf /"}
                )

        assert result.isError is True

    @pytest.mark.asyncio
    async def test_evtx_get_event_detail_over_real_protocol(self, seeded_case) -> None:
        case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.call_tool(
                    "evtx.query_events", {"case_id": case_id, "limit": 1}
                )
                event_uid = listing.structuredContent["events"][0]["uid"]

                detail = await session.call_tool(
                    "evtx.get_event_detail", {"case_id": case_id, "event_uid": event_uid}
                )

        assert detail.isError is False
        assert detail.structuredContent["uid"] == event_uid
        assert "<Event" in detail.structuredContent["raw_xml"]

    @pytest.mark.asyncio
    async def test_unknown_tool_name_returns_structured_error_via_real_protocol(
        self, seeded_case
    ) -> None:
        _case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("totally.made.up.tool", {"case_id": "x"})

        assert result.isError is False
        assert result.structuredContent["error"]["type"] == "unknown_tool"

    @pytest.mark.asyncio
    async def test_report_append_finding_persists_via_real_protocol_call(self, seeded_case) -> None:
        case_id, db_url, case_storage_dir = seeded_case
        params = _server_params(db_url, case_storage_dir)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "report.append_finding",
                    {
                        "case_id": case_id,
                        "finding_text": "Test finding via real MCP protocol.",
                        "evidence_refs": [],
                    },
                )

        assert result.isError is False
        assert result.structuredContent["acknowledged"] is True

        # Verify it actually landed in the DB, not just an in-memory ack.
        engine = make_engine(db_url)
        sf = make_session_factory(engine)
        from sqlalchemy import select

        from app.core.models import Finding

        with sf() as s:
            rows = s.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            assert len(rows) == 1
            assert rows[0].finding_text == "Test finding via real MCP protocol."
