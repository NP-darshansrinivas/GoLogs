"""Classifies and gates every tool call before it reaches the MCP server.

Per PRD §11.4 point 5: "Deny-list is code, not prompt: the set of forbidden
tools/actions is enforced in `permission_gate.py`, independent of anything
the model outputs — the model cannot 'argue its way' past it." This module
is that enforcement point. It is the *only* place in the codebase that
decides whether a requested tool call is allowed to reach
`mcp_server.tool_registry` — the orchestrator's conversation loop must
always route through here first (see PRD §10.2 module responsibility table
for `mcp_server.tool_registry`: "never executes a tool without going
through orchestrator.permission_gate first").

Three possible outcomes, in order of severity:
  - DENY: the tool name isn't one of the six explicitly registered tools.
    This is a hard rejection with no model-visible negotiation path — it
    forecloses any future "helpfully added" `run_command`-style tool from
    ever being reachable just because someone registered it in
    `tool_registry` without also updating this deny-list check. (PRD §11.2:
    "Explicitly absent by design... a deliberate scoping decision.")
  - REQUIRES_CONFIRMATION: an analyst must explicitly approve before
    execution (PRD FR-8). This is the default posture for any
    state-mutating tool, and for `mem.run_plugin` on a cache miss (real
    CPU/RAM cost).
  - AUTO_APPROVE: safe to execute immediately — every read-only query tool,
    and `mem.run_plugin` on a cache hit (already-computed, free to re-read).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.models import MemPluginResult
from app.mcp_server.tool_registry import TOOL_RISK_CLASSES

log = structlog.get_logger(__name__)


class RiskClass(StrEnum):
    AUTO_APPROVE = "AUTO_APPROVE"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """The gate's ruling on one requested tool call, with a human-readable
    reason so it can be surfaced verbatim in the UI confirmation dialog and
    in the `TOOL_CALL_AUDIT` row (PRD §13, §14 `/audit`)."""

    tool_name: str
    risk_class: RiskClass
    reason: str

    @property
    def allowed_without_confirmation(self) -> bool:
        return self.risk_class is RiskClass.AUTO_APPROVE

    @property
    def denied(self) -> bool:
        return self.risk_class is RiskClass.DENY


# The complete, closed set of tools this system will ever execute. Anything
# not in this set is DENY, full stop — this check does not consult
# TOOL_RISK_CLASSES or the MCP schema files at all, specifically so that
# registering a new tool in tool_registry.py alone can never make it
# reachable; this set must be updated deliberately, in the same review, by
# a human who has read PRD §11.2's "explicitly absent by design" list.
_REGISTERED_TOOL_NAMES = frozenset(
    {
        "case.get_metadata",
        "evtx.query_events",
        "evtx.get_event_detail",
        "mem.list_processes",
        "mem.run_plugin",
        "report.append_finding",
    }
)


def classify_tool_call(
    tool_name: str, arguments: dict[str, Any], session: Session
) -> PermissionDecision:
    """Classify one requested tool call. Never raises.

    `session` is used only for the one dynamic classification rule
    (`mem.run_plugin`'s cache-hit downgrade to AUTO_APPROVE) — every other
    tool's risk class is static and doesn't touch the database.
    """
    if tool_name not in _REGISTERED_TOOL_NAMES:
        log.warning("permission_gate_deny_unregistered_tool", tool_name=tool_name)
        return PermissionDecision(
            tool_name=tool_name,
            risk_class=RiskClass.DENY,
            reason=(
                f"{tool_name!r} is not one of GoLogs' registered tools. "
                "Shell execution, arbitrary file access, network access, and "
                "process manipulation tools do not exist in this system by design."
            ),
        )

    if tool_name == "mem.run_plugin":
        return _classify_mem_run_plugin(arguments, session)

    base_class = RiskClass(TOOL_RISK_CLASSES[tool_name])
    reason = (
        "Read-only query against already-ingested case data."
        if base_class is RiskClass.AUTO_APPROVE
        else "This tool mutates case state and always requires analyst confirmation."
    )
    return PermissionDecision(tool_name=tool_name, risk_class=base_class, reason=reason)


def _classify_mem_run_plugin(arguments: dict[str, Any], session: Session) -> PermissionDecision:
    """`mem.run_plugin`'s risk class is dynamic (PRD §11.2 table, footnote):
    AUTO_APPROVE on a cache hit (already computed, free to re-read),
    REQUIRES_CONFIRMATION on a cache miss (real CPU/RAM cost — PRD §20 R2)."""
    case_id = arguments.get("case_id")
    plugin = arguments.get("plugin")

    if not case_id or not plugin:
        # Malformed arguments — let it through to the dispatch function's
        # own validation rather than the gate guessing; the dispatch
        # function returns a structured error either way (PRD §22).
        return PermissionDecision(
            tool_name="mem.run_plugin",
            risk_class=RiskClass.REQUIRES_CONFIRMATION,
            reason="Missing case_id/plugin; requires confirmation to run with these arguments.",
        )

    cached = (
        session.query(MemPluginResult)
        .filter(MemPluginResult.case_id == case_id, MemPluginResult.plugin_name == plugin)
        .one_or_none()
    )
    if cached is not None:
        return PermissionDecision(
            tool_name="mem.run_plugin",
            risk_class=RiskClass.AUTO_APPROVE,
            reason=f"Plugin {plugin!r} already ran for this case; returning the cached result.",
        )

    return PermissionDecision(
        tool_name="mem.run_plugin",
        risk_class=RiskClass.REQUIRES_CONFIRMATION,
        reason=(
            f"Running Volatility3 plugin {plugin!r} for the first time on this case. "
            "This is CPU/RAM-intensive (up to 120s) and requires confirmation."
        ),
    )
