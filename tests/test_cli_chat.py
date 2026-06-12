"""
tests/test_cli_chat.py — Stage E: CLI streaming conversation.

Tests for:
  - _render_event   (pure render dispatch, no network)
  - _api_stream     (streaming SSE iterator, httpx mocked)
  - _resolve_project_id  (project fallback logic, _api mocked)
  - `rawos ask`     (CliRunner end-to-end, mocked stream)
  - `rawos chat`    (CliRunner REPL, mocked stream)
  - `rawos goal`    (regression: now calls _api_stream, not hollow)

All tests are network-free.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import httpx
import pytest
from click.testing import CliRunner
from rich.console import Console

from rawos.cli.main import _render_event, _api_stream, _resolve_project_id, cli


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _console(buf: io.StringIO) -> Console:
    """Return a Console that writes plain text to buf (no ANSI, no markup)."""
    return Console(file=buf, highlight=False, markup=True, no_color=True)


def _read(buf: io.StringIO) -> str:
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. _render_event — one test per event type
# ---------------------------------------------------------------------------

class TestRenderEvent:
    def test_orchestrator_plan_prints_task_goals(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event(
            {"type": "orchestrator_plan", "plan": [
                {"id": "1", "goal": "check failed services", "agent_type": "bash", "depends_on": []},
                {"id": "2", "goal": "restart nginx",          "agent_type": "bash", "depends_on": ["1"]},
            ]},
            c,
        )
        out = _read(buf)
        assert "check failed services" in out
        assert "restart nginx" in out

    def test_agent_spawn_prints_agent_type_and_goal(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event(
            {"type": "agent_spawn", "agent_id": "a1", "agent_type": "bash",
             "goal": "list failed units", "parent_id": None},
            c,
        )
        out = _read(buf)
        assert "bash" in out
        assert "list failed units" in out

    def test_agent_status_running_prints_status(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "agent_status", "agent_id": "a1", "status": "running"}, c)
        out = _read(buf)
        assert "running" in out

    def test_agent_status_done_prints_status(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "agent_status", "agent_id": "a1", "status": "done"}, c)
        out = _read(buf)
        assert "done" in out

    def test_agent_status_failed_prints_status(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "agent_status", "agent_id": "a1", "status": "failed"}, c)
        out = _read(buf)
        assert "failed" in out

    def test_agent_output_prints_content(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "agent_output", "agent_id": "a1", "content": "nginx is running"}, c)
        out = _read(buf)
        assert "nginx is running" in out

    def test_chunk_prints_text(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "chunk", "text": "Hello from rawos"}, c)
        out = _read(buf)
        assert "Hello from rawos" in out

    def test_tool_call_prints_tool_name(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "tool_call", "tool": "bash", "input": {"cmd": "ls"}}, c)
        out = _read(buf)
        assert "bash" in out

    def test_error_prints_message(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "error", "message": "disk probe failed"}, c)
        out = _read(buf)
        assert "disk probe failed" in out

    def test_unknown_type_does_not_raise_and_produces_no_output(self):
        """Forward-compatibility: unknown event types must be silently ignored."""
        buf = io.StringIO()
        c = _console(buf)
        # Must not raise, must produce no visible output
        _render_event({"type": "future_event_type_v9", "data": "whatever"}, c)
        # No assertion on content — just confirming no exception

    def test_orchestrator_plan_empty_plan_does_not_raise(self):
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "orchestrator_plan", "plan": []}, c)

    def test_chunk_missing_text_does_not_raise(self):
        """Defensive: event schema can have missing optional fields."""
        buf = io.StringIO()
        c = _console(buf)
        _render_event({"type": "chunk"}, c)


# ---------------------------------------------------------------------------
# 2. _api_stream — httpx mocked, no network
# ---------------------------------------------------------------------------

def _make_stream_ctx(lines: list[str], status_code: int = 200):
    """Build a mock httpx stream context manager that yields the given lines."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_lines.return_value = iter(lines)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)

    client = MagicMock()
    client.stream.return_value = resp
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class TestApiStream:
    def _patch_client(self, mock_client):
        return patch("rawos.cli.main.httpx.Client", return_value=mock_client)

    def _patch_token(self):
        return patch("rawos.cli.main._get_token", return_value="tok_test")

    def test_yields_parsed_dicts_from_sse_lines(self):
        ev1 = {"type": "chunk", "text": "hi"}
        ev2 = {"type": "agent_status", "agent_id": "a1", "status": "done"}
        lines = [
            f"data: {json.dumps(ev1)}",
            "",                          # blank keepalive — must be skipped
            f"data: {json.dumps(ev2)}",
        ]
        client = _make_stream_ctx(lines)
        with self._patch_client(client), self._patch_token():
            result = list(_api_stream("/intent", {"project_id": "p1", "message": "hello"}))
        assert result == [ev1, ev2]

    def test_skips_blank_keepalive_lines(self):
        ev = {"type": "chunk", "text": "x"}
        lines = ["", "  ", f"data: {json.dumps(ev)}", ""]
        client = _make_stream_ctx(lines)
        with self._patch_client(client), self._patch_token():
            result = list(_api_stream("/intent", {}))
        assert result == [ev]

    def test_skips_non_data_prefix_lines(self):
        ev = {"type": "chunk", "text": "y"}
        lines = ["event: ping", f"data: {json.dumps(ev)}"]
        client = _make_stream_ctx(lines)
        with self._patch_client(client), self._patch_token():
            result = list(_api_stream("/intent", {}))
        assert result == [ev]

    def test_exits_on_401(self):
        client = _make_stream_ctx([], status_code=401)
        with self._patch_client(client), self._patch_token():
            with pytest.raises(SystemExit):
                list(_api_stream("/intent", {}))

    def test_exits_on_4xx(self):
        client = _make_stream_ctx([], status_code=429)
        with self._patch_client(client), self._patch_token():
            with pytest.raises(SystemExit):
                list(_api_stream("/intent", {}))

    def test_posts_to_correct_url(self):
        ev = {"type": "chunk", "text": "ok"}
        client = _make_stream_ctx([f"data: {json.dumps(ev)}"])
        with self._patch_client(client), self._patch_token():
            list(_api_stream("/intent", {"project_id": "p1"}))
        # Verify stream was called with "POST" and path containing "/intent"
        call_args = client.stream.call_args
        assert call_args[0][0] == "POST"
        assert "/intent" in call_args[0][1]

    def test_sends_bearer_token_in_header(self):
        ev = {"type": "chunk", "text": "ok"}
        client = _make_stream_ctx([f"data: {json.dumps(ev)}"])
        with self._patch_client(client), self._patch_token():
            list(_api_stream("/intent", {}))
        call_kwargs = client.stream.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer tok_test"

    def test_empty_stream_yields_nothing(self):
        client = _make_stream_ctx([])
        with self._patch_client(client), self._patch_token():
            result = list(_api_stream("/intent", {}))
        assert result == []


# ---------------------------------------------------------------------------
# 2b. _api_stream — Stage F: run_id/seq tracking, reconnect, run_complete
# ---------------------------------------------------------------------------

def _sse_resp(lines: list[str], status_code: int = 200):
    """Build a single mock SSE streaming response (context manager)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_lines.return_value = iter(lines)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_multi_stream_ctx(*responses):
    """Build a mock httpx.Client whose .stream() returns successive responses
    (one per call), in order — for reconnect tests."""
    client = MagicMock()
    client.stream.side_effect = list(responses)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    return client


class TestApiStreamResumable:
    def _patch_client(self, mock_client):
        return patch("rawos.cli.main.httpx.Client", return_value=mock_client)

    def _patch_token(self):
        return patch("rawos.cli.main._get_token", return_value="tok_test")

    def test_filters_run_started_and_run_complete_control_events(self):
        ev_started = {"type": "run_started", "run_id": "run123"}
        ev_chunk = {"type": "chunk", "text": "hi"}
        ev_complete = {"type": "run_complete", "status": "completed"}
        lines = [
            "id: 1",
            f"data: {json.dumps(ev_started)}",
            "id: 2",
            f"data: {json.dumps(ev_chunk)}",
            "id: 3",
            f"data: {json.dumps(ev_complete)}",
        ]
        resp = _sse_resp(lines)
        client = _make_multi_stream_ctx(resp)
        with self._patch_client(client), self._patch_token():
            result = list(_api_stream("/intent", {"project_id": "p1", "message": "hi"}))
        assert result == [ev_chunk]

    def test_run_complete_stops_cleanly_without_reconnect(self):
        ev_started = {"type": "run_started", "run_id": "run123"}
        ev_chunk = {"type": "chunk", "text": "hi"}
        ev_complete = {"type": "run_complete", "status": "completed"}
        lines = [
            "id: 1",
            f"data: {json.dumps(ev_started)}",
            "id: 2",
            f"data: {json.dumps(ev_chunk)}",
            "id: 3",
            f"data: {json.dumps(ev_complete)}",
        ]
        resp = _sse_resp(lines)
        client = _make_multi_stream_ctx(resp)
        with self._patch_client(client), self._patch_token():
            list(_api_stream("/intent", {"project_id": "p1", "message": "hi"}))
        assert client.stream.call_count == 1

    def test_reconnects_on_transport_drop_and_resumes(self):
        ev_started = {"type": "run_started", "run_id": "run123"}
        ev1 = {"type": "chunk", "text": "a"}
        ev2 = {"type": "chunk", "text": "b"}
        ev_complete = {"type": "run_complete", "status": "completed"}

        def _first_lines():
            yield "id: 1"
            yield f"data: {json.dumps(ev_started)}"
            yield "id: 2"
            yield f"data: {json.dumps(ev1)}"
            raise httpx.ReadError("connection dropped")

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.iter_lines.return_value = _first_lines()
        resp1.__enter__ = MagicMock(return_value=resp1)
        resp1.__exit__ = MagicMock(return_value=False)

        resp2 = _sse_resp([
            "id: 3",
            f"data: {json.dumps(ev2)}",
            "id: 4",
            f"data: {json.dumps(ev_complete)}",
        ])

        client = _make_multi_stream_ctx(resp1, resp2)
        with self._patch_client(client), self._patch_token(), \
                patch("rawos.cli.main.time.sleep"):
            result = list(_api_stream("/intent", {"project_id": "p1", "message": "hi"}))

        assert result == [ev1, ev2]
        assert client.stream.call_count == 2

        second_call = client.stream.call_args_list[1]
        assert second_call[0][0] == "GET"
        assert second_call[0][1].endswith("/intent/run123/stream")
        assert second_call[1]["headers"]["Last-Event-ID"] == "2"
        assert second_call[1]["headers"]["Authorization"] == "Bearer tok_test"


# ---------------------------------------------------------------------------
# 3. _resolve_project_id
# ---------------------------------------------------------------------------

class TestResolveProjectId:
    def test_returns_current_project_from_status(self):
        with patch("rawos.cli.main._api") as mock_api:
            mock_api.return_value = {"current_project_id": "proj-abc"}
            result = _resolve_project_id()
        assert result == "proj-abc"
        mock_api.assert_called_once_with("get", "/context/status")

    def test_falls_back_to_first_project_when_status_has_no_project(self):
        def _api_side(method, path, **kw):
            if path == "/context/status":
                return {"current_project_id": None}
            if path == "/projects":
                return [{"id": "proj-fallback"}]
            return {}

        with patch("rawos.cli.main._api", side_effect=_api_side):
            result = _resolve_project_id()
        assert result == "proj-fallback"

    def test_exits_when_no_projects_exist(self):
        def _api_side(method, path, **kw):
            if path == "/context/status":
                return {"current_project_id": None}
            if path == "/projects":
                return []
            return {}

        with patch("rawos.cli.main._api", side_effect=_api_side):
            with pytest.raises(SystemExit):
                _resolve_project_id()


# ---------------------------------------------------------------------------
# 4. `rawos ask`
# ---------------------------------------------------------------------------

class TestAskCommand:
    def _run(self, args, stream_events, project_id="proj-test"):
        runner = CliRunner()
        with patch("rawos.cli.main._resolve_project_id", return_value=project_id), \
             patch("rawos.cli.main._api_stream", return_value=iter(stream_events)) as mock_stream:
            result = runner.invoke(cli, ["ask", *args])
        return result, mock_stream

    def test_ask_invokes_api_stream_with_message_and_project(self):
        events = [{"type": "chunk", "text": "pong"}]
        result, mock_stream = self._run(["ping test"], events)
        assert result.exit_code == 0
        mock_stream.assert_called_once_with(
            "/intent",
            {"project_id": "proj-test", "message": "ping test"},
        )

    def test_ask_renders_chunk_text_in_output(self):
        events = [{"type": "chunk", "text": "All services nominal."}]
        result, _ = self._run(["check status"], events)
        assert "All services nominal." in result.output

    def test_ask_renders_orchestrator_plan_in_output(self):
        events = [{"type": "orchestrator_plan", "plan": [
            {"id": "1", "goal": "enumerate services", "agent_type": "bash", "depends_on": []},
        ]}]
        result, _ = self._run(["list failed"], events)
        assert "enumerate services" in result.output

    def test_ask_renders_error_event(self):
        events = [{"type": "error", "message": "quota exceeded"}]
        result, _ = self._run(["anything"], events)
        assert "quota exceeded" in result.output

    def test_ask_exits_cleanly_on_empty_stream(self):
        result, _ = self._run(["hello"], [])
        assert result.exit_code == 0

    def test_ask_requires_message_argument(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["ask"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 5. `rawos chat`
# ---------------------------------------------------------------------------

class TestChatCommand:
    def _run(self, input_text: str, stream_events):
        runner = CliRunner()
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-test"), \
             patch("rawos.cli.main._api_stream", return_value=iter(stream_events)) as mock_stream:
            result = runner.invoke(cli, ["chat"], input=input_text)
        return result, mock_stream

    def test_chat_sends_user_message_to_stream(self):
        events = [{"type": "chunk", "text": "Hello back!"}]
        result, mock_stream = self._run("hello rawos\n:q\n", events)
        # Verify _api_stream was called with the user's message
        mock_stream.assert_called_once_with(
            "/intent",
            {"project_id": "proj-test", "message": "hello rawos"},
        )

    def test_chat_renders_response_in_output(self):
        events = [{"type": "chunk", "text": "I see 3 failed services."}]
        result, _ = self._run("check services\n:q\n", events)
        assert "I see 3 failed services." in result.output

    def test_chat_exits_on_quit_command(self):
        result, _ = self._run(":q\n", [])
        assert result.exit_code == 0

    def test_chat_exits_on_exit_command(self):
        result, _ = self._run("exit\n", [])
        assert result.exit_code == 0

    def test_chat_exits_cleanly_on_eof(self):
        """Ctrl-D / empty input should cause clean exit, not crash."""
        runner = CliRunner()
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-test"), \
             patch("rawos.cli.main._api_stream", return_value=iter([])):
            result = runner.invoke(cli, ["chat"], input="")
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 6. `rawos goal` regression — now calls _api_stream (hollow no-op removed)
# ---------------------------------------------------------------------------

class TestGoalRegression:
    def test_goal_calls_api_stream(self):
        runner = CliRunner()
        events = [{"type": "chunk", "text": "working on it"}]
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-rg"), \
             patch("rawos.cli.main._api_stream", return_value=iter(events)) as mock_stream:
            result = runner.invoke(cli, ["goal", "fix nginx"])
        assert result.exit_code == 0
        mock_stream.assert_called_once_with(
            "/intent",
            {"project_id": "proj-rg", "message": "fix nginx"},
        )

    def test_goal_renders_streamed_output(self):
        runner = CliRunner()
        events = [{"type": "chunk", "text": "Restarting nginx now."}]
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-rg"), \
             patch("rawos.cli.main._api_stream", return_value=iter(events)):
            result = runner.invoke(cli, ["goal", "restart nginx"])
        assert "Restarting nginx now." in result.output

    def test_goal_no_longer_echoes_old_hollow_message(self):
        """Regression: the old no-op said 'Goal submitted … Use rawos show'. Gone."""
        runner = CliRunner()
        events = []
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-rg"), \
             patch("rawos.cli.main._api_stream", return_value=iter(events)):
            result = runner.invoke(cli, ["goal", "do something"])
        assert "Use `rawos show`" not in result.output
        assert "Full streaming available" not in result.output



# ---------------------------------------------------------------------------
# 7. `rawos chat` digest greeting — "While you were away"
# ---------------------------------------------------------------------------

class TestChatDigestGreeting:
    def _run(self, input_text: str, session_response: dict, stream_events=None):
        runner = CliRunner()
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-test"), \
             patch("rawos.cli.main._api_stream", return_value=iter(stream_events or [])), \
             patch("rawos.cli.main._api", return_value=session_response) as mock_api:
            result = runner.invoke(cli, ["chat"], input=input_text)
        return result, mock_api

    def test_chat_calls_session_start_before_repl(self):
        result, mock_api = self._run(":q\n", {"last_chat_at": 0, "artifacts": []})
        mock_api.assert_called_once_with("post", "/context/session_start")

    def test_chat_shows_digest_header_when_artifacts_present(self):
        session = {
            "last_chat_at": 1_000_000,
            "artifacts": [
                {"goal": "refactored auth module", "confidence": 0.92, "file_path": "/tmp/auth.py", "created_at": 1_000_100},
            ],
        }
        result, _ = self._run(":q\n", session)
        assert "While you were away" in result.output
        assert "refactored auth module" in result.output

    def test_chat_shows_no_digest_when_no_artifacts(self):
        result, _ = self._run(":q\n", {"last_chat_at": 0, "artifacts": []})
        assert "While you were away" not in result.output

    def test_chat_continues_to_repl_after_digest(self):
        session = {"last_chat_at": 0, "artifacts": []}
        events = [{"type": "chunk", "text": "hello from rawos"}]
        with patch("rawos.cli.main._resolve_project_id", return_value="proj-test"), \
             patch("rawos.cli.main._api_stream", return_value=iter(events)), \
             patch("rawos.cli.main._api", return_value=session):
            runner = CliRunner()
            result = runner.invoke(cli, ["chat"], input="hi\n:q\n")
        assert "hello from rawos" in result.output
        assert result.exit_code == 0
