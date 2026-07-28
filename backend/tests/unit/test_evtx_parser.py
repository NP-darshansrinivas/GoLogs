"""Unit tests for `core.evtx_parser`.

Uses real `.evtx` binary fixtures (not synthetic mocks) sourced from the
python-evtx project's own test corpus, covering both the Security and
System channels — this exercises the actual EVTX binary format, not a
stand-in for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.evtx_parser import (
    EvtxParseError,
    parse_evtx_file,
    parse_evtx_file_with_stats,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SECURITY_EVTX = FIXTURES_DIR / "sample_security.evtx"
SYSTEM_EVTX = FIXTURES_DIR / "sample_system.evtx"


@pytest.fixture(scope="module")
def security_parsed():
    """python-evtx is pure Python and parsing 2261 real records takes real
    time; share one parse across every test in this module rather than
    re-parsing per test."""
    return parse_evtx_file_with_stats(SECURITY_EVTX)


@pytest.fixture(scope="module")
def system_parsed():
    return parse_evtx_file_with_stats(SYSTEM_EVTX)


class TestParseEvtxFileWithStats:
    def test_parses_all_records_in_security_evtx(self, security_parsed) -> None:
        records, stats = security_parsed
        assert stats.total_records_seen == 2261
        assert stats.records_parsed_ok == 2261
        assert stats.records_failed == 0
        assert len(records) == 2261

    def test_parses_all_records_in_system_evtx(self, system_parsed) -> None:
        records, stats = system_parsed
        assert stats.total_records_seen == 1601
        assert stats.records_parsed_ok == 1601
        assert stats.records_failed == 0

    def test_record_numbers_are_sequential_and_start_at_one(self, security_parsed) -> None:
        records, _ = security_parsed
        assert records[0].record_number == 1
        assert records[1].record_number == 2

    def test_record_xml_is_well_formed_and_contains_event_root(self, security_parsed) -> None:
        records, _ = security_parsed
        assert records[0].xml.strip().startswith("<Event")
        assert "EventID" in records[0].xml

    def test_nonexistent_file_raises_evtx_parse_error(self) -> None:
        with pytest.raises(EvtxParseError):
            parse_evtx_file_with_stats(FIXTURES_DIR / "does_not_exist.evtx")

    def test_non_evtx_file_raises_evtx_parse_error(self, tmp_path: Path) -> None:
        bogus = tmp_path / "not_an_evtx.evtx"
        bogus.write_bytes(b"this is not a valid evtx container")
        with pytest.raises(EvtxParseError):
            parse_evtx_file_with_stats(bogus)


class TestParseEvtxFileStreaming:
    def test_generator_yields_same_count_as_batch(self, security_parsed) -> None:
        streamed = list(parse_evtx_file(SECURITY_EVTX))
        batch_records, stats = security_parsed
        assert len(streamed) == stats.records_parsed_ok == len(batch_records)

    def test_generator_is_lazy(self) -> None:
        gen = parse_evtx_file(SECURITY_EVTX)
        first = next(gen)
        assert first.record_number == 1
