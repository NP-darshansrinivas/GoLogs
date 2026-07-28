"""Integration tests for `api.ws_chat` — the actual live chat WebSocket
handler, tested with a *real* WebSocket connection (Starlette's
`TestClient.websocket_connect`), not just unit-level function calls.

Ollama is mocked (no live server in this sandbox), via a fake `mcp_session`
whose `call_tool()` dispatches through the real M2 `TOOL_DISPATCH`
functions and wraps the result to mimic a real `CallToolResult` — so
`AppState.tool_executor` (which calls `mcp_session.call_tool(...).
structuredContent`) exercises real permission-gate + injection-scanner +
dispatch logic end-to-end, exactly as it would against a real MCP
subprocess, just without the ~15s-per-test subprocess-spawn cost.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, ChatMessage, Event, Finding
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.main import create_app
from app.mcp_server.tool_registry import TOOL_DISPATCH, ToolContext
from app.orchestrator.ollama_client import OllamaClient
from app.state import AppState

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@dataclass
class _FakeCallToolResult:
    structuredContent: dict[str, Any]


class _FakeMcpSession:
    """Mimics `mcp.ClientSession`'s `call_tool()` by dispatching through
    the real M2 `TOOL_DISPATCH` functions in-process — real permission-gate
    classification happens one layer up in `tool_call_loop`, so this fake
    only needs to reproduce *execution*, not authorization."""

    def __init__(self, tool_ctx: ToolContext):
        self._ctx = tool_ctx

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeCallToolResult:
        dispatch = TOOL_DISPATCH[name]
        result = await dispatch(arguments, self._ctx)
        return _FakeCallToolResult(structuredContent=result)


@pytest.fixture(scope="module")
def parsed_records():
    records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
    return records[:10]


def _mock_ollama_client(ndjson_chunks: list[dict]) -> OllamaClient:
    body = "\n".join(json.dumps(c) for c in ndjson_chunks).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )
    return OllamaClient(host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client)


@pytest.fixture
def app_state_and_case(tmp_path, parsed_records):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = tmp_path / "cases"
    case_storage_dir.mkdir()

    normalized = [normalize_record(r.xml) for r in parsed_records]
    with session_scope(session_factory) as s:
        case = Case(name="WS Test Case", status="parsed")
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

    tool_ctx = ToolContext(session_factory=session_factory, case_storage_dir=case_storage_dir)
    fake_mcp_session = _FakeMcpSession(tool_ctx)

    return session_factory, case_storage_dir, case_id, fake_mcp_session


def _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client) -> FastAPI:
    state = AppState(
        settings=type("S", (), {"max_tool_result_tokens": 4000})(),
        session_factory=session_factory,
        case_storage_dir=case_storage_dir,
        mcp_session=fake_mcp_session,
        ollama_client=ollama_client,
    )

    @asynccontextmanager
    async def fake_lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.gologs = state
        yield

    return create_app(lifespan_override=fake_lifespan)


class TestWsChatUnknownCase:
    def test_closes_connection_for_unknown_case(self, app_state_and_case) -> None:
        session_factory, case_storage_dir, _case_id, fake_mcp_session = app_state_and_case
        ollama_client = _mock_ollama_client([])
        app = _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client)

        with TestClient(app) as client, pytest.raises(Exception):  # noqa: B017,PT011,SIM117
            with client.websocket_connect("/ws/cases/does-not-exist/chat") as ws:
                ws.receive_json()


class TestWsChatPlainTextResponse:
    def test_user_message_streams_tokens_and_done(self, app_state_and_case) -> None:
        session_factory, case_storage_dir, case_id, fake_mcp_session = app_state_and_case
        chunks = [
            {"message": {"role": "assistant", "content": "The "}, "done": False},
            {
                "message": {"role": "assistant", "content": "logon succeeded."},
                "done": True,
                "done_reason": "stop",
            },
        ]
        ollama_client = _mock_ollama_client(chunks)
        app = _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client)

        with TestClient(app) as client, client.websocket_connect(f"/ws/cases/{case_id}/chat") as ws:
            ws.send_json({"type": "user_message", "content": "What happened?"})

            frames = []
            while True:
                frame = ws.receive_json()
                frames.append(frame)
                if frame["type"] == "done":
                    break

        token_frames = [f for f in frames if f["type"] == "token"]
        assert "".join(f["content"] for f in token_frames) == "The logon succeeded."
        assert frames[-1]["type"] == "done"
        assert frames[-1]["stopped_for_confirmation"] is False

        # The exchange was persisted for reload/reconnect (routes_chat.py's history endpoint).
        with session_factory() as session:
            from sqlalchemy import select

            rows = (
                session.execute(select(ChatMessage).where(ChatMessage.case_id == case_id))
                .scalars()
                .all()
            )
            roles = {m.role for m in rows}
            assert "user" in roles
            assert "assistant" in roles


class TestWsChatAutoApproveToolCall:
    def test_tool_call_executes_and_streams_result(self, app_state_and_case) -> None:
        session_factory, case_storage_dir, case_id, fake_mcp_session = app_state_and_case
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
        app = _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client)

        with TestClient(app) as client, client.websocket_connect(f"/ws/cases/{case_id}/chat") as ws:
            ws.send_json({"type": "user_message", "content": "Tell me about this case."})

            frames = []
            while True:
                frame = ws.receive_json()
                frames.append(frame)
                if frame["type"] == "done":
                    break

        tool_call_frames = [f for f in frames if f["type"] == "tool_call"]
        tool_result_frames = [f for f in frames if f["type"] == "tool_result"]
        assert len(tool_call_frames) == 1
        assert tool_call_frames[0]["tool_name"] == "case.get_metadata"
        assert tool_call_frames[0]["risk_class"] == "AUTO_APPROVE"
        assert len(tool_result_frames) == 1
        assert tool_result_frames[0]["result"]["event_count"] == 10


class TestWsChatRequiresConfirmationFlow:
    def test_confirmation_required_then_approved_via_confirm_frame(
        self, app_state_and_case
    ) -> None:
        session_factory, case_storage_dir, case_id, fake_mcp_session = app_state_and_case
        first_turn_chunks = [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "report.append_finding",
                                "arguments": {
                                    "case_id": case_id,
                                    "finding_text": "Suspicious logon detected.",
                                    "evidence_refs": [],
                                },
                            }
                        }
                    ],
                },
                "done": True,
            }
        ]
        # After the confirm frame, run_conversation_step is called again;
        # this time the mocked model just replies with plain text.
        second_turn_chunks = [
            {"message": {"role": "assistant", "content": "Finding recorded."}, "done": True}
        ]

        call_count = {"n": 0}
        bodies = [
            "\n".join(json.dumps(c) for c in first_turn_chunks).encode(),
            "\n".join(json.dumps(c) for c in second_turn_chunks).encode(),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            idx = min(call_count["n"], len(bodies) - 1)
            call_count["n"] += 1
            return httpx.Response(200, content=bodies[idx])

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
        )
        ollama_client = OllamaClient(
            host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client
        )
        app = _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client)

        with TestClient(app) as client, client.websocket_connect(f"/ws/cases/{case_id}/chat") as ws:
            ws.send_json({"type": "user_message", "content": "Log this finding."})

            first_frames = []
            while True:
                frame = ws.receive_json()
                first_frames.append(frame)
                if frame["type"] == "done":
                    break

            assert any(f["type"] == "confirmation_required" for f in first_frames)
            assert first_frames[-1]["stopped_for_confirmation"] is True
            # Nothing executed yet.
            assert not any(f["type"] == "tool_result" for f in first_frames)

            ws.send_json({"type": "confirm", "approved": True})

            second_frames = []
            while True:
                frame = ws.receive_json()
                second_frames.append(frame)
                if frame["type"] == "done":
                    break

        result_frames = [f for f in second_frames if f["type"] == "tool_result"]
        assert len(result_frames) == 1
        assert result_frames[0]["result"]["acknowledged"] is True

        with session_factory() as session:
            from sqlalchemy import select

            findings = (
                session.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            )
            assert len(findings) == 1
            assert findings[0].finding_text == "Suspicious logon detected."


class TestWsChatUnknownFrameType:
    def test_unknown_frame_type_returns_error_not_crash(self, app_state_and_case) -> None:
        session_factory, case_storage_dir, case_id, fake_mcp_session = app_state_and_case
        ollama_client = _mock_ollama_client([])
        app = _make_app(session_factory, case_storage_dir, fake_mcp_session, ollama_client)

        with TestClient(app) as client, client.websocket_connect(f"/ws/cases/{case_id}/chat") as ws:
            ws.send_json({"type": "not_a_real_frame_type"})
            frame = ws.receive_json()

        assert frame["type"] == "error"
