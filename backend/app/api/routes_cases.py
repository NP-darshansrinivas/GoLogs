"""Case management REST routes (PRD §14).

| Method | Path | Purpose |
|---|---|---|
| POST | /api/cases | Create a case, upload files |
| GET | /api/cases/{id} | Get case metadata + parse status |
| GET | /api/cases/{id}/events | Query normalized events (filters as query params) |
| GET | /api/cases/{id}/audit | Full tool-call audit trail |
| POST | /api/cases/{id}/report | Generate Markdown/PDF report |
| POST | /api/cases/{id}/reparse | Resume/retry parsing (PRD §22 recovery requirement) |

Ingestion (EVTX parsing -> normalized Event rows) runs as a FastAPI
background task so the upload request returns immediately; `Case.status`
and `Case.parse_checkpoint` track progress, satisfying §22's "idempotent
and resumable" parsing requirement — `reparse` just re-invokes the same
ingestion function, which skips files already reflected in the checkpoint.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import fs_sandbox
from app.core.evtx_parser import EvtxParseError, parse_evtx_file
from app.core.models import Case, Event, Finding, ToolCallAudit
from app.core.normalizer import normalize_record
from app.core.report_builder import (
    ReportAuditEntry,
    ReportData,
    ReportFinding,
    build_markdown_report,
    build_pdf_report,
)
from app.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/cases", tags=["cases"])

_SUPPORTED_EVTX_SUFFIXES = (".evtx",)
_SUPPORTED_MEM_SUFFIXES = (".raw", ".mem", ".dmp")


def _state(request: Request) -> AppState:
    return cast(AppState, request.app.state.gologs)


def ingest_case_files(
    case_id: str,
    filenames: list[str],
    session_factory: sessionmaker[Session],
    case_storage_dir: Path,
) -> None:
    """Parse every EVTX file already saved for this case and insert
    normalized Event rows. Idempotent: files already reflected in
    `Case.parse_checkpoint` are skipped, so re-running (via `/reparse`)
    after an interruption picks up cleanly rather than duplicating rows.
    """
    with session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            log.error("ingest_case_not_found", case_id=case_id)
            return

        already_done = (
            set((case.parse_checkpoint or "").split(",")) if case.parse_checkpoint else set()
        )
        case.status = "parsing"
        session.commit()

        for filename in filenames:
            if filename in already_done or not filename.endswith(_SUPPORTED_EVTX_SUFFIXES):
                continue
            try:
                path = fs_sandbox.resolve_case_path(case_storage_dir, case_id, filename)
                for raw_record in parse_evtx_file(path):
                    normalized = normalize_record(raw_record.xml)
                    session.add(
                        Event(
                            case_id=case_id,
                            channel=normalized.channel,
                            event_id=normalized.event_id,
                            time_created=normalized.time_created,
                            computer=normalized.computer,
                            user_sid=normalized.user_sid,
                            raw_xml=normalized.raw_xml,
                            normalized_fields=normalized.normalized_fields,
                        )
                    )
                already_done.add(filename)
                session.commit()
            except EvtxParseError as exc:
                log.error(
                    "ingest_file_parse_failed", case_id=case_id, filename=filename, error=str(exc)
                )
                # Don't mark this file as done — a future /reparse call will retry it.

        case.parse_checkpoint = ",".join(sorted(already_done))
        case.status = "parsed"
        session.commit()


@router.post("")
async def create_case(
    request: Request,
    background_tasks: BackgroundTasks,
    name: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> dict[str, Any]:
    state = _state(request)

    with state.session_factory() as session:
        case = Case(name=name, status="created")
        session.add(case)
        session.commit()
        case_id = case.id

    fs_sandbox.ensure_case_dir(state.case_storage_dir, case_id)
    saved_filenames: list[str] = []
    for upload in files:
        if not upload.filename:
            continue
        dest = fs_sandbox.resolve_case_path(state.case_storage_dir, case_id, upload.filename)
        content = await upload.read()
        dest.write_bytes(content)
        saved_filenames.append(dest.name)

    background_tasks.add_task(
        ingest_case_files, case_id, saved_filenames, state.session_factory, state.case_storage_dir
    )

    return {"case_id": case_id, "name": name, "status": "created", "files": saved_filenames}


@router.get("/{case_id}")
async def get_case(case_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    with state.session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")
        event_count = session.query(Event).filter(Event.case_id == case_id).count()
        return {
            "case_id": case.id,
            "name": case.name,
            "status": case.status,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "event_count": event_count,
        }


@router.get("/{case_id}/events")
async def get_events(
    case_id: str,
    request: Request,
    channel: str | None = None,
    event_id: int | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    state = _state(request)
    limit = min(limit, 200)

    with state.session_factory() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")

        stmt = select(Event).where(Event.case_id == case_id)
        if channel:
            stmt = stmt.where(Event.channel == channel)
        if event_id is not None:
            stmt = stmt.where(Event.event_id == event_id)
        if keyword:
            stmt = stmt.where(Event.raw_xml.ilike(f"%{keyword}%"))
        stmt = stmt.order_by(Event.time_created).limit(limit)

        rows = session.execute(stmt).scalars().all()
        return {
            "events": [
                {
                    "uid": e.uid,
                    "channel": e.channel,
                    "event_id": e.event_id,
                    "time_created": e.time_created.isoformat() if e.time_created else None,
                    "computer": e.computer,
                    "user_sid": e.user_sid,
                }
                for e in rows
            ],
            "count": len(rows),
        }


@router.get("/{case_id}/audit")
async def get_audit_trail(case_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    with state.session_factory() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")

        rows = (
            session.execute(
                select(ToolCallAudit)
                .where(ToolCallAudit.case_id == case_id)
                .order_by(ToolCallAudit.called_at)
            )
            .scalars()
            .all()
        )
        return {
            "entries": [
                {
                    "id": a.id,
                    "tool_name": a.tool_name,
                    "arguments": a.arguments,
                    "risk_class": a.risk_class,
                    "approval_status": a.approval_status,
                    "called_at": a.called_at.isoformat(),
                }
                for a in rows
            ]
        }


@router.get("/{case_id}/export")
@router.get("/{case_id}/report")
@router.post("/{case_id}/report")
async def generate_report(
    case_id: str, request: Request, format: str = "markdown"
) -> dict[str, Any]:
    if format not in ("markdown", "pdf"):
        raise HTTPException(status_code=400, detail="format must be 'markdown' or 'pdf'")

    state = _state(request)
    with state.session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")

        events = session.query(Event).filter(Event.case_id == case_id).all()
        channels = sorted({e.channel for e in events})
        timestamps = [e.time_created for e in events if e.time_created is not None]
        time_range = (
            (min(timestamps).isoformat(), max(timestamps).isoformat())
            if timestamps
            else (None, None)
        )

        findings = (
            session.execute(
                select(Finding).where(Finding.case_id == case_id).order_by(Finding.created_at)
            )
            .scalars()
            .all()
        )
        report_findings = [
            ReportFinding(f.finding_text, f.evidence_refs, f.created_at) for f in findings
        ]

        if not report_findings:
            from app.core.models import ChatMessage
            chat_rows = (
                session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.case_id == case_id, ChatMessage.role == "assistant")
                    .order_by(ChatMessage.created_at)
                )
                .scalars()
                .all()
            )
            for m in chat_rows:
                if m.content and len(m.content.strip()) > 10:
                    report_findings.append(
                        ReportFinding(
                            finding_text=m.content,
                            evidence_refs=[],
                            created_at=m.created_at,
                        )
                    )

        if not report_findings and events:
            ch_str = ", ".join(channels) if channels else "Unknown"
            tr_str = f"{time_range[0]} to {time_range[1]}" if time_range[0] else "N/A"
            report_findings.append(
                ReportFinding(
                    finding_text=f"**Log Evidence Overview**: Case '{case.name}' contains {len(events)} event record(s) analyzed across channel(s): {ch_str}. Observed time range: {tr_str}.",
                    evidence_refs=[],
                    created_at=datetime.now(),
                )
            )

        audits = (
            session.execute(
                select(ToolCallAudit)
                .where(ToolCallAudit.case_id == case_id)
                .order_by(ToolCallAudit.called_at)
            )
            .scalars()
            .all()
        )

        data = ReportData(
            case_name=case.name,
            case_id=case.id,
            generated_at=datetime.now(),
            event_count=len(events),
            channels=channels,
            time_range=time_range,
            findings=report_findings,
            audit_entries=[
                ReportAuditEntry(
                    a.tool_name, a.arguments, a.risk_class, a.approval_status, a.called_at
                )
                for a in audits
            ],
        )

        if format == "markdown":
            return {"format": "markdown", "content": build_markdown_report(data)}

        import base64

        pdf_bytes = build_pdf_report(data)
        return {"format": "pdf", "content_base64": base64.b64encode(pdf_bytes).decode("ascii")}


@router.post("/{case_id}/reparse")
async def reparse_case(
    case_id: str, request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    state = _state(request)
    with state.session_factory() as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")

    case_dir = state.case_storage_dir / case_id
    filenames = [p.name for p in case_dir.glob("*") if p.is_file()] if case_dir.exists() else []

    background_tasks.add_task(
        ingest_case_files, case_id, filenames, state.session_factory, state.case_storage_dir
    )
    return {"case_id": case_id, "status": "reparsing"}
