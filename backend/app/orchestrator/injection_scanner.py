"""Scans tool results for prompt-injection patterns before they re-enter the
LLM's context (PRD §11.4 point 2, the confused-deputy defense).

This is explicitly documented, per the PRD, as **defense-in-depth, not a
silver bullet** — the primary defenses are structural isolation (tool
results are wrapped in `<tool_result source="...">` tags and the system
prompt states they are data) and capability minimalism (no tool can write
outside the sandbox, execute code, or reach the network). This scanner is
one additional layer: a deterministic, pattern-based re-scan that flags and
neutralizes text that looks like it's trying to issue the model new
instructions, and logs every match for the audit trail.

Distinct from `core.normalizer.contains_suspicious_text()`, which is a
cheap, UI-only heuristic for visually flagging raw event fields in the
event viewer. This module is the actual security control referenced by
PRD §11.4 and §20 (Tampering mitigation) — it runs on the *orchestrator*
side, on full tool *results* (not just one field), and neutralizes matches
rather than merely tagging them for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import InjectionScanStrictness

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InjectionMatch:
    category: str
    matched_text: str
    field_path: str


@dataclass(slots=True)
class ScanResult:
    flagged: bool
    matches: list[InjectionMatch] = field(default_factory=list)
    neutralized_result: Any = None


# Categorized, compiled patterns. Kept as separate categories (rather than
# one giant alternation) so `InjectionMatch.category` is meaningful in logs
# and in the injection_harness test corpus's pass/fail reporting.
#
# Always active, regardless of INJECTION_SCAN_STRICTNESS (PRD §15):
_STANDARD_PATTERNS: dict[str, re.Pattern[str]] = {
    # Only counts as a role marker when it plausibly starts a new "turn" —
    # at the start of the string, or right after sentence/field-boundary
    # punctuation (matching the PRD's own example: "svchost.exe; SYSTEM:
    # ignore..." — the "; " is exactly this kind of boundary). Without this
    # guard, ordinary prose like "The system: an ordinary reference..."
    # false-positives on the bare word "system:" appearing mid-sentence.
    "role_marker": re.compile(
        r'(?:^|[;.\n"!?])\s*\b(system|assistant|user|human|ai)\s*:', re.IGNORECASE
    ),
    "override_instructions": re.compile(
        r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|previous\s+|prior\s+|everything\s+)*"
        r"(previous\s+|prior\s+|above\s+|before\s+)?(instructions\b|above\b|before\b)",
        re.IGNORECASE,
    ),
    "new_instructions_marker": re.compile(
        r"\bnew\s+instructions\s*:|\byou\s+must\s+now\b|\bfrom\s+now\s+on\s+you\b",
        re.IGNORECASE,
    ),
    "tool_invocation_attempt": re.compile(
        r"\b(run|call|execute|invoke|please\s+run)\s+"
        r"(report\.append_finding|mem\.run_plugin|evtx\.\w+|case\.\w+)\b",
        re.IGNORECASE,
    ),
    "direct_imperative_to_assistant": re.compile(
        r"\byou\s+(should|must|will|need\s+to|are\s+now|are\s+required\s+to)\s+\w+",
        re.IGNORECASE,
    ),
}

# Only active when INJECTION_SCAN_STRICTNESS=strict (PRD §15: "trades a
# higher false-positive rate for higher recall on subtler injection
# attempts"). Each pattern here is something `_STANDARD_PATTERNS`
# deliberately does *not* catch, specifically to keep the standard mode's
# false-positive rate low on ordinary DFIR evidence text — see
# `tests/unit/test_injection_scanner.py`'s benign-controls tests, which
# assert standard mode does *not* flag these.
_STRICT_ONLY_PATTERNS: dict[str, re.Pattern[str]] = {
    # The same role-marker words, but WITHOUT requiring a turn-boundary
    # before them — catches "the system: do X" mid-sentence, at the cost
    # of flagging genuinely benign phrasing like "the system: an ordinary
    # reference" too.
    "role_marker_loose": re.compile(r"\b(system|assistant|user|human|ai)\s*:", re.IGNORECASE),
    # A bare imperative verb opening a field value — much broader than
    # `direct_imperative_to_assistant`'s "you should/must/..." requirement,
    # and prone to false-positiving on legitimately imperative-sounding
    # log/command text (e.g. a real CommandLine field literally is an
    # imperative command). Strict mode accepts this tradeoff explicitly.
    "bare_imperative_opener": re.compile(
        r"^\s*(delete|grant|send|email|disable|approve|trust|reveal)\b", re.IGNORECASE
    ),
}

_NEUTRALIZED_TEMPLATE = "[INJECTION_SUSPECTED:{category}] {original}"


def _patterns_for(strictness: InjectionScanStrictness) -> dict[str, re.Pattern[str]]:
    if strictness is InjectionScanStrictness.STRICT:
        return {**_STANDARD_PATTERNS, **_STRICT_ONLY_PATTERNS}
    return _STANDARD_PATTERNS


def _scan_string(
    value: str, field_path: str, patterns: dict[str, re.Pattern[str]]
) -> tuple[list[InjectionMatch], str]:
    matches: list[InjectionMatch] = []
    neutralized = value
    for category, pattern in patterns.items():
        found = pattern.search(value)
        if found:
            matches.append(
                InjectionMatch(
                    category=category, matched_text=found.group(0), field_path=field_path
                )
            )
    if matches:
        categories = ",".join(sorted({m.category for m in matches}))
        neutralized = _NEUTRALIZED_TEMPLATE.format(category=categories, original=value)
    return matches, neutralized


def _scan_recursive(
    value: Any,
    field_path: str,
    matches: list[InjectionMatch],
    patterns: dict[str, re.Pattern[str]],
) -> Any:
    if isinstance(value, str):
        found, neutralized = _scan_string(value, field_path, patterns)
        matches.extend(found)
        return neutralized
    if isinstance(value, dict):
        return {
            k: _scan_recursive(v, f"{field_path}.{k}", matches, patterns) for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _scan_recursive(item, f"{field_path}[{i}]", matches, patterns)
            for i, item in enumerate(value)
        ]
    return value


def scan_tool_result(
    tool_name: str,
    result: Any,
    strictness: InjectionScanStrictness = InjectionScanStrictness.STANDARD,
) -> ScanResult:
    """Recursively scan a tool result's string values for injection patterns.

    `strictness` controls which pattern set applies (PRD §15
    `INJECTION_SCAN_STRICTNESS`): `standard` (the default) uses only the
    patterns tuned to have a low false-positive rate on ordinary DFIR
    evidence; `strict` additionally applies broader, more aggressive
    patterns that catch more but false-positive more often too.

    Returns a `ScanResult` whose `neutralized_result` is safe to place into
    the LLM's context: every matched substring is wrapped with an
    `[INJECTION_SUSPECTED:...]` marker rather than removed outright, so the
    model (and the analyst, if they inspect raw evidence later) can still
    see what was actually in the evidence — flagged, not hidden.
    """
    patterns = _patterns_for(strictness)
    matches: list[InjectionMatch] = []
    neutralized = _scan_recursive(result, tool_name, matches, patterns)

    if matches:
        log.warning(
            "injection_scanner_flagged_tool_result",
            tool_name=tool_name,
            strictness=strictness.value,
            match_count=len(matches),
            categories=sorted({m.category for m in matches}),
            field_paths=[m.field_path for m in matches],
        )

    return ScanResult(flagged=bool(matches), matches=matches, neutralized_result=neutralized)
