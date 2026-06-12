"""
kernel/arch/linux.LinuxShellPolicy — characterization tests.

wrap(command, workdir) must reproduce, byte-for-byte, the wrapped shell
string currently built inline in kernel/sandbox.py:run_bash:

    wrapped = (
        f"cd {workdir_abs!r} && "
        "ulimit -v 524288 -f 102400 -u 256 2>/dev/null; "
        + command
    )

readonly_whitelist() must reproduce the systemctl/journalctl-specific
subsets currently in kernel/tools.py (_BASH_READONLY_SYSTEMCTL_SUBCMDS,
_BASH_READONLY_JOURNALCTL_BLOCKED). Stage A is a zero-behavior-change
extraction — these tests are the proof.
"""
from __future__ import annotations

from rawos.kernel.arch.linux import LinuxShellPolicy


def test_wrap_reproduces_cd_and_ulimit_prefix():
    policy = LinuxShellPolicy()
    workdir = "/root/myrepo"
    shell_cmd, exec_kwargs = policy.wrap("echo hello", workdir)

    expected = (
        f"cd {workdir!r} && "
        "ulimit -v 524288 -f 102400 -u 256 2>/dev/null; "
        "echo hello"
    )
    assert shell_cmd == expected
    assert exec_kwargs == {}


def test_wrap_preserves_command_containing_quotes_via_repr():
    policy = LinuxShellPolicy()
    workdir = "/root/with 'quote"
    shell_cmd, _ = policy.wrap("ls", workdir)

    expected = (
        f"cd {workdir!r} && "
        "ulimit -v 524288 -f 102400 -u 256 2>/dev/null; "
        "ls"
    )
    assert shell_cmd == expected


def test_readonly_whitelist_systemctl_subcmds():
    whitelist = LinuxShellPolicy().readonly_whitelist()

    assert whitelist.systemctl_subcmds == frozenset({
        "status", "show", "cat", "is-active", "is-failed", "is-enabled",
        "list-units", "list-unit-files", "list-timers",
    })
    assert "restart" not in whitelist.systemctl_subcmds
    assert "stop" not in whitelist.systemctl_subcmds


def test_readonly_whitelist_journalctl_blocked():
    whitelist = LinuxShellPolicy().readonly_whitelist()

    assert whitelist.journalctl_blocked == (
        "-f", "--follow", "--flush", "--rotate", "--sync", "--relinquish-var",
    )
