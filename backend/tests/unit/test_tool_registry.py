"""Unit tests for `mcp_server.tool_registry` dispatch functions.

These call the dispatch coroutines directly (`dispatch(args, ctx)`) with no
MCP protocol involved at all — protocol-level testing (raw MCP client over
stdio) lives in `tests/integration/test_mcp_server_protocol.py`. Uses real
SQLite (in-memory) and real EVTX-derived fixture data throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.evtx_parser import parse_evtx_file_with_stats
from app.core.models import Case, Event, Finding, MemPluginResult
from app.core.normalizer import normalize_record
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.mcp_server.tool_registry import (
    TOOL_DISPATCH,
    TOOL_RISK_CLASSES,
    ToolContext,
    dispatch_case_get_metadata,
    dispatch_evtx_get_event_detail,
    dispatch_evtx_query_events,
    dispatch_mem_list_processes,
    dispatch_mem_run_plugin,
    dispatch_report_append_finding,
    load_tool_schemas,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    sf = make_session_factory(engine)
    return ToolContext(session_factory=sf, case_storage_dir=tmp_path)


@pytest.fixture(scope="module")
def parsed_security_records():
    records, _ = parse_evtx_file_with_stats(SECURITY_EVTX)
    return records[:30]


@pytest.fixture
def case_with_events(ctx: ToolContext, parsed_security_records) -> str:
    """A case pre-populated with 30 real, normalized Security-channel events."""
    normalized = [normalize_record(r.xml) for r in parsed_security_records]

    with session_scope(ctx.session_factory) as s:
        case = Case(name="Triage Case", status="parsed")
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
    return case_id


class TestSchemaLoading:
    def test_all_six_prd_tools_have_schemas(self) -> None:
        schemas = load_tool_schemas()
        assert set(schemas) == {
            "case.get_metadata",
            "evtx.query_events",
            "evtx.get_event_detail",
            "mem.list_processes",
            "mem.run_plugin",
            "report.append_finding",
        }

    def test_every_schema_name_matches_its_dispatch_and_risk_class_entry(self) -> None:
        schemas = load_tool_schemas()
        assert set(schemas) == set(TOOL_DISPATCH) == set(TOOL_RISK_CLASSES)

    def test_mem_run_plugin_schema_enum_matches_allowlist(self) -> None:
        from app.core.mem_analyzer import ALLOWED_PLUGINS

        schemas = load_tool_schemas()
        enum_values = set(schemas["mem.run_plugin"]["inputSchema"]["properties"]["plugin"]["enum"])
        assert enum_values == set(ALLOWED_PLUGINS)

    def test_evtx_query_events_limit_capped_at_200_in_schema(self) -> None:
        schemas = load_tool_schemas()
        assert schemas["evtx.query_events"]["inputSchema"]["properties"]["limit"]["maximum"] == 200

    def test_report_append_finding_is_the_only_required_confirmation_besides_mem_run(
        self,
    ) -> None:
        confirm_required = {k for k, v in TOOL_RISK_CLASSES.items() if v == "REQUIRES_CONFIRMATION"}
        assert confirm_required == {"report.append_finding", "mem.run_plugin"}


class TestCaseGetMetadata:
    @pytest.mark.asyncio
    async def test_returns_metadata_for_real_case(self, ctx, case_with_events) -> None:
        result = await dispatch_case_get_metadata({"case_id": case_with_events}, ctx)
        assert result["event_count"] == 30
        assert result["status"] == "parsed"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_unknown_case_returns_structured_error_not_exception(self, ctx) -> None:
        result = await dispatch_case_get_metadata({"case_id": "does-not-exist"}, ctx)
        assert result["error"]["type"] == "case_not_found"


class TestEvtxQueryEvents:
    @pytest.mark.asyncio
    async def test_returns_events_up_to_default_limit(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events({"case_id": case_with_events}, ctx)
        assert result["count"] == 30  # fewer than default limit of 50
        assert len(result["events"]) == 30

    @pytest.mark.asyncio
    async def test_limit_is_capped_at_200_even_if_caller_asks_for_more(
        self, ctx, case_with_events
    ) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "limit": 10000}, ctx
        )
        assert result["limit_applied"] == 200

    @pytest.mark.asyncio
    async def test_filters_by_event_id(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "event_id": 4608}, ctx
        )
        assert all(e["event_id"] == 4608 for e in result["events"])
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_filters_by_channel(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "channel": "Security"}, ctx
        )
        assert all(e["channel"] == "Security" for e in result["events"])

    @pytest.mark.asyncio
    async def test_channel_filter_excludes_nonmatching(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "channel": "Application"}, ctx
        )
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_keyword_filter_matches_raw_xml_substring(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "keyword": "Security-Auditing"}, ctx
        )
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_accepts_string_limit_and_null_like_event_id(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "limit": "20", "event_id": "null"}, ctx
        )
        assert "error" not in result
        assert result["limit_applied"] == 20

    @pytest.mark.asyncio
    async def test_keyword_pipe_is_treated_as_or(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "keyword": "no-such-token|Security-Auditing"}, ctx
        )
        assert "error" not in result
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_invalid_limit_returns_structured_error(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_query_events(
            {"case_id": case_with_events, "limit": "abc"}, ctx
        )
        assert result["error"]["type"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_unknown_case_returns_structured_error(self, ctx) -> None:
        result = await dispatch_evtx_query_events({"case_id": "nope"}, ctx)
        assert result["error"]["type"] == "case_not_found"


class TestEvtxGetEventDetail:
    @pytest.mark.asyncio
    async def test_returns_full_raw_xml_for_known_event(self, ctx, case_with_events) -> None:
        listing = await dispatch_evtx_query_events({"case_id": case_with_events}, ctx)
        target_uid = listing["events"][0]["uid"]

        result = await dispatch_evtx_get_event_detail(
            {"case_id": case_with_events, "event_uid": target_uid}, ctx
        )
        assert result["uid"] == target_uid
        assert "<Event" in result["raw_xml"]

    @pytest.mark.asyncio
    async def test_unknown_event_uid_returns_structured_error(self, ctx, case_with_events) -> None:
        result = await dispatch_evtx_get_event_detail(
            {"case_id": case_with_events, "event_uid": "not-a-real-uid"}, ctx
        )
        assert result["error"]["type"] == "event_not_found"

    @pytest.mark.asyncio
    async def test_event_from_different_case_is_not_leaked(self, ctx, case_with_events) -> None:
        with session_scope(ctx.session_factory) as s:
            other_case = Case(name="Other Case")
            s.add(other_case)
            s.flush()
            other_case_id = other_case.id

        listing = await dispatch_evtx_query_events({"case_id": case_with_events}, ctx)
        target_uid = listing["events"][0]["uid"]

        result = await dispatch_evtx_get_event_detail(
            {"case_id": other_case_id, "event_uid": target_uid}, ctx
        )
        assert result["error"]["type"] == "event_not_found"


class TestMemListProcesses:
    @pytest.mark.asyncio
    async def test_returns_not_yet_run_error_when_nothing_cached(
        self, ctx, case_with_events
    ) -> None:
        result = await dispatch_mem_list_processes({"case_id": case_with_events}, ctx)
        assert result["error"]["type"] == "not_yet_run"

    @pytest.mark.asyncio
    async def test_returns_cached_pslist_output(self, ctx, case_with_events) -> None:
        with session_scope(ctx.session_factory) as s:
            s.add(
                MemPluginResult(
                    case_id=case_with_events,
                    plugin_name="pslist",
                    output={"rows": [{"PID": 4, "ImageFileName": "System"}]},
                )
            )
        result = await dispatch_mem_list_processes({"case_id": case_with_events}, ctx)
        assert "error" not in result
        assert result["plugins"][0]["plugin_name"] == "pslist"


class TestMemRunPlugin:
    @pytest.mark.asyncio
    async def test_unknown_plugin_rejected_before_touching_filesystem(
        self, ctx, case_with_events
    ) -> None:
        result = await dispatch_mem_run_plugin(
            {"case_id": case_with_events, "plugin": "run_shell_command"}, ctx
        )
        assert result["error"]["type"] == "unknown_plugin"

    @pytest.mark.asyncio
    async def test_missing_memory_image_returns_structured_error(
        self, ctx, case_with_events
    ) -> None:
        result = await dispatch_mem_run_plugin(
            {"case_id": case_with_events, "plugin": "pslist"}, ctx
        )
        assert result["error"]["type"] == "image_not_found"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_flag_without_rerunning(
        self, ctx, case_with_events
    ) -> None:
        with session_scope(ctx.session_factory) as s:
            s.add(
                MemPluginResult(
                    case_id=case_with_events,
                    plugin_name="netscan",
                    output={"rows": [{"LocalAddr": "10.0.0.5", "Owner": "svchost.exe"}]},
                )
            )
        result = await dispatch_mem_run_plugin(
            {"case_id": case_with_events, "plugin": "netscan"}, ctx
        )
        assert result["cached"] is True
        assert result["output"]["rows"][0]["Owner"] == "svchost.exe"


class TestReportAppendFinding:
    @pytest.mark.asyncio
    async def test_creates_a_finding_and_acknowledges(self, ctx, case_with_events) -> None:
        result = await dispatch_report_append_finding(
            {
                "case_id": case_with_events,
                "finding_text": "Anomalous SYSTEM logon at 2016-07-08T18:12:51Z.",
                "evidence_refs": ["event-uid-1"],
            },
            ctx,
        )
        assert result["acknowledged"] is True
        assert result["finding_id"]

    @pytest.mark.asyncio
    async def test_finding_persists_and_is_queryable(self, ctx, case_with_events) -> None:
        await dispatch_report_append_finding(
            {"case_id": case_with_events, "finding_text": "Test finding.", "evidence_refs": []},
            ctx,
        )
        from sqlalchemy import select

        with ctx.session_factory() as s:
            rows = (
                s.execute(select(Finding).where(Finding.case_id == case_with_events))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].finding_text == "Test finding."

    @pytest.mark.asyncio
    async def test_unknown_case_returns_structured_error(self, ctx) -> None:
        result = await dispatch_report_append_finding(
            {"case_id": "nope", "finding_text": "x", "evidence_refs": []}, ctx
        )
        assert result["error"]["type"] == "case_not_found"
