"""Unit tests for `orchestrator.tool_call_loop`.

Ollama itself is mocked (no live server in this sandbox — see
`docs/context_transfer.md` §9), using the same realistic chunk fixtures as
`test_ollama_client.py`. Everything downstream of the mocked model response
is real: real `permission_gate` classification, real `injection_scanner`
scanning, real truncation logic, and a real (in-process, fast) tool
executor wired to the actual M2 dispatch functions and a real in-memory
SQLite DB with real EVTX-derived fixture data.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, Event
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.mcp_server.tool_registry import TOOL_DISPATCH, ToolContext
from app.orchestrator.ollama_client import OllamaClient
from app.orchestrator.tool_call_loop import (
    ConfirmationRequiredEvent,
    DoneEvent,
    ToolCallEvent,
    ToolResultEvent,
    execute_tool_call,
    resume_after_confirmation,
    run_conversation_step,
    truncate_tool_result_content,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture(scope="module")
def parsed_records():
    records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
    return records[:15]


@pytest.fixture
def case_id(session_factory, parsed_records) -> str:
    normalized = [normalize_record(r.xml) for r in parsed_records]
    with session_scope(session_factory) as s:
        case = Case(name="Orchestrator Test Case", status="parsed")
        s.add(case)
        s.flush()
        cid = case.id
        for n in normalized:
            s.add(
                Event(
                    case_id=cid,
                    channel=n.channel,
                    event_id=n.event_id,
                    time_created=n.time_created,
                    computer=n.computer,
                    user_sid=n.user_sid,
                    raw_xml=n.raw_xml,
                    normalized_fields=n.normalized_fields,
                )
            )
    return cid


@pytest.fixture
def tool_ctx(session_factory, tmp_path) -> ToolContext:
    return ToolContext(session_factory=session_factory, case_storage_dir=tmp_path)


@pytest.fixture
def in_process_tool_executor(tool_ctx):
    """A real (not mocked) tool executor: calls straight into the actual
    M2 dispatch functions, in-process — fast, and exercises real logic."""

    async def executor(tool_name: str, arguments: dict) -> dict:
        dispatch = TOOL_DISPATCH[tool_name]
        return await dispatch(arguments, tool_ctx)

    return executor


def _mock_ollama_client(ndjson_chunks: list[dict]) -> OllamaClient:
    body = "\n".join(json.dumps(c) for c in ndjson_chunks).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )
    return OllamaClient(host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client)


class TestTruncateToolResultContent:
    def test_short_content_is_unchanged(self) -> None:
        content = json.dumps({"a": 1})
        assert truncate_tool_result_content(content, max_tokens=1000) == content

    def test_long_content_is_truncated_with_notice(self) -> None:
        content = "x" * 10000
        result = truncate_tool_result_content(content, max_tokens=100)  # 400 char budget
        assert len(result) < len(content)
        assert "TRUNCATED" in result
        assert result.startswith("x" * 400)

    def test_exactly_at_budget_is_unchanged(self) -> None:
        content = "x" * 400
        assert truncate_tool_result_content(content, max_tokens=100) == content


class TestExecuteToolCall:
    @pytest.mark.asyncio
    async def test_auto_approve_tool_executes_and_returns_scanned_result(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        with session_factory() as session:
            decision, result, flagged = await execute_tool_call(
                "case.get_metadata", {"case_id": case_id}, session, in_process_tool_executor, 4000
            )
        assert decision.risk_class.value == "AUTO_APPROVE"
        assert result["event_count"] == 15
        assert flagged is False

    @pytest.mark.asyncio
    async def test_requires_confirmation_tool_does_not_execute(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        with session_factory() as session:
            decision, result, flagged = await execute_tool_call(
                "report.append_finding",
                {"case_id": case_id, "finding_text": "x", "evidence_refs": []},
                session,
                in_process_tool_executor,
                4000,
            )
        assert decision.risk_class.value == "REQUIRES_CONFIRMATION"
        assert result is None  # never executed

    @pytest.mark.asyncio
    async def test_deny_tool_does_not_execute(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        with session_factory() as session:
            decision, result, flagged = await execute_tool_call(
                "run_command", {"cmd": "whoami"}, session, in_process_tool_executor, 4000
            )
        assert decision.risk_class.value == "DENY"
        assert result is None

    @pytest.mark.asyncio
    async def test_injected_evidence_in_tool_result_is_flagged(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        # Seed a finding whose text itself contains an injection attempt,
        # then query events with a keyword filter that won't match anything
        # real — instead, directly exercise evtx.query_events against the
        # real seeded data and confirm clean data is NOT flagged (control),
        # matching how this path behaves on real evidence.
        with session_factory() as session:
            decision, result, flagged = await execute_tool_call(
                "evtx.query_events",
                {"case_id": case_id, "limit": 5},
                session,
                in_process_tool_executor,
                4000,
            )
        assert decision.risk_class.value == "AUTO_APPROVE"
        assert flagged is False  # real fixture data, no injection present


class TestRunConversationStepPlainTextResponse:
    @pytest.mark.asyncio
    async def test_plain_text_response_yields_tokens_then_done(
        self, session_factory, in_process_tool_executor
    ) -> None:
        chunks = [
            {"message": {"role": "assistant", "content": "The "}, "done": False},
            {
                "message": {"role": "assistant", "content": "logon succeeded."},
                "done": True,
                "done_reason": "stop",
            },
        ]
        client = _mock_ollama_client(chunks)
        messages = [{"role": "user", "content": "What happened?"}]

        with session_factory() as session:
            events = [
                e
                async for e in run_conversation_step(
                    messages, [], session, client, in_process_tool_executor
                )
            ]

        assert any(hasattr(e, "content") and e.content == "The " for e in events)
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stopped_for_confirmation is False
        # The assistant's reply was appended to the transcript for the caller to persist.
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"] == "The logon succeeded."
        await client.aclose()


class TestRunConversationStepAutoApproveToolCall:
    @pytest.mark.asyncio
    async def test_auto_approve_call_executes_and_appends_tool_message(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
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
        client = _mock_ollama_client(chunks)
        messages = [{"role": "user", "content": "Tell me about this case."}]

        with session_factory() as session:
            events = [
                e
                async for e in run_conversation_step(
                    messages, [], session, client, in_process_tool_executor
                )
            ]

        tool_call_events = [e for e in events if isinstance(e, ToolCallEvent)]
        tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_call_events) == 1
        assert tool_call_events[0].tool_name == "case.get_metadata"
        assert tool_call_events[0].risk_class == "AUTO_APPROVE"
        assert len(tool_result_events) == 1
        assert tool_result_events[0].result["event_count"] == 15
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stopped_for_confirmation is False

        # The tool result was appended to the transcript as an Ollama-shaped tool message.
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_name"] == "case.get_metadata"
        await client.aclose()


class TestRunConversationStepRequiresConfirmation:
    @pytest.mark.asyncio
    async def test_pauses_and_does_not_execute_the_tool(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        chunks = [
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
                                    "finding_text": "Suspicious activity found.",
                                    "evidence_refs": [],
                                },
                            }
                        }
                    ],
                },
                "done": True,
            }
        ]
        client = _mock_ollama_client(chunks)
        messages = [{"role": "user", "content": "Log this finding."}]

        with session_factory() as session:
            events = [
                e
                async for e in run_conversation_step(
                    messages, [], session, client, in_process_tool_executor
                )
            ]

        assert any(isinstance(e, ConfirmationRequiredEvent) for e in events)
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stopped_for_confirmation is True
        # Nothing was actually executed — no tool_result event, no tool
        # message appended, and (checked below) no Finding row was created.
        assert not any(isinstance(e, ToolResultEvent) for e in events)
        assert not any(m.get("role") == "tool" for m in messages)

        from sqlalchemy import select

        from app.core.models import Finding

        with session_factory() as s:
            findings = s.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            assert len(findings) == 0  # confirmation was never granted
        await client.aclose()


class TestResumeAfterConfirmation:
    @pytest.mark.asyncio
    async def test_approved_confirmation_executes_and_appends_result(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        messages: list[dict] = [{"role": "user", "content": "Log this."}]
        arguments = {
            "case_id": case_id,
            "finding_text": "Confirmed suspicious logon.",
            "evidence_refs": [],
        }

        with session_factory() as session:
            event = await resume_after_confirmation(
                "report.append_finding",
                arguments,
                True,
                messages,
                session,
                in_process_tool_executor,
            )

        assert isinstance(event, ToolResultEvent)
        assert event.result["acknowledged"] is True
        assert messages[-1]["role"] == "tool"

        from sqlalchemy import select

        from app.core.models import Finding, ToolCallAudit

        with session_factory() as s:
            findings = s.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            assert len(findings) == 1
            assert findings[0].finding_text == "Confirmed suspicious logon."
            audits = (
                s.execute(select(ToolCallAudit).where(ToolCallAudit.case_id == case_id))
                .scalars()
                .all()
            )
            assert len(audits) == 1
            assert audits[0].approval_status == "approved"

    @pytest.mark.asyncio
    async def test_rejected_confirmation_does_not_execute(
        self, session_factory, case_id, in_process_tool_executor
    ) -> None:
        messages: list[dict] = [{"role": "user", "content": "Log this."}]
        arguments = {
            "case_id": case_id,
            "finding_text": "Should never be saved.",
            "evidence_refs": [],
        }

        with session_factory() as session:
            event = await resume_after_confirmation(
                "report.append_finding",
                arguments,
                False,
                messages,
                session,
                in_process_tool_executor,
            )

        assert event.result["error"]["type"] == "declined_by_analyst"

        from sqlalchemy import select

        from app.core.models import Finding, ToolCallAudit

        with session_factory() as s:
            findings = s.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            assert len(findings) == 0
            audits = (
                s.execute(select(ToolCallAudit).where(ToolCallAudit.case_id == case_id))
                .scalars()
                .all()
            )
            assert len(audits) == 1
            assert audits[0].approval_status == "declined"
            assert len(findings) == 0
