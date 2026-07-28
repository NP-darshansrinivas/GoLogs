"""Unit tests for `core.mem_analyzer`.

Two testing strategies, deliberately mixed:

1. Real subprocess invocations of the actual `vol` CLI for failure paths
   that don't require a valid memory image (nonexistent file, corrupt/
   non-EVTX-format file) — these exercise the real error-handling contract
   of Volatility3, not a guess at it.
2. Mocked `subprocess.run` for the success path and the timeout path, since
   producing a real, valid Windows memory image is outside what a test
   fixture can reasonably ship. The mock payloads are modeled on
   Volatility3's actual JSON renderer output shape.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.mem_analyzer import (
    ALLOWED_PLUGINS,
    UnknownPluginError,
    run_plugin,
)


class TestPluginAllowList:
    def test_unknown_plugin_raises_before_any_subprocess_call(self) -> None:
        with patch("app.core.mem_analyzer.subprocess.run") as mock_run:
            with pytest.raises(UnknownPluginError):
                run_plugin(Path("/tmp/whatever.raw"), "run_command")
            mock_run.assert_not_called()

    def test_allowed_plugins_match_prd_section_11_2(self) -> None:
        # PRD §11.2 mem.run_plugin: plugin enum[pslist,pstree,netscan,
        # malfind,cmdline,dlllist] — the enum values are the contract.
        assert set(ALLOWED_PLUGINS) == {
            "pslist",
            "pstree",
            "netscan",
            "malfind",
            "cmdline",
            "dlllist",
        }

    def test_all_allowed_plugins_map_to_windows_namespace(self) -> None:
        for qualified in ALLOWED_PLUGINS.values():
            assert qualified.startswith("windows.")


@pytest.mark.skipif(shutil.which("vol") is None, reason="vol CLI not found on PATH")
class TestRealSubprocessFailurePaths:
    """These call the real `vol` binary — no mocking — because the failure
    behavior for a missing/invalid file doesn't require a valid memory
    image and is worth verifying against ground truth."""

    def test_nonexistent_image_file_reports_structured_failure(self) -> None:
        result = run_plugin(Path("/nonexistent-image-file.raw"), "pslist", timeout_seconds=30)
        assert result.success is False
        assert result.timed_out is False
        assert result.exit_code is not None
        assert result.exit_code != 0
        assert result.error

    def test_corrupt_image_file_reports_structured_failure_not_exception(
        self, tmp_path: Path
    ) -> None:
        garbage = tmp_path / "garbage.raw"
        garbage.write_bytes(b"\x00" * (1024 * 1024))  # 1MB of zeros, not a valid image
        result = run_plugin(garbage, "pslist", timeout_seconds=60)
        assert result.success is False
        assert result.rows == []
        assert result.error is not None


class TestMockedSubprocessPaths:
    """Success and timeout paths, mocked against Volatility3's real JSON
    renderer output shape (a JSON array of row objects)."""

    @pytest.fixture(autouse=True)
    def mock_vol_binary(self):
        with patch("app.core.mem_analyzer._vol_binary", return_value="vol"):
            yield

    def test_successful_run_parses_json_rows(self) -> None:
        fake_rows = [
            {"PID": 4, "PPID": 0, "ImageFileName": "System", "Offset(V)": "0x811234"},
            {"PID": 456, "PPID": 4, "ImageFileName": "smss.exe", "Offset(V)": "0x822345"},
        ]
        fake_completed = subprocess.CompletedProcess(
            args=["vol"], returncode=0, stdout=json.dumps(fake_rows), stderr=""
        )
        with patch("app.core.mem_analyzer.subprocess.run", return_value=fake_completed):
            result = run_plugin(Path("/tmp/valid_image.raw"), "pslist")
        assert result.success is True
        assert result.error is None
        assert len(result.rows) == 2
        assert result.rows[0]["ImageFileName"] == "System"

    def test_timeout_reports_timed_out_flag_not_exception(self) -> None:
        with patch(
            "app.core.mem_analyzer.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["vol"], timeout=120),
        ):
            result = run_plugin(Path("/tmp/hostile_image.raw"), "malfind", timeout_seconds=120)
        assert result.success is False
        assert result.timed_out is True
        assert "timed out" in result.error.lower()

    def test_non_json_stdout_on_zero_exit_is_reported_not_raised(self) -> None:
        fake_completed = subprocess.CompletedProcess(
            args=["vol"], returncode=0, stdout="not valid json at all", stderr=""
        )
        with patch("app.core.mem_analyzer.subprocess.run", return_value=fake_completed):
            result = run_plugin(Path("/tmp/valid_image.raw"), "netscan")
        assert result.success is False
        assert "JSON" in result.error

    def test_command_only_ever_contains_allowlisted_plugin_name(self) -> None:
        fake_completed = subprocess.CompletedProcess(args=["vol"], returncode=0, stdout="[]")
        with patch("app.core.mem_analyzer.subprocess.run", return_value=fake_completed) as mock_run:
            run_plugin(Path("/tmp/valid_image.raw"), "dlllist")
        called_cmd = mock_run.call_args.args[0]
        assert "windows.dlllist.DllList" in called_cmd
        assert "--offline" in called_cmd  # FR-12: never reach the network

    def test_offline_flag_always_present(self) -> None:
        fake_completed = subprocess.CompletedProcess(args=["vol"], returncode=0, stdout="[]")
        for plugin in ALLOWED_PLUGINS:
            with patch(
                "app.core.mem_analyzer.subprocess.run", return_value=fake_completed
            ) as mock_run:
                run_plugin(Path("/tmp/valid_image.raw"), plugin)
            assert "--offline" in mock_run.call_args.args[0]
