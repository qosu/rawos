"""rawos runtime configuration — all values from environment, never hardcoded."""
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str   = "0.0.0.0"
    port: int   = 8002
    debug: bool = False

    # Arch backend layer (kernel/arch) — overrides host OS detection for tests
    arch_override: str | None = None
    # Arch paths — configurable so non-Linux backends can point to their own roots
    worktree_root: str = "/root/.rawos-worktrees"
    rawos_source_root: str = "/root/rawos"

    # Database
    db_path: str = "/root/rawos/data/rawos.db"

    # Auth
    jwt_secret: str   = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int  = 15
    refresh_token_expire_days:   int  = 7

    # AI — single OpenAI-compatible provider
    llm_api_key:          str = ""
    llm_base_url:         str = "https://api.deepseek.com/v1"
    llm_agent_model:      str = ""
    llm_summarizer_model: str = ""
    llm_fallback_model:   str = ""
    llm_timeout_s:        int = 120

    # Workspaces root — each user project gets an isolated subdirectory
    workspaces_root: str = "/root/rawos/workspaces"

    # Token budgets
    free_tier_daily_tokens: int       = 50_000
    pro_tier_daily_tokens:  int       = 500_000


    # Phase 3 — semantic memory
    chroma_path:                str = "/root/rawos/data/chroma"
    summarize_after_n_memories: int = 80
    summarize_oldest_n:         int = 40
    semantic_context_results:   int = 5
    file_context_results:       int = 3



    # Phase 5 — production
    allowed_origins:         list[str] = ["https://downgrade.app"]
    redis_url:               str = "redis://localhost:6379/0"
    # Rate limits (requests per window)
    rate_limit_auth_rpm:     int = 5    # per IP, auth endpoints
    rate_limit_intent_rpm:   int = 10   # per user, intent endpoint
    rate_limit_api_rpm:      int = 120  # per user, general API
    metrics_token:           str = ""   # Bearer token to access /metrics; empty = localhost-only
    admin_emails:            list[str] = []  # emails granted admin access on first login
    enterprise_tier_daily_tokens: int = 10_000_000

    # Stripe billing
    stripe_key:               str = ""
    stripe_price_pro:         str = ""
    stripe_price_enterprise:  str = ""
    stripe_webhook_secret:    str = ""


    # Sandbox
    sandbox_docker: bool = True   # False in tests/dev; True = Docker container isolation

    # Phase 16 — self-modification
    self_probe_enabled: bool = True   # enabled 2026-06-12 after manual worktree cycle proof (commit c97781cc)

    # Phase 19 — narrative consolidation (Close the Living Loop)
    narrative_consolidation_enabled: bool = False
    narrative_consolidation_interval_s: int = 86400

    # Stage 3 ("Earned, Reversible Autonomy") — graduated auto-apply
    autonomy_auto_apply_enabled: bool = False   # dormant until a (repo, anomaly_domain)

    # Milestone 3 ('The being as the machine's operator') — managed file edits (R1)
    operator_enabled: bool = False   # dormant: auto-apply only when ON AND (operation_class,
    # target) graduated (>=3 verified successes). Propose-only when OFF or ungraduated.
    # class graduates (>=3 verified human-merged successes, see
    # kernel.track_record) AND an operator flips this flag

    # Milestone 6 ('Autonomous Operator Loop')
    operator_scan_enabled: bool = False
    operator_scan_interval_s: int = 600

    # Phase 23a ('Supervisor authority') — managed service lifecycle actions (R2)
    operator_service_enabled: bool = False   # dormant: auto-apply only when ON AND
    # (service_<action>, target) graduated (>=3 verified successes). Separate from
    # operator_enabled (R1 file edits) — each surface is enabled/reverted independently.
    # Propose-only when OFF or ungraduated.

    # Phase 22 (PAM safety-floor) — owner-approved pam.d write authority (R3-adjacent)
    operator_pam_enabled: bool = False   # dormant: no autonomous path; propose-only always.
    # PAM is R3-adjacent (single-root machine, deny root = permanent lockout) — no
    # graduation-based auto-apply exists. Only execute_approved_pam_edit() path (owner-explicit).

    # Phase 20 — system perception
    system_perception_enabled: bool = False
    system_perception_paths: list[str] = ["/root/rawos", "/etc/rawos", "/etc/systemd/system"]
    system_perception_debounce_s: float = 2.0

    # Phase 21 — system fs reflex
    system_fs_reflex_enabled: bool = False
    system_fs_reflex_interval_s: int = 30
    system_fs_reflex_lookback_s: int = 90

    # Phase 24a — eBPF kernel perception (read-only, machine-wide)
    ebpf_perception_enabled: bool = False
    ebpf_perception_comm_denylist: tuple[str, ...] = ()
    ebpf_perception_debounce_s: float = 1.0
    ebpf_perception_respawn_backoff_s: float = 5.0

    # Milestone 4 (\The window\) — Telegram front-door
    telegram_enabled:        bool = False
    telegram_bot_token:      str  = ""
    telegram_owner_chat_id:  int  = 0
    telegram_owner_email:    str  = ""
    telegram_project_id:     str  = ""   # empty = auto-create "telegram" project

    # Autonomous server scan — operator-tunable cadence (cost vs reaction-time tradeoff)
    autonomous_scan_interval_s: int = 600   # seconds between full server scans

    # Phase 4 — multi-agent orchestration
    max_sub_agent_tokens:    int = 15_000
    max_orchestrator_tokens: int = 10_000
    max_parallel_agents:     int = 5

settings = Settings()
