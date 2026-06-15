"""
rawos database store — thin async wrapper over SQLite.
All public methods enforce user_id scoping by construction.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rawos.models import (
    User, UserPublic, Project, Agent, AgentStatus,
    Intent, IntentStatus, Memory, Artifact, Tool, Event,
)

_DB_PATH: Path | None = None


def init(db_path: str | Path) -> None:
    global _DB_PATH
    _DB_PATH = Path(db_path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _apply_schema()


def _apply_schema() -> None:
    """
    Apply all SQL migrations in alphabetical order.
    Skips files prefixed 'postgres_' (PostgreSQL-only).
    For ALTER TABLE statements, silently ignores 'duplicate column' errors.
    """
    import re as _re
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        if migration_file.name.startswith("postgres_"):
            continue
        sql = migration_file.read_text()
        # Split into individual statements so we can handle errors per-statement
        statements = [s.strip() for s in _re.split(r";\s*(?:\n|$)", sql) if s.strip()]
        with _conn() as conn:
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    err = str(e).lower()
                    # Idempotent: ignore "already exists" and "duplicate column" errors
                    if "already exists" in err or "duplicate column" in err:
                        continue
                    raise


@contextmanager
def _conn():
    assert _DB_PATH, "db.init() must be called before any db operation"
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(user: User) -> User:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO users
               (id, email, password_hash, tier, token_budget_daily,
                tokens_used_today, budget_reset_date, is_admin, stripe_customer_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (user.id, user.email, user.password_hash, user.tier.value,
             user.token_budget_daily, user.tokens_used_today,
             "", 1 if user.is_admin else 0, user.stripe_customer_id,
             user.created_at, user.updated_at),
        )
    return user


def get_user_by_email(email: str) -> User | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_id(user_id: str) -> User | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_user(row) if row else None


def consume_tokens(user_id: str, tokens: int) -> None:
    """Atomically add tokens_used_today; caller must check budget before calling."""
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET tokens_used_today = tokens_used_today + ?, updated_at = ? WHERE id = ?",
            (tokens, _now(), user_id),
        )


def reset_daily_budget(user_id: str) -> None:
    import datetime
    today = datetime.date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET tokens_used_today = 0, budget_reset_date = ?, updated_at = ? WHERE id = ?",
            (today, _now(), user_id),
        )


def _row_to_user(row: sqlite3.Row) -> User:
    keys = row.keys()
    return User(
        id=row["id"], email=row["email"], password_hash=row["password_hash"],
        tier=row["tier"], token_budget_daily=row["token_budget_daily"],
        tokens_used_today=row["tokens_used_today"],
        is_admin=bool(row["is_admin"]) if "is_admin" in keys else False,
        stripe_customer_id=row["stripe_customer_id"] if "stripe_customer_id" in keys else None,
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def set_admin(user_id: str, is_admin: bool) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET is_admin = ?, updated_at = ? WHERE id = ?",
            (1 if is_admin else 0, _now(), user_id),
        )


def get_all_users(limit: int = 200) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_user(r) for r in rows]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(project: Project) -> Project:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO projects (id, user_id, name, description, workdir, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (project.id, project.user_id, project.name, project.description,
             project.workdir, project.created_at, project.updated_at),
        )
    return project


def get_projects(user_id: str) -> list[Project]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(user_id: str, project_id: str) -> Project | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return _row_to_project(row) if row else None


def update_project(user_id: str, project_id: str, **fields: Any) -> Project | None:
    allowed = {"name", "description", "workdir"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_project(user_id, project_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ? AND user_id = ?",
            (*updates.values(), project_id, user_id),
        )
    return get_project(user_id, project_id)


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"], user_id=row["user_id"], name=row["name"],
        description=row["description"], workdir=row["workdir"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def create_agent(agent: Agent) -> Agent:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO agents
               (id, user_id, project_id, parent_id, status, goal, model, token_used, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (agent.id, agent.user_id, agent.project_id, agent.parent_id,
             agent.status.value, agent.goal, agent.model,
             agent.token_used, agent.created_at, agent.updated_at),
        )
    return agent


def update_agent_status(user_id: str, agent_id: str, status: AgentStatus) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (status.value, _now(), agent_id, user_id),
        )


def add_agent_tokens(user_id: str, agent_id: str, tokens: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE agents SET token_used = token_used + ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (tokens, _now(), agent_id, user_id),
        )


def get_agent(user_id: str, agent_id: str) -> Agent | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE id = ? AND user_id = ?",
            (agent_id, user_id),
        ).fetchone()
    return _row_to_agent(row) if row else None


def _row_to_agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"], user_id=row["user_id"], project_id=row["project_id"],
        parent_id=row["parent_id"], status=AgentStatus(row["status"]),
        goal=row["goal"], model=row["model"], token_used=row["token_used"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

def create_intent(intent: Intent) -> Intent:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO intents
               (id, user_id, project_id, agent_id, raw_text, goal, status,
                result_artifact_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (intent.id, intent.user_id, intent.project_id, intent.agent_id,
             intent.raw_text, intent.goal, intent.status.value,
             intent.result_artifact_id, intent.created_at, intent.updated_at),
        )
    return intent


def update_intent(user_id: str, intent_id: str, **fields: Any) -> None:
    allowed = {"agent_id", "goal", "status", "result_artifact_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as conn:
        conn.execute(
            f"UPDATE intents SET {set_clause} WHERE id = ? AND user_id = ?",
            (*updates.values(), intent_id, user_id),
        )


def get_project_history(user_id: str, project_id: str, limit: int = 60) -> list[Intent]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM intents WHERE project_id = ? AND user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (project_id, user_id, limit),
        ).fetchall()
    return [_row_to_intent(r) for r in reversed(rows)]


def _row_to_intent(row: sqlite3.Row) -> Intent:
    return Intent(
        id=row["id"], user_id=row["user_id"], project_id=row["project_id"],
        agent_id=row["agent_id"], raw_text=row["raw_text"], goal=row["goal"],
        status=IntentStatus(row["status"]),
        result_artifact_id=row["result_artifact_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

def save_memory(memory: Memory) -> Memory:
    content_json = json.dumps(memory.content, ensure_ascii=False)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO memories
               (id, user_id, project_id, agent_id, tier, role, content,
                embedding, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (memory.id, memory.user_id, memory.project_id, memory.agent_id,
             memory.tier.value, memory.role.value, content_json,
             memory.embedding, memory.created_at, memory.expires_at),
        )
    return memory


def get_project_memories(user_id: str, project_id: str, tier: str, limit: int = 100) -> list[Memory]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM memories
               WHERE project_id = ? AND user_id = ? AND tier = ?
               AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at ASC LIMIT ?""",
            (project_id, user_id, tier, _now(), limit),
        ).fetchall()
    return [_row_to_memory(r) for r in rows]


def purge_expired_memories() -> int:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (_now(),),
        )
        return cursor.rowcount


def _row_to_memory(row: sqlite3.Row) -> Memory:
    from rawos.models import MemoryTier, MessageRole
    content = json.loads(row["content"])
    return Memory(
        id=row["id"], user_id=row["user_id"], project_id=row["project_id"],
        agent_id=row["agent_id"], tier=MemoryTier(row["tier"]),
        role=MessageRole(row["role"]), content=content,
        embedding=row["embedding"], created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


# ---------------------------------------------------------------------------
# Events (append-only audit log)
# ---------------------------------------------------------------------------

def log_event(event: Event) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO events (id, user_id, project_id, agent_id, type, payload, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (event.id, event.user_id, event.project_id, event.agent_id,
             event.type.value, json.dumps(event.payload, ensure_ascii=False),
             event.created_at),
        )


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

def save_refresh_token(token_id: str, user_id: str, token_hash: str, expires_at: int) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, created_at)
               VALUES (?,?,?,?,?)""",
            (token_id, user_id, token_hash, expires_at, _now()),
        )


def get_refresh_token(token_hash: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ? AND expires_at > ?",
            (token_hash, _now()),
        ).fetchone()
    return dict(row) if row else None


def revoke_refresh_token(token_hash: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM refresh_tokens WHERE token_hash = ?", (token_hash,))


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

def save_artifact(artifact: Artifact) -> Artifact:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO artifacts
               (id, user_id, project_id, agent_id, intent_id, type, name,
                path, content, mime_type, size_bytes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (artifact.id, artifact.user_id, artifact.project_id,
             artifact.agent_id, artifact.intent_id, artifact.type.value,
             artifact.name, artifact.path, artifact.content,
             artifact.mime_type, artifact.size_bytes, artifact.created_at),
        )
    return artifact


def get_artifact(user_id: str, artifact_id: str) -> Artifact | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id = ? AND user_id = ?",
            (artifact_id, user_id),
        ).fetchone()
    return _row_to_artifact(row) if row else None


def get_project_artifacts(user_id: str, project_id: str) -> list[Artifact]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE user_id = ? AND project_id = ? ORDER BY created_at DESC",
            (user_id, project_id),
        ).fetchall()
    return [_row_to_artifact(r) for r in rows]


def delete_artifact(user_id: str, artifact_id: str) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM artifacts WHERE id = ? AND user_id = ?",
            (artifact_id, user_id),
        )
    return cursor.rowcount > 0


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    from rawos.models import ArtifactType
    return Artifact(
        id=row["id"], user_id=row["user_id"], project_id=row["project_id"],
        agent_id=row["agent_id"], intent_id=row["intent_id"],
        type=ArtifactType(row["type"]), name=row["name"],
        path=row["path"], content=row["content"],
        mime_type=row["mime_type"], size_bytes=row["size_bytes"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Memory — Phase 3 additions
# ---------------------------------------------------------------------------

def get_all_project_memories(user_id: str, project_id: str, limit: int = 200) -> list[Memory]:
    """All tiers, newest first, for memory UI."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM memories
               WHERE project_id = ? AND user_id = ?
               AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at DESC LIMIT ?""",
            (project_id, user_id, _now(), limit),
        ).fetchall()
    return [_row_to_memory(r) for r in rows]


def get_episodic_oldest(user_id: str, project_id: str, n: int) -> list[Memory]:
    """Return the n oldest episodic memories for summarization."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM memories
               WHERE project_id = ? AND user_id = ? AND tier = 'episodic'
               ORDER BY created_at ASC LIMIT ?""",
            (project_id, user_id, n),
        ).fetchall()
    return [_row_to_memory(r) for r in rows]


def get_memory_count(user_id: str, project_id: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return row[0] if row else 0


def get_memory_by_id(user_id: str, memory_id: str) -> Memory | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        ).fetchone()
    return _row_to_memory(row) if row else None


def delete_memory_record(user_id: str, memory_id: str) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
    return cursor.rowcount > 0


def delete_memories_batch(user_id: str, memory_ids: list[str]) -> int:
    if not memory_ids:
        return 0
    placeholders = ",".join("?" * len(memory_ids))
    with _conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM memories WHERE user_id = ? AND id IN ({placeholders})",
            [user_id] + list(memory_ids),
        )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Phase 4 — Agent tree queries
# ---------------------------------------------------------------------------

def get_project_agents(user_id: str, project_id: str, limit: int = 50) -> list:
    from rawos.models import Agent as AgentModel
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM agents
               WHERE user_id = ? AND project_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, project_id, limit),
        ).fetchall()
    return [_row_to_agent(r) for r in rows]


def get_agent_children(user_id: str, parent_id: str) -> list:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM agents
               WHERE user_id = ? AND parent_id = ?
               ORDER BY created_at ASC""",
            (user_id, parent_id),
        ).fetchall()
    return [_row_to_agent(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 5 — Billing events
# ---------------------------------------------------------------------------

def create_billing_event(user_id: str, tokens: int, model: str = "",
                          intent_id: str | None = None,
                          event_type: str = "intent",
                          cache_hit_tokens: int = 0,
                          cache_miss_tokens: int = 0,
                          output_tokens: int = 0,
                          cost_usd_micros: int | None = None) -> None:
    from rawos.models import BillingEvent
    ev = BillingEvent(user_id=user_id, intent_id=intent_id,
                      tokens=tokens, model=model, event_type=event_type,
                      cache_hit_tokens=cache_hit_tokens,
                      cache_miss_tokens=cache_miss_tokens,
                      output_tokens=output_tokens,
                      cost_usd_micros=cost_usd_micros)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO billing_events (id, user_id, intent_id, tokens, model, event_type, created_at,
                                            cache_hit_tokens, cache_miss_tokens, output_tokens, cost_usd_micros)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ev.id, ev.user_id, ev.intent_id, ev.tokens, ev.model, ev.event_type.value, ev.created_at,
             ev.cache_hit_tokens, ev.cache_miss_tokens, ev.output_tokens, ev.cost_usd_micros),
        )


def get_billing_events(user_id: str, limit: int = 100) -> list:
    from rawos.models import BillingEvent
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_events WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [BillingEvent(
        id=r["id"], user_id=r["user_id"], intent_id=r["intent_id"],
        tokens=r["tokens"], model=r["model"], event_type=r["event_type"],
        cache_hit_tokens=r["cache_hit_tokens"], cache_miss_tokens=r["cache_miss_tokens"],
        output_tokens=r["output_tokens"], cost_usd_micros=r["cost_usd_micros"],
        created_at=r["created_at"],
    ) for r in rows]


def get_admin_stats() -> dict:
    """Aggregate stats for admin dashboard."""
    import time
    today_start = int(time.time()) - 86400
    with _conn() as conn:
        users_total  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        intents_today = conn.execute(
            "SELECT COUNT(*) FROM intents WHERE created_at > ?", (today_start,)
        ).fetchone()[0]
        tokens_today = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM billing_events WHERE created_at > ?", (today_start,)
        ).fetchone()[0]
        errors_today = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'error' AND created_at > ?", (today_start,)
        ).fetchone()[0]
        active_agents = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status = 'active'"
        ).fetchone()[0]
    return {
        "users_total":   users_total,
        "intents_today": intents_today,
        "tokens_today":  tokens_today,
        "errors_today":  errors_today,
        "active_agents": active_agents,
    }


def get_recent_errors(limit: int = 50) -> list[dict]:
    """Return recent error events from the audit log."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, user_id, project_id, agent_id, type, payload, created_at
               FROM events WHERE type = 'error'
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    import json as _json
    return [
        {
            "id": r["id"], "user_id": r["user_id"], "type": r["type"],
            "payload": _json.loads(r["payload"]) if r["payload"] else {},
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Phase 5 — Stripe customer management
# ---------------------------------------------------------------------------

def set_stripe_customer_id(user_id: str, customer_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE id = ?",
            (customer_id, _now(), user_id),
        )


def get_user_by_stripe_customer_id(customer_id: str) -> User | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
    return _row_to_user(row) if row else None


def update_user_tier(user_id: str, tier: str) -> None:
    from rawos.config import settings
    tier_budgets = {
        "free":       settings.free_tier_daily_tokens,
        "pro":        settings.pro_tier_daily_tokens,
        "enterprise": settings.enterprise_tier_daily_tokens,
    }
    budget = tier_budgets.get(tier, settings.free_tier_daily_tokens)
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET tier = ?, token_budget_daily = ?, updated_at = ? WHERE id = ?",
            (tier, budget, _now(), user_id),
        )


def get_workdir_by_project_id(project_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT workdir FROM projects WHERE id = ? LIMIT 1",
            (project_id,),
        ).fetchone()
    return row["workdir"] if row else None


def get_last_chat_at(user_id: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_chat_at FROM user_model WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["last_chat_at"] if row else 0


def set_last_chat_at(user_id: str, ts: int) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_model (user_id, last_chat_at)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET last_chat_at = excluded.last_chat_at""",
            (user_id, ts),
        )


def get_self_narrative(user_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT self_narrative FROM user_model WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["self_narrative"] if row else None


def set_self_narrative(user_id: str, text: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO user_model (user_id, self_narrative)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET self_narrative = excluded.self_narrative""",
            (user_id, text),
        )


def get_proactive_artifacts_since(
    user_id: str, since_ts: int, limit: int = 10
) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT goal, confidence, file_path, created_at
               FROM proactive_artifacts
               WHERE user_id = ? AND created_at > ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, since_ts, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_operator_track_record(
    user_id: str, operation_class: str, target: str
) -> "TrackRecordState":
    """Return the operator track-record state for (user, operation_class, target).

    Returns a fresh untrusted TrackRecordState (verified_successes=0, graduated=False)
    if no row exists — identical default-on-miss semantics to get_track_record().
    """
    from rawos.kernel.track_record import TrackRecordState
    with _conn() as conn:
        row = conn.execute(
            """SELECT verified_successes, graduated, last_outcome,
                      last_target_sha, pending_since
               FROM operator_track_record
               WHERE user_id = ? AND operation_class = ? AND target = ?""",
            (user_id, operation_class, target),
        ).fetchone()
    if row is None:
        return TrackRecordState()
    return TrackRecordState(
        verified_successes=row["verified_successes"],
        graduated=bool(row["graduated"]),
        last_outcome=row["last_outcome"],
        last_fix_sha=row["last_target_sha"],
        # last_fix_branch stays None — no branch concept for file edits
        pending_since=row["pending_since"],
    )


def update_operator_track_record(
    user_id: str,
    operation_class: str,
    target: str,
    *,
    verified: bool,
    now: int,
) -> "TrackRecordState":
    """Advance and persist the operator track record for one operation outcome.

    `verified=True`  → apply succeeded and validator passed (no anomaly).
    `verified=False` → apply was rolled back by the validator (anomaly).

    Reuses kernel.track_record._advance_state verbatim (pure, class-agnostic).
    `fix_branch` is always None for file edits (no git branch concept); the
    stability window advances on two consecutive verified=True calls, matching
    the code-fix graduation discipline.

    Returns the new state.  Does NOT write to operator_track_record when the
    state is unchanged (same guard as update_track_record in track_record.py).
    """
    import logging as _logging
    _log = _logging.getLogger("rawos.db.operator_track_record")
    from rawos.kernel.track_record import _advance_state, GRADUATION_THRESHOLD

    current = get_operator_track_record(user_id, operation_class, target)
    new = _advance_state(
        current,
        anomaly_present=not verified,
        branch_merged=True,
        fix_branch=None,
        fix_sha=None,
        now=now,
    )
    if new == current:
        return new

    with _conn() as conn:
        conn.execute(
            """INSERT INTO operator_track_record
                   (user_id, operation_class, target, verified_successes,
                    graduated, last_outcome, last_target_sha, pending_since,
                    updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, operation_class, target) DO UPDATE SET
                   verified_successes = excluded.verified_successes,
                   graduated          = excluded.graduated,
                   last_outcome       = excluded.last_outcome,
                   last_target_sha    = excluded.last_target_sha,
                   pending_since      = excluded.pending_since,
                   updated_at         = excluded.updated_at""",
            (
                user_id, operation_class, target,
                new.verified_successes, int(new.graduated),
                new.last_outcome, new.last_fix_sha, new.pending_since, now,
            ),
        )
    if new.graduated and not current.graduated:
        _log.info(
            "operator: class graduated operation_class=%s target=%s after %d verified successes",
            operation_class, target, new.verified_successes,
        )
    return new


def get_managed_file_target(user_id: str, target_path: str) -> dict | None:
    """Return the allowlist row for (user, target_path), or None if not allowlisted."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT target_path, validator_cmd, created_at
               FROM managed_file_targets
               WHERE user_id = ? AND target_path = ?""",
            (user_id, target_path),
        ).fetchone()
    return dict(row) if row else None


def list_managed_file_targets(user_id: str) -> list[dict]:
    """Return all allowlisted (target_path, validator_cmd) rows for user_id."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT target_path, validator_cmd, created_at
               FROM managed_file_targets
               WHERE user_id = ?""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_managed_file_target(user_id: str, target_path: str, validator_cmd: str) -> None:
    """Register (target_path, validator_cmd) as an owner-allowlisted target.

    Upserts: re-registering a path updates its validator_cmd.
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO managed_file_targets (user_id, target_path, validator_cmd)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, target_path) DO UPDATE SET
                   validator_cmd = excluded.validator_cmd""",
            (user_id, target_path, validator_cmd),
        )


def remove_managed_file_target(user_id: str, target_path: str) -> None:
    """Remove target_path from the owner allowlist (no-op if not present)."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM managed_file_targets WHERE user_id = ? AND target_path = ?",
            (user_id, target_path),
        )


def get_managed_service_target(user_id: str, service_name: str) -> dict | None:
    """Return the allowlist row for (user, service_name), or None if not allowlisted."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT service_name, validator_cmd, created_at
               FROM managed_service_targets
               WHERE user_id = ? AND service_name = ?""",
            (user_id, service_name),
        ).fetchone()
    return dict(row) if row else None


def list_managed_service_targets(user_id: str) -> list[dict]:
    """Return all allowlisted (service_name, validator_cmd) rows for user_id."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT service_name, validator_cmd, created_at
               FROM managed_service_targets
               WHERE user_id = ?""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_managed_service_target(user_id: str, service_name: str, validator_cmd: str) -> None:
    """Register (service_name, validator_cmd) as an owner-allowlisted target.

    Upserts: re-registering a service updates its validator_cmd.
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO managed_service_targets (user_id, service_name, validator_cmd)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, service_name) DO UPDATE SET
                   validator_cmd = excluded.validator_cmd""",
            (user_id, service_name, validator_cmd),
        )


def remove_managed_service_target(user_id: str, service_name: str) -> None:
    """Remove service_name from the owner allowlist (no-op if not present)."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM managed_service_targets WHERE user_id = ? AND service_name = ?",
            (user_id, service_name),
        )


# ---------------------------------------------------------------------------
# Phase 22 — PAM target allowlist (managed_pam_targets)
# ---------------------------------------------------------------------------

def get_managed_pam_target(user_id: str, pam_file: str) -> dict | None:
    """Return the allowlist row for (user, pam_file), or None if not allowlisted."""
    with _conn() as conn:
        row = conn.execute(
            """SELECT pam_file, created_at
               FROM managed_pam_targets
               WHERE user_id = ? AND pam_file = ?""",
            (user_id, pam_file),
        ).fetchone()
    return dict(row) if row else None


def list_managed_pam_targets(user_id: str) -> list[dict]:
    """Return all allowlisted pam_file rows for user_id."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT pam_file, created_at
               FROM managed_pam_targets
               WHERE user_id = ?""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_managed_pam_target(user_id: str, pam_file: str) -> None:
    """Register pam_file as an owner-allowlisted PAM target for user_id.

    Upsert: re-registering is idempotent (updates created_at is not needed —
    the constraint is existence, not recency).
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO managed_pam_targets (user_id, pam_file)
               VALUES (?, ?)
               ON CONFLICT(user_id, pam_file) DO NOTHING""",
            (user_id, pam_file),
        )


def remove_managed_pam_target(user_id: str, pam_file: str) -> None:
    """Remove pam_file from the owner allowlist (no-op if not present)."""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM managed_pam_targets WHERE user_id = ? AND pam_file = ?",
            (user_id, pam_file),
        )


# ---------------------------------------------------------------------------
# Phase 25 Stage 1 — self-reload outcome ledger (managed_self_reload)
# ---------------------------------------------------------------------------

def record_self_reload_outcome(
    old_sha: str,
    new_sha: str,
    outcome: str,
    *,
    autonomous: bool = False,
) -> None:
    """Append one row to the self-reload history ledger.

    outcome must be one of 'committed' | 'resurrected' | 'liveness_failed'
    (enforced by the migration 026 CHECK constraint — invalid values raise
    sqlite3.IntegrityError).
    autonomous=True when triggered by operate_on_self_reload() (Stage 2, I-SR11).
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO managed_self_reload (old_sha, new_sha, outcome, autonomous)
               VALUES (?, ?, ?, ?)""",
            (old_sha, new_sha, outcome, 1 if autonomous else 0),
        )


def list_self_reload_history(limit: int = 20) -> list[dict]:
    """Return the most recent self-reload outcomes, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT old_sha, new_sha, outcome, autonomous, created_at
               FROM managed_self_reload
               ORDER BY rowid DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# M3 — Owned-Resource Operator (R-own) outcome ledger (owned_resource_history)
# ---------------------------------------------------------------------------

def record_owned_op_outcome(
    op_type: str,
    target_summary: str,
    outcome: str,
    *,
    autonomous: bool = False,
    trash_ref: str | None = None,
) -> None:
    """Append one row to the owned-resource-history ledger (I-OWN6).

    outcome must be one of 'applied' | 'proposed' | 'refused' | 'failed'
    (enforced by migration 028 CHECK constraint — invalid values raise
    sqlite3.IntegrityError).
    autonomous=True when triggered by _maybe_autonomous_owned_maintenance().
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO owned_resource_history
               (op_type, target_summary, outcome, autonomous, trash_ref)
               VALUES (?, ?, ?, ?, ?)""",
            (op_type, target_summary, outcome, 1 if autonomous else 0, trash_ref),
        )


def list_owned_resource_history(limit: int = 20) -> list[dict]:
    """Return the most recent owned-resource outcomes, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, op_type, target_summary, outcome, autonomous, trash_ref, created_at
               FROM owned_resource_history
               ORDER BY rowid DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_active_workspace_dirs() -> list[str]:
    """Return workdir paths of projects bound to non-terminal intents.

    Used by owned-resource GC to protect workspaces that are currently in use
    (I-OWN2: active-intent workspace floor).
    """
    with _conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT p.workdir
               FROM projects p
               INNER JOIN intents i ON i.project_id = p.id
               WHERE i.status IN ('executing', 'pending', 'routing')
                 AND p.workdir IS NOT NULL
                 AND p.workdir != ''""",
        ).fetchall()
    return [row[workdir] for row in rows]
