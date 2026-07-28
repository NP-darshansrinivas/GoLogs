"""Unit tests for `orchestrator.ollama_client`.

`collect_chat_response` is tested against hand-built chunk dicts modeled on
Ollama's real, documented `/api/chat` streaming shape. `OllamaClient.chat()`
is tested against a mocked `httpx.AsyncClient` transport (no live Ollama
server available in this sandbox — see `docs/context_transfer.md` §9), but
the mocked responses are shaped exactly like real Ollama NDJSON output.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.orchestrator.ollama_client import (
    OllamaClient,
    OllamaConnectionError,
    build_tool_result_message,
    collect_chat_response,
)

# Realistic chunk sequences, modeled on Ollama's documented /api/chat format.

PLAIN_TEXT_STREAM = [
    {"model": "llama3.1", "message": {"role": "assistant", "content": "The "}, "done": False},
    {"model": "llama3.1", "message": {"role": "assistant", "content": "logon "}, "done": False},
    {"model": "llama3.1", "message": {"role": "assistant", "content": "succeeded."}, "done": False},
    {
        "model": "llama3.1",
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "total_duration": 3244883583,
    },
]

TOOL_CALL_STREAM = [
    {"model": "llama3.1", "message": {"role": "assistant", "content": ""}, "done": False},
    {
        "model": "llama3.1",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "evtx.query_events",
                        "arguments": {"case_id": "abc123", "event_id": 4624},
                    }
                }
            ],
        },
        "done": True,
        "done_reason": "stop",
    },
]

MULTI_TOOL_CALL_STREAM = [
    {
        "model": "llama3.1",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "case.get_metadata", "arguments": {"case_id": "abc123"}}},
                {"function": {"name": "mem.list_processes", "arguments": {"case_id": "abc123"}}},
            ],
        },
        "done": True,
    },
]


class TestCollectChatResponse:
    def test_accumulates_content_across_chunks_in_order(self) -> None:
        result = collect_chat_response(PLAIN_TEXT_STREAM)
        assert result.content == "The logon succeeded."
        assert result.done is True
        assert result.done_reason == "stop"
        assert result.tool_calls == []

    def test_extracts_single_tool_call(self) -> None:
        result = collect_chat_response(TOOL_CALL_STREAM)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "evtx.query_events"
        assert result.tool_calls[0].arguments == {"case_id": "abc123", "event_id": 4624}

    def test_extracts_multiple_tool_calls_from_one_chunk(self) -> None:
        result = collect_chat_response(MULTI_TOOL_CALL_STREAM)
        names = {tc.name for tc in result.tool_calls}
        assert names == {"case.get_metadata", "mem.list_processes"}

    def test_empty_chunk_list_returns_empty_incomplete_result(self) -> None:
        result = collect_chat_response([])
        assert result.content == ""
        assert result.done is False
        assert result.tool_calls == []

    def test_tool_call_missing_function_name_is_skipped_not_crashed(self) -> None:
        malformed = [
            {
                "message": {"role": "assistant", "tool_calls": [{"function": {"arguments": {}}}]},
                "done": True,
            }
        ]
        result = collect_chat_response(malformed)
        assert result.tool_calls == []  # skipped, no exception

    def test_chunk_with_no_message_key_does_not_crash(self) -> None:
        result = collect_chat_response([{"done": True}])
        assert result.done is True
        assert result.content == ""


class TestBuildToolResultMessage:
    def test_builds_correct_ollama_tool_message_shape(self) -> None:
        msg = build_tool_result_message("case.get_metadata", '{"name": "Test Case"}')
        assert msg == {
            "role": "tool",
            "tool_name": "case.get_metadata",
            "content": '{"name": "Test Case"}',
        }


def _mock_transport(ndjson_lines: list[dict], status_code: int = 200) -> httpx.MockTransport:
    body = "\n".join(json.dumps(chunk) for chunk in ndjson_lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return httpx.MockTransport(handler)


class TestOllamaClientStreaming:
    @pytest.mark.asyncio
    async def test_chat_yields_parsed_ndjson_chunks_in_order(self) -> None:
        transport = _mock_transport(PLAIN_TEXT_STREAM)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11434")
        client = OllamaClient(
            host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client
        )

        chunks = [c async for c in client.chat(messages=[{"role": "user", "content": "hi"}])]

        assert len(chunks) == len(PLAIN_TEXT_STREAM)
        assert chunks[0]["message"]["content"] == "The "
        assert chunks[-1]["done"] is True
        await client.aclose()

    @pytest.mark.asyncio
    async def test_chat_with_tools_sends_tools_in_request_body(self) -> None:
        captured_request: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["body"] = json.loads(request.content)
            body = "\n".join(json.dumps(c) for c in TOOL_CALL_STREAM).encode()
            return httpx.Response(200, content=body)

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11434")
        client = OllamaClient(
            host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client
        )

        tools = [{"type": "function", "function": {"name": "evtx.query_events", "parameters": {}}}]
        _ = [
            c async for c in client.chat(messages=[{"role": "user", "content": "hi"}], tools=tools)
        ]

        assert captured_request["body"]["tools"] == tools
        assert captured_request["body"]["model"] == "llama3.1"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_connection_error_raises_ollama_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused", request=request)

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11434")
        client = OllamaClient(
            host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client
        )

        with pytest.raises(OllamaConnectionError):
            _ = [c async for c in client.chat(messages=[{"role": "user", "content": "hi"}])]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_end_to_end_stream_then_collect_matches_direct_collect(self) -> None:
        transport = _mock_transport(TOOL_CALL_STREAM)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:11434")
        client = OllamaClient(
            host="http://127.0.0.1:11434", model="llama3.1", http_client=http_client
        )

        chunks = [c async for c in client.chat(messages=[{"role": "user", "content": "hi"}])]
        result = collect_chat_response(chunks)

        assert result.tool_calls[0].name == "evtx.query_events"
        await client.aclose()
