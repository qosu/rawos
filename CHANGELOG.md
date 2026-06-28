# Changelog

All notable changes to rawos are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.0] — 2026-06-28

### Added

**Open-source readiness**
- LICENSE (MIT)
- README.md: architecture, safety floors, quickstart, substrate vs API-tier requirements
- SECURITY.md: full threat model (T1-T7), twelve invariants (I-SEC1-12), SHP overview, private reporting channel
- CONTRIBUTING.md: TDD mandate, coding standards, high-consequence module guide
- VISION.md: philosophy — zero-lockout doctrine, substrate ownership thesis, honest gap list
- AGENTS.md: AI-agent contributor guide with kernel module table and invariant constraints
- .github/workflows/ci.yml: pytest + ruff lint on push/PR (Python 3.12)
- Dockerfile: multi-stage, non-root, API-tier only (substrate features documented-absent)
- docker-compose.yml: API + Redis, read-only rootfs, no-new-privileges
- .env.example: all 23 environment variables with placeholders
- .github/ISSUE_TEMPLATE/bug_report.md + feature_request.md
- .github/pull_request_template.md with invariant-aware checklist

**Security Hardening Program (SHP.2-SHP.7)**
- SHP.2: secrets migration to systemd LoadCredential; _SystemdCredentialsSource gated on CREDENTIALS_DIRECTORY
- SHP.3: sandbox isolation hardening (capability gate, container enforcement, tenant boundary)
- SHP.4: trust boundary enforcement (provenance tagging, output guard, SSRF deny)
- SHP.5: tamper-evident audit chain (hash-chained, ECDSA-signed, off-box mirror)
- SHP.6: BPF LSM enforce flip wired in _start_bpf_lsm_heartbeat_loop
- SHP.7: supply chain and boot integrity hardening (I-SEC9)

**Phase 26 — Landlock self-MAC**
- Landlock v4 restricts AI's own bash subprocesses at kernel level
- Structurally zero-lockout: no path produces a locked-out substrate

**Phase 23-full — Unit topology authorship**
- AI entity authors and manages its own systemd units
- Stack inversion complete: AI IS the policy layer, not a tenant of it

### Fixed

- audit_chain.py: replaced hardcoded server IP with socket.gethostname()
- proactive.py: per-user exponential backoff on LLM 429/timeout failures
- cli: disabled Rich markup parsing for LLM chunk/agent_output events
- intent: bounded LLM inference call with 8s timeout + graceful fallback
- landlock.py: added rawos_source_root to rw_paths for git-worktree subprocesses

### Removed

- .env from git history (git-filter-repo rewrite; 2.8 GB -> 2.2 MB)
- data/, rawos.db, venv/ from git history
- fix_app.py (orphan one-shot patch script)

### Changed

- Internal planning documents moved to docs/internal/
  (PLAN.md, CHECKPOINT.md, DELTA.md, .invariants.md)
- Version corrected: 0.1.0 -> 0.6.0 in pyproject.toml

---

## [0.1.0] — 2026-05-01 (estimated)

Initial implementation: 8 primitives, FastAPI, JWT auth, PostgreSQL,
agent loop, LLM integration, 39 passing tests.
