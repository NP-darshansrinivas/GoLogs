"""Declares GoLogs' MCP tool schemas and dispatches calls to `core.*`/`db`.

Per PRD §10.2 module responsibility table: this module declares tool JSON
Schemas and dispatches to `core.*` — it never decides *whether* a call is
allowed to happen. That decision belongs to `orchestrator.permission_gate`,
which runs in the FastAPI backend process *before* it ever asks the MCP
client to invoke a tool here. By the time a call reaches this module, it has
already been approved; this module's job is only correct execution and
structured error handling (PRD §22), never authorization.

Every dispatch function:
  - takes the tool's already-validated arguments dict plus a `ToolContext`
    (uniform across all six tools, even though only `mem.run_plugin` needs
    filesystem access — this keeps `TOOL_DISPATCH` a plain dict of
    identically-callable coroutines rather than special-casing one tool),
  - never raises for expected failure modes (bad case_id, missing file,
    plugin failure) — it always returns a JSON-serializable dict, wrapping
    failures as `{"error": {"type": ..., "message": ...}}`,
  - is a plain async function with no MCP-protocol-specific code, so it can
    be unit tested directly without a running stdio server.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core import fs_sandbox, mem_analyzer
from app.core.models import Case, Event, Finding, MemPluginResult

log = structlog.get_logger(__name__)

_SCHEMAS_DIR = Path(__file__).parent / "schemas"

TOOL_RISK_CLASSES: dict[str, str] = {
    "case.get_metadata": "AUTO_APPROVE",
    "evtx.query_events": "AUTO_APPROVE",
    "evtx.get_event_detail": "AUTO_APPROVE",
    "mem.list_processes": "AUTO_APPROVE",
    "mem.run_plugin": "REQUIRES_CONFIRMATION",
    "report.append_finding": "REQUIRES_CONFIRMATION",
}


def load_tool_schemas() -> dict[str, dict[str, Any]]:
    """Load every tool's JSON Schema definition from `mcp_server/schemas/`."""
    schemas: dict[str, dict[str, Any]] = {}
    for schema_file in sorted(_SCHEMAS_DIR.glob("*.json")):
        data = json.loads(schema_file.read_text())
        schemas[data["name"]] = data
    return schemas


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"error": {"type": error_type, "message": message}}


def _hash_result(payload: dict[str, Any]) -> str:
    """Stable hash of a tool result for TOOL_CALL_AUDIT.result_hash (PRD §13)."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_case(session: Session, case_id: str | None) -> Case | None:
    if case_id and isinstance(case_id, str) and case_id.strip():
        cid = case_id.strip()
        if cid.lower() not in ("default-case", "null", "none"):
            # 1. Try exact primary key UUID lookup
            c = session.get(Case, cid)
            if c is not None:
                return c
            # 2. Try lookup by case name (e.g. "test 4")
            c = session.execute(select(Case).where(Case.name == cid)).scalars().first()
            if c is not None:
                return c
            return None

    # 3. Fallback: get the most recently created case if case_id was missing/null/default-case
    return session.execute(select(Case).order_by(Case.created_at.desc())).scalars().first()


@dataclass(slots=True)
class ToolContext:
    """Shared, uniform context passed to every dispatch function."""

    session_factory: sessionmaker[Session]
    case_storage_dir: Path
    mem_image_filename: str = "memory.raw"


async def dispatch_case_get_metadata(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")
        case_id = case.id

        n_events = session.query(Event).filter(Event.case_id == case_id).count()
        n_mem_results = (
            session.query(MemPluginResult).filter(MemPluginResult.case_id == case_id).count()
        )

        return {
            "case_id": case.id,
            "name": case.name,
            "status": case.status,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "event_count": n_events,
            "mem_plugin_results_cached": n_mem_results,
        }


async def dispatch_evtx_query_events(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_limit = args.get("limit", 50)
    try:
        limit = min(int(raw_limit), 200)
    except (TypeError, ValueError):
        return _error(
            "invalid_arguments",
            f"limit must be an integer between 1 and 200 (got {raw_limit!r})",
        )
    if limit < 1:
        return _error("invalid_arguments", f"limit must be >= 1 (got {limit!r})")

    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")

        case_id = case.id
        stmt = select(Event).where(Event.case_id == case_id)
        if channel := args.get("channel"):
            if isinstance(channel, str) and channel.strip().lower() not in ("", "null", "none"):
                stmt = stmt.where(Event.channel == channel)

        raw_event_id = args.get("event_id")
        if raw_event_id is not None:
            if isinstance(raw_event_id, int):
                stmt = stmt.where(Event.event_id == raw_event_id)
            elif isinstance(raw_event_id, list):
                valid_ids: list[int] = []
                for item in raw_event_id:
                    if isinstance(item, int):
                        valid_ids.append(item)
                    elif isinstance(item, str):
                        digits = re.findall(r"\d+", item)
                        valid_ids.extend([int(d) for d in digits])
                if valid_ids:
                    stmt = stmt.where(Event.event_id.in_(valid_ids))
            elif isinstance(raw_event_id, str) and raw_event_id.strip().lower() not in ("", "null", "none"):
                digits = re.findall(r"\d+", raw_event_id)
                if len(digits) == 1:
                    stmt = stmt.where(Event.event_id == int(digits[0]))
                elif len(digits) > 1:
                    stmt = stmt.where(Event.event_id.in_([int(d) for d in digits]))

        if time_range := args.get("time_range"):
            if isinstance(time_range, dict):
                if start := time_range.get("start"):
                    stmt = stmt.where(Event.time_created >= start)
                if end := time_range.get("end"):
                    stmt = stmt.where(Event.time_created <= end)

        if raw_keyword := args.get("keyword"):
            keyword = str(raw_keyword).strip()
            split_pattern = r"\s+or\s+|\s*\|\s*|\s*,\s*"
            terms = [t.strip() for t in re.split(split_pattern, keyword, flags=re.IGNORECASE) if t.strip()]
            if terms:
                conds = []
                for term in terms:
                    digits = re.findall(r"\d+", term)
                    if re.search(r"event\s*id", term, re.IGNORECASE) and digits:
                        target_id = int(digits[0])
                        conds.append(or_(Event.event_id == target_id, Event.raw_xml.ilike(f"%<EventID>{target_id}</EventID>%")))
                    else:
                        conds.append(Event.raw_xml.ilike(f"%{term}%"))
                if conds:
                    if len(conds) == 1:
                        stmt = stmt.where(conds[0])
                    else:
                        stmt = stmt.where(or_(*conds))

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
                    "normalized_fields": e.normalized_fields,
                }
                for e in rows
            ],
            "count": len(rows),
            "limit_applied": limit,
        }


async def dispatch_evtx_get_event_detail(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    event_uid = args.get("event_uid", "")

    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")

        event = session.get(Event, event_uid)
        if event is None or event.case_id != case.id:
            return _error("event_not_found", f"No event {event_uid!r} in case {case.id!r}")

        return {
            "uid": event.uid,
            "channel": event.channel,
            "event_id": event.event_id,
            "time_created": event.time_created.isoformat() if event.time_created else None,
            "computer": event.computer,
            "user_sid": event.user_sid,
            "raw_xml": event.raw_xml,
            "normalized_fields": event.normalized_fields,
        }


async def dispatch_mem_list_processes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")

        case_id = case.id
        results = (
            session.query(MemPluginResult)
            .filter(
                MemPluginResult.case_id == case_id,
                MemPluginResult.plugin_name.in_(["pslist", "pstree"]),
            )
            .all()
        )
        if not results:
            return _error(
                "not_yet_run",
                "No cached pslist/pstree results for this case. Call mem.run_plugin first.",
            )

        return {
            "plugins": [
                {"plugin_name": r.plugin_name, "run_at": r.run_at.isoformat(), "output": r.output}
                for r in results
            ]
        }


async def dispatch_mem_run_plugin(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    plugin = args.get("plugin", "")

    if plugin not in mem_analyzer.ALLOWED_PLUGINS:
        return _error("unknown_plugin", f"Plugin {plugin!r} is not allow-listed")

    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")

        case_id = case.id
        cached = (
            session.query(MemPluginResult)
            .filter(MemPluginResult.case_id == case_id, MemPluginResult.plugin_name == plugin)
            .one_or_none()
        )
        if cached is not None:
            return {
                "plugin": plugin,
                "cached": True,
                "run_at": cached.run_at.isoformat(),
                "output": cached.output,
            }

        try:
            image_path = fs_sandbox.resolve_case_path(
                ctx.case_storage_dir, case_id, ctx.mem_image_filename
            )
        except fs_sandbox.PathTraversalError as exc:
            return _error("invalid_path", str(exc))

        if not image_path.exists():
            return _error(
                "image_not_found",
                f"No memory image found for case {case_id!r} at expected path.",
            )

        result = mem_analyzer.run_plugin(image_path, plugin)
        if not result.success:
            return _error(
                "plugin_execution_failed",
                result.error or "Unknown Volatility3 failure",
            )

        session.add(
            MemPluginResult(case_id=case_id, plugin_name=plugin, output={"rows": result.rows})
        )
        session.commit()

        return {
            "plugin": plugin,
            "cached": False,
            "duration_seconds": round(result.duration_seconds, 2),
            "output": {"rows": result.rows},
        }


async def dispatch_report_append_finding(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    title = args.get("title")
    description = args.get("description")
    severity = args.get("severity")
    raw_finding_text = args.get("finding_text")

    text_parts: list[str] = []
    if title:
        text_parts.append(f"**{title}**")
    if severity:
        text_parts.append(f"[Severity: {severity}]")
    if description:
        text_parts.append(description)
    if raw_finding_text:
        text_parts.append(raw_finding_text)

    finding_text = "\n\n".join(text_parts) if text_parts else "Finding recorded."

    raw_refs = args.get("evidence_refs") or args.get("evidence") or []
    if isinstance(raw_refs, str):
        evidence_refs = [raw_refs]
    elif isinstance(raw_refs, list):
        evidence_refs = [str(ref) for ref in raw_refs]
    else:
        evidence_refs = []

    with ctx.session_factory() as session:
        case = _get_case(session, args.get("case_id"))
        if case is None:
            return _error("case_not_found", "No active case found.")

        case_id = case.id
        finding = Finding(case_id=case_id, finding_text=finding_text, evidence_refs=evidence_refs)
        session.add(finding)
        session.commit()

        return {"acknowledged": True, "finding_id": finding.id}


TOOL_DISPATCH = {
    "case.get_metadata": dispatch_case_get_metadata,
    "evtx.query_events": dispatch_evtx_query_events,
    "evtx.get_event_detail": dispatch_evtx_get_event_detail,
    "mem.list_processes": dispatch_mem_list_processes,
    "mem.run_plugin": dispatch_mem_run_plugin,
    "report.append_finding": dispatch_report_append_finding,
}
