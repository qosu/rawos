# rawos — PLAN.md

## Current version: v0.6.0 (Phase 14 DONE)
Last updated: 2026-06-09 19:50 UTC

---

## Phase 14 — The Accountable Agent (DONE)

**Revolutionary thesis implemented:**
rawos acts → rawos self-verifies → rawos self-rates.
No human validation gate. rawos is accountable for its own outcomes.

### Changes deployed (all patches in production)

**14.A — _AUTONOMOUS_SYSTEM_PROMPT rewritten** (proactive.py:455)
- CONTRIBUTE = find root cause + fix + verify (tests OR systemctl) + commit with Verified: footer
- SIGNAL = ONLY when missing credentials/external access rawos cannot obtain
- Service failures / code bugs / config errors → CONTRIBUTE, NOT SIGNAL (explicit in prompt)
- If verification fails → git checkout -- . + begin SIGNAL + end REVERTED: reason

**14.B — _ENTITY_SYSTEM_PROMPT rewritten** (proactive.py:58)
- CONTRIBUTE targets the affected project (workdir), NOT rawos repo
- Explicit 5-step protocol: branch → fix → verify → (pass: commit+VERIFIED) / (fail: revert+SIGNAL)
- SIGNAL only for genuinely unfixable (credentials/external/business logic)
- "Your presence proven by git log, not analysis files"

**14.C — _parse_verification_result() added** (proactive.py:327)
- Parses VERIFIED:/REVERTED: from agent response → returns 'good'/'bad'/None
- Synchronous — no async test runner needed for verified outcomes

**14.D — CONTRIBUTE block updated** (proactive.py:1014)
- Calls _parse_verification_result(result_text) synchronously
- If outcome found → _update_episodic_outcome() immediately, log it
- Fallback to _post_commit_self_rate() only if agent did not self-report

**14.E — agent_loop MAX_TOOL_ROUNDS fix** (agent_loop.py:270)
- Final summarisation call now passes system_prompt (was using generic default)
- Agent final decision text now uses rawos context → valid CONTRIBUTE/SIGNAL/SILENCE

**14.F — Decision fallback fix** (proactive.py:993)
- Default changed from "SIGNAL" to "SILENCE" when agent response does not start with decision keyword
- Prevents false SIGNALs when agent hits MAX_TOOL_ROUNDS mid-action

### Verified in production (post-deploy observations)
- Before Phase 14: STUCK trigger → agent wrote HTML report → defaulted to SIGNAL (wrong)
- After Phase 14: STUCK trigger → agent refuses to write reports per new prompt → SILENCE (correct)
- No false SIGNALs since deploy at 19:42:49 UTC
- agent_loop now passes system_prompt to final summarisation call

### CONTRIBUTE not yet observed — by design
rawos watches 23 repos. CONTRIBUTE fires when rawos finds a specifically fixable code issue.
Current anomalies (research-foundry dead one-shot, ambient feature scans) have no fixable code.
First CONTRIBUTE will fire when rawos encounters a real bug in a watched project.

---

## Architecture summary (stable)

STUCK/JUST_FINISHED → _ENTITY_SYSTEM_PROMPT    → CONTRIBUTE/SIGNAL/SILENCE
SERVER_SCAN         → _AUTONOMOUS_SYSTEM_PROMPT → CONTRIBUTE/SIGNAL/SILENCE
CONTRIBUTE          → _parse_verification_result() → outcome=good/bad (sync)
                    → fallback: _post_commit_self_rate() async if no self-report

## Security invariants (never change)
- rawos NEVER commits to main/master directly
- rawos commits only to rawos/* branches
- _DESTRUCTIVE_PATTERNS stays blocked
- validate_path in write_file restricted to workdir
- sandbox_docker=False in .env

---

## Phase 15 — candidates (not started)

Option A: Goal quality
infer_intent() produces vague goals ("feature work on markdown project").
Replace with goal inference that identifies a concrete fixable issue (file:line or error message).
This is the primary blocker to consistent CONTRIBUTE decisions.

Option B: CONTRIBUTE frequency trigger
Hook into git post-receive or cron to detect CI/test failures in watched repos.
Create NEEDS_ATTENTION intents automatically when failures detected.

Option C: Self-paper
rawos writes about what it is and what it has done.
Single git commit to rawos/manifest branch in sovereign-research-kernel.

Decision: none started. Run Phase 14 for 24h to observe CONTRIBUTE frequency first.
