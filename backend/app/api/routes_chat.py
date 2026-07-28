"""Chat-adjacent REST routes. The live conversation itself is WebSocket-only
(`ws_chat.py`) — this module only serves the persisted history so the UI
can repopulate a chat panel on page load/reconnect."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.core.models import Case, ChatMessage
from app.state import AppState

import json

router = APIRouter(prefix="/cases", tags=["chat"])


def _state(request: Request) -> AppState:
    return cast(AppState, request.app.state.gologs)


def _format_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content)
    return str(content) if content is not None else ""


@router.get("/{case_id}/chat/history")
async def get_chat_history(case_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    with state.session_factory() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(status_code=404, detail=f"No case with id {case_id!r}")

        rows = (
            session.execute(
                select(ChatMessage)
                .where(ChatMessage.case_id == case_id)
                .order_by(ChatMessage.created_at)
            )
            .scalars()
            .all()
        )
        return {
            "messages": [
                {
                    "role": m.role,
                    "content": _format_content(m.content),
                    "created_at": m.created_at.isoformat(),
                }
                for m in rows
            ]
        }
