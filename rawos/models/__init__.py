"""
rawos core primitives — the 8 fundamental units of the OS.
Every primitive is user-scoped: no query ever touches another user's data.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator
import time


def _now() -> int:
    return int(time.time())


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class UserTier(str, Enum):
    FREE       = "free"
    PRO        = "pro"
    ENTERPRISE = "enterprise"


class AgentStatus(str, Enum):
    DORMANT   = "dormant"
    ACTIVE    = "active"
    SUSPENDED = "suspended"
    ARCHIVED  = "archived"


class IntentStatus(str, Enum):
    PENDING   = "pending"
    ROUTING   = "routing"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED    = "failed"


class MemoryTier(str, Enum):
    WORKING    = "working"    # Redis — volatile, current agent context
    EPISODIC   = "episodic"   # SQLite — persisted conversation history
    SEMANTIC   = "semantic"   # SQLite + embedding — long-term queryable
    PROCEDURAL = "procedural" # SQLite — learned patterns and preferences


class MessageRole(str, Enum):
    USER        = "user"
    ASSISTANT   = "assistant"
    SYSTEM      = "system"
    TOOL_RESULT = "tool_result"


class ArtifactType(str, Enum):
    FILE     = "file"
    WEBSITE  = "website"
    CHART    = "chart"
    DOCUMENT = "document"
    CODE     = "code"


class SandboxLevel(str, Enum):
    READ    = "read"
    WRITE   = "write"
    NETWORK = "network"
    SYSTEM  = "system"


class EventType(str, Enum):
    AGENT_STARTED    = "agent_started"
    AGENT_SUSPENDED  = "agent_suspended"
    AGENT_ARCHIVED   = "agent_archived"
    TASK_COMPLETED   = "task_completed"
    TOOL_CALLED      = "tool_called"
    QUOTA_EXCEEDED   = "quota_exceeded"
    AUTH_SIGNUP      = "auth_signup"
    AUTH_LOGIN       = "auth_login"
    ERROR            = "error"


# ---------------------------------------------------------------------------
# Primitive 1: User
# ---------------------------------------------------------------------------

class User(BaseModel):
    id:                 str      = Field(default_factory=_uuid)
    email:              str
    password_hash:      str
    tier:               UserTier = UserTier.FREE
    token_budget_daily: int      = 100_000   # max tokens/day across all agents
    tokens_used_today:  int      = 0
    is_admin:           bool     = False
    stripe_customer_id: str | None = None
    created_at:         int      = Field(default_factory=_now)
    updated_at:         int      = Field(default_factory=_now)

    @field_validator("email")
    @classmethod
    def email_lowercase(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email address")
        return v


class UserPublic(BaseModel):
    """Safe subset — never includes password_hash."""
    id:                 str
    email:              str
    tier:               UserTier
    token_budget_daily: int
    tokens_used_today:  int
    is_admin:           bool
    stripe_customer_id: str | None
    created_at:         int


# ---------------------------------------------------------------------------
# Primitive 2: Project (workspace — replaces "workdir")
# ---------------------------------------------------------------------------

class Project(BaseModel):
    id:          str  = Field(default_factory=_uuid)
    user_id:     str
    name:        str
    description: str  = ""
    workdir:     str  = ""   # absolute fs path, set by db layer on create
    created_at:  int  = Field(default_factory=_now)
    updated_at:  int  = Field(default_factory=_now)

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("project name cannot be empty")
        if len(v) > 128:
            raise ValueError("project name max 128 chars")
        return v


# ---------------------------------------------------------------------------
# Primitive 3: Agent (running AI instance)
# ---------------------------------------------------------------------------

class Agent(BaseModel):
    id:          str         = Field(default_factory=_uuid)
    user_id:     str
    project_id:  str
    parent_id:   str | None  = None   # set when spawned by another agent
    status:      AgentStatus = AgentStatus.DORMANT
    goal:        str
    model:       str         = "deepseek-chat"
    token_used:  int         = 0
    created_at:  int         = Field(default_factory=_now)
    updated_at:  int         = Field(default_factory=_now)

    def transition(self, new_status: AgentStatus) -> "Agent":
        """Return new Agent with updated status; validates FSM transitions."""
        valid: dict[AgentStatus, set[AgentStatus]] = {
            AgentStatus.DORMANT:   {AgentStatus.ACTIVE},
            AgentStatus.ACTIVE:    {AgentStatus.SUSPENDED, AgentStatus.ARCHIVED},
            AgentStatus.SUSPENDED: {AgentStatus.ACTIVE, AgentStatus.ARCHIVED},
            AgentStatus.ARCHIVED:  set(),
        }
        if new_status not in valid[self.status]:
            raise ValueError(
                f"invalid agent transition: {self.status} → {new_status}"
            )
        return self.model_copy(update={"status": new_status, "updated_at": _now()})


# ---------------------------------------------------------------------------
# Primitive 4: Intent (structured, validated user request)
# ---------------------------------------------------------------------------

class Intent(BaseModel):
    id:                   str          = Field(default_factory=_uuid)
    user_id:              str
    project_id:           str
    agent_id:             str | None   = None
    raw_text:             str          # original user message, preserved verbatim
    goal:                 str          = ""   # extracted by intent router
    status:               IntentStatus = IntentStatus.PENDING
    result_artifact_id:   str | None   = None
    created_at:           int          = Field(default_factory=_now)
    updated_at:           int          = Field(default_factory=_now)

    @field_validator("raw_text")
    @classmethod
    def raw_text_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("intent raw_text cannot be empty")
        if len(v) > 32_000:
            raise ValueError("intent too long (max 32 000 chars)")
        return v


# ---------------------------------------------------------------------------
# Primitive 5: Memory (tiered storage)
# ---------------------------------------------------------------------------

class Memory(BaseModel):
    id:         str         = Field(default_factory=_uuid)
    user_id:    str
    project_id: str | None  = None
    agent_id:   str | None  = None
    tier:       MemoryTier
    role:       MessageRole
    content:    Any         # str for simple messages, list[dict] for tool-call chains
    embedding:  bytes | None = None   # local sentence-transformer vector
    created_at: int          = Field(default_factory=_now)
    expires_at: int | None   = None   # None = permanent


# ---------------------------------------------------------------------------
# Primitive 6: Artifact (output — file, website, chart, etc.)
# ---------------------------------------------------------------------------

class Artifact(BaseModel):
    id:         str          = Field(default_factory=_uuid)
    user_id:    str
    project_id: str
    agent_id:   str | None   = None
    intent_id:  str | None   = None
    type:       ArtifactType
    name:       str
    path:       str | None   = None   # absolute fs path when stored as file
    content:    str | None   = None   # inline content when small (<64 KB)
    mime_type:  str | None   = None
    size_bytes: int          = 0
    created_at: int          = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Primitive 7: Tool (typed capability in the registry)
# ---------------------------------------------------------------------------

class Tool(BaseModel):
    id:           str          = Field(default_factory=_uuid)
    name:         str          # unique slug, e.g. "bash", "read_file"
    description:  str
    input_schema: dict[str, Any]   # JSON Schema for input validation
    sandbox_level: SandboxLevel = SandboxLevel.READ
    enabled:      bool         = True
    created_at:   int          = Field(default_factory=_now)

    @field_validator("name")
    @classmethod
    def name_slug(cls, v: str) -> str:
        import re
        v = v.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]{0,63}$", v):
            raise ValueError("tool name must be lowercase slug, 1-64 chars")
        return v


# ---------------------------------------------------------------------------
# Primitive 8: Event (audit log entry)
# ---------------------------------------------------------------------------

class Event(BaseModel):
    id:         str       = Field(default_factory=_uuid)
    user_id:    str
    project_id: str | None = None
    agent_id:   str | None = None
    type:       EventType
    payload:    dict[str, Any] = Field(default_factory=dict)
    created_at: int        = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Phase 5: BillingEvent (immutable token consumption log)
# ---------------------------------------------------------------------------

class BillingEventType(str, Enum):
    INTENT         = "intent"
    MANUAL_CREDIT  = "manual_credit"
    RESET          = "reset"


class BillingEvent(BaseModel):
    id:         str              = Field(default_factory=_uuid)
    user_id:    str
    intent_id:  str | None       = None
    tokens:     int
    model:      str              = ""
    event_type: BillingEventType = BillingEventType.INTENT
    created_at: int              = Field(default_factory=_now)
