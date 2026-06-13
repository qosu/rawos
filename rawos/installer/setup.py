"""rawos/installer/setup.py — SetupWizard for Milestone 5 Step 4."""
from __future__ import annotations

import os
from pathlib import Path

from rawos.kernel.arch.linux import LinuxServiceManager


class SetupWizard:
    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    def create_dirs(self) -> None:
        (self._base / "workspaces").mkdir(parents=True, exist_ok=True)
        (self._base / "logs").mkdir(parents=True, exist_ok=True)

    def write_env(
        self,
        openai_api_key: str,
        telegram_token: str,
        telegram_owner_id: str,
        port: int = 8002,
        force: bool = False,
    ) -> None:
        env_path = self._base / ".env"
        if env_path.exists() and not force:
            raise FileExistsError(f"{env_path} already exists; pass force=True to overwrite")
        lines = [
            f"OPENAI_API_KEY={openai_api_key}",
            f"TELEGRAM_BOT_TOKEN={telegram_token}",
            f"TELEGRAM_OWNER_ID={telegram_owner_id}",
            f"RAWOS_PORT={port}",
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
