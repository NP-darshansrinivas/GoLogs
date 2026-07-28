# Security Policy & Vulnerability Disclosure

## Overview

**GoLogs** is a local-first, AI-augmented Digital Forensics & Incident Response (DFIR) copilot built with defensive security and threat modeling as core architectural priorities. Because GoLogs analyzes potentially hostile evidence (such as attacker-controlled event log strings and memory process structures), we treat the application itself—and the underlying local LLM—as an active attack surface.

---

## Supported Versions

Security updates and patches are actively maintained for the following versions:

| Version | Supported | Notes |
|---|---|---|
| `v0.1.x` (main) | :white_check_mark: Yes | Current development release |
| `< 0.1.0` | :x: No | Legacy pre-release builds |

---

## Threat Model & Security Architecture Summary

GoLogs implements a three-tier security architecture to ensure evidence data can never manipulate the investigation or escalate execution privileges:

1. **Capability Minimalism**: The MCP tool server exposes a closed set of 6 read-only/scoped tools. There are **no shell execution tools**, no arbitrary file writing tools, and no external network outbound tools.
2. **Code-Enforced Deny List (Permission Gate)**: Evaluated directly in Python (`orchestrator/permission_gate.py`) via a `frozenset` check prior to dispatching any tool. Model output cannot bypass or override this gate. Sensitive operations mandate explicit analyst approval.
3. **Post-Execution Injection Scanning**: Every tool result is scanned before re-entering the LLM prompt context (`orchestrator/injection_scanner.py`). Instruction-like text found inside evidence fields is flagged and neutralized so the LLM treats it strictly as data, not commands.

---

## Reporting a Vulnerability

If you discover a potential security vulnerability in GoLogs (including prompt injection bypasses, permission gate evasions, or local file sandbox escapes), please report it responsibly:

### How to Report
- **Email**: Send vulnerability reports directly to **Darshan S** at [`np767672@gmail.com`](mailto:np767672@gmail.com).
- **Subject**: `[SECURITY VULNERABILITY] GoLogs - <Short Description>`

### Please Include
1. Steps to reproduce the issue (including sample `.evtx` files or prompt payloads if applicable).
2. The expected vs. actual security behavior.
3. Impact assessment (e.g., confused-deputy escalation, scope bypass).

### Response Timeline
- **Acknowledgement**: Within 24-48 hours.
- **Triage & Assessment**: Within 5 business days.
- **Fix & Patch Release**: Critical security issues will be patched on `main` as a priority release.

Thank you for helping keep GoLogs and the open-source security community safe!
