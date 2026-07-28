"""Wraps Volatility3's plugin execution (PRD §7 FR-3, §10.2 module table).

Responsibility: run a fixed, allow-listed set of Volatility3 plugins against
a case's memory image and return structured results. Never writes to the
memory image (read-only); never touches the network.

Design decision (documented per Phase 2 engineering critique): rather than
embedding Volatility3's low-level framework API (`contexts`/`automagic`/
`plugins`) directly in-process, this module shells out to the `vol` CLI
entrypoint as an isolated subprocess. This directly satisfies three PRD
requirements at once:
  - §20 R2 / §22: per-plugin timeout + isolated execution, so a crafted or
    corrupt memory image can hang or crash without taking down the backend
    process (`subprocess.run(..., timeout=...)` gives this for free; an
    in-process call would require much more invasive isolation to get the
    same guarantee).
  - §23: bounded concurrency via a subprocess pool is simple to enforce
    externally (a semaphore around subprocess launches) without fighting
    Volatility3's internal threading model.
  - §11.2 / §21: only an explicit, hard-coded allow-list of plugin names is
    ever placed on the command line — never a caller-supplied string —
    which forecloses argument-injection into the `vol` invocation.

`--offline` is passed on every invocation: PRD FR-12 requires the app work
fully offline, and Volatility3 will otherwise attempt to fetch missing ISF
symbol files from the network on first use of an unfamiliar OS build. Analysts
should pre-populate `~/.cache/volatility3` (or point `--symbol-dirs` at a
local symbol pack) during setup — see docs/INSTALLATION_GUIDE.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# PRD §11.2 mem.run_plugin input schema: plugin enum[pslist,pstree,netscan,
# malfind,cmdline,dlllist]. This is the *only* place short names are mapped
# to fully-qualified Volatility3 plugin names — mcp_server/orchestrator must
# never construct a plugin name themselves.
ALLOWED_PLUGINS: dict[str, str] = {
    "pslist": "windows.pslist.PsList",
    "pstree": "windows.pstree.PsTree",
    "netscan": "windows.netscan.NetScan",
    "malfind": "windows.malfind.Malfind",
    "cmdline": "windows.cmdline.CmdLine",
    "dlllist": "windows.dlllist.DllList",
}

DEFAULT_TIMEOUT_SECONDS = 120  # PRD §20 R2, §22


class UnknownPluginError(Exception):
    """Raised when a caller requests a plugin name outside ALLOWED_PLUGINS.

    This is a hard rejection, not a warning: an unrecognized plugin name
    must never reach the `vol` subprocess command line.
    """


class VolatilityNotFoundError(Exception):
    """Raised when the `vol` CLI entrypoint isn't on PATH."""


@dataclass(slots=True)
class MemPluginRunResult:
    """Outcome of one Volatility3 plugin invocation."""

    plugin: str
    success: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    duration_seconds: float = 0.0
    exit_code: int | None = None


def _vol_binary() -> str:
    binary = shutil.which("vol")
    if binary is None:
        raise VolatilityNotFoundError(
            "The 'vol' CLI entrypoint was not found on PATH. Install volatility3 "
            "(pip install volatility3) — see docs/INSTALLATION_GUIDE.md."
        )
    return binary


def run_plugin(
    image_path: Path,
    plugin: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> MemPluginRunResult:
    """Run one allow-listed Volatility3 plugin against `image_path`.

    Never raises for plugin-execution failures (missing symbols, corrupt
    image, timeout) — those are reported in the returned
    `MemPluginRunResult.error`/`timed_out` fields so the orchestrator can
    surface them to the LLM as a normal tool result (PRD §22 error-handling
    policy). Only raises for programmer errors: an unrecognized plugin name,
    or a missing `vol` binary.

    Callers are responsible for resolving `image_path` through
    `core.fs_sandbox` before calling here, exactly as with `evtx_parser`.
    """
    if plugin not in ALLOWED_PLUGINS:
        raise UnknownPluginError(
            f"Plugin {plugin!r} is not in the allow-list: {sorted(ALLOWED_PLUGINS)}"
        )
    qualified_name = ALLOWED_PLUGINS[plugin]
    binary = _vol_binary()

    cmd = [
        binary,
        "-q",  # suppress progress bar noise
        "-r",
        "json",
        "--offline",  # FR-12: never let Volatility3 reach the network
        "-f",
        str(image_path),
        qualified_name,
    ]

    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 # nosec B603 - fixed binary path + allow-listed args only, see module docstring
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        log.warning(
            "mem_plugin_timeout", plugin=plugin, image=str(image_path), timeout=timeout_seconds
        )
        return MemPluginRunResult(
            plugin=plugin,
            success=False,
            error=f"Plugin '{plugin}' timed out after {timeout_seconds}s",
            timed_out=True,
            duration_seconds=duration,
        )

    duration = time.monotonic() - start

    if proc.returncode != 0:
        # On failure, Volatility3's JSON renderer never actually runs — the
        # framework prints a plain-text diagnostic to stdout before reaching
        # the render stage (verified empirically; see mem_analyzer tests).
        # Capture and truncate it as the error message rather than trying to
        # parse it as JSON.
        diagnostic = (proc.stdout or proc.stderr or "").strip()
        log.warning(
            "mem_plugin_failed",
            plugin=plugin,
            image=str(image_path),
            exit_code=proc.returncode,
            diagnostic=diagnostic[:500],
        )
        return MemPluginRunResult(
            plugin=plugin,
            success=False,
            error=diagnostic[:2000] or f"vol exited with code {proc.returncode}",
            exit_code=proc.returncode,
            duration_seconds=duration,
        )

    try:
        rows = json.loads(proc.stdout)
        if not isinstance(rows, list):
            raise ValueError(f"Expected a JSON array, got {type(rows).__name__}")
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("mem_plugin_unparseable_output", plugin=plugin, error=str(exc))
        return MemPluginRunResult(
            plugin=plugin,
            success=False,
            error=f"Could not parse plugin output as JSON: {exc}",
            exit_code=proc.returncode,
            duration_seconds=duration,
        )

    return MemPluginRunResult(
        plugin=plugin,
        success=True,
        rows=rows,
        exit_code=proc.returncode,
        duration_seconds=duration,
    )
