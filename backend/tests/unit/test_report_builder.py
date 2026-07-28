"""Unit tests for `core.report_builder` (PRD FR-9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.report_builder import (
    ReportAuditEntry,
    ReportData,
    ReportFinding,
    build_markdown_report,
    build_pdf_report,
)


@pytest.fixture
def sample_data() -> ReportData:
    now = datetime(2026, 7, 17, 8, 0, 0, tzinfo=UTC)
    return ReportData(
        case_name="Acme Breach Investigation",
        case_id="abc-123",
        generated_at=now,
        event_count=2261,
        channels=["Security", "System"],
        time_range=("2016-07-08T18:12:51Z", "2016-07-09T02:00:00Z"),
        findings=[
            ReportFinding(
                "Anomalous SYSTEM logon detected outside business hours.",
                ["event-uid-1", "event-uid-2"],
                now,
            )
        ],
        audit_entries=[
            ReportAuditEntry(
                "evtx.query_events", {"case_id": "abc-123"}, "AUTO_APPROVE", "approved", now
            ),
            ReportAuditEntry(
                "report.append_finding",
                {"case_id": "abc-123"},
                "REQUIRES_CONFIRMATION",
                "approved",
                now,
            ),
        ],
    )


@pytest.fixture
def empty_data() -> ReportData:
    now = datetime(2026, 7, 17, 8, 0, 0, tzinfo=UTC)
    return ReportData(
        case_name="Empty Case",
        case_id="empty-1",
        generated_at=now,
        event_count=0,
        channels=[],
        time_range=(None, None),
        findings=[],
        audit_entries=[],
    )


class TestBuildMarkdownReport:
    def test_includes_product_branding(self, sample_data: ReportData) -> None:
        md = build_markdown_report(sample_data)
        assert "GoLogs" in md
        assert "Interrogate Your Evidence" in md

    def test_includes_case_identity(self, sample_data: ReportData) -> None:
        md = build_markdown_report(sample_data)
        assert "Acme Breach Investigation" in md
        assert "abc-123" in md
        assert "2261" in md

    def test_includes_finding_text_and_evidence_refs(self, sample_data: ReportData) -> None:
        md = build_markdown_report(sample_data)
        assert "Anomalous SYSTEM logon" in md
        assert "event-uid-1" in md
        assert "event-uid-2" in md

    def test_includes_audit_trail_table(self, sample_data: ReportData) -> None:
        md = build_markdown_report(sample_data)
        assert "evtx.query_events" in md
        assert "report.append_finding" in md
        assert "AUTO_APPROVE" in md
        assert "REQUIRES_CONFIRMATION" in md

    def test_handles_no_findings_gracefully(self, empty_data: ReportData) -> None:
        md = build_markdown_report(empty_data)
        assert "No findings" in md

    def test_handles_no_audit_entries_gracefully(self, empty_data: ReportData) -> None:
        md = build_markdown_report(empty_data)
        assert "No tool calls recorded" in md

    def test_handles_unknown_time_range_gracefully(self, empty_data: ReportData) -> None:
        md = build_markdown_report(empty_data)
        assert "unknown" in md

    def test_never_raises_on_empty_data(self, empty_data: ReportData) -> None:
        build_markdown_report(empty_data)  # must not raise


class TestBuildPdfReport:
    def test_produces_valid_pdf_bytes(self, sample_data: ReportData) -> None:
        pdf_bytes = build_pdf_report(sample_data)
        assert pdf_bytes.startswith(b"%PDF-")
        assert len(pdf_bytes) > 500

    def test_produces_valid_pdf_for_empty_case(self, empty_data: ReportData) -> None:
        pdf_bytes = build_pdf_report(empty_data)
        assert pdf_bytes.startswith(b"%PDF-")

    def test_pdf_is_parseable_by_a_reader(self, sample_data: ReportData, tmp_path) -> None:
        from pypdf import PdfReader

        pdf_bytes = build_pdf_report(sample_data)
        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(pdf_bytes)

        reader = PdfReader(str(pdf_path))
        assert len(reader.pages) >= 1
        text = reader.pages[0].extract_text()
        assert "Acme Breach Investigation" in text
        assert "GoLogs" in text

    def test_finding_text_appears_in_extracted_pdf_text(
        self, sample_data: ReportData, tmp_path
    ) -> None:
        from pypdf import PdfReader

        pdf_bytes = build_pdf_report(sample_data)
        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(pdf_bytes)

        reader = PdfReader(str(pdf_path))
        full_text = "\n".join(p.extract_text() for p in reader.pages)
        assert "Anomalous SYSTEM logon" in full_text
