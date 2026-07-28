"""Integration tests for `api.routes_chat` (chat history REST endpoint)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.core.models import Case, ChatMessage
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.main import create_app
from app.orchestrator.ollama_client import OllamaClient
from app.state import AppState


@pytest.fixture
def app_and_state(tmp_path) -> tuple[FastAPI, AppState]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = tmp_path / "cases"
    case_storage_dir.mkdir()

    state = AppState(
        settings=None,
        session_factory=session_factory,
        case_storage_dir=case_storage_dir,
        mcp_session=AsyncMock(),
        ollama_client=AsyncMock(spec=OllamaClient),
    )

    @asynccontextmanager
    async def fake_lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.gologs = state
        yield

    app = create_app(lifespan_override=fake_lifespan)
    return app, state


@pytest.fixture
async def client(app_and_state) -> AsyncIterator[httpx.AsyncClient]:
    app, _ = app_and_state
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


class TestGetChatHistory:
    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_case(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/cases/does-not-exist/chat/history")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_case_with_no_messages(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="No Chat Case")
            s.add(case)
            s.flush()
            case_id = case.id

        response = await client.get(f"/api/cases/{case_id}/chat/history")
        assert response.status_code == 200
        assert response.json()["messages"] == []

    @pytest.mark.asyncio
    async def test_returns_messages_in_chronological_order(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="Chat History Case")
            s.add(case)
            s.flush()
            case_id = case.id
            s.add(ChatMessage(case_id=case_id, role="user", content="First message"))
            s.add(ChatMessage(case_id=case_id, role="assistant", content="First reply"))
            s.add(ChatMessage(case_id=case_id, role="user", content="Second message"))

        response = await client.get(f"/api/cases/{case_id}/chat/history")
        messages = response.json()["messages"]
        assert len(messages) == 3
        assert [m["content"] for m in messages] == [
            "First message",
            "First reply",
            "Second message",
        ]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
