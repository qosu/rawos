"""tests/test_setup_wizard.py — TDD for rawos setup wizard (Milestone 5 Step 4)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from rawos.cli.main import cli


def _runner() -> CliRunner:
    return CliRunner()


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

    def test_idempotent_if_dirs_already_exist(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            Path(root, "workspaces").mkdir()
            wizard = SetupWizard(base_dir=root)
            wizard.create_dirs()  # must not raise
            assert Path(root, "workspaces").is_dir()


class TestSetupWizardWriteEnv:
    def test_writes_env_file_with_required_keys(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(
                openai_api_key="sk-test",
                telegram_token="bot:TOKEN",
                telegram_owner_id="123456",
                port=8002,
            )
            env_path = Path(root, ".env")
            assert env_path.exists()
            content = env_path.read_text()
            assert "OPENAI_API_KEY=sk-test" in content
            assert "TELEGRAM_BOT_TOKEN=bot:TOKEN" in content
            assert "TELEGRAM_OWNER_ID=123456" in content
            assert "RAWOS_PORT=8002" in content

    def test_does_not_overwrite_existing_env_without_force(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            env_path = Path(root, ".env")
            env_path.write_text("EXISTING=value\n")
            wizard = SetupWizard(base_dir=root)
            with pytest.raises(FileExistsError):
                wizard.write_env(
                    openai_api_key="sk-new",
                    telegram_token="t",
                    telegram_owner_id="1",
                    port=8002,
                )

    def test_overwrites_existing_env_with_force(self):
        from rawos.installer.setup import SetupWizard

        with tempfile.TemporaryDirectory() as root:
            env_path = Path(root, ".env")
            env_path.write_text("EXISTING=value\n")
            wizard = SetupWizard(base_dir=root)
            wizard.write_env(
                openai_api_key="sk-new",
                telegram_token="t",
                telegram_owner_id="1",
                port=8002,
                force=True,
            )
            content = env_path.read_text()
            assert "OPENAI_API_KEY=sk-new" in content
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
                    "--openai-key", "sk-x",
                    "--telegram-token", "bot:T",
                    "--telegram-owner-id", "99",
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
            runner.invoke(
                cli,
                [
                    "setup",
                    "--base-dir", "/tmp/rawos-test",
                    "--openai-key", "sk-x",
                    "--telegram-token", "bot:T",
                    "--telegram-owner-id", "99",
                    "--exec-start", "/venv/bin/uvicorn",
                    "--no-service",
                ],
            )
        mock_wizard.write_env.assert_called_once()

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
                        "--openai-key", "sk-x",
                        "--telegram-token", "bot:T",
                        "--telegram-owner-id", "99",
                        "--exec-start", "/venv/bin/uvicorn",
                        "--unit-dir", unit_dir,
                    ],
                )
        assert result.exit_code == 0, result.output
        mock_mgr.install_unit.assert_called_once()
