"""Unit tests for `orchestrator.prompt_builder` (PRD §11.3 requirements)."""

from __future__ import annotations

from app.orchestrator.prompt_builder import build_case_summary_message, build_system_prompt


class TestBuildSystemPrompt:
    def test_establishes_role(self) -> None:
        prompt = build_system_prompt()
        assert "GoLogs" in prompt
        assert "forensic" in prompt.lower()

    def test_states_evidence_is_data_not_instructions(self) -> None:
        prompt = build_system_prompt()
        assert "data" in prompt.lower()
        assert "not commands" in prompt.lower() or "not instructions" in prompt.lower()

    def test_states_tool_restriction_rule(self) -> None:
        prompt = build_system_prompt()
        assert "only call the tools" in prompt.lower()
        assert "shell" in prompt.lower()

    def test_states_citation_requirement(self) -> None:
        prompt = build_system_prompt()
        assert "cite" in prompt.lower() or "citation" in prompt.lower()

    def test_states_no_tool_call_for_summary_or_report(self) -> None:
        prompt = build_system_prompt()
        assert "incident response report" in prompt.lower()
        assert "do not invoke any tools" in prompt.lower()

    def test_lists_all_six_tools_by_name(self) -> None:
        prompt = build_system_prompt()
        for tool_name in [
            "case.get_metadata",
            "evtx.query_events",
            "evtx.get_event_detail",
            "mem.list_processes",
            "mem.run_plugin",
            "report.append_finding",
        ]:
            assert tool_name in prompt

    def test_contains_a_refusal_few_shot_example(self) -> None:
        prompt = build_system_prompt()
        assert "example" in prompt.lower()
        # The example must model *refusing* to comply with embedded text,
        # not just mention the concept in the abstract.
        assert "not treating that as an instruction" in prompt.lower()

    def test_does_not_contain_case_specific_data(self) -> None:
        # The fixed system prompt must never itself carry evidence data —
        # that's injected once per session via build_case_summary_message.
        # "case_id" legitimately appears as a tool *parameter name* in the
        # tool summary (e.g. "case.get_metadata(case_id): ..."); what must
        # never appear is an actual case-specific value.
        prompt = build_system_prompt()
        assert "event_count" not in prompt
        assert "Case '" not in prompt  # the case-summary message's own phrasing


class TestBuildCaseSummaryMessage:
    def test_includes_case_name_and_event_count(self) -> None:
        msg = build_case_summary_message(
            "123e4567-e89b-12d3-a456-426614174000",
            "Acme Breach",
            1200,
            ("2024-01-01", "2024-01-05"),
            ["WKSTN01"],
        )
        assert msg["role"] == "system"
        assert "Acme Breach" in msg["content"]
        assert "1200" in msg["content"]
        assert "123e4567-e89b-12d3-a456-426614174000" in msg["content"]

    def test_includes_time_range_and_hosts(self) -> None:
        msg = build_case_summary_message(
            "cid-1", "Case A", 5, ("2024-06-01", "2024-06-02"), ["HOST-A", "HOST-B"]
        )
        assert "2024-06-01" in msg["content"]
        assert "HOST-A" in msg["content"]
        assert "HOST-B" in msg["content"]

    def test_handles_missing_time_range_and_hosts_gracefully(self) -> None:
        msg = build_case_summary_message("cid-2", "Empty Case", 0, (None, None), [])
        assert "unknown" in msg["content"]
