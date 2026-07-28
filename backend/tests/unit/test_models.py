"""Unit tests for `core.models` — schema round-trip against real fixture data."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, ChatMessage, Event, Finding, MemPluginResult, ToolCallAudit
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


def _fresh_session_factory():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return make_session_factory(engine)


class TestCaseAndEventRoundTrip:
    def test_case_creation_generates_uuid_and_defaults(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Test Case")
            s.add(case)
            s.flush()
            assert case.id
            assert case.status == "created"
            assert case.created_at is not None

    def test_real_evtx_records_persist_and_query_back_correctly(self) -> None:
        sf = _fresh_session_factory()
        records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
        normalized = [normalize_record(r.xml) for r in records[:25]]

        with session_scope(sf) as s:
            case = Case(name="Security Triage")
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

        with session_scope(sf) as s:
            rows = s.execute(select(Event).where(Event.case_id == case_id)).scalars().all()
            assert len(rows) == 25
            assert all(r.channel == "Security" for r in rows)
            assert all(isinstance(r.normalized_fields, dict) for r in rows)

    def test_case_delete_cascades_to_events(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Cascade Test")
            s.add(case)
            s.flush()
            case_id = case.id
            s.add(
                Event(
                    case_id=case_id,
                    channel="Security",
                    event_id=4624,
                    time_created=None,
                    computer=None,
                    user_sid=None,
                    raw_xml="<Event/>",
                    normalized_fields={},
                )
            )

        with session_scope(sf) as s:
            case = s.get(Case, case_id)
            s.delete(case)

        with session_scope(sf) as s:
            rows = s.execute(select(Event).where(Event.case_id == case_id)).scalars().all()
            assert len(rows) == 0


class TestOtherTables:
    def test_mem_plugin_result_persists_json_output(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Mem Case")
            s.add(case)
            s.flush()
            s.add(
                MemPluginResult(
                    case_id=case.id,
                    plugin_name="pslist",
                    output={"processes": [{"pid": 4, "name": "System"}]},
                )
            )
            case_id = case.id

        with session_scope(sf) as s:
            rows = (
                s.execute(select(MemPluginResult).where(MemPluginResult.case_id == case_id))
                .scalars()
                .all()
            )
            assert rows[0].output["processes"][0]["name"] == "System"

    def test_finding_stores_evidence_refs_list(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Finding Case")
            s.add(case)
            s.flush()
            s.add(
                Finding(
                    case_id=case.id,
                    finding_text="Suspicious PowerShell encoded command detected.",
                    evidence_refs=["event-uid-1", "event-uid-2"],
                )
            )
            case_id = case.id

        with session_scope(sf) as s:
            rows = s.execute(select(Finding).where(Finding.case_id == case_id)).scalars().all()
            assert rows[0].evidence_refs == ["event-uid-1", "event-uid-2"]

    def test_tool_call_audit_persists_all_required_fields(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Audit Case")
            s.add(case)
            s.flush()
            s.add(
                ToolCallAudit(
                    case_id=case.id,
                    tool_name="evtx.query_events",
                    arguments={"channel": "Security", "limit": 50},
                    result_hash="deadbeef",
                    risk_class="AUTO_APPROVE",
                    approval_status="approved",
                )
            )
            case_id = case.id

        with session_scope(sf) as s:
            rows = (
                s.execute(select(ToolCallAudit).where(ToolCallAudit.case_id == case_id))
                .scalars()
                .all()
            )
            assert rows[0].tool_name == "evtx.query_events"
            assert rows[0].risk_class == "AUTO_APPROVE"

    def test_chat_message_round_trip(self) -> None:
        sf = _fresh_session_factory()
        with session_scope(sf) as s:
            case = Case(name="Chat Case")
            s.add(case)
            s.flush()
            s.add(ChatMessage(case_id=case.id, role="user", content="Any suspicious logons?"))
            case_id = case.id

        with session_scope(sf) as s:
            rows = (
                s.execute(select(ChatMessage).where(ChatMessage.case_id == case_id)).scalars().all()
            )
            assert rows[0].role == "user"
