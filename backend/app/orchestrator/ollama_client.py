"""Thin async client for Ollama's `/api/chat` endpoint (PRD §11.1, §11.3).

Wraps the real, documented Ollama chat API contract:
  - Request: `{"model", "messages", "tools"?, "stream"}`.
  - Streaming response: newline-delimited JSON objects, each shaped like
    `{"message": {"role": "assistant", "content": "...", "tool_calls"?: [...]},
    "done": bool, ...}`; the final object has `"done": true` plus stats.
  - Tool results are fed back as a message:
    `{"role": "tool", "tool_name": ..., "content": ...}`.

This module is deliberately split into two independently-testable pieces:
  - `OllamaClient.chat()`: a thin async generator over the real HTTP/NDJSON
    wire format — the only part that needs a live Ollama server (or a
    mocked `httpx.AsyncClient` transport) to exercise.
  - `collect_chat_response()`: a pure function that accumulates a stream of
    chunk dicts into one `OllamaChatResult` — testable with a fake async
    generator, no HTTP involved at all.

No live Ollama instance is available in this sandbox (see
`docs/context_transfer.md` §9) — this module is built and tested against
Ollama's real, documented contract, with the HTTP transport mocked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class OllamaConnectionError(Exception):
    """Raised when Ollama isn't reachable at all (connection refused, DNS, etc.).

    Deliberately distinct from a tool-execution failure — the caller (the
    conversation loop / `/health` endpoint, PRD §22) needs to tell "Ollama
    isn't running" apart from "a tool call failed" so it can surface the
    right guidance to the analyst (e.g. "run `ollama serve`").
    """


@dataclass(slots=True)
class OllamaToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class OllamaChatResult:
    """Accumulated result of one full (possibly streamed) chat response."""

    content: str = ""
    tool_calls: list[OllamaToolCall] = field(default_factory=list)
    done: bool = False
    done_reason: str | None = None


class OllamaClient:
    """Talks to a local Ollama server over HTTP. No cloud calls ever (FR-12)."""

    def __init__(self, host: str, model: str, http_client: httpx.AsyncClient | None = None):
        self._host = host.rstrip("/")
        self._model = model
        # Allow injecting a client (e.g. one bound to a mock transport) for
        # tests; production code gets a real client bound to `host`.
        self._http_client = http_client or httpx.AsyncClient(base_url=self._host, timeout=120.0)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream raw NDJSON chunk dicts from `/api/chat`, faithfully — no
        accumulation here (see `collect_chat_response` for that)."""
        payload: dict[str, Any] = {"model": self._model, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools

        try:
            async with self._http_client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    yield json.loads(line)
        except httpx.ConnectError as exc:
            raise OllamaConnectionError(
                f"Could not connect to Ollama at {self._host}. Is `ollama serve` running?"
            ) from exc

    async def aclose(self) -> None:
        await self._http_client.aclose()


def collect_chat_response(chunks: list[dict[str, Any]]) -> OllamaChatResult:
    """Accumulate a list of raw `/api/chat` chunk dicts into one result.

    Pure function, no I/O — takes a materialized list rather than an async
    iterator so it's trivial to unit test with hand-built fixture chunks
    modeled on Ollama's real streaming shape (verified against the
    documented API, July 2026).
    """
    result = OllamaChatResult()
    for chunk in chunks:
        message = chunk.get("message", {})
        content_piece = message.get("content")
        if content_piece:
            result.content += content_piece

        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function", {})
            name = function.get("name")
            arguments = function.get("arguments", {})
            if name:
                result.tool_calls.append(OllamaToolCall(name=name, arguments=arguments))

        if chunk.get("done"):
            result.done = True
            result.done_reason = chunk.get("done_reason")

    return result


def build_tool_result_message(tool_name: str, content: str) -> dict[str, str]:
    """Build the `{"role": "tool", ...}` message Ollama expects for a tool result."""
    return {"role": "tool", "tool_name": tool_name, "content": content}
