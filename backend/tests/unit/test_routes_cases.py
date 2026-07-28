"""Unit tests for `app.api.routes_cases` with isolated app/db state."""

from __future__ import annotations

import base64
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import sqlite
from starlette.requests import Request

from app.api import routes_cases
from app.core.evtx_parser import EvtxParseError
from app.core.models import Case, Event, Finding, ToolCallAudit


class _SessionContext:
    def __init__(self, session: Any) -> None:
        self._session = session

    def __enter__(self) -> Any:
        return self._session

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _SessionFactoryQueue:
    def __init__(self, *sessions: Any) -> None:
        self._sessions = deque(sessions)

    def __call__(self) -> _SessionContext:
        return _SessionContext(self._sessions.popleft())


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _QueryStub:
    def __init__(self, *, rows: list[Any] | None = None, count_value: int = 0) -> None:
        self._rows = rows or []
        self._count_value = count_value

    def filter(self, *_args: Any, **_kwargs: Any) -> _QueryStub:
        return self

    def count(self) -> int:
        return self._count_value

    def all(self) -> list[Any]:
        return list(self._rows)


class _IngestSession:
    def __init__(self, case_obj: Case | None) -> None:
        self._case_obj = case_obj
        self.added: list[Any] = []
        self.commits = 0

    def get(self, _model: Any, _case_id: str) -> Case | None:
        return self._case_obj

    def add(self, value: Any) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1


def _make_client(state: Any) -> TestClient:
    app = FastAPI()
    app.include_router(routes_cases.router, prefix="/api")
    app.state.gologs = state
    return TestClient(app)


def _make_request_with_state(state: Any) -> Request:
    app = FastAPI()
    app.state.gologs = state
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "http_version": "1.1",
        }
    )


class TestIngestCaseFiles:
    def test_returns_early_when_case_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _IngestSession(case_obj=None)
        session_factory = _SessionFactoryQueue(session)
        log_error = Mock()
        monkeypatch.setattr(routes_cases.log, "error", log_error)

        routes_cases.ingest_case_files("missing", ["a.evtx"], session_factory, tmp_path)

        assert session.commits == 0
        assert session.added == []
        log_error.assert_called_once()

    def test_ingests_supported_files_tracks_checkpoint_and_skips_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = Case(name="Case A", status="created")
        case.id = "case-a"
        case.parse_checkpoint = "done.evtx"
        session = _IngestSession(case_obj=case)
        session_factory = _SessionFactoryQueue(session)

        def fake_parse(path: Path) -> list[Any]:
            if path.name == "bad.evtx":
                raise EvtxParseError("bad evtx")
            return [SimpleNamespace(xml="<Event><System/></Event>")]

        normalized = SimpleNamespace(
            channel="Security",
            event_id=4624,
            time_created=datetime(2024, 1, 1, tzinfo=UTC),
            computer="host1",
            user_sid="S-1-5-18",
            raw_xml="<Event><System/></Event>",
            normalized_fields={"k": "v"},
        )

        monkeypatch.setattr(routes_cases.fs_sandbox, "resolve_case_path", lambda *_args: tmp_path / _args[-1])
        monkeypatch.setattr(routes_cases, "parse_evtx_file", fake_parse)
        monkeypatch.setattr(routes_cases, "normalize_record", lambda _xml: normalized)
        log_error = Mock()
        monkeypatch.setattr(routes_cases.log, "error", log_error)

        routes_cases.ingest_case_files(
            "case-a",
            ["done.evtx", "note.txt", "good.evtx", "bad.evtx"],
            session_factory,
            tmp_path,
        )

        assert case.status == "parsed"
        assert case.parse_checkpoint == "done.evtx,good.evtx"
        assert session.commits == 3
        assert len(session.added) == 1
        assert isinstance(session.added[0], Event)
        assert session.added[0].channel == "Security"
        assert session.added[0].event_id == 4624
        log_error.assert_called_once()


class TestCreateCase:
    def test_creates_case_saves_files_and_queues_background_ingest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = Mock()

        def add_case(case_obj: Any) -> None:
            if isinstance(case_obj, Case):
                case_obj.id = "case-123"

        session.add.side_effect = add_case
        session_factory = _SessionFactoryQueue(session)
        state = SimpleNamespace(session_factory=session_factory, case_storage_dir=tmp_path)

        ensure_case_dir = Mock()
        monkeypatch.setattr(routes_cases.fs_sandbox, "ensure_case_dir", ensure_case_dir)
        monkeypatch.setattr(
            routes_cases.fs_sandbox,
            "resolve_case_path",
            lambda *_args: tmp_path / "saved.evtx",
        )
        ingest_mock = Mock()
        monkeypatch.setattr(routes_cases, "ingest_case_files", ingest_mock)

        with _make_client(state) as client:
            response = client.post(
                "/api/cases",
                data={"name": "Case Upload"},
                files=[("files", ("input.evtx", b"abc", "application/octet-stream"))],
            )

        assert response.status_code == 200
        assert response.json() == {
            "case_id": "case-123",
            "name": "Case Upload",
            "status": "created",
            "files": ["saved.evtx"],
        }
        assert (tmp_path / "saved.evtx").read_bytes() == b"abc"
        ensure_case_dir.assert_called_once_with(tmp_path, "case-123")
        ingest_mock.assert_called_once_with("case-123", ["saved.evtx"], session_factory, tmp_path)

    @pytest.mark.asyncio
    async def test_ignores_uploads_without_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = Mock()
        session.add.side_effect = lambda case_obj: setattr(case_obj, "id", "case-blank")
        session_factory = _SessionFactoryQueue(session)
        state = SimpleNamespace(session_factory=session_factory, case_storage_dir=tmp_path)

        ensure_case_dir = Mock()
        monkeypatch.setattr(routes_cases.fs_sandbox, "ensure_case_dir", ensure_case_dir)
        resolve_case_path = Mock()
        monkeypatch.setattr(routes_cases.fs_sandbox, "resolve_case_path", resolve_case_path)
        ingest_mock = Mock()
        monkeypatch.setattr(routes_cases, "ingest_case_files", ingest_mock)

        class _EmptyUpload:
            filename = ""

            async def read(self) -> bytes:
                return b"ignored"

        request = _make_request_with_state(state)
        background_tasks = BackgroundTasks()
        response = await routes_cases.create_case(
            request=request,
            background_tasks=background_tasks,
            name="Case Empty File",
            files=[_EmptyUpload()],
        )

        assert response["files"] == []
        resolve_case_path.assert_not_called()
        ensure_case_dir.assert_called_once_with(tmp_path, "case-blank")
        assert len(background_tasks.tasks) == 1
        task = background_tasks.tasks[0]
        assert task.func is ingest_mock
        assert task.args == ("case-blank", [], session_factory, tmp_path)


class TestGetCase:
    def test_returns_404_for_unknown_case(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = None
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/nope")

        assert response.status_code == 404

    def test_returns_case_metadata_with_event_count(self, tmp_path: Path) -> None:
        case = Case(name="Known Case", status="parsed")
        case.id = "case-1"
        case.created_at = datetime(2024, 2, 2, 12, 0, tzinfo=UTC)
        session = Mock()
        session.get.return_value = case
        session.query.return_value = _QueryStub(count_value=7)
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/case-1")

        assert response.status_code == 200
        assert response.json() == {
            "case_id": "case-1",
            "name": "Known Case",
            "status": "parsed",
            "created_at": "2024-02-02T12:00:00+00:00",
            "event_count": 7,
        }


class TestGetEvents:
    def test_returns_404_when_case_missing(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = None
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/nope/events")

        assert response.status_code == 404

    def test_applies_filters_caps_limit_and_serializes_events(self, tmp_path: Path) -> None:
        event = Event(
            case_id="case-22",
            channel="Security",
            event_id=4624,
            time_created=datetime(2024, 3, 1, 11, 30, tzinfo=UTC),
            computer="workstation-1",
            user_sid="S-1-5-18",
            raw_xml="<Event/>",
            normalized_fields={},
        )
        event.uid = "event-1"

        session = Mock()
        session.get.return_value = Case(id="case-22", name="Events", status="parsed")
        session.execute.return_value = _ScalarsResult([event])
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get(
                "/api/cases/case-22/events",
                params={"channel": "Security", "event_id": 4624, "keyword": "admin", "limit": 9999},
            )

        assert response.status_code == 200
        assert response.json() == {
            "events": [
                {
                    "uid": "event-1",
                    "channel": "Security",
                    "event_id": 4624,
                    "time_created": "2024-03-01T11:30:00+00:00",
                    "computer": "workstation-1",
                    "user_sid": "S-1-5-18",
                }
            ],
            "count": 1,
        }
        stmt = session.execute.call_args.args[0]
        sql = str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        assert "events.channel = 'Security'" in sql
        assert "events.event_id = 4624" in sql
        assert "lower(events.raw_xml) LIKE lower('%admin%')" in sql
        assert " LIMIT 200" in sql

    def test_returns_events_without_optional_filters(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = Case(id="case-plain", name="Plain", status="parsed")
        session.execute.return_value = _ScalarsResult([])
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/case-plain/events")

        assert response.status_code == 200
        assert response.json() == {"events": [], "count": 0}


class TestGetAuditTrail:
    def test_returns_404_when_case_missing(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = None
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/missing/audit")

        assert response.status_code == 404

    def test_returns_serialized_audit_entries(self, tmp_path: Path) -> None:
        called_at = datetime(2024, 5, 20, 8, 15, tzinfo=UTC)
        audit = ToolCallAudit(
            case_id="case-5",
            tool_name="evtx.query_events",
            arguments={"limit": 10},
            result_hash="abc123",
            risk_class="AUTO_APPROVE",
            approval_status="approved",
            called_at=called_at,
        )
        audit.id = "audit-1"
        session = Mock()
        session.get.return_value = Case(id="case-5", name="Audit", status="parsed")
        session.execute.return_value = _ScalarsResult([audit])
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.get("/api/cases/case-5/audit")

        assert response.status_code == 200
        assert response.json() == {
            "entries": [
                {
                    "id": "audit-1",
                    "tool_name": "evtx.query_events",
                    "arguments": {"limit": 10},
                    "risk_class": "AUTO_APPROVE",
                    "approval_status": "approved",
                    "called_at": "2024-05-20T08:15:00+00:00",
                }
            ]
        }


class TestGenerateReport:
    def test_rejects_invalid_format(self, tmp_path: Path) -> None:
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(Mock()), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post("/api/cases/case-6/report", params={"format": "docx"})

        assert response.status_code == 400
        assert response.json()["detail"] == "format must be 'markdown' or 'pdf'"

    def test_returns_404_for_missing_case(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = None
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post("/api/cases/missing/report", params={"format": "markdown"})

        assert response.status_code == 404

    def test_generates_markdown_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        event_early = Event(
            case_id="case-7",
            channel="System",
            event_id=1,
            time_created=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
            computer=None,
            user_sid=None,
            raw_xml="<Event/>",
            normalized_fields={},
        )
        event_late = Event(
            case_id="case-7",
            channel="Security",
            event_id=2,
            time_created=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
            computer=None,
            user_sid=None,
            raw_xml="<Event/>",
            normalized_fields={},
        )
        event_without_time = Event(
            case_id="case-7",
            channel="Security",
            event_id=3,
            time_created=None,
            computer=None,
            user_sid=None,
            raw_xml="<Event/>",
            normalized_fields={},
        )
        finding = Finding(
            case_id="case-7",
            finding_text="Suspicious activity",
            evidence_refs=["ev-1"],
            created_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        )
        audit = ToolCallAudit(
            case_id="case-7",
            tool_name="tool.x",
            arguments={"a": 1},
            result_hash="h",
            risk_class="AUTO_APPROVE",
            approval_status="approved",
            called_at=datetime(2024, 1, 1, 12, 5, tzinfo=UTC),
        )

        session = Mock()
        session.get.return_value = Case(id="case-7", name="Report Case", status="parsed")
        session.query.return_value = _QueryStub(rows=[event_late, event_without_time, event_early])
        session.execute.side_effect = [_ScalarsResult([finding]), _ScalarsResult([audit])]

        captured_data: list[Any] = []

        def fake_markdown_builder(data: Any) -> str:
            captured_data.append(data)
            return "MARKDOWN-REPORT"

        monkeypatch.setattr(routes_cases, "build_markdown_report", fake_markdown_builder)
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post("/api/cases/case-7/report", params={"format": "markdown"})

        assert response.status_code == 200
        assert response.json() == {"format": "markdown", "content": "MARKDOWN-REPORT"}
        report_data = captured_data[0]
        assert report_data.case_name == "Report Case"
        assert report_data.case_id == "case-7"
        assert report_data.event_count == 3
        assert report_data.channels == ["Security", "System"]
        assert report_data.time_range == ("2024-01-01T09:00:00+00:00", "2024-01-01T11:00:00+00:00")
        assert len(report_data.findings) == 1
        assert len(report_data.audit_entries) == 1

    def test_generates_pdf_payload_as_base64(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = Mock()
        session.get.return_value = Case(id="case-8", name="PDF Case", status="parsed")
        session.query.return_value = _QueryStub(rows=[])
        session.execute.side_effect = [_ScalarsResult([]), _ScalarsResult([]), _ScalarsResult([])]
        monkeypatch.setattr(routes_cases, "build_pdf_report", lambda _data: b"%PDF-test")
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post("/api/cases/case-8/report", params={"format": "pdf"})

        assert response.status_code == 200
        assert response.json() == {
            "format": "pdf",
            "content_base64": base64.b64encode(b"%PDF-test").decode("ascii"),
        }


class TestReparseCase:
    def test_returns_404_for_unknown_case(self, tmp_path: Path) -> None:
        session = Mock()
        session.get.return_value = None
        state = SimpleNamespace(session_factory=_SessionFactoryQueue(session), case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post("/api/cases/none/reparse")

        assert response.status_code == 404

    def test_queues_reparse_with_only_files_in_case_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case_id = "case-r"
        case_dir = tmp_path / case_id
        case_dir.mkdir()
        (case_dir / "a.evtx").write_text("x")
        (case_dir / "b.raw").write_text("y")
        (case_dir / "nested").mkdir()

        session = Mock()
        session.get.return_value = Case(id=case_id, name="Reparse", status="parsed")
        session_factory = _SessionFactoryQueue(session)
        ingest_mock = Mock()
        monkeypatch.setattr(routes_cases, "ingest_case_files", ingest_mock)
        state = SimpleNamespace(session_factory=session_factory, case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post(f"/api/cases/{case_id}/reparse")

        assert response.status_code == 200
        assert response.json() == {"case_id": case_id, "status": "reparsing"}
        ingest_mock.assert_called_once()
        called_args = ingest_mock.call_args.args
        assert called_args[0] == case_id
        assert sorted(called_args[1]) == ["a.evtx", "b.raw"]
        assert called_args[2] is session_factory
        assert called_args[3] == tmp_path

    def test_queues_reparse_with_empty_filenames_when_case_dir_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case_id = "case-no-dir"
        session = Mock()
        session.get.return_value = Case(id=case_id, name="No Dir", status="parsed")
        session_factory = _SessionFactoryQueue(session)
        ingest_mock = Mock()
        monkeypatch.setattr(routes_cases, "ingest_case_files", ingest_mock)
        state = SimpleNamespace(session_factory=session_factory, case_storage_dir=tmp_path)

        with _make_client(state) as client:
            response = client.post(f"/api/cases/{case_id}/reparse")

        assert response.status_code == 200
        ingest_mock.assert_called_once_with(case_id, [], session_factory, tmp_path)
