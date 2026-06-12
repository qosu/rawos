"""
Phase 3 tests — semantic memory, summarisation, context builder, memory routes.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Memory Index
# ---------------------------------------------------------------------------

class TestMemoryIndex:
    """Uses a temp ChromaDB path to avoid polluting production data."""

    def setup_method(self):
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        from rawos.kernel import memory_index as mi

        self.tmp = tempfile.mkdtemp()
        # Override the module-level client/collection for tests
        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=self.tmp)
        col = client.get_or_create_collection(
            "test_memories", embedding_function=ef, metadata={"hnsw:space": "cosine"}
        )
        mi._client = client
        mi._col = col

    def test_upsert_and_search(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_memory("m1", "we built a coffee shop website in HTML", "p1", "u1", "episodic", "assistant", 1000)
        mi.upsert_memory("m2", "user wanted dark theme and modern design", "p1", "u1", "episodic", "user", 2000)
        results = mi.search_memories("p1", "coffee shop website", n_results=2)
        docs = [r[0] for r in results]
        assert any("coffee" in d for d in docs)

    def test_project_isolation(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_memory("px1", "project alpha secret info", "project_x", "u1", "episodic", "user", 1000)
        mi.upsert_memory("py1", "project beta public info", "project_y", "u1", "episodic", "user", 1000)
        results = mi.search_memories("project_x", "secret", n_results=5)
        # Should only return project_x docs
        for doc, meta in results:
            assert meta["project_id"] == "project_x"

    def test_delete_memory(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_memory("del1", "to be deleted content", "p1", "u1", "episodic", "user", 1000)
        mi.delete_memory("del1")
        # After deletion, search should not return it
        results = mi.search_memories("p1", "deleted content", n_results=5)
        ids_returned = [r[1] for r in results]  # metadata dicts
        # The deleted doc should not appear
        for doc, meta in results:
            assert "del1" not in meta.get("id", "")

    def test_upsert_file(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_file("f1", "<html><body>Bakery website</body></html>", "p1", "u1", "index.html", "index.html")
        results = mi.search_files("p1", "bakery", n_results=3)
        assert len(results) >= 1
        assert "Bakery" in results[0][0]

    def test_empty_project_returns_empty(self):
        from rawos.kernel import memory_index as mi
        results = mi.search_memories("nonexistent_project", "anything", n_results=5)
        assert results == []

    def test_upsert_is_idempotent(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_memory("idem1", "version 1 content", "p1", "u1", "episodic", "user", 1000)
        mi.upsert_memory("idem1", "version 2 content", "p1", "u1", "episodic", "user", 2000)
        # Should not raise; collection count should not grow
        results = mi.search_memories("p1", "version", n_results=10)
        idem_docs = [d for d, _ in results if "version" in d]
        assert len(idem_docs) == 1   # only one doc with id idem1

    def test_delete_batch(self):
        from rawos.kernel import memory_index as mi
        mi.upsert_memory("b1", "batch one", "p1", "u1", "episodic", "user", 1000)
        mi.upsert_memory("b2", "batch two", "p1", "u1", "episodic", "user", 2000)
        mi.delete_memories_batch(["b1", "b2"])
        results = mi.search_memories("p1", "batch", n_results=5)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Context Builder
# ---------------------------------------------------------------------------

class TestContextBuilder:
    def setup_method(self):
        import rawos.db as db
        import hashlib
        from rawos.models import User, Project
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email="ctx@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.project = db.create_project(Project(
            user_id=self.user.id, name="CtxTest", workdir=self.tmp,
        ))

        # Point ChromaDB to temp dir
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        from rawos.kernel import memory_index as mi
        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=self.tmp + "/chroma")
        col = client.get_or_create_collection("test_cb", embedding_function=ef, metadata={"hnsw:space": "cosine"})
        mi._client = client
        mi._col = col

    def test_empty_project_returns_empty_messages(self):
        from rawos.kernel.context_builder import build_context
        messages, sys_ctx = build_context(self.user.id, self.project.id, "test query")
        assert messages == []
        assert sys_ctx == ""

    def test_recent_messages_in_order(self):
        import rawos.db as db
        from rawos.models import Memory, MemoryTier, MessageRole
        db.save_memory(Memory(user_id=self.user.id, project_id=self.project.id,
                              tier=MemoryTier.EPISODIC, role=MessageRole.USER, content="hello"))
        db.save_memory(Memory(user_id=self.user.id, project_id=self.project.id,
                              tier=MemoryTier.EPISODIC, role=MessageRole.ASSISTANT, content="world"))
        from rawos.kernel.context_builder import build_context
        messages, _ = build_context(self.user.id, self.project.id, "test")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_system_memories_excluded_from_messages(self):
        import rawos.db as db
        from rawos.models import Memory, MemoryTier, MessageRole
        db.save_memory(Memory(user_id=self.user.id, project_id=self.project.id,
                              tier=MemoryTier.SEMANTIC, role=MessageRole.SYSTEM, content="summary"))
        from rawos.kernel.context_builder import build_context
        messages, _ = build_context(self.user.id, self.project.id, "test")
        # system role messages should not appear in messages list
        for m in messages:
            assert m["role"] in ("user", "assistant")

    def test_build_context_omits_continuity_when_no_user_model(self):
        from rawos.kernel.context_builder import build_context
        _, sys_ctx = build_context(self.user.id, self.project.id, "test")
        assert "<continuity>" not in sys_ctx
        assert sys_ctx == ""

    def test_build_context_injects_continuity_when_user_model_present(self):
        import json
        import rawos.db as db
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO user_model
                   (user_id, inferred_goal, goal_confidence, goal_domain, active_domains, recent_activity)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.user.id, "Ship the checkout flow", 0.82, "feature",
                 json.dumps(["feature", "api"]), json.dumps([])),
            )
        from rawos.kernel.context_builder import build_context
        _, sys_ctx = build_context(self.user.id, self.project.id, "test")
        assert "<continuity>" in sys_ctx
        assert "Ship the checkout flow" in sys_ctx
        if "<project_memory>" in sys_ctx:
            assert sys_ctx.index("<continuity>") < sys_ctx.index("<project_memory>")


# ---------------------------------------------------------------------------
# Summariser
# ---------------------------------------------------------------------------

class TestSummarizer:
    def test_empty_input_returns_empty(self):
        from rawos.kernel.summarizer import summarize_memories
        result = asyncio.run(summarize_memories([]))
        assert result == ""

    def test_returns_string(self):
        """Verifies summarize_memories returns str (may be empty if no keys)."""
        import rawos.db as db
        import hashlib
        from rawos.models import User, Project, Memory, MemoryTier, MessageRole
        tmp = tempfile.mkdtemp()
        db.init(os.path.join(tmp, "test.db"))
        user = db.create_user(User(email="sum@test.com", password_hash=hashlib.sha256(b"p").hexdigest()))
        project = db.create_project(Project(user_id=user.id, name="Sum", workdir=tmp))
        mems = [
            Memory(user_id=user.id, project_id=project.id, tier=MemoryTier.EPISODIC,
                   role=MessageRole.USER, content="Build me a landing page"),
            Memory(user_id=user.id, project_id=project.id, tier=MemoryTier.EPISODIC,
                   role=MessageRole.ASSISTANT, content="Created index.html with hero section"),
        ]
        from rawos.kernel.summarizer import summarize_memories
        result = asyncio.run(summarize_memories(mems))
        assert isinstance(result, str)   # may be "" if groq key invalid, that's ok


# ---------------------------------------------------------------------------
# Memory DB methods
# ---------------------------------------------------------------------------

class TestMemoryDB:
    def setup_method(self):
        import rawos.db as db
        import hashlib
        from rawos.models import User, Project
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email="memdb@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.project = db.create_project(Project(
            user_id=self.user.id, name="MemDB", workdir=self.tmp,
        ))

    def _make_memory(self, role="user", content="test content"):
        from rawos.models import Memory, MemoryTier, MessageRole
        return Memory(
            user_id=self.user.id, project_id=self.project.id,
            tier=MemoryTier.EPISODIC,
            role=MessageRole.USER if role == "user" else MessageRole.ASSISTANT,
            content=content,
        )

    def test_get_all_project_memories(self):
        import rawos.db as db
        for i in range(5):
            db.save_memory(self._make_memory(content=f"msg {i}"))
        all_mems = db.get_all_project_memories(self.user.id, self.project.id)
        assert len(all_mems) == 5

    def test_get_memory_count(self):
        import rawos.db as db
        assert db.get_memory_count(self.user.id, self.project.id) == 0
        db.save_memory(self._make_memory())
        assert db.get_memory_count(self.user.id, self.project.id) == 1

    def test_get_episodic_oldest(self):
        import rawos.db as db
        for i in range(10):
            db.save_memory(self._make_memory(content=f"msg {i}"))
        oldest = db.get_episodic_oldest(self.user.id, self.project.id, 3)
        assert len(oldest) == 3
        # Should be the 3 oldest (created_at ascending)
        assert oldest[0].content == "msg 0"

    def test_delete_memory_record(self):
        import rawos.db as db
        m = self._make_memory()
        db.save_memory(m)
        assert db.get_memory_count(self.user.id, self.project.id) == 1
        assert db.delete_memory_record(self.user.id, m.id)
        assert db.get_memory_count(self.user.id, self.project.id) == 0

    def test_delete_memory_wrong_user(self):
        import rawos.db as db
        m = self._make_memory()
        db.save_memory(m)
        assert not db.delete_memory_record("wrong-user", m.id)

    def test_delete_memories_batch(self):
        import rawos.db as db
        mems = [self._make_memory(content=f"m{i}") for i in range(5)]
        for m in mems:
            db.save_memory(m)
        ids = [m.id for m in mems[:3]]
        deleted = db.delete_memories_batch(self.user.id, ids)
        assert deleted == 3
        assert db.get_memory_count(self.user.id, self.project.id) == 2

    def test_get_memory_by_id(self):
        import rawos.db as db
        m = self._make_memory(content="specific content")
        db.save_memory(m)
        fetched = db.get_memory_by_id(self.user.id, m.id)
        assert fetched is not None
        assert fetched.content == "specific content"


# ---------------------------------------------------------------------------
# Memory routes (API)
# ---------------------------------------------------------------------------

class TestMemoryRoutes:
    def setup_method(self):
        import rawos.db as db
        import hashlib
        from rawos.models import User, Project
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email="memapi@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.project = db.create_project(Project(
            user_id=self.user.id, name="MemAPI", workdir=self.tmp,
        ))

        # Disable ChromaDB for route tests (no actual indexing needed)
        from rawos.kernel import memory_index as mi
        mi._col = None   # force re-init — will fail but routes handle it

    def _client_with_auth(self):
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        import rawos.auth as auth
        token = auth.create_access_token(self.user.id)
        return TestClient(app), {"Authorization": f"Bearer {token}"}

    def test_list_memories_empty(self):
        client, headers = self._client_with_auth()
        resp = client.get(f"/projects/{self.project.id}/memories", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_memory(self):
        client, headers = self._client_with_auth()
        resp = client.post(
            f"/projects/{self.project.id}/memories",
            json={"content": "user prefers TypeScript", "tier": "procedural", "role": "system"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["content"] == "user prefers TypeScript"
        assert data["tier"] == "procedural"

    def test_list_memories_after_create(self):
        client, headers = self._client_with_auth()
        client.post(f"/projects/{self.project.id}/memories",
                    json={"content": "test memory"}, headers=headers)
        resp = client.get(f"/projects/{self.project.id}/memories", headers=headers)
        assert len(resp.json()) == 1

    def test_delete_memory(self):
        import rawos.db as db
        from rawos.models import Memory, MemoryTier, MessageRole
        m = Memory(user_id=self.user.id, project_id=self.project.id,
                   tier=MemoryTier.SEMANTIC, role=MessageRole.SYSTEM, content="to delete")
        db.save_memory(m)
        client, headers = self._client_with_auth()
        resp = client.delete(f"/projects/{self.project.id}/memories/{m.id}", headers=headers)
        assert resp.status_code == 204

    def test_wrong_user_cannot_list(self):
        import rawos.db as db
        from rawos.models import User
        import hashlib
        other = db.create_user(User(email="other3@test.com", password_hash=hashlib.sha256(b"x").hexdigest()))
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        import rawos.auth as auth
        token = auth.create_access_token(other.id)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.get(f"/projects/{self.project.id}/memories", headers=headers)
        assert resp.status_code == 404

    def test_create_memory_empty_content_rejected(self):
        client, headers = self._client_with_auth()
        resp = client.post(f"/projects/{self.project.id}/memories",
                           json={"content": "   "}, headers=headers)
        assert resp.status_code == 400
