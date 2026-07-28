"""Raw EVTX record extraction (PRD §7 FR-2, §10.2 module table).

Responsibility: parse a `.evtx` binary file into a stream of raw XML record
strings. This module never interprets field semantics (that is
`core.normalizer`'s job) and never touches the network or the LLM.

Callers are responsible for resolving the input path through
`core.fs_sandbox` before calling here — this module trusts the `Path` it is
given and does not itself enforce the case-storage sandbox.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import structlog
from Evtx.Evtx import Evtx
from Evtx.Views import evtx_record_xml_view

log = structlog.get_logger(__name__)


class EvtxParseError(Exception):
    """Raised when an .evtx file cannot be opened at all (fatal, not per-record)."""


@dataclass(frozen=True, slots=True)
class RawEvtxRecord:
    """One raw record pulled from an .evtx file, prior to normalization."""

    record_number: int
    xml: str


@dataclass(slots=True)
class EvtxParseStats:
    """Summary counters for a parse run — surfaced to the analyst on ingest.

    A corrupt or partially-truncated .evtx file must never crash ingestion
    (NFR Reliability, PRD §8); individual bad records are counted and
    skipped rather than aborting the whole file.
    """

    total_records_seen: int = 0
    records_parsed_ok: int = 0
    records_failed: int = 0


def _open_evtx(path: Path) -> Evtx:
    """Open an .evtx file, raising EvtxParseError on any open-time failure.

    Note: Evtx()'s constructor only stores the filename — the underlying
    file handle isn't opened until `__enter__`, so the guard must wrap
    that call, not construction.
    """
    try:
        return Evtx(str(path)).__enter__()
    except Exception as exc:  # noqa: BLE001 - fatal open failure, wrap and re-raise
        raise EvtxParseError(f"Failed to open EVTX file {path}: {exc}") from exc


def _iter_raw_records(
    evtx_file: Evtx, path: Path, stats: EvtxParseStats | None = None
) -> Iterator[RawEvtxRecord]:
    """Iterate records, distinguishing two failure classes:

    1. Structural chunk/header corruption on the *first* record attempted —
       the file isn't a usable EVTX container at all, so this is fatal
       (raises EvtxParseError), matching a wrong-magic-bytes-style failure
       that this library only surfaces lazily during iteration rather than
       at open time.
    2. The same class of corruption *after* at least one record has already
       been yielded successfully (e.g. a file truncated partway through) —
       treated as end-of-usable-data: logged and iteration stops gracefully,
       returning everything parsed so far, per the Reliability NFR (§8) that
       no unhandled exception may crash ingestion.
    3. A single bad record's XML rendering failing — always non-fatal,
       skipped and counted, iteration continues.
    """
    record_iter = evtx_file.records()
    any_yielded = False

    while True:
        try:
            record = next(record_iter)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 - structural chunk/header failure
            if not any_yielded:
                raise EvtxParseError(f"EVTX file {path} is not a usable container: {exc}") from exc
            log.warning("evtx_truncated_or_corrupt_tail", path=str(path), error=str(exc))
            return

        if stats is not None:
            stats.total_records_seen += 1

        try:
            xml_string = evtx_record_xml_view(record)
            record_number = record.record_num()
        except Exception as exc:  # noqa: BLE001 - single corrupt record, non-fatal
            if stats is not None:
                stats.records_failed += 1
            log.warning(
                "evtx_record_parse_failed",
                path=str(path),
                record_number=getattr(record, "record_num", lambda: -1)(),
                error=str(exc),
            )
            continue

        any_yielded = True
        yield RawEvtxRecord(record_number=record_number, xml=xml_string)


def parse_evtx_file(path: Path) -> Iterator[RawEvtxRecord]:
    """Stream raw XML records out of an `.evtx` file.

    Uses a generator so multi-hundred-thousand-record files never need to be
    fully materialized in memory at once, keeping this on budget for
    [HW-RULE].

    Raises:
        EvtxParseError: if the file cannot be parsed as an EVTX container at
            all (e.g. wrong magic bytes, zero-length file, immediate header
            corruption). Per-record corruption, and corruption encountered
            only after some records were already read, does *not* raise —
            see `_iter_raw_records` for the exact policy.
    """
    evtx_file = _open_evtx(path)
    try:
        yield from _iter_raw_records(evtx_file, path)
    finally:
        evtx_file.__exit__(None, None, None)


def parse_evtx_file_with_stats(path: Path) -> tuple[list[RawEvtxRecord], EvtxParseStats]:
    """Convenience wrapper: fully materialize records + return parse stats.

    Intended for smaller files / test fixtures and for the ingestion summary
    shown to the analyst. Large-file ingestion should prefer the streaming
    `parse_evtx_file()` generator directly.
    """
    stats = EvtxParseStats()
    records: list[RawEvtxRecord] = []

    evtx_file = _open_evtx(path)
    try:
        for raw_record in _iter_raw_records(evtx_file, path, stats=stats):
            records.append(raw_record)
            stats.records_parsed_ok += 1
    finally:
        evtx_file.__exit__(None, None, None)

    return records, stats
