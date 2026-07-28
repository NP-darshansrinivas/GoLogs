"""Unit tests for `api.ws_chat` (intent-based tool stripping)."""

from __future__ import annotations

import pytest

from app.api.ws_chat import REPORT_KEYWORDS, is_report_request


class TestIsReportRequest:
    @pytest.mark.parametrize("keyword", REPORT_KEYWORDS)
    def test_detects_all_report_keywords(self, keyword: str) -> None:
        prompt = f"Please generate an {keyword.upper()} for this case."
        assert is_report_request(prompt) is True

    def test_returns_false_for_normal_investigation_prompts(self) -> None:
        assert is_report_request("Any suspicious logons in the Security log?") is False
        assert is_report_request("Show me process trees for PID 456") is False

    def test_case_insensitive_matching(self) -> None:
        assert is_report_request("Give me an EXECUTIVE SUMMARY") is True
        assert is_report_request("Find IoCs in the evidence") is True
