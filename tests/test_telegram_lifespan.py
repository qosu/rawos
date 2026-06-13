"""tests/test_telegram_lifespan.py — TDD for Telegram gate app lifespan integration."""
from __future__ import annotations

import hashlib
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTelegramLifespan:
    @pytest.mark.asyncio
    async def test_telegram_gate_not_instantiated_when_disabled(self, monkeypatch):
        """If telegram_enabled=False, TelegramGate.__init__ must never be called."""
        from rawos.config import settings as _settings
        monkeypatch.setattr(_settings, "telegram_enabled", False)

        with patch("rawos.api.app.TelegramGate") as mock_cls:
            from rawos.api.app import _start_telegram_gate
            await _start_telegram_gate()
        mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_telegram_gate_started_when_enabled(self, monkeypatch):
        """If telegram_enabled=True and token/chat_id set, TelegramGate.start() is called."""
        from rawos.config import settings as _settings
        monkeypatch.setattr(_settings, "telegram_enabled", True)
        monkeypatch.setattr(_settings, "telegram_bot_token", "fake-token")
        monkeypatch.setattr(_settings, "telegram_owner_chat_id", 12345)
        monkeypatch.setattr(_settings, "telegram_owner_email", "owner@test.com")
        monkeypatch.setattr(_settings, "telegram_project_id", "")

        mock_gate = MagicMock()
        mock_gate.start = AsyncMock()

        with patch("rawos.api.app.TelegramGate", return_value=mock_gate) as mock_cls:
            from rawos.api.app import _start_telegram_gate
            result = await _start_telegram_gate()

        mock_cls.assert_called_once_with(
            bot_token="fake-token",
            owner_chat_id=12345,
            owner_email="owner@test.com",
            project_id="",
        )
        mock_gate.start.assert_called_once()
        assert result is mock_gate

    @pytest.mark.asyncio
    async def test_telegram_gate_not_started_when_token_missing(self, monkeypatch):
        """telegram_enabled=True but token empty → gate not started (misconfiguration)."""
        from rawos.config import settings as _settings
        monkeypatch.setattr(_settings, "telegram_enabled", True)
        monkeypatch.setattr(_settings, "telegram_bot_token", "")

        with patch("rawos.api.app.TelegramGate") as mock_cls:
            from rawos.api.app import _start_telegram_gate
            result = await _start_telegram_gate()

        mock_cls.assert_not_called()
        assert result is None
