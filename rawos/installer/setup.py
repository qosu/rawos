"""rawos/installer/setup.py — SetupWizard for Milestone 5 Step 4."""
from __future__ import annotations

import secrets
from pathlib import Path

from rawos.kernel.arch.linux import LinuxServiceManager

_JWT_SECRET_BYTES = 32


class SetupWizard:
    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    def create_dirs(self) -> None:
        (self._base / "workspaces").mkdir(parents=True, exist_ok=True)
        (self._base / "logs").mkdir(parents=True, exist_ok=True)
        (self._base / "data").mkdir(parents=True, exist_ok=True)
        (self._base / "data" / "chroma").mkdir(parents=True, exist_ok=True)
        (self._base / "worktrees").mkdir(parents=True, exist_ok=True)

    def write_env(
        self,
        *,
        llm_api_key: str,
        llm_agent_model: str,
        llm_summarizer_model: str,
        telegram_owner_email: str,
        llm_base_url: str = "https://api.deepseek.com/v1",
        llm_fallback_model: str = "",
        llm_timeout_s: int = 120,
        telegram_enabled: bool = False,
        telegram_bot_token: str = "",
        telegram_owner_chat_id: int = 0,
        port: int = 8002,
        source_root: str | None = None,
        force: bool = False,
    ) -> None:
        env_path = self._base / ".env"
        if env_path.exists() and not force:
            raise FileExistsError(f"{env_path} already exists; pass force=True to overwrite")

        base = str(self._base)
        resolved_source_root = source_root if source_root is not None else base

        lines = [
            f"JWT_SECRET={secrets.token_urlsafe(_JWT_SECRET_BYTES)}",
            f"PORT={port}",
            f"DB_PATH={base}/data/rawos.db",
            f"CHROMA_PATH={base}/data/chroma",
            f"WORKSPACES_ROOT={base}/workspaces",
            f"WORKTREE_ROOT={base}/worktrees",
            f"RAWOS_SOURCE_ROOT={resolved_source_root}",
            f"LLM_API_KEY={llm_api_key}",
            f"LLM_BASE_URL={llm_base_url}",
            f"LLM_AGENT_MODEL={llm_agent_model}",
            f"LLM_SUMMARIZER_MODEL={llm_summarizer_model}",
            f"LLM_FALLBACK_MODEL={llm_fallback_model}",
            f"LLM_TIMEOUT_S={llm_timeout_s}",
            f"TELEGRAM_ENABLED={telegram_enabled}",
            f"TELEGRAM_BOT_TOKEN={telegram_bot_token}",
            f"TELEGRAM_OWNER_CHAT_ID={telegram_owner_chat_id}",
            f"TELEGRAM_OWNER_EMAIL={telegram_owner_email}",
        ]
        env_path.write_text("\n".join(lines) + "\n")

    def generate_service(self, exec_start: str, name: str = "rawos") -> str:
        mgr = LinuxServiceManager()
        return mgr.generate_unit(
            name=name,
            exec_start=exec_start,
            working_dir=str(self._base),
            env_file=str(self._base / ".env"),
        )
