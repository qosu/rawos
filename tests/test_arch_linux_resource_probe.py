"""
kernel/arch/linux — LinuxResourceProbe.disk_percent.

Characterization test: must reproduce, byte-for-byte, the
`df <path> --output=pcent` command and percentage parsing currently
inlined in context/server_scanner.py:_check_resources. Stage A is a
zero-behavior-change extraction — this test is the proof.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from rawos.kernel.arch.linux import LinuxResourceProbe


def _mock_df(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_disk_percent_runs_df_with_output_pcent():
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_df("Use%\n66%\n")) as mock_run:
        pct = probe.disk_percent("/")

    mock_run.assert_called_once_with(
        ["df", "/", "--output=pcent"],
        capture_output=True, text=True, timeout=3.0,
    )
    assert pct == 66


def test_disk_percent_parses_last_line_strips_percent_sign():
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_df("Use%\n 90% \n")):
        assert probe.disk_percent("/") == 90


def test_disk_percent_returns_none_on_nonzero_exit():
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_df("", returncode=1)):
        assert probe.disk_percent("/") is None


def test_disk_percent_returns_none_on_exception():
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("boom")):
        assert probe.disk_percent("/") is None


def test_disk_percent_returns_none_on_empty_stdout():
    """df exits 0 but stdout is empty — parse must not raise IndexError."""
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_df("")):
        assert probe.disk_percent("/") is None


def test_disk_percent_returns_none_on_non_numeric_output():
    """df exits 0 but last line has no numeric content — int() must not raise."""
    probe = LinuxResourceProbe()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_df("Use%\n")):
        assert probe.disk_percent("/") is None
