"""Unit tests for `app.state.AppState`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from app.state import AppState


def _build_state(tool_result: Any) -> tuple[AppState, AsyncMock]:
    call_tool = AsyncMock(return_value=tool_result)
    mcp_session = SimpleNamespace(call_tool=call_tool)
    state = AppState(
        settings=cast(Any, object()),
        session_factory=cast(Any, object()),
        case_storage_dir=Path("."),
        mcp_session=cast(Any, mcp_session),
        ollama_client=cast(Any, object()),
    )
    return state, call_tool


async def test_tool_executor_returns_structured_content_when_present() -> None:
    structured = {"status": "ok", "rows": [1, 2, 3]}
    result = SimpleNamespace(
        structuredContent=structured,
        content=[SimpleNamespace(text='{"ignored": true}')],
    )
    state, call_tool = _build_state(result)

    response = await state.tool_executor("analyze", {"limit": 10})

    assert response == structured
    call_tool.assert_awaited_once_with("analyze", {"limit": 10})


async def test_tool_executor_parses_first_json_dict_from_text_blocks() -> None:
    result = SimpleNamespace(
        structuredContent=None,
        content=[
            SimpleNamespace(text="not-json"),
            SimpleNamespace(text='["list", "is", "not", "dict"]'),
            SimpleNamespace(text='{"parsed": "value"}'),
            SimpleNamespace(text='{"later": "block"}'),
        ],
    )
    state, _ = _build_state(result)

    response = await state.tool_executor("scan", {"target": "x"})

    assert response == {"parsed": "value"}


async def test_tool_executor_skips_blocks_without_text_before_parsing() -> None:
    result = SimpleNamespace(
        structuredContent=None,
        content=[
            object(),
            SimpleNamespace(text=""),
            SimpleNamespace(text=None),
            SimpleNamespace(text='{"ok": true}'),
        ],
    )
    state, _ = _build_state(result)

    response = await state.tool_executor("triage", {})

    assert response == {"ok": True}


async def test_tool_executor_returns_error_when_content_is_none() -> None:
    result = SimpleNamespace(structuredContent=None, content=None)
    state, _ = _build_state(result)

    response = await state.tool_executor("empty_tool", {})

    assert response == {
        "error": {
            "type": "empty_tool_result",
            "message": "Tool 'empty_tool' returned no structured content.",
        }
    }


async def test_tool_executor_returns_error_when_no_dict_can_be_extracted() -> None:
    result = SimpleNamespace(
        structuredContent=None,
        content=[
            SimpleNamespace(text="invalid-json"),
            SimpleNamespace(text="[]"),
            SimpleNamespace(text='"plain string"'),
            SimpleNamespace(text="123"),
        ],
    )
    state, _ = _build_state(result)

    response = await state.tool_executor("non_dict_only", {"a": 1})

    assert response == {
        "error": {
            "type": "empty_tool_result",
            "message": "Tool 'non_dict_only' returned no structured content.",
        }
    }
