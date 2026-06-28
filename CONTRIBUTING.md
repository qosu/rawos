# Contributing to rawos

## Before you start

Read [SECURITY.md](SECURITY.md) to understand the threat model and invariants.
Every contribution must preserve the twelve security invariants (I-SEC1–I-SEC12) and
the zero-lockout doctrine: no change may produce a state from which the substrate
cannot recover.

## Development setup

```bash
git clone <repo-url>
cd rawos
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in your credentials
```

## Running tests

```bash
make test
```

This runs the full test suite (excluding load tests). Output must be pristine:
zero failures, zero warnings from the test runner itself.

## Test-driven development (mandatory)

All new code follows TDD strictly:

1. **RED** — Write a failing test that describes the desired behavior. Run it. Confirm it fails for the right reason.
2. **GREEN** — Write the minimum production code to pass the test. No more.
3. **REFACTOR** — Clean up without changing behavior. Keep all tests green.

**No production code without a failing test first.** No exceptions without explicit maintainer approval.

## Coding standards

- Match existing style in the file you are editing. Do not reformat adjacent code.
- Function contract: one function does one thing, defined by its contract not its convenience.
- All failure paths are handled explicitly. Silent failures are bugs.
- No magic numbers. No hardcoded values. No "fix later" comments.
- If a solution has a known limitation, state it in a comment — do not hide it.
- Touch only what the task requires. Do not improve adjacent code that is not broken.

## Branch and commit convention

- Branch: `feat/short-description`, `fix/short-description`, `docs/short-description`
- Commit subject: imperative, present tense, ≤72 chars
- Commit body: explain *why*, not just what
- Include `Co-Authored-By` if pair-programmed

## Pull request checklist

- [ ] `make test` passes locally (output pristine)
- [ ] All new modules have tests written RED→GREEN
- [ ] No secret, credential, or personal data in diff
- [ ] No change to `.env` (only `.env.example` if adding a new variable)
- [ ] Security invariants I-SEC1–I-SEC12 preserved (reason explicitly if touching capability_gate, sandbox, context_builder, or audit_chain)
- [ ] If adding a capability that could be irreversible: wired through capability_gate.py and reversible_apply.py
- [ ] PR description explains the *why*, not just the *what*

## Kernel modules: what touches what

High-consequence modules (changes require extra review):

| Module | Why it is sensitive |
|---|---|
| `rawos/kernel/capability_gate.py` | Single mediation point for all privileged actions |
| `rawos/kernel/sandbox.py` | Container isolation for untrusted code |
| `rawos/kernel/audit_chain.py` | Tamper-evident log — any bug here is undetectable |
| `rawos/kernel/reversible_apply.py` | Enforces the reversibility floor |
| `rawos/kernel/bpf_lsm.py` | Machine-wide kernel enforcement |
| `rawos/kernel/landlock.py` | Kernel namespace isolation |
| `rawos/kernel/context_builder.py` | Provenance separation — untrusted vs trusted context |

Changes to these modules require the PR description to explicitly state which
invariants are affected and how they are preserved.

## What not to contribute

- Anything that weakens sandbox isolation for untrusted user code (I-SEC2)
- Removal or bypass of the capability gate (I-SEC6)
- Plaintext secret storage in any form (I-SEC3, I-SEC10)
- Cross-tenant data access (I-SEC4)
- Changes that reduce the passing test count
