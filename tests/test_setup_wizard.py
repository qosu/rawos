"""tests/test_setup_wizard.py — TDD for rawos setup wizard (Milestone 5 Step 4)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from rawos.cli.main import cli
from rawos.config import Settings


def _runner() -> CliRunner:
    return CliRunner()


_REQUIRED_ENV_KWARGS = dict(
    llm_api_key="sk-test",
    llm_agent_model="agent-model",
    llm_summarizer_model="summarizer-model",
    telegram_owner_email="owner@example.com",
)


# ---------------------------------------------------------------------------
# SetupWizard unit tests
# ---------------------------------------------------------------------------


class TestSetupWizardCreateDirs:
    def test_creates_workspaces_dir(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()
            assert Path(root, "workspaces").is_dir()

    def test_creates_logs_dir(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()
            assert Path(root, "logs").is_dir()

    def test_creates_data_and_worktree_dirs(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()
            assert Path(root, "data").is_dir()
            assert Path(root, "data", "chroma").is_dir()
            assert Path(root, "worktrees").is_dir()

    def test_idempotent_if_dirs_already_exist(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            Path(root, "workspaces").mkdir()
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()  # must not raise
            assert Path(root, "workspaces").is_dir()


class TestSetupWizardWriteEnv:
    def test_writes_llm_fields(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS)
            content = Path(root, ".env").read_text()
            assert "LLM_API_KEY=sk-test" in content
            assert "LLM_AGENT_MODEL=agent-model" in content
            assert "LLM_SUMMARIZER_MODEL=summarizer-model" in content
            assert "LLM_BASE_URL=https://api.deepseek.com/v1" in content
            assert "LLM_FALLBACK_MODEL=" in content
            assert "LLM_TIMEOUT_S=120" in content

    def test_writes_telegram_owner_email(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS)
            content = Path(root, ".env").read_text()
            assert "TELEGRAM_OWNER_EMAIL=owner@example.com" in content

    def test_generates_random_jwt_secret_not_default(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS)
            content = Path(root, ".env").read_text()
            secret_line = next(l for l in content.splitlines() if l.startswith("JWT_SECRET="))
            secret = secret_line.split("=", 1)[1]
            assert secret != "CHANGE_ME_IN_PRODUCTION"
            assert len(secret) >= 32

    def test_remaps_data_paths_under_base_dir(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS)
            content = Path(root, ".env").read_text()
            assert f"DB_PATH={root}/data/rawos.db" in content
            assert f"CHROMA_PATH={root}/data/chroma" in content
            assert f"WORKSPACES_ROOT={root}/workspaces" in content
            assert f"WORKTREE_ROOT={root}/worktrees" in content

    def test_source_root_defaults_to_base_dir(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS)
            content = Path(root, ".env").read_text()
            assert f"RAWOS_SOURCE_ROOT={root}" in content

    def test_source_root_override(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS, source_root="/opt/rawos-src")
            content = Path(root, ".env").read_text()
            assert "RAWOS_SOURCE_ROOT=/opt/rawos-src" in content

    def test_does_not_overwrite_existing_env_without_force(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            env_path = Path(root, ".env")
            env_path.write_text("EXISTING=value\n")
            wizard = SetupWizard(base_dir=root)
            with pytest.raises(FileExistsError):
                wizard.write_env(**_REQUIRED_ENV_KWARGS)

    def test_overwrites_existing_env_with_force(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            env_path = Path(root, ".env")
            env_path.write_text("EXISTING=value\n")
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(**_REQUIRED_ENV_KWARGS, force=True)
            content = env_path.read_text()
            assert "LLM_API_KEY=sk-test" in content
            assert "EXISTING" not in content


class TestSetupWizardGenerateService:
    def test_generate_service_delegates_to_linux_service_manager(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            mock_mgr = MagicMock()
            mock_mgr.generate_unit.return_value = "[Unit]\n"
            with patch("rawos.installer.setup.LinuxServiceManager", return_value=mock_mgr):
                content = wizard.generate_service(
                    exec_start="/venv/bin/uvicorn rawos.api.app:app",
                )
            mock_mgr.generate_unit.assert_called_once()
            assert content == "[Unit]\n"

    def test_generate_service_passes_env_file_from_base_dir(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            mock_mgr = MagicMock()
            mock_mgr.generate_unit.return_value = ""
            with patch("rawos.installer.setup.LinuxServiceManager", return_value=mock_mgr):
                wizard.generate_service(exec_start="/bin/uvicorn")
            _, kwargs = mock_mgr.generate_unit.call_args
            assert kwargs.get("env_file") == str(Path(root, ".env"))


# ---------------------------------------------------------------------------
# End-to-end: the .env written by the wizard must produce a correct,
# non-default Settings instance for a fresh install at an arbitrary path.
# ---------------------------------------------------------------------------


class TestSetupWizardEndToEndSettings:
    # Several test modules set process-wide os.environ overrides for
    # DB_PATH/WORKSPACES_ROOT/JWT_SECRET/etc. at import time and never
    # restore them. monkeypatch.delenv isolates this test from that
    # pollution so it verifies the .env file's own values, not leftovers
    # from another test module's tempdir.
    _ENV_KEYS_TO_ISOLATE = (
        "LLM_API_KEY", "LLM_AGENT_MODEL", "LLM_SUMMARIZER_MODEL", "LLM_BASE_URL",
        "LLM_FALLBACK_MODEL", "LLM_TIMEOUT_S", "JWT_SECRET", "PORT",
        "TELEGRAM_OWNER_EMAIL", "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID", "DB_PATH", "CHROMA_PATH", "WORKSPACES_ROOT",
        "WORKTREE_ROOT", "RAWOS_SOURCE_ROOT",
    )

    def test_fresh_install_settings_are_complete_and_secure(self, monkeypatch):
        from rawos.installer.setup import SetupWizard

        for key in self._ENV_KEYS_TO_ISOLATE:
            monkeypatch.delenv(key, raising=False)

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()
            wizard.write_env(**_REQUIRED_ENV_KWARGS, port=9999)

            settings = Settings(_env_file=str(Path(root, ".env")))

            assert settings.llm_api_key == "sk-test"
            assert settings.llm_agent_model == "agent-model"
            assert settings.llm_summarizer_model == "summarizer-model"
            assert settings.jwt_secret != "CHANGE_ME_IN_PRODUCTION"
            assert settings.port == 9999
            assert settings.telegram_owner_email == "owner@example.com"
            assert settings.db_path == f"{root}/data/rawos.db"
            assert settings.chroma_path == f"{root}/data/chroma"
            assert settings.workspaces_root == f"{root}/workspaces"
            assert settings.worktree_root == f"{root}/worktrees"
            assert settings.rawos_source_root == root


# ---------------------------------------------------------------------------
# rawos setup CLI tests
# ---------------------------------------------------------------------------


class TestSetupCLI:
    def test_setup_command_exists(self):
        runner = _runner()
        result = runner.invoke(cli, ["setup", "--help"])
        assert result.exit_code == 0

    def test_setup_invokes_wizard_create_dirs(self):
        runner = _runner()
        mock_wizard = MagicMock()
        mock_wizard.generate_service.return_value = "[Unit]\n"
        with patch("rawos.cli.main.SetupWizard", return_value=mock_wizard):
            result = runner.invoke(
                cli,
                [
                    "setup",
                    "--base-dir", "/tmp/rawos-test",
                    "--llm-api-key", "sk-x",
                    "--llm-agent-model", "agent-model",
                    "--llm-summarizer-model", "summarizer-model",
                    "--telegram-owner-email", "owner@example.com",
                    "--exec-start", "/venv/bin/uvicorn",
                    "--no-service",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_wizard.create_dirs.assert_called_once()

    def test_setup_invokes_wizard_write_env(self):
        runner = _runner()
        mock_wizard = MagicMock()
        mock_wizard.generate_service.return_value = "[Unit]\n"
        with patch("rawos.cli.main.SetupWizard", return_value=mock_wizard):
            result = runner.invoke(
                cli,
                [
                    "setup",
                    "--base-dir", "/tmp/rawos-test",
                    "--llm-api-key", "sk-x",
                    "--llm-agent-model", "agent-model",
                    "--llm-summarizer-model", "summarizer-model",
                    "--telegram-owner-email", "owner@example.com",
                    "--exec-start", "/venv/bin/uvicorn",
                    "--no-service",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_wizard.write_env.assert_called_once()
        _, kwargs = mock_wizard.write_env.call_args
        assert kwargs["llm_api_key"] == "sk-x"
        assert kwargs["llm_agent_model"] == "agent-model"
        assert kwargs["llm_summarizer_model"] == "summarizer-model"
        assert kwargs["telegram_owner_email"] == "owner@example.com"

    def test_setup_with_service_installs_unit(self):
        runner = _runner()
        mock_wizard = MagicMock()
        mock_wizard.generate_service.return_value = "[Unit]\nDescription=rawos\n"
        mock_mgr = MagicMock()
        with (
            patch("rawos.cli.main.SetupWizard", return_value=mock_wizard),
            patch("rawos.cli.main.LinuxServiceManager", return_value=mock_mgr),
        ):
            with tempfile.TemporaryDirectory() as unit_dir:
                result = runner.invoke(
                    cli,
                    [
                        "setup",
                        "--base-dir", "/tmp/rawos-test",
                        "--llm-api-key", "sk-x",
                        "--llm-agent-model", "agent-model",
                        "--llm-summarizer-model", "summarizer-model",
                        "--telegram-owner-email", "owner@example.com",
                        "--exec-start", "/venv/bin/uvicorn",
                        "--unit-dir", unit_dir,
                    ],
                )
        assert result.exit_code == 0, result.output
        mock_mgr.install_unit.assert_called_once()
