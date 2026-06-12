"""
context/server_scanner._check_resources — wired to kernel/arch ABI.

Characterization test: _check_resources must call
get_arch().resource_probe.disk_percent("/") instead of inlining
`subprocess.run(["df", "/", "--output=pcent"], ...)`, and must produce
the exact same anomalies as before (severity 9 at >=90%, severity 6 at
>=85%, none below, none on probe failure). Stage A is a
zero-behavior-change extraction — this test is the proof.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rawos.context.server_scanner import _check_resources


def _mock_arch(disk_percent_return):
    backend = MagicMock()
    backend.resource_probe.disk_percent.return_value = disk_percent_return
    return backend


def test_check_resources_calls_get_arch_disk_percent_root():
    backend = _mock_arch(50)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend) as mock_get_arch:
        _check_resources()

    mock_get_arch.assert_called_once_with()
    backend.resource_probe.disk_percent.assert_called_once_with("/")


def test_check_resources_critical_at_90_percent():
    backend = _mock_arch(90)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_resources()

    assert len(anomalies) == 1
    assert anomalies[0].kind == "disk_critical"
    assert anomalies[0].severity == 9
    assert anomalies[0].affected_path == "/root"
    assert anomalies[0].detail == "Disk at 90% — critical (≥90%). Immediate action required."


def test_check_resources_warning_at_85_percent():
    backend = _mock_arch(85)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_resources()

    assert len(anomalies) == 1
    assert anomalies[0].kind == "disk_warning"
    assert anomalies[0].severity == 6
    assert anomalies[0].affected_path == "/root"
    assert anomalies[0].detail == "Disk at 85% — warning (≥85%). Review large files/logs."


def test_check_resources_no_anomaly_below_85_percent():
    backend = _mock_arch(50)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_resources()

    assert anomalies == []


def test_check_resources_no_anomaly_on_probe_failure():
    backend = _mock_arch(None)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_resources()

    assert anomalies == []
