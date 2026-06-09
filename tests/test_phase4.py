"""
Phase 4 tests — multi-agent orchestration: specialized agents, orchestrator, agent routes.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DEEPSEEK_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret")


# ---------------------------------------------------------------------------
# Specialized Agents
# ---------------------------------------------------------------------------

class TestSpecializedAgents:
    def test_agent_configs_present(self):
        from rawos.kernel.specialized_agents import AGENT_CONFIGS
        assert set(AGENT_CONFIGS.keys()) == {"code", "design", "research", "data"}

    def test_code_tools_include_bash(self):
        from rawos.kernel.specialized_agents import get_tool_definitions
        tools = get_tool_definitions("code")
        names = {t["function"]["name"] for t in tools}
        assert "bash" in names
        assert "write_file" in names

    def test_research_tools_exclude_bash(self):
        from rawos.kernel.specialized_agents import get_tool_definitions
        tools = get_tool_definitions("research")
        names = {t["function"]["name"] for t in tools}
        assert "bash" not in names
        assert "fetch_url" in names

    def test_design_tools_exclude_bash(self):
        from rawos.kernel.specialized_agents import get_tool_definitions
        tools = get_tool_definitions("design")
        names = {t["function"]["name"] for t in tools}
        assert "bash" not in names
        assert "write_file" in names

    def test_unknown_type_returns_all_tools(self):
        from rawos.kernel.specialized_agents import get_tool_definitions
        from rawos.kernel.tools import TOOL_DEFINITIONS
        tools = get_tool_definitions("unknown_type")
        assert tools == TOOL_DEFINITIONS

    def test_system_prompt_contains_role(self):
        from rawos.kernel.specialized_agents import get_system_prompt
        prompt = get_system_prompt("code")
        assert "CodeAgent" in prompt
        prompt2 = get_system_prompt("research")
        assert "ResearchAgent" in prompt2

    def test_system_prompt_with_base_context(self):
        from rawos.kernel.specialized_agents import get_system_prompt
        prompt = get_system_prompt("code", base_context="Project: e-commerce app")
        assert "Project: e-commerce app" in prompt
        assert "CodeAgent" in prompt

    def test_all_tool_definitions_are_valid_dicts(self):
        from rawos.kernel.specialized_agents import get_tool_definitions
        for agent_type in ("code", "design", "research", "data"):
            tools = get_tool_definitions(agent_type)
            assert isinstance(tools, list)
            for t in tools:
                assert "type" in t
                assert "function" in t
                assert "name" in t["function"]


# ---------------------------------------------------------------------------
# agent_loop tool_definitions param
# ---------------------------------------------------------------------------

class TestAgentLoopToolDefinitions:
    """Verify that agent_loop.run() accepts and uses the tool_definitions param."""

    def test_run_signature_has_tool_definitions(self):
        import inspect
        from rawos.kernel.agent_loop import run
        sig = inspect.signature(run)
        assert "tool_definitions" in sig.parameters

    def test_run_signature_has_agent_id(self):
        import inspect
        from rawos.kernel.agent_loop import run
        sig = inspect.signature(run)
        assert "agent_id" in sig.parameters

    def test_done_event_carries_agent_id(self):
        """When agent_id is passed, the done event includes it."""
        import asyncio

        async def _run():
            # Mock _llm_tool_call to return no tool calls (direct answer)
            with patch("rawos.kernel.agent_loop._llm_tool_call") as mock_llm:
                mock_llm.return_value = {"content": "hello", "tool_calls": []}
                events = []
                with tempfile.TemporaryDirectory() as td:
                    async for ev in __import__("rawos.kernel.agent_loop", fromlist=["run"]).run(
                        messages=[{"role": "user", "content": "hi"}],
                        workdir=td,
                        model="deepseek-chat",
                        intent_id="itest",
                        agent_id="agent-abc",
                    ):
                        events.append(ev)
            return events

        events = asyncio.run(_run())
        done_events = [e for e in events if e["type"] == "done"]
        assert done_events
        assert done_events[0]["agent_id"] == "agent-abc"


# ---------------------------------------------------------------------------
# Orchestrator classify_intent
# ---------------------------------------------------------------------------

class TestOrchestratorClassify:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_classify_returns_direct_on_api_error(self):
        from rawos.kernel.orchestrator import _classify_intent
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=Exception("network error")
            )
            result = self._run(_classify_intent(
                [{"role": "user", "content": "hello"}], "deepseek-chat"
            ))
        assert result == {"mode": "direct"}

    def test_classify_returns_direct_for_bad_json(self):
        from rawos.kernel.orchestrator import _classify_intent
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "not json at all"}}]
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            result = self._run(_classify_intent(
                [{"role": "user", "content": "hello"}], "deepseek-chat"
            ))
        assert result == {"mode": "direct"}

    def test_classify_returns_multi_plan(self):
        from rawos.kernel.orchestrator import _classify_intent
        plan = {
            "mode": "multi",
            "tasks": [
                {"id": "t1", "agent_type": "code", "goal": "Build backend", "depends_on": []},
                {"id": "t2", "agent_type": "design", "goal": "Build frontend", "depends_on": ["t1"]},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": json.dumps(plan)}}]}
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            result = self._run(_classify_intent(
                [{"role": "user", "content": "build a restaurant app"}], "deepseek-chat"
            ))
        assert result["mode"] == "multi"
        assert len(result["tasks"]) == 2

    def test_classify_rejects_too_many_tasks(self):
        from rawos.kernel.orchestrator import _classify_intent
        plan = {
            "mode": "multi",
            "tasks": [{"id": f"t{i}", "agent_type": "code", "goal": f"Task {i}", "depends_on": []}
                      for i in range(10)]  # exceeds max_parallel_agents=5
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": json.dumps(plan)}}]}
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            result = self._run(_classify_intent(
                [{"role": "user", "content": "build something"}], "deepseek-chat"
            ))
        assert result == {"mode": "direct"}  # falls back — too many tasks


# ---------------------------------------------------------------------------
# Orchestrator run() — direct path
# ---------------------------------------------------------------------------

class TestOrchestratorDirect:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_direct_mode_delegates_to_agent_loop(self):
        from rawos.kernel import orchestrator

        collected = []

        async def _mock_agent_loop_run(**kwargs):
            yield {"type": "chunk", "text": "hello from direct"}
            yield {"type": "done", "intent_id": "i1", "agent_id": ""}

        direct_plan = {"mode": "direct"}

        async def _run_test():
            with patch.object(orchestrator, "_classify_intent", return_value=direct_plan):
                with patch.object(orchestrator.agent_loop, "run", side_effect=_mock_agent_loop_run):
                    async for ev in orchestrator.run(
                        user_id="u1", project_id="p1", intent_id="i1",
                        messages=[{"role": "user", "content": "hi"}],
                        workdir="/tmp", model="deepseek-chat",
                    ):
                        collected.append(ev)

        self._run(_run_test())
        assert any(e["type"] == "chunk" for e in collected)
        assert any(e["type"] == "done" for e in collected)


# ---------------------------------------------------------------------------
# Orchestrator run() — multi-agent path
# ---------------------------------------------------------------------------

class TestOrchestratorMulti:
    def _run(self, coro):
        return asyncio.run(coro)

    def _setup_db(self, tmpdir):
        import rawos.db as db
        from rawos.config import settings
        db_path = str(Path(tmpdir) / "test.db")
        db.init(db_path)
        return db

    def test_multi_mode_emits_plan_and_spawn_events(self):
        from rawos.kernel import orchestrator
        import rawos.db as db

        multi_plan = {
            "mode": "multi",
            "tasks": [
                {"id": "t1", "agent_type": "code", "goal": "Write backend", "depends_on": []},
                {"id": "t2", "agent_type": "design", "goal": "Write frontend", "depends_on": []},
            ]
        }

        async def _mock_agent_loop_run(**kwargs):
            yield {"type": "chunk", "text": "done"}
            yield {"type": "done", "intent_id": "i1", "agent_id": kwargs.get("agent_id", "")}

        collected = []

        async def _run_test():
            with tempfile.TemporaryDirectory() as td:
                db.init(str(Path(td) / "test.db"))
                # Create user + project required by db.create_agent
                from rawos.models import User, Project, UserTier
                import rawos.db as db2
                user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
                db2.create_user(user)
                proj = Project(id="p1", user_id="u1", name="Test", workdir=td)
                db2.create_project(proj)

                with patch.object(orchestrator, "_classify_intent", return_value=multi_plan):
                    with patch.object(orchestrator.agent_loop, "run", side_effect=_mock_agent_loop_run):
                        async for ev in orchestrator.run(
                            user_id="u1", project_id="p1", intent_id="i1",
                            messages=[{"role": "user", "content": "build an app"}],
                            workdir=td, model="deepseek-chat",
                        ):
                            collected.append(ev)

        self._run(_run_test())

        types = [e["type"] for e in collected]
        assert "orchestrator_plan" in types
        assert "agent_spawn" in types
        spawn_events = [e for e in collected if e["type"] == "agent_spawn"]
        assert len(spawn_events) == 2
        for ev in spawn_events:
            assert "agent_id" in ev
            assert "agent_type" in ev
            assert "goal" in ev

    def test_multi_mode_emits_agent_status_events(self):
        from rawos.kernel import orchestrator

        multi_plan = {
            "mode": "multi",
            "tasks": [
                {"id": "t1", "agent_type": "code", "goal": "Write code", "depends_on": []},
            ]
        }

        async def _mock_agent_loop_run(**kwargs):
            yield {"type": "chunk", "text": "result"}
            yield {"type": "done", "intent_id": "i1", "agent_id": ""}

        collected = []

        async def _run_test():
            with tempfile.TemporaryDirectory() as td:
                import rawos.db as db2
                db2.init(str(Path(td) / "test.db"))
                from rawos.models import User, Project, UserTier
                user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
                db2.create_user(user)
                proj = Project(id="p1", user_id="u1", name="Test", workdir=td)
                db2.create_project(proj)

                with patch.object(orchestrator, "_classify_intent", return_value=multi_plan):
                    with patch.object(orchestrator.agent_loop, "run", side_effect=_mock_agent_loop_run):
                        async for ev in orchestrator.run(
                            user_id="u1", project_id="p1", intent_id="i1",
                            messages=[{"role": "user", "content": "build"}],
                            workdir=td, model="deepseek-chat",
                        ):
                            collected.append(ev)

        self._run(_run_test())
        status_events = [e for e in collected if e["type"] == "agent_status"]
        statuses = {e["status"] for e in status_events}
        assert "running" in statuses
        assert "done" in statuses


# ---------------------------------------------------------------------------
# Agent DB functions
# ---------------------------------------------------------------------------

class TestAgentDB:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(str(Path(self.tmp) / "test.db"))
        from rawos.models import User, Project, UserTier
        import rawos.db as db2
        self.db = db2
        user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
        db2.create_user(user)
        proj = Project(id="p1", user_id="u1", name="Test", workdir=self.tmp)
        db2.create_project(proj)

    def test_get_project_agents_empty(self):
        agents = self.db.get_project_agents("u1", "p1")
        assert agents == []

    def test_get_project_agents_returns_created(self):
        from rawos.models import Agent, AgentStatus
        a = Agent(user_id="u1", project_id="p1", goal="test goal")
        a = a.transition(AgentStatus.ACTIVE)
        self.db.create_agent(a)
        agents = self.db.get_project_agents("u1", "p1")
        assert len(agents) == 1
        assert agents[0].goal == "test goal"

    def test_get_agent_children_returns_sub_agents(self):
        from rawos.models import Agent, AgentStatus
        parent = Agent(user_id="u1", project_id="p1", goal="orchestrate")
        parent = parent.transition(AgentStatus.ACTIVE)
        self.db.create_agent(parent)
        child = Agent(user_id="u1", project_id="p1", parent_id=parent.id, goal="sub-task")
        child = child.transition(AgentStatus.ACTIVE)
        self.db.create_agent(child)
        children = self.db.get_agent_children("u1", parent.id)
        assert len(children) == 1
        assert children[0].parent_id == parent.id

    def test_get_agent_children_isolated_by_user(self):
        from rawos.models import Agent, AgentStatus, User, UserTier
        # Create another user
        u2 = User(id="u2", email="u2@t.com", password_hash="x", tier=UserTier.FREE)
        self.db.create_user(u2)
        parent = Agent(user_id="u1", project_id="p1", goal="parent")
        parent = parent.transition(AgentStatus.ACTIVE)
        self.db.create_agent(parent)
        # get_agent_children queries by user_id=u2 — should return empty
        children = self.db.get_agent_children("u2", parent.id)
        assert children == []


# ---------------------------------------------------------------------------
# Agent Routes
# ---------------------------------------------------------------------------

class TestAgentRoutes:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(str(Path(self.tmp) / "test.db"))

    def _app(self, user_id: str = "u1"):
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        from rawos.api.deps import current_user
        from rawos.models import User, UserTier

        user = User(id=user_id, email="t@t.com", password_hash="x", tier=UserTier.FREE)
        app.dependency_overrides[current_user] = lambda: user
        return TestClient(app, raise_server_exceptions=True)

    def test_list_agents_empty(self):
        import rawos.db as db
        from rawos.models import User, Project, UserTier
        user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
        db.create_user(user)
        proj = Project(id="p1", user_id="u1", name="Test", workdir=self.tmp)
        db.create_project(proj)
        client = self._app("u1")
        resp = client.get("/projects/p1/agents")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_agents_returns_tree(self):
        import rawos.db as db
        from rawos.models import User, Project, Agent, AgentStatus, UserTier
        user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
        db.create_user(user)
        proj = Project(id="p1", user_id="u1", name="Test", workdir=self.tmp)
        db.create_project(proj)
        parent = Agent(user_id="u1", project_id="p1", goal="orchestrate")
        parent = parent.transition(AgentStatus.ACTIVE)
        db.create_agent(parent)
        child = Agent(user_id="u1", project_id="p1", parent_id=parent.id, goal="sub-task")
        child = child.transition(AgentStatus.ACTIVE)
        db.create_agent(child)
        client = self._app("u1")
        resp = client.get("/projects/p1/agents")
        assert resp.status_code == 200
        data = resp.json()
        # parent agent is root, child is nested
        root = next((a for a in data if a["id"] == parent.id), None)
        assert root is not None
        assert len(root["children"]) == 1
        assert root["children"][0]["id"] == child.id

    def test_get_agent_404(self):
        import rawos.db as db
        from rawos.models import User, Project, UserTier
        user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
        db.create_user(user)
        proj = Project(id="p1", user_id="u1", name="Test", workdir=self.tmp)
        db.create_project(proj)
        client = self._app("u1")
        resp = client.get("/projects/p1/agents/nonexistent")
        assert resp.status_code == 404
