"""
context/server_scanner — _check_failed_services / _check_recent_errors
wired to kernel/arch ABI.

Characterization: _check_failed_services must call
get_arch().service_manager.list_failed() for the failed-unit list and
get_arch().log_reader.tail(service, 8) for the trailing log (sliced to
600 chars), and _check_recent_errors must call
get_arch().log_reader.recent_errors(unit, "15 minutes ago") (sliced to
800 chars) — instead of inlining subprocess/journalctl/systemctl calls.
Stage A is a zero-behavior-change extraction — this test is the proof.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rawos.context.server_scanner import _check_failed_services, _check_recent_errors, _MONITORED_SERVICES


def _mock_arch(list_failed=(), tail_return="", recent_errors_return=""):
    backend = MagicMock()
    backend.service_manager.list_failed.return_value = list(list_failed)
    backend.log_reader.tail.return_value = tail_return
    backend.log_reader.recent_errors.return_value = recent_errors_return
    return backend


def test_check_failed_services_calls_list_failed_and_tail():
    backend = _mock_arch(list_failed=["foo.service"], tail_return="last log line")
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_failed_services()

    backend.service_manager.list_failed.assert_called_once_with()
    backend.log_reader.tail.assert_called_once_with("foo.service", 8)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.kind == "service_failed"
    assert a.service == "foo.service"
    assert a.detail == "foo.service is in FAILED state — needs immediate diagnosis"
    assert a.last_log == "last log line"
    assert a.severity == 8


def test_check_failed_services_truncates_tail_to_600_chars():
    backend = _mock_arch(list_failed=["foo.service"], tail_return="x" * 1000)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_failed_services()

    assert anomalies[0].last_log == "x" * 600


def test_check_failed_services_returns_empty_when_no_failed_units():
    backend = _mock_arch(list_failed=[])
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        assert _check_failed_services() == []
    backend.log_reader.tail.assert_not_called()


def test_check_failed_services_maps_known_service_to_repo():
    known_service, known_repo = next(iter(
        __import__("rawos.context.server_scanner", fromlist=["_SERVICE_TO_REPO"])._SERVICE_TO_REPO.items()
    ))
    backend = _mock_arch(list_failed=[f"{known_service}.service"], tail_return="")
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_failed_services()

    assert anomalies[0].affected_path == known_repo


def test_check_recent_errors_calls_recent_errors_for_each_monitored_service():
    backend = _mock_arch(recent_errors_return="error output")
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_recent_errors()

    assert backend.log_reader.recent_errors.call_count == len(_MONITORED_SERVICES)
    for svc_name in _MONITORED_SERVICES:
        backend.log_reader.recent_errors.assert_any_call(f"{svc_name}.service", "15 minutes ago")

    assert len(anomalies) == len(_MONITORED_SERVICES)
    for a in anomalies:
        assert a.kind == "service_error"
        assert a.last_log == "error output"
        assert a.severity == 6


def test_check_recent_errors_skips_services_with_no_output():
    backend = _mock_arch(recent_errors_return="")
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        assert _check_recent_errors() == []


def test_check_recent_errors_truncates_to_800_chars():
    backend = _mock_arch(recent_errors_return="y" * 1000)
    with patch("rawos.context.server_scanner.get_arch", return_value=backend):
        anomalies = _check_recent_errors()

    assert anomalies[0].last_log == "y" * 800
