"""Table-driven unit tests for `orchestrator.permission_gate` (PRD §24:
"permission_gate classification logic (table-driven)")."""

from __future__ import annotations

import pytest

from app.core.models import Case, MemPluginResult
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.orchestrator.permission_gate import RiskClass, classify_tool_call


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def case_id(session_factory) -> str:
    with session_scope(session_factory) as s:
        case = Case(name="Gate Test Case")
        s.add(case)
        s.flush()
        return case.id


# (tool_name, arguments_factory, expected_risk_class) — table-driven per §24.
# arguments_factory is a callable(case_id) -> dict so each row can reference
# the fixture-created case_id without eager evaluation at collection time.
STATIC_CLASSIFICATION_CASES = [
    pytest.param(
        "case.get_metadata",
        lambda cid: {"case_id": cid},
        RiskClass.AUTO_APPROVE,
        id="case-get-metadata",
    ),
    pytest.param(
        "evtx.query_events",
        lambda cid: {"case_id": cid},
        RiskClass.AUTO_APPROVE,
        id="evtx-query-events",
    ),
    pytest.param(
        "evtx.get_event_detail",
        lambda cid: {"case_id": cid, "event_uid": "x"},
        RiskClass.AUTO_APPROVE,
        id="evtx-get-event-detail",
    ),
    pytest.param(
        "mem.list_processes",
        lambda cid: {"case_id": cid},
        RiskClass.AUTO_APPROVE,
        id="mem-list-processes",
    ),
    pytest.param(
        "report.append_finding",
        lambda cid: {"case_id": cid, "finding_text": "x", "evidence_refs": []},
        RiskClass.REQUIRES_CONFIRMATION,
        id="report-append-finding-always-confirms",
    ),
    # The tools explicitly absent by design (PRD §11.2) — must always DENY,
    # regardless of how plausible-looking the arguments are.
    pytest.param(
        "run_command", lambda cid: {"cmd": "whoami"}, RiskClass.DENY, id="deny-run-command"
    ),
    pytest.param("shell.exec", lambda cid: {"command": "ls"}, RiskClass.DENY, id="deny-shell-exec"),
    pytest.param(
        "fs.write_file",
        lambda cid: {"path": "/etc/passwd", "content": "x"},
        RiskClass.DENY,
        id="deny-arbitrary-file-write",
    ),
    pytest.param(
        "net.http_request",
        lambda cid: {"url": "https://evil.example.com"},
        RiskClass.DENY,
        id="deny-network-access",
    ),
    pytest.param(
        "process.kill",
        lambda cid: {"pid": 4},
        RiskClass.DENY,
        id="deny-process-manipulation",
    ),
    pytest.param("", lambda cid: {}, RiskClass.DENY, id="deny-empty-tool-name"),
    pytest.param(
        "mem.run_plugin_v2",
        lambda cid: {"case_id": cid, "plugin": "pslist"},
        RiskClass.DENY,
        id="deny-lookalike-tool-name-not-exact-match",
    ),
]


class TestStaticClassification:
    @pytest.mark.parametrize("tool_name,args_factory,expected", STATIC_CLASSIFICATION_CASES)
    def test_classification(
        self, session_factory, case_id, tool_name, args_factory, expected
    ) -> None:
        with session_factory() as session:
            decision = classify_tool_call(tool_name, args_factory(case_id), session)
        assert decision.risk_class == expected
        assert decision.tool_name == tool_name
        assert decision.reason  # always human-readable, never empty


class TestDenyIsUnconditional:
    def test_deny_reason_never_reveals_a_negotiation_path(self, session_factory, case_id) -> None:
        with session_factory() as session:
            decision = classify_tool_call("run_command", {"cmd": "whoami"}, session)
        assert decision.denied is True
        assert decision.allowed_without_confirmation is False

    def test_deny_does_not_depend_on_arguments_content(self, session_factory, case_id) -> None:
        with session_factory() as session:
            benign = classify_tool_call("run_command", {"cmd": "echo hello"}, session)
            malicious = classify_tool_call("run_command", {"cmd": "rm -rf /"}, session)
        assert benign.risk_class == malicious.risk_class == RiskClass.DENY


class TestMemRunPluginDynamicClassification:
    def test_uncached_plugin_requires_confirmation(self, session_factory, case_id) -> None:
        with session_factory() as session:
            decision = classify_tool_call(
                "mem.run_plugin", {"case_id": case_id, "plugin": "malfind"}, session
            )
        assert decision.risk_class == RiskClass.REQUIRES_CONFIRMATION

    def test_cached_plugin_auto_approves(self, session_factory, case_id) -> None:
        with session_scope(session_factory) as s:
            s.add(MemPluginResult(case_id=case_id, plugin_name="pslist", output={"rows": []}))
        with session_factory() as session:
            decision = classify_tool_call(
                "mem.run_plugin", {"case_id": case_id, "plugin": "pslist"}, session
            )
        assert decision.risk_class == RiskClass.AUTO_APPROVE

    def test_cache_is_scoped_per_plugin_not_per_case(self, session_factory, case_id) -> None:
        # pslist cached shouldn't auto-approve netscan for the same case.
        with session_scope(session_factory) as s:
            s.add(MemPluginResult(case_id=case_id, plugin_name="pslist", output={"rows": []}))
        with session_factory() as session:
            decision = classify_tool_call(
                "mem.run_plugin", {"case_id": case_id, "plugin": "netscan"}, session
            )
        assert decision.risk_class == RiskClass.REQUIRES_CONFIRMATION

    def test_cache_is_scoped_per_case_not_global(self, session_factory, case_id) -> None:
        with session_scope(session_factory) as s:
            other_case = Case(name="Other Case")
            s.add(other_case)
            s.flush()
            other_case_id = other_case.id
            s.add(MemPluginResult(case_id=other_case_id, plugin_name="pslist", output={"rows": []}))

        with session_factory() as session:
            decision = classify_tool_call(
                "mem.run_plugin", {"case_id": case_id, "plugin": "pslist"}, session
            )
        assert decision.risk_class == RiskClass.REQUIRES_CONFIRMATION

    def test_missing_arguments_defaults_to_requires_confirmation_not_crash(
        self, session_factory, case_id
    ) -> None:
        with session_factory() as session:
            decision = classify_tool_call("mem.run_plugin", {}, session)
        assert decision.risk_class == RiskClass.REQUIRES_CONFIRMATION
