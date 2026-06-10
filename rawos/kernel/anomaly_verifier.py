"""
rawos anomaly verifier — independent re-detection of whether a proposed
fix resolves a ServerAnomaly.

"Unfakeable verdict" (Stage 2 of the Earned, Reversible Autonomy design —
see PLAN.md / squishy-watching-stroustrup plan). The agent that authored a
rawos/fix-* branch does not get to decide whether its fix worked. This
module re-runs the AFFECTED repo's own pytest suite twice inside the
disposable worktree (kernel/worktree.py) — once on the pre-fix commit (the
state the anomaly was detected against), once on the proposed fix branch —
and compares pass/fail outcomes. Only a fail -> pass transition is reported
as resolved=True.

Trust model — why this does not reuse kernel/sandbox.py:
sandbox.run_bash() exists to contain commands an LLM CHOOSES to run (30s
timeout, 512MB vmem cap, output truncation — defence against an adversarial
or buggy agent). This module runs exactly ONE command rawos itself selects
(the affected repo's test runner) against code already isolated in a
disposable worktree. That is the same trust model as a CI job, not an agent
tool call, so it uses its own subprocess runner with CI-appropriate limits
(longer timeout, full memory, larger output capture).

Stated limitations (do not hide):
- Only anomaly.kind in {"service_failed", "service_error"} are verifiable
  here — these are the only kinds whose affected_path is a git repo with a
  test suite that can be re-run. disk_critical/disk_warning have no
  associated code change; verify_fix() raises ValueError for those.
- If the repo has no discoverable pytest suite (no tests/ or test/ dir with
  test_*.py / *_test.py files), resolved=None ("unknown") is returned. This
  is NOT a failure — it means rawos cannot independently confirm the fix and
  a human must review the proposed branch manually.
- A fail -> pass transition proves the fix resolves whatever the test suite
  exercises. It does NOT prove the LIVE systemd unit will report `is-active`
  after redeploy — that is Stage 3's job (reversible_apply.py + health
  gate), which actually restarts the service behind a rollback guard. This
  module never restarts anything and never touches the live working tree
  (only the disposable worktree it is given).
- Uses the ORIGIN repo's interpreter (<repo>/venv/bin/python3 if present,
  else <repo>/.venv/bin/python3, else system python3) but PYTHONPATH-
  prepends the WORKTREE so imports resolve to the worktree's (possibly
  fixed) source. This is correct for pure-Python packages; repos with
  compiled extensions or a build step are not supported — their tests
  would silently run against the origin's already-built artifacts.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rawos.context.server_scanner import ServerAnomaly

# Anomaly kinds whose affected_path is a git repo with a re-runnable test
# suite. disk_critical / disk_warning have affected_path == "/" and no
# associated code change — not verifiable here.
VERIFIABLE_ANOMALY_KINDS: frozenset[str] = frozenset({"service_failed", "service_error"})

# CI-style limits — generous, since this runs a known command rawos itself
# selected (a test suite), not an agent-chosen one. Distinct from
# kernel.sandbox's agent-facing 30s/512MB/50KB limits.
_TEST_RUN_TIMEOUT_S = 180
_OUTPUT_LIMIT = 20_000  # chars of combined stdout+stderr retained, tail-truncated
_GIT_TIMEOUT_S = 30


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of re-running the affected repo's test suite before/after a fix.

    resolved:
      True  — pre-fix run failed, post-fix run passed (the fix verifiably
              resolves a regression the test suite covers).
      False — post-fix run still fails, OR post-fix run BREAKS tests that
              passed pre-fix (regression introduced by the "fix" itself).
      None  — inconclusive: no test suite found, both runs passed (fix may
              address something untested), or a run errored/timed out.
              A human must review the proposed branch manually.
    method: short machine string describing how the verdict was reached,
      e.g. "pytest:tests/ -x -q" or "none" or "git-checkout-failed".
    evidence: human-readable explanation + truncated command output, for the
      SIGNAL/PROPOSED summary and audit trail.
    """
    resolved: bool | None
    method: str
    evidence: str


async def _run_command(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: int,
) -> tuple[int, str]:
    """Run argv in cwd, return (exit_code, combined output, tail-truncated).

    exit_code == -1 signals the command could not be run at all (interpreter
    missing) or timed out — callers must not treat -1 as "tests failed".
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return -1, f"could not execute {argv!r}: {exc}"

    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"{' '.join(argv)} timed out after {timeout}s"

    output = stdout_b.decode("utf-8", errors="replace")
    if len(output) > _OUTPUT_LIMIT:
        output = "...(truncated)...\n" + output[-_OUTPUT_LIMIT:]
    return proc.returncode, output


async def _git_checkout(ref: str, worktree: Path) -> tuple[bool, str]:
    """Checkout ref in worktree. Returns (ok, evidence-on-failure)."""
    code, output = await _run_command(
        ["git", "checkout", "-q", ref], cwd=worktree, env=dict(os.environ),
        timeout=_GIT_TIMEOUT_S,
    )
    if code != 0:
        return False, f"git checkout {ref} failed (exit {code}):\n{output}"
    return True, ""


def _discover_python_interpreter(repo_path: Path) -> str:
    """Return the best available Python interpreter for repo_path.

    Prefers the origin repo's own venv (where its dependencies are
    installed) over system python3 — see module docstring for why this is
    combined with PYTHONPATH=<worktree> rather than running inside the
    worktree's (nonexistent — venvs are gitignored) venv.
    """
    for candidate in ("venv/bin/python3", ".venv/bin/python3"):
        p = repo_path / candidate
        if p.is_file():
            return str(p)
    return "python3"


def _discover_test_command(worktree: Path, interpreter: str) -> list[str] | None:
    """Return the pytest invocation argv, or None if no suite is discoverable."""
    for tests_dir in ("tests", "test"):
        d = worktree / tests_dir
        if not d.is_dir():
            continue
        if any(d.glob("test_*.py")) or any(d.glob("*_test.py")):
            return [interpreter, "-m", "pytest", f"{tests_dir}/", "-x", "-q"]
    return None


def _build_env(worktree: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(worktree) + (os.pathsep + existing if existing else "")
    return env


async def verify_fix(
    anomaly: "ServerAnomaly",
    worktree_path: str,
    fix_branch: str,
    base_ref: str = "HEAD",
) -> VerificationResult:
    """Independently verify whether fix_branch resolves anomaly.

    Re-runs the affected repo's pytest suite on base_ref (pre-fix) and on
    fix_branch (post-fix) inside worktree_path, and compares outcomes. See
    module docstring for the trust model and stated limitations.

    Raises ValueError if anomaly.kind is not in VERIFIABLE_ANOMALY_KINDS — callers
    must not invoke this for disk_critical/disk_warning etc.
    """
    if anomaly.kind not in VERIFIABLE_ANOMALY_KINDS:
        raise ValueError(
            f"anomaly.kind={anomaly.kind!r} is not verifiable in a worktree "
            f"(only {sorted(VERIFIABLE_ANOMALY_KINDS)} have a re-runnable test "
            f"suite) — affected_path={anomaly.affected_path!r} has no "
            f"associated code change to diff"
        )

    worktree = Path(worktree_path)
    repo_path = Path(anomaly.affected_path)
    interpreter = _discover_python_interpreter(repo_path)
    env = _build_env(worktree)

    test_cmd = _discover_test_command(worktree, interpreter)
    if test_cmd is None:
        return VerificationResult(
            resolved=None,
            method="none",
            evidence=(
                f"no pytest suite discoverable under {worktree} "
                f"(checked tests/ and test/ for test_*.py / *_test.py) — "
                f"rawos cannot independently verify this fix; a human must "
                f"review {fix_branch} manually"
            ),
        )

    ok, err = await _git_checkout(base_ref, worktree)
    if not ok:
        return VerificationResult(resolved=None, method="git-checkout-failed", evidence=err)

    before_code, before_out = await _run_command(
        test_cmd, cwd=worktree, env=env, timeout=_TEST_RUN_TIMEOUT_S,
    )
    if before_code == -1:
        return VerificationResult(
            resolved=None,
            method=f"pytest:{' '.join(test_cmd[2:])} (pre-fix run errored)",
            evidence=f"pre-fix test run on {base_ref} could not complete:\n{before_out}",
        )

    ok, err = await _git_checkout(fix_branch, worktree)
    if not ok:
        return VerificationResult(resolved=None, method="git-checkout-failed", evidence=err)

    after_code, after_out = await _run_command(
        test_cmd, cwd=worktree, env=env, timeout=_TEST_RUN_TIMEOUT_S,
    )
    if after_code == -1:
        return VerificationResult(
            resolved=None,
            method=f"pytest:{' '.join(test_cmd[2:])} (post-fix run errored)",
            evidence=f"post-fix test run on {fix_branch} could not complete:\n{after_out}",
        )

    method = f"pytest:{' '.join(test_cmd[2:])}"
    before_pass, after_pass = before_code == 0, after_code == 0

    if not before_pass and after_pass:
        return VerificationResult(
            resolved=True,
            method=method,
            evidence=(
                f"RESOLVED: {base_ref} failed (exit {before_code}), "
                f"{fix_branch} passes (exit 0).\n"
                f"--- pre-fix ({base_ref}) tail ---\n{before_out[-2000:]}\n"
                f"--- post-fix ({fix_branch}) tail ---\n{after_out[-2000:]}"
            ),
        )

    if before_pass and not after_pass:
        return VerificationResult(
            resolved=False,
            method=method,
            evidence=(
                f"REGRESSION: {base_ref} passed, but {fix_branch} BREAKS "
                f"previously-passing tests (exit {after_code}). DO NOT MERGE.\n"
                f"--- post-fix ({fix_branch}) tail ---\n{after_out[-2000:]}"
            ),
        )

    if not before_pass and not after_pass:
        return VerificationResult(
            resolved=False,
            method=method,
            evidence=(
                f"NOT RESOLVED: {base_ref} failed (exit {before_code}) and "
                f"{fix_branch} still fails (exit {after_code}).\n"
                f"--- post-fix ({fix_branch}) tail ---\n{after_out[-2000:]}"
            ),
        )

    return VerificationResult(
        resolved=None,
        method=method,
        evidence=(
            f"INCONCLUSIVE: tests pass on both {base_ref} and {fix_branch} "
            f"(exit 0/0) — the fix may address an issue not covered by the "
            f"test suite. A human must review {fix_branch} manually."
        ),
    )
