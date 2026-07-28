"""Filesystem sandboxing for case evidence storage.

PRD §21: "Filesystem access from `core/` is confined to `CASE_STORAGE_DIR` via
a path-sandboxing wrapper (`core.fs_sandbox`) that rejects any resolved path
outside the case directory (defends against path traversal in filenames)."

Every function that touches uploaded evidence on disk must go through
`resolve_case_path()` rather than joining paths directly.
"""

from __future__ import annotations

from pathlib import Path


class PathTraversalError(Exception):
    """Raised when a requested path would resolve outside its case sandbox."""


def resolve_case_path(case_storage_dir: Path, case_id: str, filename: str) -> Path:
    """Resolve `filename` inside `case_storage_dir/case_id`, rejecting traversal.

    Args:
        case_storage_dir: The root sandbox directory for all cases
            (`CASE_STORAGE_DIR` config value).
        case_id: The case's UUID. Used as-is to build the case subdirectory;
            callers must supply a value already validated as a UUID (e.g. by
            the `Case` model's primary key), never raw user input.
        filename: The requested filename, potentially attacker-controlled
            (e.g. an uploaded file's original name).

    Returns:
        The resolved, absolute Path, guaranteed to live inside
        `case_storage_dir/case_id`.

    Raises:
        PathTraversalError: if the resolved path would escape the case
            sandbox directory, e.g. via `../`, absolute paths, or symlink
            tricks embedded in `filename`.
    """
    storage_root = case_storage_dir.resolve()
    case_root = (storage_root / case_id).resolve()

    # A malicious/malformed case_id (e.g. containing "../") could itself
    # walk case_root outside storage_root, independent of filename — check
    # this *before* trusting case_root as a sandbox boundary. In practice
    # case_id is always a UUID from the Case model's primary key, never raw
    # user input, but this check is defense in depth, not the only guard.
    try:
        case_root.relative_to(storage_root)
    except ValueError as exc:
        raise PathTraversalError(
            f"Case id {case_id!r} resolves outside storage root {storage_root}"
        ) from exc

    # Path(filename).name strips any directory components outright; we still
    # resolve() and re-check containment as defense in depth against
    # platform-specific edge cases (e.g. Windows alternate data streams,
    # unicode normalization tricks).
    candidate = (case_root / Path(filename).name).resolve()

    try:
        candidate.relative_to(case_root)
    except ValueError as exc:
        raise PathTraversalError(
            f"Resolved path {candidate} escapes case sandbox {case_root}"
        ) from exc

    return candidate


def ensure_case_dir(case_storage_dir: Path, case_id: str) -> Path:
    """Create (if needed) and return the sandbox directory for a case."""
    storage_root = case_storage_dir.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    case_root = (storage_root / case_id).resolve()

    try:
        case_root.relative_to(storage_root)
    except ValueError as exc:
        raise PathTraversalError(
            f"Case id {case_id!r} resolves outside storage root {storage_root}"
        ) from exc

    case_root.mkdir(parents=True, exist_ok=True)
    return case_root
