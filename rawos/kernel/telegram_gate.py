"""rawos/kernel/telegram_gate.py — Milestone 4: The window.

Telegram front-door to the rawos being. Single-owner: only the configured
chat_id is accepted; all other senders are silently ignored.

Polling mode (no webhook/TLS required).

Voice pipeline: OGG → OpenAI Whisper STT → text → orchestrator → response.
"""
from __future__ import annotations

import io
import logging
import os
import uuid
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import CallbackContext

logger = logging.getLogger(__name__)


class TelegramGateError(Exception):
    """Fatal configuration or runtime error in TelegramGate."""


class TelegramGate:
    """Connects a Telegram bot to the rawos being via direct orchestrator calls.

    Contract:
    - Only messages from `owner_chat_id` are processed; all others silently dropped.
    - Text messages and voice messages (OGG) are supported.
    - Voice → Whisper STT → orchestrator → text reply.
    - `_user_id`, `_resolved_project_id`, `_workdir` are resolved once on first
      owner message (lazy init) via `_resolve_owner()`.
    - `start()` / `stop()` manage the polling lifecycle.
    """

    def __init__(
        self,
        bot_token: str,
        owner_chat_id: int,
        owner_email: str,
        project_id: str,
    ) -> None:
        self._bot_token = bot_token
        self._owner_chat_id = owner_chat_id
        self._owner_email = owner_email
        self._project_id = project_id

        self._app: object | None = None
        self._user_id: str | None = None
        self._resolved_project_id: str | None = None
        self._workdir: str | None = None

    # ------------------------------------------------------------------
    # Owner resolution (lazy, idempotent)
    # ------------------------------------------------------------------

    async def _resolve_owner(self) -> tuple[str, str, str]:
        """Return (user_id, project_id, workdir). Raises TelegramGateError on failure."""
        import rawos.db as db
        from rawos.models import Project

        user = db.get_user_by_email(self._owner_email)
        if user is None:
            raise TelegramGateError(
                f"Telegram owner email not found in DB: {self._owner_email!r}"
            )

        if self._project_id:
            project = db.get_project(user.id, self._project_id)
            if project is None:
                raise TelegramGateError(
                    f"Telegram project not found: {self._project_id!r} for user {user.id!r}"
                )
        else:
            # Auto: find or create a project named "telegram"
            projects = db.get_projects(user.id)
            telegram_projects = [p for p in projects if p.name == "telegram"]
            if telegram_projects:
                project = telegram_projects[0]
            else:
                workdir = f"/root/rawos-telegram-{user.id[:8]}"
                os.makedirs(workdir, exist_ok=True)
                project = db.create_project(Project(
                    user_id=user.id,
                    name="telegram",
                    description="Telegram front-door workspace",
                    workdir=workdir,
                ))

        return user.id, project.id, project.workdir

    # ------------------------------------------------------------------
    # Voice transcription
    # ------------------------------------------------------------------

    async def _transcribe_voice(self, ogg_bytes: bytes) -> str:
        """Transcribe OGG voice bytes via OpenAI Whisper API. Returns stripped text."""
        client = AsyncOpenAI()
        audio_file = io.BytesIO(ogg_bytes)
        audio_file.name = "voice.ogg"
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="text",
        )
        # API returns plain str when response_format="text"
        return transcript.strip() if isinstance(transcript, str) else transcript.text.strip()

    # ------------------------------------------------------------------
    # Agent turn
    # ------------------------------------------------------------------

    async def _run_turn(
        self,
        user_id: str,
        project_id: str,
        raw_message: str,
        workdir: str,
    ) -> str:
        """Run one agent turn: context → orchestrator → collect chunks → return text."""
        from rawos.config import settings
        from rawos.kernel import context_builder, orchestrator
        from rawos.kernel.billing_context import set_billing_context

        intent_id = str(uuid.uuid4())
        model = settings.deepseek_model_pro

        messages, system_ctx = context_builder.build_context(user_id, project_id, raw_message)
        # context_builder appends the user message; remove if already there to avoid duplication
        if messages and messages[-1]["role"] == "user" and messages[-1]["content"] == raw_message:
            messages = messages[:-1]
        messages.append({"role": "user", "content": raw_message})

        from rawos.kernel.agent_loop import _SYSTEM_PROMPT as BASE_PROMPT
        enriched_system = BASE_PROMPT + system_ctx if system_ctx else None

        chunks: list[str] = []
        with set_billing_context(user_id=user_id, intent_id=intent_id, event_type="telegram"):
            async for event in orchestrator.run(
                user_id=user_id,
                project_id=project_id,
                intent_id=intent_id,
                messages=messages,
                workdir=workdir,
                model=model,
                on_artifact=None,
                system_prompt=enriched_system,
            ):
                if event.get("type") == "chunk":
                    chunks.append(event["text"])

        return "".join(chunks)

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    async def handle_message(self, update: "Update", context: "CallbackContext") -> None:
        """Handle one incoming Telegram update. Silently drop non-owner messages."""
        if update.effective_chat.id != self._owner_chat_id:
            return

        # Lazy-resolve owner on first message
        if self._user_id is None:
            try:
                self._user_id, self._resolved_project_id, self._workdir = (
                    await self._resolve_owner()
                )
            except TelegramGateError as exc:
                logger.error("TelegramGate: owner resolution failed: %s", exc)
                await update.effective_message.reply_text(
                    f"[rawos] configuration error: {exc}"
                )
                return

        message = update.effective_message

        if message.voice:
            try:
                voice_file = await context.bot.get_file(message.voice.file_id)
                ogg_bytes = bytes(await voice_file.download_as_bytearray())
                text = await self._transcribe_voice(ogg_bytes)
            except Exception as exc:
                logger.error("TelegramGate: voice transcription failed: %s", exc)
                await message.reply_text("[rawos] voice transcription failed")
                return
        elif message.text:
            text = message.text
        else:
            return  # ignore unsupported message types silently

        try:
            response = await self._run_turn(
                self._user_id,
                self._resolved_project_id,
                text,
                self._workdir,
            )
        except Exception as exc:
            logger.error("TelegramGate: agent turn failed: %s", exc)
            await message.reply_text(f"[rawos] error: {exc}")
            return

        await message.reply_text(response or "(no response)")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build Application and begin polling Telegram API."""
        from telegram.ext import Application, MessageHandler, filters

        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT | filters.VOICE, self.handle_message)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("TelegramGate: polling started (owner_chat_id=%d)", self._owner_chat_id)

    async def stop(self) -> None:
        """Gracefully stop the Telegram polling loop."""
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None
        logger.info("TelegramGate: stopped")
