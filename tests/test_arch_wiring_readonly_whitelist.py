"""
kernel/tools._is_bash_readonly_safe — wired to kernel/arch ShellPolicy ABI.

Characterization: _is_bash_readonly_safe must consult
get_arch().shell_policy.readonly_whitelist() for the systemctl/journalctl
subcommand/flag sets, instead of the module-level
_BASH_READONLY_SYSTEMCTL_SUBCMDS / _BASH_READONLY_JOURNALCTL_BLOCKED
constants — so a different arch backend's whitelist takes effect.
Stage A is a zero-behavior-change extraction on Linux — proven by
test_server_scan_isolation.py's existing systemctl/journalctl assertions
continuing to pass unchanged.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rawos.kernel.arch.base import ReadonlyWhitelist
from rawos.kernel.tools import _is_bash_readonly_safe


def _mock_arch(whitelist: ReadonlyWhitelist):
    backend = MagicMock()
    backend.shell_policy.readonly_whitelist.return_value = whitelist
    return backend


def test_uses_arch_whitelist_to_allow_extra_systemctl_subcmd():
    whitelist = ReadonlyWhitelist(
        systemctl_subcmds=frozenset({"reload-or-restart"}),
        journalctl_blocked=(),
    )
    with patch("rawos.kernel.tools.get_arch", return_value=_mock_arch(whitelist)):
        assert _is_bash_readonly_safe("systemctl reload-or-restart rawos.service")
        # Default subcommand no longer present in arch whitelist -> rejected
        assert not _is_bash_readonly_safe("systemctl status rawos.service")


def test_uses_arch_whitelist_for_journalctl_blocked_flags():
    whitelist = ReadonlyWhitelist(
        systemctl_subcmds=frozenset(),
        journalctl_blocked=("--custom-blocked-flag",),
    )
    with patch("rawos.kernel.tools.get_arch", return_value=_mock_arch(whitelist)):
        assert not _is_bash_readonly_safe("journalctl -u rawos.service --custom-blocked-flag")
        # Default-blocked flag no longer in arch whitelist -> now allowed
        assert _is_bash_readonly_safe("journalctl -u rawos.service -f")
