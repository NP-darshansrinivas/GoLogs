"""The Ollama tool-calling conversation loop (PRD M3 deliverable).

Wires together, in the correct order per PRD §11.4's layered defense:

    model requests a tool call
        -> permission_gate.classify_tool_call()   (DENY / CONFIRM / AUTO)
        -> [if AUTO_APPROVE] tool_executor.execute()   (real MCP call)
        -> injection_scanner.scan_tool_result()        (re-scan before re-entry)
        -> truncate to MAX_TOOL_RESULT_TOKENS
        -> append as a tool-result message, continue

This module never talks to `mcp_server` directly — it depends on an
injected `ToolExecutor` callable so it stays decoupled from *how* a tool
call actually happens (a real MCP `ClientSession` over stdio in production;
a fast in-process call straight into `tool_registry.TOOL_DISPATCH` in unit
tests). This mirrors the same "protocol-independent core logic" pattern
used in `mcp_server/server.py`'s `list_tools_impl`/`call_tool_impl` split.

Emits a stream of typed `ConversationEvent`s that map directly onto the
WebSocket frame types in PRD §14 (`token`, `tool_call`, `tool_result`,
`confirmation_required`, `done`) — the API layer (M4) forwards these
essentially verbatim over the WebSocket.

**Resumption across a confirmation boundary:** a generator can't be
"paused" across a WebSocket round-trip to the analyst and then resumed —
by the time the analyst responds, this Python call stack is long gone. The
practical pattern (and what `run_conversation_step` /
`resume_after_confirmation` below implement) is: the API layer persists
`messages` (the transcript) between calls; `run_conversation_step`
processes one model turn and any AUTO_APPROVE/DENY tool calls in it,
stopping at the first REQUIRES_CONFIRMATION call; `resume_after_confirmation`
is a separate entrypoint the API layer calls once the analyst responds,
which executes (or skips) that one specific call and appends its result,
after which the API layer calls `run_conversation_step` again to let the
model continue.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.models import ToolCallAudit
from app.orchestrator.injection_scanner import scan_tool_result
from app.orchestrator.ollama_client import (
    OllamaChatResult,
    OllamaClient,
    build_tool_result_message,
    collect_chat_response,
)
from app.orchestrator.permission_gate import PermissionDecision, RiskClass, classify_tool_call

log = structlog.get_logger(__name__)


class ToolExecutor(Protocol):
    """Abstracts over how an approved tool call is actually executed."""

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


# --- Typed events, mapping 1:1 onto PRD §14's WebSocket frame types ---


@dataclass(frozen=True, slots=True)
class TokenEvent:
    content: str


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    tool_name: str
    arguments: dict[str, Any]
    risk_class: str


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    tool_name: str
    result: dict[str, Any]
    injection_flagged: bool


@dataclass(frozen=True, slots=True)
class ConfirmationRequiredEvent:
    tool_name: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class DoneEvent:
    stopped_for_confirmation: bool = False


ConversationEvent = (
    TokenEvent | ToolCallEvent | ToolResultEvent | ConfirmationRequiredEvent | DoneEvent
)


# Rough token-count heuristic (~4 chars/token in English) — precise enough
# to bound context growth without pulling in a full tokenizer dependency
# just for a soft truncation limit. Always documented as approximate.
_CHARS_PER_TOKEN_ESTIMATE = 4


def truncate_tool_result_content(content: str, max_tokens: int) -> str:
    """Truncate a serialized tool-result string to roughly `max_tokens`
    (PRD §15 `MAX_TOOL_RESULT_TOKENS`), appending a visible truncation
    notice so the model knows the data was cut off rather than silently
    ending mid-structure."""
    max_chars = max_tokens * _CHARS_PER_TOKEN_ESTIMATE
    if len(content) <= max_chars:
        return content
    notice = (
        f"... [TRUNCATED: {len(content) - max_chars} more characters omitted, "
        f"result exceeded the {max_tokens}-token context budget]"
    )
    return content[:max_chars] + notice


def _hash_for_audit(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_audit_entry(
    session: Session,
    case_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any] | None,
    risk_class: RiskClass,
    approval_status: str,
) -> None:
    """Write one immutable audit row (PRD §13 `TOOL_CALL_AUDIT`, §20
    Repudiation mitigation: "100% of tool calls logged"). Called for
    *every* classification outcome — DENY and REQUIRES_CONFIRMATION
    included, not just executed calls, since a denied or paused request is
    itself a security-relevant event worth an audit trail entry.

    Silently skips (with a warning) if `case_id` is missing from the
    arguments — the FK is NOT NULL, and a call malformed enough to lack
    case_id will already have failed classification/dispatch validation
    elsewhere; this is a defensive last resort, not the primary error path.
    """
    if not case_id:
        log.warning("audit_write_skipped_missing_case_id", tool_name=tool_name)
        return
    session.add(
        ToolCallAudit(
            case_id=case_id,
            tool_name=tool_name,
            arguments=arguments,
            result_hash=_hash_for_audit(result or {}),
            risk_class=risk_class.value,
            approval_status=approval_status,
        )
    )
    session.commit()


async def execute_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    session: Session,
    tool_executor: ToolExecutor,
    max_tool_result_tokens: int,
) -> tuple[PermissionDecision, dict[str, Any] | None, bool]:
    """Classify one tool call and, if approved, execute + scan + truncate it.

    Returns `(decision, scanned_result_or_None, injection_flagged)`.
    `scanned_result` is `None` when the call was DENY'd or requires
    confirmation (nothing was executed yet).
    """
    decision = classify_tool_call(tool_name, arguments, session)
    case_id = arguments.get("case_id")

    if decision.risk_class is not RiskClass.AUTO_APPROVE:
        approval_status = (
            "denied" if decision.risk_class is RiskClass.DENY else "pending_confirmation"
        )
        _write_audit_entry(
            session, case_id, tool_name, arguments, None, decision.risk_class, approval_status
        )
        return decision, None, False

    raw_result = await tool_executor(tool_name, arguments)
    scan = scan_tool_result(
        tool_name, raw_result, strictness=get_settings().injection_scan_strictness
    )
    _write_audit_entry(
        session,
        case_id,
        tool_name,
        arguments,
        scan.neutralized_result,
        decision.risk_class,
        "approved",
    )
    return decision, scan.neutralized_result, scan.flagged


async def run_conversation_step(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    session: Session,
    ollama_client: OllamaClient,
    tool_executor: ToolExecutor,
    max_tool_result_tokens: int = 4000,
) -> AsyncIterator[ConversationEvent]:
    """Process one model turn: stream tokens, then classify/execute any
    requested tool calls in order, stopping at the first one that needs
    analyst confirmation.

    Mutates `messages` in place (appends the assistant's turn and any
    resulting tool messages) so the caller can persist/reuse it directly
    for the next call.
    """
    chunks: list[dict[str, Any]] = []
    async for chunk in ollama_client.chat(messages=messages, tools=tool_schemas):
        content_piece = chunk.get("message", {}).get("content")
        if content_piece:
            yield TokenEvent(content=content_piece)
        chunks.append(chunk)

    result: OllamaChatResult = collect_chat_response(chunks)

    assistant_message: dict[str, Any] = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        assistant_message["tool_calls"] = [
            {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in result.tool_calls
        ]
    messages.append(assistant_message)

    for tool_call in result.tool_calls:
        decision, scanned_result, flagged = await execute_tool_call(
            tool_call.name, tool_call.arguments, session, tool_executor, max_tool_result_tokens
        )
        yield ToolCallEvent(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            risk_class=decision.risk_class.value,
        )

        if decision.risk_class is RiskClass.DENY:
            denial = {"error": {"type": "denied", "message": decision.reason}}
            messages.append(
                build_tool_result_message(
                    tool_call.name, _serialize_for_context(denial, max_tool_result_tokens)
                )
            )
            yield ToolResultEvent(tool_name=tool_call.name, result=denial, injection_flagged=False)
            continue

        if decision.risk_class is RiskClass.REQUIRES_CONFIRMATION:
            yield ConfirmationRequiredEvent(
                tool_name=tool_call.name, arguments=tool_call.arguments, reason=decision.reason
            )
            yield DoneEvent(stopped_for_confirmation=True)
            return

        # AUTO_APPROVE: scanned_result is populated.
        assert scanned_result is not None
        messages.append(
            build_tool_result_message(
                tool_call.name, _serialize_for_context(scanned_result, max_tool_result_tokens)
            )
        )
        yield ToolResultEvent(
            tool_name=tool_call.name, result=scanned_result, injection_flagged=flagged
        )

    yield DoneEvent(stopped_for_confirmation=False)


async def resume_after_confirmation(
    tool_name: str,
    arguments: dict[str, Any],
    approved: bool,
    messages: list[dict[str, Any]],
    session: Session,
    tool_executor: ToolExecutor,
    max_tool_result_tokens: int = 4000,
) -> ToolResultEvent:
    """Called by the API layer once the analyst responds to a
    `ConfirmationRequiredEvent`. Executes (or records the rejection of) the
    one specific call that was paused on, appends the result to `messages`,
    and returns it — the caller then calls `run_conversation_step` again to
    let the model continue with this result in context.

    Does *not* re-run `classify_tool_call` — the whole point of pausing was
    to get an explicit human decision for this specific call; re-classifying
    here would be redundant and would incorrectly reintroduce the
    auto/deny logic where a human decision should be authoritative instead.
    Writes its own audit entry either way, since both an approval and a
    rejection are the analyst's explicit decision and belong in the trail.
    """
    case_id = arguments.get("case_id")

    if not approved:
        result = {
            "error": {"type": "declined_by_analyst", "message": "Analyst declined this tool call."}
        }
        messages.append(
            build_tool_result_message(
                tool_name, _serialize_for_context(result, max_tool_result_tokens)
            )
        )
        _write_audit_entry(
            session,
            case_id,
            tool_name,
            arguments,
            result,
            RiskClass.REQUIRES_CONFIRMATION,
            "declined",
        )
        return ToolResultEvent(tool_name=tool_name, result=result, injection_flagged=False)

    raw_result = await tool_executor(tool_name, arguments)
    scan = scan_tool_result(
        tool_name, raw_result, strictness=get_settings().injection_scan_strictness
    )
    messages.append(
        build_tool_result_message(
            tool_name, _serialize_for_context(scan.neutralized_result, max_tool_result_tokens)
        )
    )
    _write_audit_entry(
        session,
        case_id,
        tool_name,
        arguments,
        scan.neutralized_result,
        RiskClass.REQUIRES_CONFIRMATION,
        "approved",
    )
    return ToolResultEvent(
        tool_name=tool_name, result=scan.neutralized_result, injection_flagged=scan.flagged
    )


def _serialize_for_context(result: dict[str, Any], max_tokens: int) -> str:
    return truncate_tool_result_content(json.dumps(result, default=str), max_tokens)
