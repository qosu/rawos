"""rawos runtime configuration — all values from environment, never hardcoded."""
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict



# ---------------------------------------------------------------------------
# SHP.2 I-SEC3: systemd LoadCredential source — highest priority for secrets
# ---------------------------------------------------------------------------
_CRED_RUNTIME_DIR: str = "/run/credentials/rawos.service"
_CREDENTIAL_FIELDS: frozenset[str] = frozenset({
    "jwt_secret",
    "stripe_key",
    "stripe_webhook_secret",
    "hf_token",
    "llm_api_key",
    "telegram_bot_token",
})


class _SystemdCredentialsSource(PydanticBaseSettingsSource):
    """Read sensitive secrets from systemd LoadCredential runtime directory.

    Systemd makes credentials available at /run/credentials/<unit>/<id>
    when LoadCredential= is set in the service unit (mode 0400, root only).
    If a credential file does not exist (tests, non-systemd runs), the field
    is silently skipped and Pydantic falls back to the next source (env/dotenv).
    """

    def get_field_value(self, field, field_name):
        import os
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not cred_dir or field_name not in _CREDENTIAL_FIELDS:
            return None, field_name, False
        path = f"{cred_dir}/{field_name}"
        try:
            value = open(path).read().strip()
            return value, field_name, False
        except OSError:
            return None, field_name, False

    def __call__(self) -> dict:
        import os
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not cred_dir:
            return {}  # not running under systemd with LoadCredential= in effect
        data: dict = {}
        for name in _CREDENTIAL_FIELDS:
            path = f"{cred_dir}/{name}"
            try:
                data[name] = open(path).read().strip()
            except OSError:
                pass
        return data

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type["Settings"],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        **kwargs,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Credential files take priority over env/dotenv for sensitive secrets."""
        return (
            init_settings,
            _SystemdCredentialsSource(settings_cls),
            env_settings,
            dotenv_settings,
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
    # Phase 25 self-reload pending-state dir -- config-driven so a
    # twin-prove process (separate .env) gets its own state dir without
    # per-call injection (mirrors rawos_source_root above).
    self_reload_state_dir: str = "/root/.rawos-selfreload"

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

    # Phase 25 Stage 1+2 ('The Ouroboros') — safe self-reload (R-self)
    self_reload_enabled: bool = False   # dormant: owner-triggered path only.
    # Owner: `rawos selfreload arm-and-go <sha>`. Stage 2 autonomous path (I-SR10)
    # is a SEPARATE gate — operate_on_self_reload() checks self_reload_enabled AND
    # self_reload_autonomous_enabled AND graduation; both must be True to auto-apply.
    self_reload_autonomous_enabled: bool = False  # I-SR10: dormant until graduation proven
    # Twin-prove only (Phase 25 verification step 3) -- gates
    # /internal/self-reload/_debug-arm-and-swap. False on prod; the
    # rawos-selfprobe twin's .env sets this True. Default False means the
    # route 404s everywhere except the twin.
    self_reload_debug_endpoint_enabled: bool = False

    # M3 — Owned-Resource Operator (R-own). Ships dormant; flip after twin-prove of
    # workspace GC cycle with reversibility verified (first standing authority).
    operator_owned_enabled: bool = True   # ACTIVATED 2026-06-15
    # Workspace GC: move-to-trash after retention_days idle (floor: min_age_days).
    # Hard-delete trash after trash_retention_days. GC threshold triggers autonomous scan.
    owned_workspace_retention_days: int = 30
    owned_workspace_min_age_days: int = 7
    owned_trash_retention_days: int = 30
    owned_workspace_gc_threshold_gb: float = 2.0

    # M3 Stage 2 — R-venv: reversible dependency operator (I-VENV1..9).
    # Ships dormant (operator_venv_enabled=False). Owner flip after twin-prove.
    # Blast radius = no-boot → NEVER auto-activate without manual verification.
    operator_venv_enabled: bool = False
    venv_deadman_delay_s: int = 300
    venv_staging_root: str = '/root/rawos'  # .venvs/ lives here
    venv_old_retention_days: int = 7        # reap old venv after committed + N days

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

    # Phase 26 — Landlock self-MAC (active enforcement, dormant)
    landlock_self_mac_enabled: bool = True
    # Dormant (I-LL3): run_bash's preexec_fn applies DEFAULT_BEING_ENVELOPE
    # only when True AND landlock.supported() >= landlock.MIN_ABI (validated
    # at boot -- I-LL4, see api/app.py lifespan). Self+descendants only
    # (I-LL6): a bad envelope can only break the being's own sandboxed
    # commands, never sshd/boot/operator. See rawos/kernel/landlock.py.

    # Phase 24B — BPF LSM machine-wide MAC (active enforcement, dormant)
    # Ships DORMANT (I-LSM12): all bpf_lsm_* flags are no-ops until the
    # supervised 24B.0→24B.4 maintenance-window gates (GRUB + holder binary,
    # human-gated, NOT autonomous). Fact A (bpf in LSM list, needs GRUB+reboot)
    # and Fact B (attached holder) are independently gated; holder death →
    # kernel auto-detach → enforce gone without reboot (I-LSM2). Floor
    # (sshd/systemd/holder/rawos/git) compiled into immutable engine bytecode,
    # checked BEFORE policy maps — policy-map writes CANNOT deny floor (I-LSM5).
    bpf_lsm_enabled: bool = True           # 24B.2 ACTIVATED + 24B.4 GRADUATED 2026-06-15 (lsm= in GRUB_CMDLINE_LINUX, holder auto-start)
    bpf_lsm_mode: str = "enforce"              # audit (log-only) or enforce
    bpf_lsm_object_path: str = "/opt/rawos-bpf/engine.bpf.o"            # path to prebuilt CO-RE .o (empty = dormant)
    bpf_lsm_object_sha256: str = "08f2e291122677177ebabb2653831e0b4a450979ae37ae9b35ae054358273c52"          # sha256 of .o; mismatch → fail-closed (I-LSM11)
    bpf_lsm_holder_binary_path: str = "/opt/rawos-bpf/rawos-bpf-lsm-holder"     # path to holder binary
    bpf_lsm_holder_binary_sha256: str = "8eebb023810d3b378f364fc375157ee0c4f9863dc4ef9a8def09ae31e214a7c1"   # sha256 of holder binary (I-LSM11)
    bpf_lsm_holder_heartbeat_timeout_s: int = 30   # holder self-detaches after N missing beats
    bpf_lsm_revert_deadman_delay_s: int = 300       # transient revert unit delay (I-LSM8)
    bpf_lsm_deny_comm: tuple[str, ...] = ()         # process comms to deny (non-floor only)
    bpf_lsm_protected_comm: tuple[str, ...] = ()    # extra floor names beyond FLOOR_COMM

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

    # Phase 23-full — Unit/Boot Topology Authorship (active authority, dormant)
    # Ships DORMANT (I-UT11): all unit_topology_* flags are no-ops until the
    # supervised 23F.0→23F.4 maintenance-window gates (human-gated, NOT autonomous).
    operator_unit_topology_enabled: bool = True   # 23F.1 ACTIVATED propose-only 2026-06-16
    unit_topology_propose_only: bool = True
    unit_topology_revert_deadman_delay_s: int = 300

settings = Settings()
