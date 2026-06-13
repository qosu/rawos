CHANGED: rawos/kernel/telegram_gate.py (NEW — TelegramGate: auth gate, text+voice dispatch, OpenAI Whisper STT, orchestrator integration).
CHANGED: rawos/config.py (+5 telegram_* fields: enabled, bot_token, owner_chat_id, owner_email, project_id).
CHANGED: rawos/api/app.py (+TelegramGate import, +_start_telegram_gate fn, lifespan startup+shutdown wired).
CHANGED: tests/test_telegram_gate.py (NEW — 13 tests: config, auth, text dispatch, _run_turn, voice pipeline, _resolve_owner).
CHANGED: tests/test_telegram_lifespan.py (NEW — 3 tests: disabled/enabled/token-missing).
WHY: Milestone 4 (The window) — Telegram phone client talking to the being. 648/648 pass.
NEXT: To activate: set TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_CHAT_ID + TELEGRAM_OWNER_EMAIL + TELEGRAM_ENABLED=true in env. Restart rawos.service.
