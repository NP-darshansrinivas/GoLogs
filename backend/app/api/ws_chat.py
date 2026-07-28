"""WebSocket chat endpoint (PRD §14: `WS /ws/cases/{id}/chat`).

Server pushes frames of type `token`, `tool_call`, `tool_result`,
`confirmation_required`, `done` — a direct 1:1 serialization of
`tool_call_loop`'s typed events (see that module's docstring for the full
design rationale, especially around why confirmation is a two-call
protocol rather than a paused generator).

Inbound client frames:
  - `{"type": "user_message", "content": "..."}`
  - `{"type": "confirm", "approved": true|false}`

**Security note on the confirm frame:** it deliberately does *not* accept
`tool_name`/`arguments` from the client. The server tracks which specific
call is pending confirmation (set when a `ConfirmationRequiredEvent` was
last sent) and uses that server-side record exclusively. Accepting a
client-supplied tool_name/arguments here would let a confirm frame silently
substitute a *different* call than the one the analyst actually saw and
approved — since `resume_after_confirmation` deliberately doesn't
re-classify (that's the whole point of a human decision being
authoritative), trusting client-supplied call details would reopen exactly
the confused-deputy gap the permission gate exists to close.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Case, ChatMessage, Event
from app.mcp_server.tool_registry import _get_case, load_tool_schemas
from app.orchestrator.prompt_builder import build_case_summary_message, build_system_prompt
from app.orchestrator.tool_call_loop import (
    ConfirmationRequiredEvent,
    DoneEvent,
    ToolCallEvent,
    ToolResultEvent,
    resume_after_confirmation,
    run_conversation_step,
)
from app.state import AppState

log = structlog.get_logger(__name__)

router = APIRouter()

REPORT_KEYWORDS: list[str] = [
    "report",
    "summary",
    "executive summary",
    "system override",
    "remediation",
    "incident response",
    "iocs",
]


def is_report_request(prompt: str) -> bool:
    """Check if incoming user prompt contains any report/summary keywords (case-insensitive)."""
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in REPORT_KEYWORDS)


_TOOLS_AS_OLLAMA_FUNCTIONS: list[dict[str, Any]] | None = None


def _ollama_tool_schemas() -> list[dict[str, Any]]:
    """Convert our JSON-Schema tool definitions to Ollama's `tools` request
    shape (`{"type": "function", "function": {name, description, parameters}}`),
    cached at module import since the schema set is fixed."""
    global _TOOLS_AS_OLLAMA_FUNCTIONS
    if _TOOLS_AS_OLLAMA_FUNCTIONS is None:
        schemas = load_tool_schemas()
        _TOOLS_AS_OLLAMA_FUNCTIONS = [
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": schema["inputSchema"],
                },
            }
            for schema in schemas.values()
        ]
    return _TOOLS_AS_OLLAMA_FUNCTIONS


async def _event_to_frame(event: Any) -> dict[str, Any]:
    if isinstance(event, ToolCallEvent):
        return {
            "type": "tool_call",
            "tool_name": event.tool_name,
            "arguments": event.arguments,
            "risk_class": event.risk_class,
        }
    if isinstance(event, ToolResultEvent):
        return {
            "type": "tool_result",
            "tool_name": event.tool_name,
            "result": event.result,
            "injection_flagged": event.injection_flagged,
        }
    if isinstance(event, ConfirmationRequiredEvent):
        return {
            "type": "confirmation_required",
            "tool_name": event.tool_name,
            "arguments": event.arguments,
            "reason": event.reason,
        }
    if isinstance(event, DoneEvent):
        return {"type": "done", "stopped_for_confirmation": event.stopped_for_confirmation}
    # TokenEvent
    return {
        "type": "token",
        "content": event.content
        if isinstance(event.content, str)
        else json.dumps(event.content)
        if isinstance(event.content, (dict, list))
        else str(event.content or ""),
    }


def _build_case_summary(session: Session, case: Case) -> dict[str, str]:
    events = session.query(Event).filter(Event.case_id == case.id).all()
    timestamps = [e.time_created for e in events if e.time_created is not None]
    time_range = (
        (min(timestamps).isoformat(), max(timestamps).isoformat()) if timestamps else (None, None)
    )
    hosts = sorted({e.computer for e in events if e.computer})
    return build_case_summary_message(case.id, case.name, len(events), time_range, hosts)


@router.websocket("/ws/cases/{case_id}/chat")
async def chat_websocket(websocket: WebSocket, case_id: str) -> None:
    state: AppState = websocket.app.state.gologs

    with state.session_factory() as session:
        case = _get_case(session, case_id)
        if case is None:
            await websocket.close(code=4404, reason="No case found in database")
            return
        case_id = case.id

    await websocket.accept()

    with state.session_factory() as session:
        case = _get_case(session, case_id)
        assert case is not None
        case_id = case.id
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt()},
            _build_case_summary(session, case),
        ]
        history = (
            session.execute(
                select(ChatMessage)
                .where(ChatMessage.case_id == case_id)
                .order_by(ChatMessage.created_at)
            )
            .scalars()
            .all()
        )
        for m in history:
            if m.role in ("user", "assistant"):
                messages.append({"role": m.role, "content": m.content})

    # Tracks the one call this connection is currently waiting on analyst
    # confirmation for — see module docstring's security note.
    pending_confirmation: tuple[str, dict[str, Any]] | None = None

    try:
        while True:
            frame = await websocket.receive_json()
            frame_type = frame.get("type")

            if frame_type == "user_message":
                content = frame.get("content", "")
                messages.append({"role": "user", "content": content})
                with state.session_factory() as session:
                    session.add(ChatMessage(case_id=case_id, role="user", content=content))
                    session.commit()

                try:
                    with state.session_factory() as session:
                        async for event in run_conversation_step(
                            messages,
                            _ollama_tool_schemas(),
                            session,
                            state.ollama_client,
                            state.tool_executor,
                            max_tool_result_tokens=state.settings.max_tool_result_tokens,
                        ):
                            if isinstance(event, ConfirmationRequiredEvent):
                                pending_confirmation = (event.tool_name, event.arguments)
                            await websocket.send_json(await _event_to_frame(event))
                except Exception as exc:
                    log.error("chat_step_failed", error=str(exc))
                    await websocket.send_json({"type": "error", "message": str(exc)})

                if (
                    messages
                    and messages[-1].get("role") == "assistant"
                    and messages[-1].get("content")
                ):
                    with state.session_factory() as session:
                        session.add(
                            ChatMessage(
                                case_id=case_id, role="assistant", content=messages[-1]["content"]
                            )
                        )
                        session.commit()

            elif frame_type == "confirm":
                if pending_confirmation is None:
                    await websocket.send_json(
                        {"type": "error", "message": "No confirmation is currently pending."}
                    )
                    continue

                tool_name, arguments = pending_confirmation
                approved = bool(frame.get("approved", False))
                pending_confirmation = None

                with state.session_factory() as session:
                    result_event = await resume_after_confirmation(
                        tool_name,
                        arguments,
                        approved,
                        messages,
                        session,
                        state.tool_executor,
                        max_tool_result_tokens=state.settings.max_tool_result_tokens,
                    )
                await websocket.send_json(await _event_to_frame(result_event))

                with state.session_factory() as session:
                    async for event in run_conversation_step(
                        messages,
                        _ollama_tool_schemas(),
                        session,
                        state.ollama_client,
                        state.tool_executor,
                        max_tool_result_tokens=state.settings.max_tool_result_tokens,
                    ):
                        if isinstance(event, ConfirmationRequiredEvent):
                            pending_confirmation = (event.tool_name, event.arguments)
                        await websocket.send_json(await _event_to_frame(event))

            else:
                await websocket.send_json(
                    {"type": "error", "message": f"Unknown frame type: {frame_type!r}"}
                )

    except WebSocketDisconnect:
        log.info("chat_websocket_disconnected", case_id=case_id)
