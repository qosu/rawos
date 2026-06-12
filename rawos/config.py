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

    # AI — DeepSeek only (one provider)
    deepseek_key:        str = ""
    deepseek_base_url:   str = "https://api.deepseek.com/v1"
    deepseek_model_pro:  str = "deepseek-v4-pro"
    deepseek_model_fast: str = "deepseek-v4-flash"

    # Internal compression (Groq — system-only, never user-facing)
    groq_keys: list[str] = []

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
    self_probe_enabled: bool = False   # dormant until a human enables it after a manual worktree cycle

    # Stage 3 ("Earned, Reversible Autonomy") — graduated auto-apply
    autonomy_auto_apply_enabled: bool = False   # dormant until a (repo, anomaly_domain)
    # class graduates (>=3 verified human-merged successes, see
    # kernel.track_record) AND an operator flips this flag

    # Autonomous server scan — operator-tunable cadence (cost vs reaction-time tradeoff)
    autonomous_scan_interval_s: int = 600   # seconds between full server scans

    # Phase 4 — multi-agent orchestration
    max_sub_agent_tokens:    int = 15_000
    max_orchestrator_tokens: int = 10_000
    max_parallel_agents:     int = 5

settings = Settings()
