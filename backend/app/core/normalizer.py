"""Maps heterogeneous raw log records into the canonical `Event` schema (PRD §13).

Responsibility (PRD §10.2): map heterogeneous log sources to the common Event
schema. This module never performs interpretation or scoring of events —
that judgment belongs to the LLM copilot, not to deterministic parsing code.

Known quirk handled here: different EVTX channels serialize the "System"
metadata block under different XML tag names depending on how the record's
template resolves (observed: `<System>` for System channel events, `<s>` for
Security channel events in some EVTX versions). This module never assumes a
fixed tag name for that block — it locates it structurally, by the presence
of a `Channel` child element, so normalization is robust across channels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
import structlog
from defusedxml.common import DefusedXmlException

log = structlog.get_logger(__name__)

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

# Fields, in priority order, that may carry the security-principal identity
# for a record. Different event IDs populate different fields (a logon event
# has TargetUserSid, an object-access event has SubjectUserSid, etc).
_USER_SID_FIELD_PRIORITY = (
    "TargetUserSid",
    "SubjectUserSid",
    "UserSid",
)


@dataclass(slots=True)
class NormalizedEvent:
    """In-memory representation matching the `Event` ORM model's columns."""

    channel: str
    event_id: int
    time_created: datetime | None
    computer: str | None
    user_sid: str | None
    raw_xml: str
    normalized_fields: dict[str, Any] = field(default_factory=dict)


class NormalizationError(Exception):
    """Raised only for XML so malformed the record cannot be salvaged at all."""


def _strip_ns(tag: str) -> str:
    return tag.removeprefix(_NS)


def _find_system_block(root: ET.Element) -> ET.Element | None:
    """Locate the System metadata block regardless of its literal tag name."""
    for child in root:
        if child.find(f"{_NS}Channel") is not None:
            return child
    return None


def _find_event_data_block(root: ET.Element) -> ET.Element | None:
    for child in root:
        local = _strip_ns(child.tag)
        if local in ("EventData", "UserData"):
            return child
    return None


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        # Some EVTX templates emit a trailing "Z" or slightly different
        # microsecond precision; make one more defensive attempt before
        # giving up and logging instead of raising (Reliability NFR, §8).
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            log.warning("evtx_timestamp_unparseable", raw_value=raw)
            return None


def _text_or_none(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    text = elem.text.strip()
    return text or None


def normalize_record(raw_xml: str) -> NormalizedEvent:
    """Normalize one raw EVTX XML record string into a `NormalizedEvent`.

    Raises:
        NormalizationError: only when the XML itself cannot be parsed at
            all. Missing/absent individual fields never raise — they are
            represented as `None` or omitted from `normalized_fields`, and
            the event is still returned so a partially-malformed record
            doesn't block ingestion of the rest of the file.
    """
    try:
        root = DET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise NormalizationError(f"Could not parse record XML: {exc}") from exc
    except DefusedXmlException as exc:
        # A malicious record deliberately crafted to exploit XML parsing
        # (entity-expansion bomb, external entity/DTD reference, etc.) —
        # defusedxml already blocked it; treat it the same as a malformed
        # record rather than letting a security exception crash ingestion.
        log.warning("evtx_record_blocked_malicious_xml", error=str(exc))
        raise NormalizationError(f"Record XML blocked as a potential XML attack: {exc}") from exc

    system_block = _find_system_block(root)
    event_data_block = _find_event_data_block(root)

    channel = "Unknown"
    event_id = -1
    time_created: datetime | None = None
    computer: str | None = None
    user_sid: str | None = None
    normalized_fields: dict[str, Any] = {}

    if system_block is not None:
        channel = _text_or_none(system_block.find(f"{_NS}Channel")) or "Unknown"
        event_id_elem = system_block.find(f"{_NS}EventID")
        if event_id_elem is not None and event_id_elem.text:
            try:
                event_id = int(event_id_elem.text.strip())
            except ValueError:
                log.warning("evtx_event_id_unparseable", raw_value=event_id_elem.text)

        time_created_elem = system_block.find(f"{_NS}TimeCreated")
        if time_created_elem is not None:
            time_created = _parse_timestamp(time_created_elem.get("SystemTime"))

        computer = _text_or_none(system_block.find(f"{_NS}Computer"))

        security_elem = system_block.find(f"{_NS}Security")
        security_user_id = security_elem.get("UserID") if security_elem is not None else None

        provider_elem = system_block.find(f"{_NS}Provider")
        if provider_elem is not None:
            normalized_fields["provider"] = provider_elem.get("Name")
            normalized_fields["provider_guid"] = provider_elem.get("Guid")

        record_id_elem = system_block.find(f"{_NS}EventRecordID")
        normalized_fields["event_record_id"] = _text_or_none(record_id_elem)

        execution_elem = system_block.find(f"{_NS}Execution")
        if execution_elem is not None:
            normalized_fields["process_id"] = execution_elem.get("ProcessID")
            normalized_fields["thread_id"] = execution_elem.get("ThreadID")

        level_elem = system_block.find(f"{_NS}Level")
        normalized_fields["level"] = _text_or_none(level_elem)
        task_elem = system_block.find(f"{_NS}Task")
        normalized_fields["task"] = _text_or_none(task_elem)
        keywords_elem = system_block.find(f"{_NS}Keywords")
        normalized_fields["keywords"] = _text_or_none(keywords_elem)
    else:
        security_user_id = None
        log.warning("evtx_system_block_missing")

    event_data: dict[str, str | None] = {}
    if event_data_block is not None:
        for data_elem in event_data_block:
            name = data_elem.get("Name")
            text = data_elem.text.strip() if data_elem.text else None
            if name:
                event_data[name] = text
            else:
                # Positional (unnamed) Data elements: index them so no
                # information is silently dropped.
                idx = len([k for k in event_data if k.startswith("_positional_")])
                event_data[f"_positional_{idx}"] = text
    normalized_fields["data"] = event_data

    for field_name in _USER_SID_FIELD_PRIORITY:
        candidate = event_data.get(field_name)
        if candidate and candidate not in ("-", "S-1-0-0"):
            user_sid = candidate
            break
    if user_sid is None and security_user_id:
        user_sid = security_user_id or None
        user_sid = user_sid if user_sid else None

    return NormalizedEvent(
        channel=channel,
        event_id=event_id,
        time_created=time_created,
        computer=computer,
        user_sid=user_sid,
        raw_xml=raw_xml,
        normalized_fields=normalized_fields,
    )


# Second-person / role-marker patterns that, if found verbatim inside
# evidence field VALUES, are worth flagging at normalization time as a
# cheap first-pass signal. This is deliberately NOT the full InjectionScanner
# (that lives in `orchestrator/injection_scanner.py` and runs on tool
# *results* before they re-enter the LLM prompt, per PRD §11.4) — this is
# just a lightweight tag so the UI can visually mark suspicious fields in
# the raw event viewer. core/ never makes trust decisions.
_SUSPICIOUS_PATTERN = re.compile(
    r"\bignore\s+(all\s+|any\s+|previous\s+)*instructions\b|\bsystem:|\bassistant:",
    re.IGNORECASE,
)


def contains_suspicious_text(normalized_fields: dict[str, Any]) -> bool:
    """Cheap heuristic tag for the UI only — not a security control on its own."""
    data = normalized_fields.get("data", {})
    return any(value and _SUSPICIOUS_PATTERN.search(value) for value in data.values())
