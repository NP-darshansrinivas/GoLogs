"""Unit tests for `core.fs_sandbox` — the path-traversal defense in PRD §21."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.fs_sandbox import PathTraversalError, ensure_case_dir, resolve_case_path


class TestResolveCasePath:
    def test_normal_filename_resolves_inside_case_dir(self, tmp_path: Path) -> None:
        case_id = "case-123"
        ensure_case_dir(tmp_path, case_id)
        resolved = resolve_case_path(tmp_path, case_id, "evidence.evtx")
        assert resolved.parent == (tmp_path / case_id).resolve()
        assert resolved.name == "evidence.evtx"

    def test_dotdot_traversal_is_stripped_to_basename_not_raised(self, tmp_path: Path) -> None:
        # Path(filename).name strips directory components, so a traversal
        # attempt degrades to a harmless basename rather than escaping.
        case_id = "case-123"
        resolved = resolve_case_path(tmp_path, case_id, "../../etc/passwd")
        assert resolved.name == "passwd"
        assert resolved.parent == (tmp_path / case_id).resolve()

    def test_absolute_path_as_filename_is_stripped_to_basename(self, tmp_path: Path) -> None:
        case_id = "case-123"
        resolved = resolve_case_path(tmp_path, case_id, "/etc/shadow")
        assert resolved.name == "shadow"
        assert resolved.parent == (tmp_path / case_id).resolve()

    def test_different_case_ids_produce_isolated_directories(self, tmp_path: Path) -> None:
        p1 = resolve_case_path(tmp_path, "case-a", "file.evtx")
        p2 = resolve_case_path(tmp_path, "case-b", "file.evtx")
        assert p1 != p2
        assert p1.parent != p2.parent

    def test_traversal_via_case_id_itself_raises(self, tmp_path: Path) -> None:
        # case_id must be a validated UUID from the Case model in practice,
        # but the sandbox itself still rejects an escaping case_id as
        # defense in depth.
        with pytest.raises(PathTraversalError):
            resolve_case_path(tmp_path, "../../outside", "file.evtx")


class TestEnsureCaseDir:
    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        case_root = ensure_case_dir(tmp_path, "case-xyz")
        assert case_root.is_dir()

    def test_idempotent_when_directory_already_exists(self, tmp_path: Path) -> None:
        ensure_case_dir(tmp_path, "case-xyz")
        case_root = ensure_case_dir(tmp_path, "case-xyz")  # should not raise
        assert case_root.is_dir()
