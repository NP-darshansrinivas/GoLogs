"""Database models for GoLogs.

Implements the schema specified in PRD §13 exactly:
CASE ||--o{ EVENT, MEM_PLUGIN_RESULT, FINDING, TOOL_CALL_AUDIT, CHAT_MESSAGE.

This module belongs to `core/` and therefore has zero dependency on
`orchestrator/`, `api/`, or `mcp_server/` (enforced by import-linter,
see pyproject.toml). It has no I/O side effects beyond what SQLAlchemy's
declarative layer requires.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all GoLogs ORM models."""


class Case(Base):
    """A forensic investigation case: one or more uploaded EVTX/memory artifacts."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    status: Mapped[str] = mapped_column(String(32), default="created")
    # Checkpoint for idempotent/resumable parsing (PRD §22 Recovery).
    parse_checkpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    events: Mapped[list[Event]] = relationship(back_populates="case", cascade="all, delete-orphan")
    mem_plugin_results: Mapped[list[MemPluginResult]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    tool_call_audits: Mapped[list[ToolCallAudit]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class Event(Base):
    """A normalized Windows Event Log record (from EVTX ingestion, FR-2)."""

    __tablename__ = "events"

    uid: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id"), index=True)
    channel: Mapped[str] = mapped_column(String(128), index=True)
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    # Nullable: core.normalizer legitimately returns None for a record whose
    # TimeCreated field is missing or unparseable rather than raising, so
    # ingestion of the rest of a file isn't blocked by one bad timestamp.
    time_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    computer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_sid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_xml: Mapped[str] = mapped_column(Text)
    normalized_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    case: Mapped[Case] = relationship(back_populates="events")


class MemPluginResult(Base):
    """Cached output of a single Volatility3 plugin run against a case's memory image."""

    __tablename__ = "mem_plugin_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id"), index=True)
    plugin_name: Mapped[str] = mapped_column(String(64), index=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[Case] = relationship(back_populates="mem_plugin_results")


class Finding(Base):
    """An analyst- or copilot-recorded finding, the only entity created via a
    state-mutating tool (`report.append_finding`, PRD §11.2)."""

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id"), index=True)
    finding_text: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    case: Mapped[Case] = relationship(back_populates="findings")


class ToolCallAudit(Base):
    """Immutable audit record of every MCP tool call. PRD §14 /audit endpoint,
    PRD §20 Repudiation mitigation. 100% of tool calls must be logged here."""

    __tablename__ = "tool_call_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_hash: Mapped[str] = mapped_column(String(64))
    risk_class: Mapped[str] = mapped_column(String(32))
    approval_status: Mapped[str] = mapped_column(String(32))
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    case: Mapped[Case] = relationship(back_populates="tool_call_audits")


class ChatMessage(Base):
    """A single message in the analyst<->copilot conversation for a case."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("cases.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    case: Mapped[Case] = relationship(back_populates="chat_messages")
