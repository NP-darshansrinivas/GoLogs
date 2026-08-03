"""Integration test: the conversation loop wired to a *real* MCP stdio
server subprocess — not the fast in-process dispatch used in
`tests/unit/test_tool_call_loop.py`. This is the actual production topology
(orchestrator -> permission_gate -> real MCP ClientSession -> real
subprocess), proving the M3 deliverable ("Ollama tool-calling loop +
permission gate") works end-to-end through the real protocol layer built
in M2. Ollama itself is still mocked (no live server in this sandbox).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, Event
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.orchestrator.ollama_client import OllamaClient
from app.orchestrator.tool_call_loop import (
    DoneEvent,
    ToolCallEvent,
    ToolResultEvent,
    run_conversation_step,
)

BACKEND_ROOT = Path(__file__).parent.parent.parent
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@pytest.fixture(scope="module")
def parsed_records():
    records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
    return records[:10]


@pytest.fixture
def seeded_case(tmp_path, parsed_records) -> tuple[str, str, Path]:
    db_path = tmp_path / "orchestrator_test.db"
    db_url = f"sqlite:///{db_path}"
    case_storage_dir = tmp_path / "cases"
    case_storage_dir.mkdir()

    engine = make_engine(db_url)
    init_db(engine)
    sf = make_session_factory(engine)
    normalized = [normalize_record(r.xml) for r in parsed_records]

    with session_scope(sf) as s:
        case = Case(name="Real MCP Orchestrator Test", status="parsed")
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


def _mock_ollama_client(ndjson_chunks: list[dict]) -> OllamaClient:
    body = "\n".join(json.dumps(c) for c in ndjson_chunks).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )
    return OllamaClient(host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client)


def _make_real_mcp_tool_executor(session: ClientSession):
    """A ToolExecutor backed by a real MCP ClientSession — the actual
    production topology, as opposed to the in-process fast path used in
    `tests/unit/test_tool_call_loop.py`."""

    async def executor(tool_name: str, arguments: dict) -> dict:
        result = await session.call_tool(tool_name, arguments)
        return (
            result.structured_content
            if hasattr(result, "structured_content")
            else result.structuredContent
        )

    return executor


class TestConversationLoopOverRealMcpProtocol:
    @pytest.mark.asyncio
    async def test_auto_approve_tool_call_round_trips_through_real_mcp_subprocess(
        self, seeded_case
    ) -> None:
        case_id, db_url, case_storage_dir = seeded_case
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server.server"],
            cwd=str(BACKEND_ROOT),
            env={
                "DB_URL": db_url,
                "CASE_STORAGE_DIR": str(case_storage_dir),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
        )

        chunks = [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "case.get_metadata",
                                "arguments": {"case_id": case_id},
                            }
                        }
                    ],
                },
                "done": True,
            }
        ]
        ollama_client = _mock_ollama_client(chunks)

        # Use a real DB session for permission_gate's dynamic classification
        # (the mem.run_plugin cache check) — separate connection from the
        # subprocess's, both pointed at the same on-disk SQLite file.
        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                tool_executor = _make_real_mcp_tool_executor(mcp_session)

                messages = [{"role": "user", "content": "What's in this case?"}]
                with session_factory() as gate_session:
                    events = [
                        e
                        async for e in run_conversation_step(
                            messages, [], gate_session, ollama_client, tool_executor
                        )
                    ]

        tool_call_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]

        assert len(tool_call_events) == 1
        assert tool_call_events[0].tool_name == "case.get_metadata"
        assert len(tool_result_events) == 1
        # This value came from the real subprocess querying the real SQLite
        # file — not an in-process mock.
        assert tool_result_events[0].result["event_count"] == 10
        assert tool_result_events[0].result["name"] == "Real MCP Orchestrator Test"
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stopped_for_confirmation is False

        await ollama_client.aclose()
