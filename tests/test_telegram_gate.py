"""tests/test_telegram_gate.py — TDD for TelegramGate (Milestone 4: The window).

Covers:
- Config fields for Telegram
- Auth: non-owner chat_id rejected silently
- Text message dispatch to _run_turn
- Voice message: download → _transcribe_voice → _run_turn
- _run_turn collects orchestrator chunks → returns joined text
- Response sent back via reply_text
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — fake Telegram Update objects
# ---------------------------------------------------------------------------

def _make_text_update(chat_id: int, text: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    msg = MagicMock()
    msg.text = text
    msg.voice = None
    msg.reply_text = AsyncMock(return_value=None)
    update.effective_message = msg
    return update


def _make_voice_update(chat_id: int, file_id: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    msg = MagicMock()
    msg.text = None
    voice = MagicMock()
    voice.file_id = file_id
    msg.voice = voice
    msg.reply_text = AsyncMock(return_value=None)
    update.effective_message = msg
    return update


# ---------------------------------------------------------------------------
# Test 1 — Config fields
# ---------------------------------------------------------------------------

class TestTelegramConfig:
    def test_config_telegram_fields_exist(self):
        from rawos.config import Settings
        s = Settings()
        assert hasattr(s, "telegram_bot_token")
        assert hasattr(s, "telegram_owner_chat_id")
        assert hasattr(s, "telegram_owner_email")
        assert hasattr(s, "telegram_enabled")
        assert hasattr(s, "telegram_project_id")

    def test_config_telegram_defaults(self):
        from rawos.config import Settings
        s = Settings()
        assert s.telegram_enabled is False
        assert s.telegram_bot_token == ""
        assert s.telegram_owner_chat_id == 0
        assert s.telegram_owner_email == ""
        assert s.telegram_project_id == ""


# ---------------------------------------------------------------------------
# Test 2 — Auth: non-owner rejected
# ---------------------------------------------------------------------------

class TestTelegramGateAuth:
    @pytest.mark.asyncio
    async def test_non_owner_chat_id_silently_rejected(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate(
            bot_token="token",
            owner_chat_id=12345,
            owner_email="owner@test.com",
            project_id="proj-1",
        )
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        run_turn_mock = AsyncMock(return_value="response")
        gate._run_turn = run_turn_mock

        update = _make_text_update(chat_id=99999, text="hack attempt")
        await gate.handle_message(update, MagicMock())

        run_turn_mock.assert_not_called()
        update.effective_message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_chat_id_accepted(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate(
            bot_token="token",
            owner_chat_id=12345,
            owner_email="owner@test.com",
            project_id="proj-1",
        )
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        gate._run_turn = AsyncMock(return_value="hello from rawos")

        update = _make_text_update(chat_id=12345, text="hi")
        await gate.handle_message(update, MagicMock())

        gate._run_turn.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3 — Text dispatch
# ---------------------------------------------------------------------------

class TestTelegramGateTextDispatch:
    @pytest.mark.asyncio
    async def test_text_dispatched_to_run_turn_with_correct_args(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        gate._run_turn = AsyncMock(return_value="response text")

        update = _make_text_update(chat_id=12345, text="what is rawos?")
        await gate.handle_message(update, MagicMock())

        gate._run_turn.assert_called_once_with(
            "user-1", "proj-1", "what is rawos?", "/tmp/workdir"
        )

    @pytest.mark.asyncio
    async def test_response_sent_via_reply_text(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        gate._run_turn = AsyncMock(return_value="rawos response here")

        update = _make_text_update(chat_id=12345, text="hello")
        await gate.handle_message(update, MagicMock())

        update.effective_message.reply_text.assert_called_once_with("rawos response here")

    @pytest.mark.asyncio
    async def test_empty_response_sends_fallback(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        gate._run_turn = AsyncMock(return_value="")

        update = _make_text_update(chat_id=12345, text="test")
        await gate.handle_message(update, MagicMock())

        update.effective_message.reply_text.assert_called_once_with("(no response)")


# ---------------------------------------------------------------------------
# Test 4 — _run_turn collects orchestrator chunks
# ---------------------------------------------------------------------------

class TestRunTurn:
    @pytest.mark.asyncio
    async def test_run_turn_collects_chunks(self, monkeypatch):
        import rawos.db as db
        tmp = tempfile.mkdtemp()
        db.init(os.path.join(tmp, "test.db"))

        async def mock_orch_run(*, user_id, project_id, intent_id, messages,
                                 workdir, model, on_artifact=None, system_prompt=None):
            yield {"type": "chunk", "text": "hello "}
            yield {"type": "chunk", "text": "world"}
            yield {"type": "run_done"}

        monkeypatch.setattr("rawos.kernel.orchestrator.run", mock_orch_run)
        monkeypatch.setattr(
            "rawos.kernel.context_builder.build_context",
            lambda user_id, project_id, text: ([], ""),
        )

        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        result = await gate._run_turn("user-1", "proj-1", "query text", "/tmp/w")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_run_turn_ignores_non_chunk_events(self, monkeypatch):
        import rawos.db as db
        tmp = tempfile.mkdtemp()
        db.init(os.path.join(tmp, "test.db"))

        async def mock_orch_run(*, user_id, project_id, intent_id, messages,
                                 workdir, model, on_artifact=None, system_prompt=None):
            yield {"type": "tool_call", "name": "bash", "input": {}}
            yield {"type": "chunk", "text": "answer"}
            yield {"type": "run_done"}

        monkeypatch.setattr("rawos.kernel.orchestrator.run", mock_orch_run)
        monkeypatch.setattr(
            "rawos.kernel.context_builder.build_context",
            lambda user_id, project_id, text: ([], ""),
        )

        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        result = await gate._run_turn("user-1", "proj-1", "q", "/tmp/w")
        assert result == "answer"


# ---------------------------------------------------------------------------
# Test 5 — Voice pipeline
# ---------------------------------------------------------------------------

class TestVoicePipeline:
    @pytest.mark.asyncio
    async def test_voice_message_transcribed_then_dispatched(self):
        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        gate._user_id = "user-1"
        gate._resolved_project_id = "proj-1"
        gate._workdir = "/tmp/workdir"

        gate._transcribe_voice = AsyncMock(return_value="transcribed speech text")
        gate._run_turn = AsyncMock(return_value="voice response")

        mock_file = MagicMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x00\x01ogg_data"))

        ctx = MagicMock()
        ctx.bot.get_file = AsyncMock(return_value=mock_file)

        update = _make_voice_update(chat_id=12345, file_id="file-abc")
        await gate.handle_message(update, ctx)

        gate._transcribe_voice.assert_called_once_with(b"\x00\x01ogg_data")
        gate._run_turn.assert_called_once_with(
            "user-1", "proj-1", "transcribed speech text", "/tmp/workdir"
        )
        update.effective_message.reply_text.assert_called_once_with("voice response")

    @pytest.mark.asyncio
    async def test_transcribe_voice_calls_openai_whisper(self, monkeypatch):
        """_transcribe_voice sends bytes to OpenAI Whisper API."""
        mock_transcription = MagicMock()
        mock_transcription.text = "  hello from voice  "
        mock_client = MagicMock()
        mock_client.audio = MagicMock()
        mock_client.audio.transcriptions = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_transcription)

        monkeypatch.setattr(
            "rawos.kernel.telegram_gate.AsyncOpenAI",
            lambda: mock_client,
        )

        from rawos.kernel.telegram_gate import TelegramGate
        gate = TelegramGate("token", 12345, "owner@test.com", "proj-1")
        result = await gate._transcribe_voice(b"ogg_bytes_here")
        assert result == "hello from voice"
        assert mock_client.audio.transcriptions.create.called


# ---------------------------------------------------------------------------
# Test 6 — _resolve_owner
# ---------------------------------------------------------------------------

class TestResolveOwner:
    @pytest.mark.asyncio
    async def test_resolve_owner_raises_if_user_not_found(self, monkeypatch):
        monkeypatch.setattr("rawos.db.get_user_by_email", lambda email: None)
        from rawos.kernel.telegram_gate import TelegramGate, TelegramGateError
        gate = TelegramGate("token", 12345, "missing@test.com", "")
        with pytest.raises(TelegramGateError):
            await gate._resolve_owner()

    @pytest.mark.asyncio
    async def test_resolve_owner_raises_if_project_not_found(self, monkeypatch):
        user = MagicMock()
        user.id = "user-1"
        monkeypatch.setattr("rawos.db.get_user_by_email", lambda email: user)
        monkeypatch.setattr("rawos.db.get_project", lambda uid, pid: None)
        from rawos.kernel.telegram_gate import TelegramGate, TelegramGateError
        gate = TelegramGate("token", 12345, "owner@test.com", "nonexistent-proj")
        with pytest.raises(TelegramGateError):
            await gate._resolve_owner()
