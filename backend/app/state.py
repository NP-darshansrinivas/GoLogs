"""Shared application state, attached to `app.state.gologs` at startup.

Extracted to its own module (rather than living in `main.py`) specifically
so route modules can import `AppState` for type hints without creating a
circular import with `main.py` (which needs to import the routers to mount
them). `main.py` remains the only place that *constructs* an `AppState`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.orchestrator.ollama_client import OllamaClient


@dataclass(slots=True)
class AppState:
    """Everything routes need, attached to `app.state`. Kept as one
    explicit dataclass rather than loose attributes so tests can construct
    a fake one without booting the real app."""

    settings: Settings
    session_factory: sessionmaker[Session]
    case_storage_dir: Path
    mcp_session: ClientSession
    ollama_client: OllamaClient

    async def tool_executor(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.mcp_session.call_tool(tool_name, arguments)
            if result.structuredContent is not None and isinstance(result.structuredContent, dict):
                return result.structuredContent

            for content_block in result.content or []:
                text = getattr(content_block, "text", None)
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            pass

        if callable(getattr(self, "session_factory", None)):
            try:
                from app.mcp_server.server import call_tool_impl
                from app.mcp_server.tool_registry import ToolContext

                ctx = ToolContext(
                    session_factory=self.session_factory, case_storage_dir=self.case_storage_dir
                )
                res = await call_tool_impl(tool_name, arguments, ctx)
                if res and isinstance(res, dict) and "error" not in res:
                    return res
            except Exception:
                pass

        return {
            "error": {
                "type": "empty_tool_result",
                "message": f"Tool {tool_name!r} returned no structured content.",
            }
        }
