"""Generates investigation reports (PRD FR-9): Markdown and PDF, summarizing
findings, evidence highlights, and the full tool-call audit trail.

Both formats are built directly from the same structured inputs (not by
converting Markdown text to PDF via an HTML round-trip) — this keeps
formatting logic in one place and avoids a fragile Markdown-to-PDF
converter for content this module fully controls the shape of.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import get_settings


@dataclass(frozen=True, slots=True)
class ReportFinding:
    finding_text: str
    evidence_refs: list[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReportAuditEntry:
    tool_name: str
    arguments: dict[str, Any]
    risk_class: str
    approval_status: str
    called_at: datetime


@dataclass(frozen=True, slots=True)
class ReportData:
    """Everything a report needs, decoupled from the ORM so this module has
    no direct SQLAlchemy dependency — callers (the API layer) query the DB
    and hand over plain data."""

    case_name: str
    case_id: str
    generated_at: datetime
    event_count: int
    channels: list[str]
    time_range: tuple[str | None, str | None]
    findings: list[ReportFinding]
    audit_entries: list[ReportAuditEntry]


def _product_header() -> str:
    settings = get_settings()
    return f"{settings.product_name} — {settings.product_tagline}"


def build_markdown_report(data: ReportData) -> str:
    """Build the Markdown investigation report."""
    lines: list[str] = []
    lines.append(f"# {_product_header()}")
    lines.append(f"## Investigation Report: {data.case_name}")
    lines.append("")
    lines.append(f"**Case ID:** `{data.case_id}`  ")
    lines.append(f"**Generated:** {data.generated_at.isoformat()}  ")
    lines.append(f"**Events analyzed:** {data.event_count}  ")
    start, end = data.time_range
    time_desc = f"{start} to {end}" if start and end else "unknown"
    lines.append(f"**Time range:** {time_desc}  ")
    lines.append(f"**Channels present:** {', '.join(data.channels) if data.channels else 'none'}")
    lines.append("")

    lines.append("## Findings")
    if not data.findings:
        lines.append("_No findings have been recorded for this case yet._")
    else:
        for i, finding in enumerate(data.findings, start=1):
            lines.append(f"### Finding {i}")
            lines.append(finding.finding_text)
            if finding.evidence_refs:
                refs = ", ".join(f"`{r}`" for r in finding.evidence_refs)
                lines.append(f"**Evidence:** {refs}")
            lines.append(f"_Recorded: {finding.created_at.isoformat()}_")
            lines.append("")

    lines.append("## Tool-Call Audit Trail")
    lines.append(
        "_Every tool call made during this investigation, per the immutable audit log "
        "(PRD §14 `/audit`, §20 Repudiation mitigation)._"
    )
    lines.append("")
    if not data.audit_entries:
        lines.append("_No tool calls recorded._")
    else:
        lines.append("| Time | Tool | Risk Class | Status |")
        lines.append("|---|---|---|---|")
        for entry in data.audit_entries:
            lines.append(
                f"| {entry.called_at.isoformat()} | `{entry.tool_name}` | "
                f"{entry.risk_class} | {entry.approval_status} |"
            )
    lines.append("")

    return "\n".join(lines)


_STYLES = getSampleStyleSheet()
_TITLE_STYLE = ParagraphStyle(
    "GoLogsTitle", parent=_STYLES["Title"], textColor=colors.HexColor("#1a1a2e")
)
_HEADING_STYLE = ParagraphStyle("GoLogsHeading", parent=_STYLES["Heading2"])
_BODY_STYLE = _STYLES["BodyText"]
_ITALIC_STYLE = ParagraphStyle(
    "GoLogsItalic", parent=_STYLES["BodyText"], fontName="Helvetica-Oblique"
)


def build_pdf_report(data: ReportData) -> bytes:
    """Build the PDF investigation report. Returns raw PDF bytes."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    story: list[Any] = []

    story.append(Paragraph(_product_header(), _TITLE_STYLE))
    story.append(Paragraph(f"Investigation Report: {data.case_name}", _HEADING_STYLE))
    story.append(Spacer(1, 0.15 * inch))

    start, end = data.time_range
    time_desc = f"{start} to {end}" if start and end else "unknown"
    meta_lines = [
        f"<b>Case ID:</b> {data.case_id}",
        f"<b>Generated:</b> {data.generated_at.isoformat()}",
        f"<b>Events analyzed:</b> {data.event_count}",
        f"<b>Time range:</b> {time_desc}",
        f"<b>Channels present:</b> {', '.join(data.channels) if data.channels else 'none'}",
    ]
    for line in meta_lines:
        story.append(Paragraph(line, _BODY_STYLE))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Findings", _HEADING_STYLE))
    if not data.findings:
        story.append(Paragraph("No findings have been recorded for this case yet.", _ITALIC_STYLE))
    else:
        for i, finding in enumerate(data.findings, start=1):
            story.append(Paragraph(f"Finding {i}", ParagraphStyle("f", parent=_STYLES["Heading3"])))
            story.append(Paragraph(finding.finding_text, _BODY_STYLE))
            if finding.evidence_refs:
                story.append(
                    Paragraph(f"Evidence: {', '.join(finding.evidence_refs)}", _ITALIC_STYLE)
                )
            story.append(Spacer(1, 0.1 * inch))
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Tool-Call Audit Trail", _HEADING_STYLE))
    if not data.audit_entries:
        story.append(Paragraph("No tool calls recorded.", _ITALIC_STYLE))
    else:
        table_data = [["Time", "Tool", "Risk Class", "Status"]]
        for entry in data.audit_entries:
            table_data.append(
                [
                    entry.called_at.isoformat(),
                    entry.tool_name,
                    entry.risk_class,
                    entry.approval_status,
                ]
            )
        table = Table(table_data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(table)

    doc.build(story)
    return buffer.getvalue()
