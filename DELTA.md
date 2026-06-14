CHANGED: rawos/installer/setup.py — SetupWizard.write_env() full rewrite
CHANGED: rawos/cli/main.py — `rawos setup` CLI flags rewritten to match
CHANGED: tests/test_setup_wizard.py — 19 tests (was 11), incl. e2e Settings load
  WHY: old write_env wrote 4 stale fields (OPENAI_API_KEY/TELEGRAM_*_ID/RAWOS_PORT) —
       fresh install crashed (LLM_API_KEY missing) + insecure default jwt_secret.
       Now writes all 17 required fields, jwt_secret random (secrets.token_urlsafe),
       db_path/chroma_path/workspaces_root/worktree_root/rawos_source_root remapped
       to --base-dir. create_dirs() also makes data/, data/chroma/, worktrees/.
VERIFY: pytest -q → 818 passed (was 696/817, zero regressions)
VERIFY: ssh root@178.104.255.197 → being greeting + rawos> (frontdoor unaffected)
NEXT: M5 "real" now (fresh install produces secure+complete .env). Phase 22/23/24
      sequencing still open — pending user decision.
