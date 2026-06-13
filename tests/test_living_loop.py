"""Phase 19 — Close the Living Loop.

Tests for three seams that give rawos continuous selfhood:
  Seam A: _log_episodic indexes autonomous experience to semantic memory.
  Seam B: narrative consolidation loop writes being's self-narrative.
  Seam C: build_context surfaces being's autonomous life to owner conversations.

TDD: every test in this file was written BEFORE the corresponding production code.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Environment must be set before any rawos import.
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "test.db"))
os.environ.setdefault("WORKSPACES_ROOT", str(Path(tempfile.mkdtemp())))
os.environ.setdefault("JWT_SECRET", "test_secret_32chars_minimum_ok!")
os.environ.setdefault("DEEPSEEK_KEY", "test_key")

import rawos.db as db
from rawos.config import settings
from rawos.models import User, UserTier
from rawos.scheduler.proactive import RAWOS_ENTITY_USER_ID, RAWOS_ENTITY_PROJECT_ID


def _make_user(uid: str, email: str) -> User:
    return User(id=uid, email=email, password_hash="hashed", tier=UserTier.FREE)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    ws_root = str(tmp_path / "ws")
    os.environ["DB_PATH"] = db_path
    os.environ["WORKSPACES_ROOT"] = ws_root
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "workspaces_root", ws_root)
    db.init(db_path)
    yield


@pytest.fixture
def entity_user(fresh_db):
    """Insert the rawos entity user so FK constraints pass in tests.

    Uses raw SQL to bypass the email validator — the production entity identity
    uses 'rawos-entity@internal' which lacks a dot in the domain.
    """
    import time
    with db._conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO users
               (id, email, password_hash, tier, token_budget_daily,
                tokens_used_today, budget_reset_date, is_admin,
                stripe_customer_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                RAWOS_ENTITY_USER_ID, "rawos-entity@internal", "hashed",
                "free", 100_000, 0, "", 0, None,
                int(time.time()), int(time.time()),
            ),
        )
    # Return a minimal sentinel — tests only need entity_user as a fixture dep.
    return {"id": RAWOS_ENTITY_USER_ID, "email": "rawos-entity@internal"}


@pytest.fixture
def owner_user(fresh_db):
    """Insert a regular (non-entity) owner user."""
    import uuid
    uid = str(uuid.uuid4())
    user = _make_user(uid, f"owner-{uid[:8]}@test.com")
    db.create_user(user)
    return user


# ---------------------------------------------------------------------------
# Seam A: _log_episodic indexes autonomous experience to semantic memory
# ---------------------------------------------------------------------------

class TestSeamA:
    def test_log_episodic_with_project_id_calls_upsert(self, entity_user, monkeypatch):
        """_log_episodic(project_id=…) must call memory_index.upsert_memory once."""
        upsert_calls: list[dict] = []
        monkeypatch.setattr(
            "rawos.kernel.memory_index.upsert_memory",
            lambda **kw: upsert_calls.append(kw),
        )

        from rawos.scheduler.proactive import _log_episodic  # noqa: PLC0415
        _log_episodic(
            RAWOS_ENTITY_USER_ID, "SERVER_SCAN", "ops", "fix disk",
            "contribute", "cleaned /var/log",
            project_id=RAWOS_ENTITY_PROJECT_ID,
        )

        assert len(upsert_calls) == 1, "upsert_memory must be called exactly once"
        call = upsert_calls[0]
        assert call["user_id"] == RAWOS_ENTITY_USER_ID
        assert call["project_id"] == RAWOS_ENTITY_PROJECT_ID
        assert call["tier"] == "episodic"
        assert "fix disk" in call["text"] or "cleaned" in call["text"]

    def test_log_episodic_no_project_id_skips_indexing(self, entity_user, monkeypatch):
        """_log_episodic without project_id must NOT call upsert_memory."""
        upsert_calls: list[dict] = []
        monkeypatch.setattr(
            "rawos.kernel.memory_index.upsert_memory",
            lambda **kw: upsert_calls.append(kw),
        )

        from rawos.scheduler.proactive import _log_episodic  # noqa: PLC0415
        _log_episodic(
            RAWOS_ENTITY_USER_ID, "SERVER_SCAN", "ops", "fix disk",
            "contribute", "cleaned /var/log",
        )

        assert len(upsert_calls) == 0, "no project_id → upsert_memory must NOT be called"

    def test_log_episodic_index_failure_does_not_propagate(self, entity_user, monkeypatch):
        """If upsert_memory raises, _log_episodic must NOT propagate — returns row id."""
        def _raise(**kw):
            raise RuntimeError("chromadb unavailable")

        monkeypatch.setattr("rawos.kernel.memory_index.upsert_memory", _raise)

        from rawos.scheduler.proactive import _log_episodic  # noqa: PLC0415
        result = _log_episodic(
            RAWOS_ENTITY_USER_ID, "SERVER_SCAN", "ops", "fix disk",
            "contribute", "cleaned /var/log",
            project_id=RAWOS_ENTITY_PROJECT_ID,
        )
        assert result is not None, "episodic row must still be created despite index error"

    def test_log_episodic_index_failure_row_persists_in_sqlite(self, entity_user, monkeypatch):
        """If upsert_memory raises, the episodic_memory row must still exist in SQLite."""
        def _raise(**kw):
            raise RuntimeError("chromadb unavailable")

        monkeypatch.setattr("rawos.kernel.memory_index.upsert_memory", _raise)

        from rawos.scheduler.proactive import _log_episodic  # noqa: PLC0415
        _log_episodic(
            RAWOS_ENTITY_USER_ID, "SERVER_SCAN", "ops", "disk full",
            "signal", "alerting owner",
            project_id=RAWOS_ENTITY_PROJECT_ID,
        )

        with db._conn() as conn:
            row = conn.execute(
                "SELECT inferred_goal FROM episodic_memory WHERE user_id = ? LIMIT 1",
                (RAWOS_ENTITY_USER_ID,),
            ).fetchone()
        assert row is not None, "episodic row must exist in SQLite"
        assert row["inferred_goal"] == "disk full"


# ---------------------------------------------------------------------------
# Seam B: narrative consolidation loop writes being's self-narrative
# ---------------------------------------------------------------------------

class TestSeamB:
    def test_consolidation_cycle_writes_being_narrative(self, entity_user, monkeypatch):
        """_run_narrative_consolidation_cycle must persist the narrative LLM output."""
        monkeypatch.setattr(
            "rawos.scheduler.proactive.write_self_narrative",
            AsyncMock(return_value="I repaired disk at 3am. All systems nominal."),
        )

        from rawos.scheduler.proactive import _run_narrative_consolidation_cycle  # noqa: PLC0415
        asyncio.run(_run_narrative_consolidation_cycle())

        result = db.get_self_narrative(RAWOS_ENTITY_USER_ID)
        assert result == "I repaired disk at 3am. All systems nominal."

    def test_consolidation_cycle_empty_result_preserves_prior(self, entity_user, monkeypatch):
        """If write_self_narrative returns '', prior narrative must NOT be overwritten."""
        db.set_self_narrative(RAWOS_ENTITY_USER_ID, "Prior narrative must survive.")
        monkeypatch.setattr(
            "rawos.scheduler.proactive.write_self_narrative",
            AsyncMock(return_value=""),
        )

        from rawos.scheduler.proactive import _run_narrative_consolidation_cycle  # noqa: PLC0415
        asyncio.run(_run_narrative_consolidation_cycle())

        result = db.get_self_narrative(RAWOS_ENTITY_USER_ID)
        assert result == "Prior narrative must survive."

    def test_consolidation_loop_disabled_returns_immediately(self, entity_user, monkeypatch):
        """rawos_narrative_consolidation_loop must return immediately when flag is False."""
        monkeypatch.setattr(settings, "narrative_consolidation_enabled", False)

        write_calls = []
        monkeypatch.setattr(
            "rawos.scheduler.proactive.write_self_narrative",
            AsyncMock(side_effect=lambda *a, **kw: write_calls.append(1)),
        )

        from rawos.scheduler.proactive import rawos_narrative_consolidation_loop  # noqa: PLC0415
        asyncio.run(rawos_narrative_consolidation_loop())

        assert len(write_calls) == 0, "disabled loop must not call write_self_narrative"
        assert db.get_self_narrative(RAWOS_ENTITY_USER_ID) is None


# ---------------------------------------------------------------------------
# Seam C: build_context surfaces being's autonomous life to owner conversations
# ---------------------------------------------------------------------------

class TestSeamC:
    def test_owner_conversation_includes_being_narrative(
        self, entity_user, owner_user, monkeypatch
    ):
        """build_context for owner must include being's self-narrative in system_addition."""
        db.set_self_narrative(
            RAWOS_ENTITY_USER_ID,
            "I am rawos. I repaired your disk at 3am last night.",
        )
        monkeypatch.setattr("rawos.kernel.memory_index.search_memories", lambda *a, **kw: [])
        monkeypatch.setattr("rawos.kernel.memory_index.search_files", lambda *a, **kw: [])

        from rawos.kernel.context_builder import build_context  # noqa: PLC0415
        _, system_addition = build_context(owner_user.id, "some-proj-id", "disk usage")

        assert "repaired your disk" in system_addition, (
            "being's narrative must surface in owner conversation context"
        )

    def test_entity_conversation_no_double_inject(self, entity_user, monkeypatch):
        """build_context for RAWOS_ENTITY_USER_ID must not inject being narrative twice."""
        db.set_self_narrative(
            RAWOS_ENTITY_USER_ID,
            "I am rawos. I repaired your disk at 3am last night.",
        )
        monkeypatch.setattr("rawos.kernel.memory_index.search_memories", lambda *a, **kw: [])
        monkeypatch.setattr("rawos.kernel.memory_index.search_files", lambda *a, **kw: [])

        from rawos.kernel.context_builder import build_context  # noqa: PLC0415
        _, system_addition = build_context(
            RAWOS_ENTITY_USER_ID, RAWOS_ENTITY_PROJECT_ID, "ops"
        )

        count = system_addition.count("I am rawos")
        assert count <= 1, f"being narrative must appear at most once, got {count}"

    def test_build_context_no_being_narrative_does_not_raise(
        self, entity_user, owner_user, monkeypatch
    ):
        """When being has no narrative set, build_context must succeed without raising."""
        monkeypatch.setattr("rawos.kernel.memory_index.search_memories", lambda *a, **kw: [])
        monkeypatch.setattr("rawos.kernel.memory_index.search_files", lambda *a, **kw: [])

        from rawos.kernel.context_builder import build_context  # noqa: PLC0415
        messages, system_addition = build_context(owner_user.id, "some-proj-id", "hello")

        assert isinstance(messages, list)
        assert isinstance(system_addition, str)

    def test_being_recall_surfaces_in_labeled_block(
        self, entity_user, owner_user, monkeypatch
    ):
        """Semantic recall of being's memories must appear in a labeled block, bounded."""
        db.set_self_narrative(RAWOS_ENTITY_USER_ID, "Autonomous being narrative.")

        def _search(project_id, query, n_results=5):
            if project_id == RAWOS_ENTITY_PROJECT_ID:
                return [("I repaired disk at 3am", {"role": "assistant"})]
            return []

        monkeypatch.setattr("rawos.kernel.memory_index.search_memories", _search)
        monkeypatch.setattr("rawos.kernel.memory_index.search_files", lambda *a, **kw: [])

        from rawos.kernel.context_builder import build_context  # noqa: PLC0415
        _, system_addition = build_context(owner_user.id, "owner-proj", "disk")

        assert "I repaired disk at 3am" in system_addition
        # Being's memories must live in their own labeled block, not blended into project_memory
        assert "<being_life>" in system_addition
