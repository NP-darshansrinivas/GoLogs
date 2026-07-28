"""Integration tests for `api.routes_cases` (PRD §24: "pytest + httpx +
in-memory SQLite | Full API request -> orchestrator -> mocked-tool round
trip"). The real FastAPI app is used via `httpx.ASGITransport` (in-process,
no live server) with a fake lifespan that injects a test-double `AppState` —
no real MCP subprocess or Ollama connection needed here, since none of
these routes touch either (ingestion parses EVTX directly via `core.*`, not
through an MCP tool call).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.core.models import Case, Finding, ToolCallAudit
from app.db.session import init_db, make_engine, make_session_factory, session_scope
from app.main import create_app
from app.orchestrator.ollama_client import OllamaClient
from app.state import AppState

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"


@pytest.fixture
def app_and_state(tmp_path) -> tuple[FastAPI, AppState]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = make_session_factory(engine)
    case_storage_dir = tmp_path / "cases"
    case_storage_dir.mkdir()

    state = AppState(
        settings=None,  # not used by routes_cases; report generation doesn't need it either
        session_factory=session_factory,
        case_storage_dir=case_storage_dir,
        mcp_session=AsyncMock(),  # unused by these routes
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


class TestCreateCase:
    @pytest.mark.asyncio
    async def test_creates_case_and_saves_uploaded_file(self, client: httpx.AsyncClient) -> None:
        with open(SECURITY_EVTX, "rb") as f:
            response = await client.post(
                "/api/cases",
                data={"name": "Test Case"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Test Case"
        assert body["status"] == "created"
        assert "sample_security.evtx" in body["files"]

    @pytest.mark.asyncio
    async def test_uploaded_file_is_actually_ingested_in_background(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with open(SECURITY_EVTX, "rb") as f:
            response = await client.post(
                "/api/cases",
                data={"name": "Ingest Test"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        case_id = response.json()["case_id"]

        # BackgroundTasks in a TestClient/ASGITransport context run
        # synchronously before the response fully completes in this
        # httpx+ASGI setup, so the case should already be parsed.
        with state.session_factory() as session:
            case = session.get(Case, case_id)
            assert case.status == "parsed"

        get_response = await client.get(f"/api/cases/{case_id}")
        assert get_response.json()["event_count"] == 2261


class TestGetCase:
    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_case(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/cases/does-not-exist")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_case_metadata(self, client: httpx.AsyncClient, app_and_state) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="Direct Case", status="created")
            s.add(case)
            s.flush()
            case_id = case.id

        response = await client.get(f"/api/cases/{case_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Direct Case"


class TestGetEvents:
    @pytest.mark.asyncio
    async def test_returns_events_after_ingestion(self, client: httpx.AsyncClient) -> None:
        with open(SECURITY_EVTX, "rb") as f:
            create_response = await client.post(
                "/api/cases",
                data={"name": "Events Test"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        case_id = create_response.json()["case_id"]

        response = await client.get(f"/api/cases/{case_id}/events", params={"limit": 10})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 10
        assert len(body["events"]) == 10

    @pytest.mark.asyncio
    async def test_limit_is_capped_at_200(self, client: httpx.AsyncClient) -> None:
        with open(SECURITY_EVTX, "rb") as f:
            create_response = await client.post(
                "/api/cases",
                data={"name": "Cap Test"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        case_id = create_response.json()["case_id"]

        response = await client.get(f"/api/cases/{case_id}/events", params={"limit": 10000})
        assert response.json()["count"] == 200

    @pytest.mark.asyncio
    async def test_filters_by_event_id(self, client: httpx.AsyncClient) -> None:
        with open(SECURITY_EVTX, "rb") as f:
            create_response = await client.post(
                "/api/cases",
                data={"name": "Filter Test"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        case_id = create_response.json()["case_id"]

        response = await client.get(f"/api/cases/{case_id}/events", params={"event_id": 4608})
        events = response.json()["events"]
        assert all(e["event_id"] == 4608 for e in events)
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_case(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/cases/nope/events")
        assert response.status_code == 404


class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_returns_empty_list_for_case_with_no_tool_calls(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="No Audit Case")
            s.add(case)
            s.flush()
            case_id = case.id

        response = await client.get(f"/api/cases/{case_id}/audit")
        assert response.status_code == 200
        assert response.json()["entries"] == []

    @pytest.mark.asyncio
    async def test_returns_recorded_audit_entries(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="Audit Case")
            s.add(case)
            s.flush()
            case_id = case.id
            s.add(
                ToolCallAudit(
                    case_id=case_id,
                    tool_name="evtx.query_events",
                    arguments={"case_id": case_id},
                    result_hash="deadbeef",
                    risk_class="AUTO_APPROVE",
                    approval_status="approved",
                )
            )

        response = await client.get(f"/api/cases/{case_id}/audit")
        entries = response.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "evtx.query_events"


class TestGenerateReport:
    @pytest.mark.asyncio
    async def test_generates_markdown_report(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="Report Case")
            s.add(case)
            s.flush()
            case_id = case.id
            s.add(Finding(case_id=case_id, finding_text="Test finding.", evidence_refs=[]))

        response = await client.post(f"/api/cases/{case_id}/report", params={"format": "markdown"})
        assert response.status_code == 200
        body = response.json()
        assert body["format"] == "markdown"
        assert "Report Case" in body["content"]
        assert "Test finding." in body["content"]

    @pytest.mark.asyncio
    async def test_generates_pdf_report_as_base64(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="PDF Case")
            s.add(case)
            s.flush()
            case_id = case.id

        response = await client.post(f"/api/cases/{case_id}/report", params={"format": "pdf"})
        assert response.status_code == 200
        body = response.json()
        assert body["format"] == "pdf"

        import base64

        pdf_bytes = base64.b64decode(body["content_base64"])
        assert pdf_bytes.startswith(b"%PDF-")

    @pytest.mark.asyncio
    async def test_rejects_invalid_format(self, client: httpx.AsyncClient, app_and_state) -> None:
        _, state = app_and_state
        with session_scope(state.session_factory) as s:
            case = Case(name="Bad Format Case")
            s.add(case)
            s.flush()
            case_id = case.id

        response = await client.post(f"/api/cases/{case_id}/report", params={"format": "docx"})
        assert response.status_code == 400


class TestReparse:
    @pytest.mark.asyncio
    async def test_reparse_returns_404_for_unknown_case(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/cases/nope/reparse")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_reparse_is_idempotent_does_not_duplicate_events(
        self, client: httpx.AsyncClient, app_and_state
    ) -> None:
        _, state = app_and_state
        with open(SECURITY_EVTX, "rb") as f:
            create_response = await client.post(
                "/api/cases",
                data={"name": "Reparse Test"},
                files={"files": ("sample_security.evtx", f, "application/octet-stream")},
            )
        case_id = create_response.json()["case_id"]

        first_events = await client.get(f"/api/cases/{case_id}/events", params={"limit": 1})
        assert first_events.status_code == 200

        reparse_response = await client.post(f"/api/cases/{case_id}/reparse")
        assert reparse_response.status_code == 200

        final = await client.get(f"/api/cases/{case_id}")
        assert final.json()["event_count"] == 2261  # not duplicated to 4522
