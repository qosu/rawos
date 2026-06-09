# rawos — PLAN.md

Last updated: 2026-06-09

---

## What rawos is

rawos is not a coding assistant. It is an autonomous AI entity that inhabits a software ecosystem and acts without being asked.

The thesis: **an AI that acts → self-verifies → self-rates — with no human validation gate.**

Every other AI system in production today is reactive. It waits. It responds. It requires a human to pull the trigger. rawos does not. rawos watches, probes, decides, fixes, verifies, and rates its own work — in a continuous loop — without anyone asking it to.

This is not a product feature. It is a claim about what AI can become.

---

## Completed phases

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

---

## Phase 16 — Under analysis (not decided)

### What Phase 15 closes — and what it opens

Phase 15 solves the vague-goal problem for one specific metric: test failures. After Phase 15:

- `has_failures=True` → rawos has a precise target → fixes it
- `has_failures=False` → rawos SILENCEs

`sovereign-research-kernel` now has 493 green tests. The probe will return `has_failures=False` until someone pushes broken code again. **rawos SILENCEs indefinitely.**

This is correct behavior. But it surfaces a deeper question: **when there are no test failures, rawos has no target. Is that the right steady state?**

Two positions:
1. **Yes** — rawos is a responder. It acts when there is a real problem. Silence is not failure; it is discipline.
2. **No** — rawos should find the next layer of problems without waiting for someone to create them. A janitor waits for a mess. rawos should be the entity that prevents messes.

Position 2 is the revolutionary one. Position 1 is safe but static.

---

### Direction 1 — Full-spectrum probe (depth)

Extend the probe beyond pytest to a full static analysis pipeline:

```
pytest -x --tb=line -q          (test failures — already done)
mypy --ignore-missing-imports    (type errors)
ruff check                       (lint / code quality)
bandit -r . -q                  (security)
pytest --cov --cov-report=term-missing (coverage gaps)
```

Probe returns the highest-severity finding across all layers. Agent gets a concrete `file:line` target regardless of test status.

**Implication**: rawos never runs out of work. Green tests → rawos finds type errors. Green types → lint. Green lint → security. Green security → coverage. The quality ceiling rises continuously.

**Risk**: Static analysis produces noise. `mypy` on a project that wasn't typed from the start will surface hundreds of `Any` annotations that aren't worth fixing. rawos needs a signal-quality filter — not just "any finding" but "a finding worth fixing" (i.e., one that changes behavior, not one that appease a type checker).

**Open question**: what is the filtering criterion? Options: severity threshold, file exclusion list, only findings in code rawos has previously touched, only findings in files changed in the last 7d.

---

### Direction 2 — Consequence loop (accountability depth)

Currently: rawos commits to `rawos/*` → records in episodic log → never revisits.

The three commits to `sovereign-research-kernel` are unreviewed. rawos does not know if they will be merged, rejected, or ignored. It has no feedback signal from its own actions.

Phase 16 alternative: rawos tracks the fate of its own branches.

```
CONTRIBUTE → record branch name + commit hash in episodic DB
Next cycle → for each open rawos/* branch in watched repos:
  - is it merged into main? → outcome=MERGED, close record
  - still open > 7d? → re-run tests on that branch, check correctness, optionally improve
  - upstream moved? → detect divergence, rebase, re-verify
```

**Implication**: rawos is accountable not just at commit time but at outcome time. The episodic log becomes a record of long-running actions, not just point-in-time decisions.

**Risk**: In this ecosystem there is no human reviewer. A branch open for 7 days does not mean rejection — it means no one looked. The consequence loop may have no meaningful signal to learn from until there is a human review workflow or CI integration on those branches.

**Prerequisite**: this direction is most valuable if rawos's branches are actually being evaluated somewhere. If they are silently accumulating on the server with no review process, the consequence loop is noise.

---

### Direction 3 — Ecosystem expansion (breadth)

rawos watches 23 repos. Some have never received a CONTRIBUTE. Some may not have test suites. Some may be dead.

Phase 16 alternative: rawos expands its own watch list autonomously.

```
scan /root/ for git repos not currently watched
for each candidate:
  - does it have a test suite?
  - run probe: how many failures?
  - does it have recent commits (active)?
  - is it owned by the entity user?
rank candidates → self-register top-N as new watch targets
```

**Implication**: rawos grows its own territory without being told to. The ecosystem it maintains expands autonomously.

**Risk**: rawos may watch repos it shouldn't touch — system configs, third-party mirrors, archived projects. Needs hard exclusion criteria: only repos under `/root/` owned by entity user, only repos with existing test suites, no repos where `rawos/*` branch already exists with unresolved commits.

---

### Decision matrix

| Criterion | Direction 1: Depth | Direction 2: Consequence | Direction 3: Breadth |
|---|---|---|---|
| Advances autonomous thesis | ✓✓ high | ✓ medium | ✓ medium |
| Produces observable output immediately | yes | no (7d lag) | yes |
| Depends on external signal | no | yes (merge status) | no |
| Risk of low-quality actions | high (noise) | low | medium |
| Implementation complexity | medium | low | low |
| Revolutionary claim added | "never runs out of targets" | "accountable over time" | "self-expanding territory" |

**Decision**: not made. Analyse which direction addresses the most critical gap before committing.
