"""Tests for rawos.kernel.llm_client — unified OpenAI-compatible LLM client."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rawos.kernel import llm_client


def _mock_async_client(resp):
    """Build a mock httpx.AsyncClient context manager returning `resp` from .post()."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestComplete:
    @pytest.mark.asyncio
    async def test_returns_content_and_usage(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "test-key")
        monkeypatch.setattr(llm_client.settings, "llm_base_url", "https://api.example.com/v1")
        monkeypatch.setattr(llm_client.settings, "llm_timeout_s", 120)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": {"content": "hello world"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

        with patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
            content, usage = await llm_client.complete(
                [{"role": "user", "content": "hi"}],
                model="test-model",
                max_tokens=100,
            )

        assert content == "hello world"
        assert usage == {"prompt_tokens": 10, "completion_tokens": 2}

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_non_200(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "test-key")
        monkeypatch.setattr(llm_client.settings, "llm_base_url", "https://api.example.com/v1")
        monkeypatch.setattr(llm_client.settings, "llm_timeout_s", 120)

        resp = MagicMock()
        resp.status_code = 500
        resp.text = "internal error"

        with patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
            with pytest.raises(RuntimeError):
                await llm_client.complete(
                    [{"role": "user", "content": "hi"}],
                    model="test-model",
                    max_tokens=100,
                )

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_api_key_missing(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "")

        with pytest.raises(RuntimeError):
            await llm_client.complete(
                [{"role": "user", "content": "hi"}],
                model="test-model",
                max_tokens=100,
            )


class TestToolCall:
    @pytest.mark.asyncio
    async def test_returns_message_and_usage(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "test-key")
        monkeypatch.setattr(llm_client.settings, "llm_base_url", "https://api.example.com/v1")
        monkeypatch.setattr(llm_client.settings, "llm_timeout_s", 120)

        message = {"role": "assistant", "tool_calls": [{"id": "1", "type": "function"}]}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }

        with patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
            result_message, usage = await llm_client.tool_call(
                [{"role": "user", "content": "do something"}],
                tools=[{"type": "function", "function": {"name": "noop"}}],
                model="test-model",
            )

        assert result_message == message
        assert usage == {"prompt_tokens": 5, "completion_tokens": 1}

    @pytest.mark.asyncio
    async def test_raises_runtime_error_on_non_200(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "test-key")
        monkeypatch.setattr(llm_client.settings, "llm_base_url", "https://api.example.com/v1")
        monkeypatch.setattr(llm_client.settings, "llm_timeout_s", 120)

        resp = MagicMock()
        resp.status_code = 503
        resp.text = "unavailable"

        with patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
            with pytest.raises(RuntimeError):
                await llm_client.tool_call(
                    [{"role": "user", "content": "do something"}],
                    tools=[],
                    model="test-model",
                )

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_api_key_missing(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "")

        with pytest.raises(RuntimeError):
            await llm_client.tool_call(
                [{"role": "user", "content": "do something"}],
                tools=[],
                model="test-model",
            )


def _mock_stream_client(resp):
    """Build a mock httpx.AsyncClient context manager whose .stream() yields `resp`."""
    client = AsyncMock()
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=resp)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_cm)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestStreamFinal:
    @pytest.mark.asyncio
    async def test_yields_text_chunks(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "test-key")
        monkeypatch.setattr(llm_client.settings, "llm_base_url", "https://api.example.com/v1")
        monkeypatch.setattr(llm_client.settings, "llm_timeout_s", 120)

        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
            "data: [DONE]",
        ]

        async def aiter_lines():
            for line in lines:
                yield line

        resp = MagicMock()
        resp.status_code = 200
        resp.aiter_lines = aiter_lines

        with patch("httpx.AsyncClient", return_value=_mock_stream_client(resp)):
            chunks = [
                chunk
                async for chunk in llm_client.stream_final(
                    [{"role": "user", "content": "hi"}],
                    model="test-model",
                )
            ]

        assert chunks == ["Hel", "lo"]

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_api_key_missing(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_api_key", "")

        with pytest.raises(RuntimeError):
            async for _ in llm_client.stream_final(
                [{"role": "user", "content": "hi"}],
                model="test-model",
            ):
                pass
