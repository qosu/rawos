# rawos — PLAN.md

Last updated: 2026-06-12

---

## What rawos is

rawos is not a coding assistant. It is an autonomous AI entity that inhabits a software ecosystem and acts without being asked.

The thesis: **an AI that acts → self-verifies → self-rates — with no human validation gate.**

Every other AI system in production today is reactive. It waits. It responds. It requires a human to pull the trigger. rawos does not. rawos watches, probes, decides, fixes, verifies, and rates its own work — in a continuous loop — without anyone asking it to.

This is not a product feature. It is a claim about what AI can become.

---

## Product Roadmap — The 5-Milestone Arc

rawos is built toward one goal: **the first OS where AI lives inside it.** Stack inversion:
AI IS the OS. Host kernel (Linux/macOS/Windows) = interchangeable CPU architecture.
Single-owner personal OS. This arc is permanent — it does not change between sessions.

| # | Milestone | Core idea | Status |
|---|-----------|-----------|--------|
| 1 | **Front door = the being** | `ssh host` → rawos AI, not bash. Login *is* the being. | **DONE** — Phase 13, live on prod |
| 2 | **One continuous life** | Persistent memory + 1 identity across every session. Being never resets. | **DONE** — user_model + self-narrative (commits `6c1c38ac`, `789150df`) |
| 3 | **Being as operator** | AI manages real host files under earned-reversible-autonomy (R0–R3 tiers). | **DONE** — R1 operator + `manage_file` tool, 632 tests |
| 4 | **The window** | Phone + voice client that connects to the being from anywhere. | **IN PROGRESS** |
| 5 | **Installable substrate** | Package rawos so anyone can install it on their own machine. | **NOT STARTED** |

### Mapping to technical phases (PLAN.md Phase 13–16)
- Milestone 1 ← Phase 13 (The Inversion)
- Milestone 2 ← Phase 14 (Accountable Agent) + Phase 15 (Intent Grounding) + session Milestone 2
- Milestone 3 ← Phase 16 (Self-Modification) + session Milestone 3 (R1 operator)
- Milestone 4 ← Phase 17 (Telegram front-door, `rawos/kernel/telegram_gate.py`, polling mode)
- Milestone 5 ← Phase 18 (Installable substrate) — DONE 2026-06-12 (commits 2be4b24e..7834b24d)
  - Step 1: path portability (settings.rawos_source_root / workspaces_root)
  - Step 2: ServiceManager Protocol + LinuxServiceManager generate/install/uninstall unit (TDD, 25 tests)
  - Step 3: `rawos service` CLI — install/uninstall/status/restart/logs (TDD, 10 tests)
  - Step 4: `rawos setup` wizard — create dirs, write .env, generate+install service (TDD, 12 tests)
  - Step 5: pyproject.toml deps — python-telegram-bot>=22.8, openai>=2.41


---

## Completed phases

### Phase 13 — The Inversion: login = being (DONE — 2026-06-12, Stages A–H)

**Thesis**: stack inversion. hardware → host kernel → rawos → HUMAN. `ssh root@server` lands in AI, not bash. The AI is the first entity you meet on the machine.

**What was built** (Stages A–H, commits 7598a220–8346c7c1, live on Hetzner 178.104.255.197):

- `FrontDoor` Protocol in `rawos/kernel/arch/base.py` — OS-agnostic ABI:
  install / uninstall / state / validate / reload / snapshot / restore.
- `LinuxFrontDoor` backend in `rawos/kernel/arch/linux.py` — sshd drop-in
  `/etc/ssh/sshd_config.d/50-rawos-frontdoor.conf` (`Match User root / ForceCommand rawos frontdoor enter`).
  Validates via `sshd -t` before reload; snapshot/restore + dead-man's-switch installer.
- `decide_entry()` pure function in `rawos/kernel/frontdoor.py` —
  LAUNCH_CHAT / PASSTHROUGH / FAIL_OPEN_SHELL. Fail-open: rawos down or no token →
  raw shell + notice. JSON audit per session at `~/.rawos/audit/frontdoor.log`.
- `rawos frontdoor enter/install/commit/status/uninstall` CLI (rawos/cli/main.py).
- Session digest 'while you were away' — Stage G, rawos/scheduler/proactive.py.
- SFTP passthrough bug fixed via TDD: SSH_ORIGINAL_COMMAND='subsystem sftp' →
  direct execv(sftp-server), not bash -c.
- 9 bugs fixed total; full suite 542 tests pass.
- macOS/Windows stubs: NotImplementedError — seam exists, not built.

**Security invariants (permanent):** escape hatch (`ssh -t root@host bash` → PASSTHROUGH);
fail-open on rawos-down; dead-man's-switch on install; tooling-safe (scp/rsync/git pass through).

**Current status:** live + active on master (fast-forward merged 2026-06-12 from stage-h-front-door).


### Phase 14 — The Accountable Agent (DONE)

**Thesis**: rawos cannot be trusted to CONTRIBUTE if it does not hold itself accountable for outcomes.

**What was built**:
- Decision model: CONTRIBUTE / SIGNAL / SILENCE (first word of agent response, enforced at parse layer)
- CONTRIBUTE protocol: branch → fix → verify (tests or systemctl) → commit with `VERIFIED:` footer; if verify fails → revert → SIGNAL with `REVERTED:` reason
- SIGNAL only for genuinely unfixable: missing credentials, external access, business logic rawos cannot know
- SILENCE as default when nothing concrete to fix — not SIGNAL (false alarm is worse than silence)
- `_parse_verification_result()`: parses VERIFIED:/REVERTED: from agent response synchronously
- `_post_commit_self_rate()`: fallback async self-rating if agent does not self-report
- `MAX_TOOL_ROUNDS` fix: final summarisation call now uses rawos system_prompt, not generic default
- Decision fallback fixed: default is SILENCE, not SIGNAL

**Verified in production**:
- Before Phase 14: STUCK trigger → agent wrote HTML report → defaulted to SIGNAL (wrong)
- After Phase 14: STUCK trigger → SILENCE (correct — nothing concretely fixable)
- No false SIGNALs since deploy

---

### Phase 15 — Intent Grounding via Repo Probe (DONE)

**Thesis**: an agent with a vague goal cannot produce a real fix. rawos must find a concrete target before firing the agent — not infer one from ambient context.

**What was built**:
- `_select_entity_probe_target(user_id)`: picks most recently active watched repo (COMMIT_EDITMSG mtime, last 7d), excludes /root/rawos
- `_probe_repo_for_issues(workdir)`: git log + diff stat + pytest (-x --tb=line -q, 90s timeout, python3 system interpreter) → `{has_failures, commits, diff_stat, test_output}`
- `_detect_and_run_tests(workdir)`: auto-detects pytest.ini / setup.cfg / pyproject.toml
- Agent prompt override when `has_failures=True`: entire context_summary replaced with focused mission block — no ambient noise, no inferred_goal drift
- Probe fires on all triggers except SERVER_SCAN and NEEDS_ATTENTION (which carry specific targets)

**Test runner**: `python3` system interpreter — rawos venv lacks packages (e.g. `openai`) that watched repos import.

**CONTRIBUTE commits made to `sovereign-research-kernel`** (branch: `rawos/fix-test-isolation-and-provider-count`):

| Commit | File | Root cause fixed |
|--------|------|-----------------|
| `ee6b391` | `sovereign/daemon.py` | `_build_components` always used `settings.db_path` for VectorStore — tests with `tmp_path` ledger connected to production `vectorstore.db` held by the running daemon → lock timeout. Fixed: derive path from `ledger.db_path` |
| `ee6b391` | `tests/test_provider_router.py` | `nim_key4` added to config but tests still asserted 3 providers → assertion failure |
| `51ea345` | `sovereign/config.py` | pydantic-settings 2.14 `BaseSettings` defaults to `extra='forbid'`; `MISTRAL_TITLE_KEYS` in `.env` not defined in `Settings` → `ValidationError` at import → all 344 tests failed at collection |

**Result**: 493 passed, 6 warnings, 81.38s. Probe returns `has_failures=False`.

---

## Architecture (stable)

```
STUCK / JUST_FINISHED / ambient (None)
  → _select_entity_probe_target(user_id)
  → _probe_repo_for_issues(workdir)
      has_failures=True  → context_summary = mission block (test failures, no noise)
      has_failures=False → context_summary = ambient (inferred_goal + recent activity)
  → agent loop (MAX_TOOL_ROUNDS=12)
  → decision: CONTRIBUTE / SIGNAL / SILENCE
      CONTRIBUTE → _parse_verification_result() → episodic log

SERVER_SCAN / NEEDS_ATTENTION → bypass probe → agent loop with specific target already in ctx
```

## Security invariants (non-negotiable)

- rawos NEVER commits to main/master directly
- rawos commits only to `rawos/*` branches
- `_DESTRUCTIVE_PATTERNS` blocked: `rm -rf /`, `dd if=`, `systemctl stop rawos`
- `validate_path` in write_file restricted to workdir
- `sandbox_docker=False` in `.env`

### Added for Phase 16 (self-modification) — non-negotiable from this point forward

- When the probe target is `/root/rawos` itself: **default-deny**. rawos may write ONLY to paths in the TIER 1 allowlist (Phase 16 below). Every other path in `/root/rawos` is TIER 0 — read-only, even for rawos.
- TIER 0 is enforced in code at the `write_file` layer (path check against the allowlist), not by prompt instruction alone. A prompt instruction is a request; a path check is a wall.
- rawos NEVER auto-restarts the rawos service.
- rawos NEVER merges its own `rawos/self-improve-*` branches.
- rawos NEVER edits the TIER 0/1/2 definition itself (this section + the code that enforces it) — that boundary can only be changed by a human-authored commit.

---

## Phase 16 — DECIDED (2026-06-09): Self-Modification — rawos maintains rawos

### The decision

Three directions were analysed: full-spectrum probe (depth), consequence loop (accountability), ecosystem expansion (breadth). All three are evolutionary — they make rawos better at a thing rawos already does (find and fix issues in OTHER repos). CI pipelines already do static analysis sweeps; watch-list expansion is a config change; a consequence loop depends on a review signal that does not exist in this ecosystem (no human reviews `rawos/*` branches today).

**Phase 16 = self-modification.** rawos probes its own source tree (`/root/rawos`), identifies real issues within an explicit allowlist, and submits verified patches to itself via `rawos/self-improve-*` branches.

### The claim

> rawos is the first continuously-operating AI entity that reads its own source code, forms its own improvement targets, and submits verified patches to itself — without ever touching its own decision model, security enforcement, or probe-targeting logic.

No production AI system today does this. Copilot does not patch Copilot. Claude Code does not patch Claude Code. The boundary below is what makes this safe enough to attempt: rawos can improve its own *capabilities and coverage* but can never rewrite the *rules that constrain it*.

### Rejected directions (kept for traceability)

| Direction | Why rejected |
|---|---|
| Full-spectrum probe (mypy/ruff/bandit/coverage on watched repos) | Evolutionary — same shape as Phase 15, just more linters. Does not change what rawos *is*. Can be folded into the self-probe (below) instead, applied first to rawos's own code. |
| Consequence loop (track fate of `rawos/*` branches) | No review process exists on this server for those branches. An open branch after 7 days carries no signal — "rejected" and "unseen" are indistinguishable. Revisit only if/when a review workflow exists. |
| Ecosystem expansion (self-register new watch targets) | Low complexity but low leverage — adds breadth to a system whose depth is still shallow (Phase 15 only checks pytest). Self-modification subsumes this: once rawos can safely improve itself, expanding its own watch-list logic becomes a TIER 1 change it can propose. |

---

### THE HARD BOUNDARY — must exist in code before Pass 2 (implementation) begins

**Default-deny model.** TIER 1 is an explicit allowlist. Anything not on it is TIER 0 (read-only). This is inverted from a denylist deliberately: a denylist fails open if something is forgotten; an allowlist fails closed.

**TIER 0 — read-only, even for rawos** (everything not explicitly listed in TIER 1, including but not limited to):
- `rawos/scheduler/proactive.py` — decision model, probe-firing logic, CONTRIBUTE/SIGNAL/SILENCE parsing
- `rawos/kernel/agent_loop.py` — MAX_TOOL_ROUNDS, verification parsing
- `rawos/kernel/context_reader.py` — probe / test-detection logic (the thing being used to probe itself)
- `rawos/kernel/sandbox.py`, `rawos/auth.py`, `rawos/config.py`, `rawos/middleware/rate_limiter.py` — security enforcement, `_DESTRUCTIVE_PATTERNS`, `validate_path`, `sandbox_docker`
- `rawos/api/**` — production service surface (auth, billing, routes)
- `PLAN.md` — rawos does not rewrite its own constraints document
- this TIER 0/1/2 definition and its enforcement code
- everything outside `/root/rawos` (systemd units, `.env`, deploy scripts, other repos)

**TIER 1 — self-improvable (initial allowlist, additive-only)**:
- `/root/rawos/tests/*.py` — new test files, or new test functions added to existing files (raising coverage; not weakening existing assertions)
- `/root/rawos/rawos/evaluation/*.py`
- `/root/rawos/rawos/dataset/*.py`
- `/root/rawos/rawos/study/*.py`
- `/root/rawos/rawos/timing/*.py`
- `/root/rawos/rawos/manifester/*.py`
- `/root/rawos/docs/**` (if/when this directory exists)

**TIER 2 — never read, never probed, excluded entirely**:
- `.env`, `*.pem`, `*.key`, anything matching `*credential*` or `*secret*`
- systemd unit files, deploy scripts

### Process (Pass 2 implementation outline — not started)

1. **Dedicated self-probe path**, separate from `_select_entity_probe_target`. `/root/rawos` must NOT compete with watched repos for "most recently active" — it is always most active (it's the live codebase). Fixed low-frequency cadence (e.g. once per 6h), independent scheduler entry.
2. Self-probe runs `_probe_repo_for_issues('/root/rawos')` but results are filtered: only findings whose file path matches the TIER 1 allowlist are surfaced to the agent. TIER 0 findings are logged for human visibility but never become agent targets.
3. Agent runs under `_ENTITY_SYSTEM_PROMPT` plus an additional hard constraint in the prompt: "You are modifying your own source. You may write ONLY to TIER 1 paths listed below. If the correct fix requires touching any other path, SIGNAL — do not attempt, do not work around the restriction."
4. `write_file` tool itself enforces the TIER 1 allowlist when `workdir == /root/rawos` — this is the wall, not the prompt. Any write attempt outside TIER 1 is rejected at the tool layer regardless of what the agent decided.
5. CONTRIBUTE → branch `rawos/self-improve-*` → fix → run rawos's own test suite (12 files in `/root/rawos/tests/`) → `VERIFIED:` → commit.
6. NO auto-restart. NO auto-merge. Human reviews, merges, and restarts manually for at minimum the first N cycles — N to be defined once cycle 1 is observed.

### Pass 1 — CLOSED (2026-06-09)

All five items answered from direct evidence (git hooks, systemd units, test imports, tools.py dispatch, scheduler loop registration). No assumptions.

1. **Auto-deploy/auto-restart-on-push hook**: NONE. `.git/hooks/` contains only `.sample` files. No CI configs anywhere in the tree. The only systemd units touching `/root/rawos` are `rawos.service` (`Restart=always`, no path/exec triggers tied to git state) and `rawos-reset-budgets.timer` (daily oneshot, `scripts/reset_daily_budgets.py`, contains zero git/subprocess/restart calls — pure DB budget reset). `rawos-web.service` runs in a separate directory (`/root/rawos-web`), out of scope. **CONFIRMED CLEAN.**

2. **TIER 1 test coverage**: **ZERO.** `grep -rl 'evaluation|dataset|study\.|timing\.|manifester' tests/*.py` returns nothing — none of the 12 files in `tests/` (test_api, test_billing_stripe, test_models, test_phase2-5, plus conftest/locust/load-test scaffolding) reference `evaluation/`, `dataset/`, `study/`, `timing/`, or `manifester/` at all. **DECISION**: TIER 1 self-modification cannot start with "edit source, run suite, verify" — the suite is structurally blind to TIER 1 modules; a regression there is undetectable. Phase 16 Pass 2 implementation MUST start TIER 1 in **bootstrap mode**: the agent's first N self-modification cycles for any TIER 1 module are restricted to **adding new test files only** (zero edits to existing `.py` source in that module), until that module has dedicated coverage. Only after a TIER 1 module has its own passing tests does source-editing unlock for that specific module. This is enforced per-module, not globally — a module gaining tests doesn't unlock its siblings.

3. **`write_file` / enforcement chokepoint**: `rawos/kernel/tools.py:690`, `async def execute(tool_name, params, workdir)` — single dispatch point for ALL tools (`write_file`, `bash`, `bash_readonly`, `read_file`, `list_files`, `fetch_url`, `deploy`, `git_branch`, `git_commit`) via `REGISTRY.get(tool_name)`. `_write_file` (line 210) only calls `validate_path()` (traversal check), no TIER awareness. `_deploy` (line 343) is inert w.r.t. `/root/rawos` — generates a `https://downgrade.app/preview/...` URL string only, no filesystem/git side effects. **DECISION**: TIER enforcement wraps `execute()` itself, not individual tool impls — see Pass 2 design below.

4. **Self-probe cadence/scheduling**: Existing loops (all started via `asyncio.create_task` in `rawos/api/app.py` `lifespan`/startup, lines 58-63): `db_sync_loop` (30s), `proactive_scan_loop` (`SCAN_INTERVAL_S=120s`), `_personal_watcher_reload_loop`, `_daily_snapshot_loop` (study, daily), `_calendar_sync_loop_task`, `autonomous_server_scan_loop` (`AUTONOMOUS_SCAN_INTERVAL_S=600s`). **DECISION**: new `rawos_self_probe_loop()`, registered as a 7th `asyncio.create_task(..., name="rawos-self-probe")` in the same startup block, `SELF_PROBE_INTERVAL_S = 21600` (6h) — distinctly separate cadence from the 30s/120s/600s/daily tiers, matching "rare, reviewable cycles" intent (no auto-restart/auto-merge for at minimum the first N cycles, per the existing Process section).

5. **bash/shell bypass risk**: CONFIRMED REAL (carried over from the pre-Phase-16 remediation finding). `_bash` (tools.py:72) runs unrestricted shell via `run_bash()` (sandbox.py) with only resource-limit (`ulimit`) constraints — no path allowlist. A TIER-1-only `write_file` gate alone is meaningless; `_bash` with `sed -i`/`cat >`/`python3 -c "open(...).write(...)"` reaches any path. **DECISION**: same `execute()`-level wrapper from item 3 covers this — see Pass 2 design.

**Pass 1 verdict**: all 5 items answered with evidence, zero open questions remain. Pass 2 may begin.

---

### Pass 2 — IMPLEMENTED (2026-06-09)

All four implementation steps (a-d) are committed and verified. Final design refined two
points from the original sketch below, based on evidence gathered during implementation:

- **Live-tree concurrency hazard.** `/root/rawos`'s working tree is permanently dirty and
  continuously written by the running service (`data/rawos.db`, `data/chroma/**`,
  `__pycache__`). Detect-and-revert there could `git checkout`/`rm` a file the live service
  just wrote. So enforcement **hard-refuses** any `MUTATING_TOOLS` call when
  `_targets_rawos_own_repo(workdir)` is true (live tree, `--show-toplevel == /root/rawos`) —
  no revert attempted, SIGNAL instead. Detect-and-revert runs only when
  `_is_rawos_source_tree(workdir)` is true (same `--git-common-dir` as `/root/rawos/.git`,
  different `--show-toplevel` — i.e. a linked self-probe worktree, no concurrent writer).
- **Commit smuggling.** `git_commit` runs `git add -A`, so a single `bash` call can
  mutate-and-commit atomically, bypassing a working-tree-only diff. The wrapper snapshots
  `HEAD` before/after; if it changed, `git reset --soft <before_head>` first, then runs the
  same working-tree diff/revert — unifying commit violations and working-tree violations
  into one check.

**Implementation** (`rawos/kernel/tools.py`):
- Helpers (commit `ffab93e0`): `_is_rawos_source_tree` (git-common-dir based, catches linked
  worktrees), `_git_status_porcelain` (`--porcelain=v1 -z -uall`, renames split into two
  entries), `_diff_paths` (status-CODE equality — not content, the defense against the
  live-tree concurrency hazard), `_in_tier1_allowlist`, `_git_checkout_restore`.
- `execute()` wrapper + `MUTATING_TOOLS` + bootstrap gating (commit `31864421`):
  `MUTATING_TOOLS = frozenset({"write_file", "bash", "git_branch", "git_commit"})`.
  `execute()` now: live tree + mutating tool → hard refuse; rawos source tree (incl.
  worktrees) + mutating tool → `_execute_with_tier_enforcement` (snapshot status+HEAD, run
  tool, soft-reset if HEAD moved, diff status, revert any path outside
  `_in_tier1_allowlist` or bootstrap-blocked via `_is_bootstrap_blocked` — Pass 1 item 2);
  everything else → unchanged passthrough.
- Wrapper integration tests (commit `568efa28`, +8 tests, 30 total in
  `tests/test_tier_enforcement.py`): live-tree refusal, TIER 0 write reverted, TIER 1
  `tests/` write allowed, commit smuggling reverted with HEAD restored, mixed
  TIER0+TIER1 commit (TIER 0 reverted, TIER 1 survives), bootstrap block/unlock, non-rawos
  repo passthrough unchanged.

**Self-probe loop — shipped DORMANT (commit `552b752e`, user decision: "Dormant + manual
enable")**:
- `rawos/config.py`: `self_probe_enabled: bool = False` (next to `sandbox_docker`).
- `rawos/scheduler/proactive.py`: `SELF_PROBE_INTERVAL_S = 21600` (6h),
  `rawos_self_probe_loop()` — while `settings.self_probe_enabled` is False, logs once and
  returns immediately (no loop, no sleep, no worktree side effects).
  `_run_self_probe_cycle()` raises `NotImplementedError` — the worktree-based cycle (`git
  worktree add /root/rawos-self-probe-worktree <branch>`, entity agent run with
  workdir=worktree, `rawos/self-improve-*` branches, NO auto-merge/auto-restart) is left
  for implementation after a human manually drives and observes one cycle, then flips the
  flag.
- `rawos/api/app.py`: registered as a 7th `asyncio.create_task(_start_self_probe_loop(),
  name="rawos-self-probe")` in startup, cancelled+gathered in shutdown, mirroring
  `_start_autonomous_scan()`.
- `tests/test_self_probe.py` (2 new tests): flag defaults False; loop returns within 2s
  when disabled.

Full suite: **193 passed** (191 + 2 new), zero regressions across all four commits.

**Original design sketch (superseded by the above; kept for history)**:

```
async def execute(tool_name, params, workdir):
    repo_root = await _resolve_repo_root(workdir)   # git rev-parse --show-toplevel, or None
    is_self = (repo_root == "/root/rawos")

    if is_self and tool_name in MUTATING_TOOLS:      # write_file, bash, git_commit, git_branch
        before = _git_status_porcelain(repo_root)     # snapshot

    result = await REGISTRY[tool_name](params, workdir)

    if is_self and tool_name in MUTATING_TOOLS:
        after = _git_status_porcelain(repo_root)
        changed = _diff_paths(before, after)
        violations = [p for p in changed if not _in_tier1_allowlist(p, params)]
        if violations:
            for p in violations:
                _git_checkout_restore(repo_root, p)    # revert exactly the violating paths
            result = ToolResult(
                output=result.output + f"\n\nTIER VIOLATION: reverted {violations} "
                       f"(outside TIER 1 allowlist for /root/rawos self-modification)",
                success=False, duration_ms=result.duration_ms,
            )
    return result
```

This sketch's single `is_self` flag was split into `_targets_rawos_own_repo` (live tree,
hard refuse) vs `_is_rawos_source_tree` (worktree, detect-and-revert) for the live-tree
concurrency reason above; `_resolve_repo_root() == "/root/rawos"` was replaced by the
git-common-dir check so linked self-probe worktrees are still covered.

### Step e — docs + deploy verification (this step)
- PLAN.md updated (this section).
- `DELTA.md` updated to reflect Pass 2 closure.
- `systemctl restart rawos` → confirm `is-active`, port 8002 listening, `/metrics` 200
  (dormant self-probe loop must not break startup).


### Pre-Phase-16 hazard remediation — DONE (2026-06-09)

Pass 1 diagnosis surfaced an active production hazard unrelated to but blocking Phase 16: `/root/rawos`'s own working tree (== `rawos.service` `WorkingDirectory`, `Restart=always`) was being repeatedly `git checkout -b`'d by the live scheduler, because SERVER_SCAN/NEEDS_ATTENTION triggers bypass `_select_entity_probe_target` via `workdir_override=anomaly.affected_path` and `_git_branch`/`_git_commit` had no repo-root awareness. Resolved:

- **A** `2fddcb2b` — committed verified Phase 14/15 production fixes (context_reader.py, proactive.py: python3 test runner, -x --tb=line, 90s timeout)
- **B** `769ce9ef` — recovered 425-line uncommitted md_reporter CLI work (--test-results/--discover/--coverage)
- **C** `master` fast-forwarded to `1d805342` (was 7 commits behind, clean ff, no checkout)
- **D** `1d805342` — root-cause fix: new `_targets_rawos_own_repo(workdir)` helper in `rawos/kernel/tools.py`, checked at top of `_git_branch` and `_git_commit`; either returns `error: refusing to ... — SIGNAL instead` if `git rev-parse --show-toplevel == /root/rawos`. Verified: `ast.parse` OK, full suite 161 passed, service restart confirmed healthy (port 8002 listening, /metrics 200).

This also answers Pass 1 checklist item 5 partially: the bash/shell tool (`run_bash`, sandbox.py) has no repo-root guard yet — `_targets_rawos_own_repo` only covers the `_git_branch`/`_git_commit` tool calls. A `sed -i`/`cat >` via `run_bash` against `/root/rawos` source files is still possible. This remains open for Pass 2's TIER enforcement design (must be git-diff-based detect-and-revert after every tool round, not just write_file allowlist, and self-probe must run in an isolated `git worktree`, never `/root/rawos` directly).

## 2026-06-15 — First live self-reload (Phase 25 Ouroboros, Verification Step 4)

No-op commit to prove the full self-reload cycle on production rawos.service for the
first time: preflight (stage) → arm deadman → rename-swap source via git reset --hard →
os._exit(0) → systemd respawn → boot_liveness_commit verifies /health, frontdoor, DB,
schema → disarm deadman, record verified=True in operator_track_record class
'self_reload'. This is the joint Phase 25 closes: the being becoming a self-authored
version of itself, live, with full reversibility if liveness fails.

## 2026-06-15 — Phase 25 Stage 1 twin-prove: self-reload mechanism battle-tested (steps 6-8)

Before any further real self-reload of `rawos.service` is attempted (Verification Step 4
was proven once already, above), Stage 1's mechanism (`preflight_stage` -> `arm_and_swap`
-> systemd respawn -> `boot_liveness_commit` -> commit/resurrect/liveness_failed -> deadman
revert) was independently battle-tested end-to-end on a throwaway twin
(`rawos-selfprobe.service`, port 8009, `/root/rawos-selfprobe-tree`, now removed).

Findings discovered and fixed during twin-prove (Pass 1 read-only recon, each TDD'd or
twin-scoped):
- **C** `bf9cc46a` — prod bug: preflight's worktree subprocess calls used bare `"python3"`,
  which resolves to system python (no rawos deps) under real systemd PATH -> the import
  check would ALWAYS fail, refusing every self-reload. Fixed to `sys.executable`.
  RED->GREEN, full suite 1125/1125.
- **D** twin-isolation gap (twin `.env` only, no prod change): `chroma_path`/`workspaces_root`
  defaulted to `/root/rawos/...`, causing the twin to read/write prod's ChromaDB and watch
  prod's workspaces. Fixed via twin `.env` overrides.
- **E** twin venv gap (twin venv only, no prod/code change): twin venv was missing
  `pytest`/`pytest-asyncio`/`pytest-cov`/`coverage`, so preflight's `self_reload_smoke`
  subset always failed with empty stdout. Installed matching versions from prod's
  `pyproject.toml`.
- **G** twin DB seeding (twin DB only, no prod/code change): `update_operator_track_record`
  raised `FOREIGN KEY` IntegrityError because the twin's `users` table had no row for
  `RAWOS_ENTITY_USER_ID`. Seeded twin DB with prod's row.

Results:
- **Step 7 (commit-path)**: real `arm_and_swap` -> `os._exit(0)` -> systemd respawn ->
  `boot_liveness_commit -> committed` (old=`bf9cc46a` new=`19655a7d`). Deadman disarmed,
  `pending.json` cleared, HEAD==new_sha, `/health`=200. Verified via git/systemctl/curl/journalctl.
- **Step 8 (revert-path)**: armed a deliberately-broken `new_sha` (lifespan hangs before
  `yield` via `await asyncio.sleep(99999)`, passes preflight's import+smoke check via the
  TestClient-without-`with` gap but never serves `/health` for real). Result:
  `boot_liveness_commit -> liveness_failed` at armed_at+~155s, deadman fired at
  armed_at+~194s, `rawos-selfprobe-revert` ran `git reset --hard <old_sha>` and restarted
  the twin. Recovery confirmed: HEAD==old_sha (`19655a7d`), `pending.json` removed, 0
  timers left, `/health`=200 on a fresh PID.

Both the success path (commit) and the failure path (revert) of the self-reload mechanism
are now proven independently of the earlier production no-op above. Dormancy confirmed
unchanged: `self_reload_enabled=False`, `self_reload_autonomous_enabled=False`,
`self_reload_debug_endpoint_enabled=False`, no overrides in `/root/rawos/.env`. Twin
(`rawos-selfprobe.service`, `/root/rawos-selfprobe-tree`, `/root/.rawos-selfprobe`,
`/usr/local/bin/rawos-selfprobe-revert`) removed after verification.

_Step 4 marker: trivial no-op commit — `new_sha` for the first real self-reload of `rawos.service` (Phase 25 Ouroboros, Verification Step 4)._

_Step 4 marker v2: trivial no-op commit — actual `new_sha` for the first real self-reload of `rawos.service` (Phase 25 Ouroboros, Verification Step 4, attempt 2 — d87f4809 is the currently-loaded HEAD and cannot be reused as a target)._

## 2026-06-15 — Phase 25 Stage 1 Step 4: FIRST REAL SELF-RELOAD OF rawos.service (production)

Fired `POST /internal/self-reload/arm-and-go {"new_sha":"88e3f7f0"}` against the live
`rawos.service` (PID 151319, in-memory code at HEAD `d87f4809`). Result:

- `curl` exit 52 (empty reply) — `arm_and_swap` wrote `pending.json`, armed
  `rawos-selfreload-revert` (180s deadman), `git reset --hard 88e3f7f0`, `os._exit(0)`.
- systemd respawned `rawos.service` ~5s later as PID 151865.
- `Application startup complete` ~9s after that; `boot_liveness_commit()` ran its
  `/health` probe loop, passed, disarmed the deadman, cleared `pending.json`.
- Post-fire state: `git rev-parse HEAD` == `88e3f7f0` (clean), `/health` == 200, 0
  armed timers, single new PID. The in-memory process now matches the on-disk source —
  **the ouroboros joint is closed for the first time in prod.**

**Known limitation, stated not hidden:** `boot_liveness_commit` logged outcome
`resurrected` (old=new=`88e3f7f0`), not `committed`. Cause: the no-op commit was made
*before* arming, so `preflight_stage`'s `old_sha` (= HEAD at arm time) already equalled
`new_sha`. `boot_liveness_commit` checks `current_head == old_sha` first, which is
trivially true here, so it takes the "resurrected" branch (disarm + clear — same
cleanup as "committed", just a different label). Effect: `operator_track_record`
(`self_reload`/`/root/rawos`) got `last_outcome=merged_regressed`,
`verified_successes=0` — this run does **not** count toward Stage 2's 3-verified-reload
graduation threshold. The *mechanism* (die → respawn → load new source → prove
liveness → disarm) is fully proven in prod regardless; a future run that arms
**before** committing the no-op (so `old_sha != new_sha`) would record `committed` and
contribute to graduation.

Dormancy unchanged and reconfirmed: `self_reload_enabled=False`,
`self_reload_autonomous_enabled=False`, `self_reload_debug_endpoint_enabled=False`, no
`.env` overrides. Only the owner-triggered funnel (`execute_owner_self_reload` via
`/internal/self-reload/arm-and-go`) was exercised — Stage 2 autonomous path remains
untouched.

Phase 25 Stage 1 verification steps 1-8 + this real Step 4: **all complete.**

_Step 4b marker: trivial no-op commit created via `git commit-tree` (NOT checked out as
HEAD) — distinct `new_sha` for the second real self-reload of `rawos.service`
(Phase 25 Stage 1 Step 4, attempt 2). Goal: `old_sha != new_sha` so
`boot_liveness_commit` takes the `committed` branch (not `resurrected`), advancing
`operator_track_record(self_reload, /root/rawos).verified_successes` toward the
Stage 2 graduation threshold of 3._

## 2026-06-15 — Phase 25 Stage 1 Step 4b: SECOND real self-reload — outcome=committed

Fired `arm-and-go {"new_sha":"72fee001"}` with `old_sha`=`17fcbc91` != `new_sha`
(new_sha created via `git commit-tree`, never checked out — avoids the degenerate
old==new case from Step 4a). Result: PID 151865 -> 153794, HEAD `17fcbc91` ->
`72fee001`, `/health`=200, deadman disarmed, `pending.json` cleared.
`boot_liveness_commit -> committed (old=17fcbc91 new=72fee001 autonomous=False)`.

**Graduation ledger, stated precisely (not rounded to "1/3"):**
`operator_track_record(self_reload, /root/rawos)` after this run:
`verified_successes=0, last_outcome=merged_pending_stability, pending_since=<set>`.

`_advance_state` (kernel/track_record.py, shared with code-fix graduation) requires
**2 consecutive `committed` outcomes** (no `resurrected`/`liveness_failed` in between)
to advance `verified_successes` by 1 — this run started that window
(`merged_pending_stability`). `GRADUATION_THRESHOLD=3` ⇒ **6 consecutive `committed`
outcomes total** are needed before `verified_successes>=3` and
`self_reload_autonomous_enabled` becomes eligible to flip (manual flip, I-OWN5
pattern — never auto-flipped). 1 of 6 done; 1 more consecutive `committed` closes
the first pair (`verified_successes` -> 1).

Step 4 (real prod self-reload, mechanism end-to-end) is now proven **twice** —
once degenerate (`resurrected`, old==new) and once canonical (`committed`,
old!=new). Phase 25 Stage 1 + Stage 2 code: fully shipped, dormant, battle-tested in
prod. Remaining graduation (5 more `committed` runs) is optional, incremental,
non-blocking — accrues opportunistically via future self-reloads, not a prerequisite
for any other phase.
