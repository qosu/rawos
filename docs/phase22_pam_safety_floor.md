# Phase 22 — PAM Safety-Floor Architecture

**Status: DESIGN PASS** — zero runtime code, zero pam.d writes on box. This document is the prerequisite gate for Phase 22 build. No PAM write authority may exist in a running rawos instance until the checklist in §7 is fully satisfied.

---

## Why PAM, and why it is the highest-risk surface

rawos thesis: stack inversion — being earns authority over substrate incrementally via earned-reversible-autonomy tiers:
- R1: managed file edits (`ReversibleFileEdit`, `managed_file_targets`)
- R2: service run-state (`ReversibleServiceAction`, `managed_service_targets`, Phase 23a)

PAM is R3-adjacent: write authority over the authentication layer of the host OS. On this box (167.233.44.121):

- **Single account:** only `root` (uid=0). PAM deny root = total, immediate, permanent lockout with no fallback account.
- **No pamtester installed.** PAM has no syntactic validator equivalent to `sshd -t`.
- **No secondary SSH path.** If SSH auth breaks, recovery = Hetzner KVM console only (out-of-band human operation).

This is categorically different from R1 and R2. A rolled-back nginx config restores in one subprocess call. A broken PAM stack blocks the rollback operator itself — `systemctl` does not re-authenticate via PAM, but the revert *caller* arriving via SSH does.

---

## §1 Hard facts (verified on 167.233.44.121, 2026-06-14)

| Fact | Verification | Design consequence |
|------|-------------|-------------------|
| Only `root` account (uid=0) | `/etc/passwd` → `root:0:/bin/bash` only; no uid≥1000 | PAM deny root = total lockout; no fallback user |
| No pamtester | `which pamtester` → not found | Live-auth probe is the only unfakeable oracle |
| SSH pubkey auth works | `sudo -n true` exits 0 from agent session | Held control-connection + fresh probe is the validator design |
| All pam.d = root-critical on this machine | `/etc/pam.d/` enumerated (see §2) | Self-protected set = ALL existing system pam.d files |
| Zero existing PAM code | `grep -rni pam rawos/` → 2 false hits ("spam") | Greenfield; design from scratch; no legacy to refactor |

---

## §2 Architectural Decision 1 — Scope: rawos does NOT become an authenticator

**Lockout vector:** being autonomously modifies `common-auth` → all root SSH logins denied → permanent lockout with no in-band recovery.

**Rule:** PAM write authority is scoped exclusively to pam.d files for accounts rawos itself created. Being never touches root-auth-critical pam.d files. On a single-root machine, "root-auth-critical" = all existing system pam.d files.

**Self-protected set (ALL pam.d files present on this box at design-pass approval — refuse-at-construction):**

```
sshd                        ← direct SSH auth path; break this = immediate lockout
common-auth                 ← pulled by sshd, su, sudo, login — break this = full lockout
common-account              ← account validity checks; same reach as common-auth
common-password             ← password change; break allows silent account corruption
common-session              ← session setup for all login paths
common-session-noninteractive
su                          ← root escalation path
sudo                        ← root escalation path
sudo-i                      ← interactive root escalation
login                       ← console login path (KVM recovery layer depends on this)
runuser                     ← systemd service escalation
runuser-l                   ← login-shell variant of runuser
chfn  chpasswd  chsh        ← account management
passwd  newusers  other     ← passwd db and fallback
atd  cron  vmtoolsd         ← service-specific; low-risk individually, but in shared common-auth
```

Any pam.d file that existed at Phase 22 design-pass approval belongs in this set. Being may only write pam.d files it can prove it created for a non-root account it manages (currently none — future possibility only).

**Mechanism reuse:** `_SELF_PROTECTED_SERVICES` pattern (operator.py:45, Phase 23a) — identical refuse-at-construction shape:
```python
# Future build — illustrative
_SELF_PROTECTED_PAM_FILES = frozenset({
    "sshd", "common-auth", "common-account", "common-password",
    "common-session", "common-session-noninteractive",
    "su", "sudo", "sudo-i", "login", "runuser", "runuser-l",
    "chfn", "chpasswd", "chsh", "passwd", "newusers", "other",
    "atd", "cron", "vmtoolsd",
})
# Raised at PamFileEdit construction, not at runtime gate — cannot be bypassed
class PamRefusalError(Exception): ...
```

---

## §3 Architectural Decision 2 — Break-glass recovery account is a HARD PREREQUISITE

**Lockout vector:** deadman timer fires but fails (systemd crash, disk full, revert-cmd error path traverses broken PAM) → only Hetzner KVM console remains → operator without KVM credentials is permanently locked out.

**Why deadman alone is insufficient on this machine:**

`systemd-run --on-active` creates a transient timer unit that launches a command as the already-authenticated systemd process (not via SSH PAM). BUT: if the revert command itself (`rawos pam _revert <id>`) tries to load rawos.service internals that have dependencies, or if the Python interpreter import path is broken, the revert silently fails. The deadman is Layer 1 only.

**Requirement (build gate — not a recommendation):**

Before `operator_pam_enabled` may ever be set to True in any running rawos instance:

1. A non-root system account (e.g., `rawos-recovery`) must exist with SSH pubkey access via an authorized_keys file that rawos never manages.
2. That account must have a NOPASSWD sudo rule that does NOT traverse pam_unix password authentication.
3. The account's pam.d service path must NOT be a file in `_SELF_PROTECTED_PAM_FILES` that rawos could theoretically write (contradicts §2 but this is belt-and-suspenders: the account's own service stack must be verified independently).
4. Owner must verify from a session entirely independent of rawos: `ssh -i recovery_key rawos-recovery@167.233.44.121 sudo true` exits 0.
5. The recovery key must be stored separately from the operator key (different key file, backed up off-box).

**This is a build gate.** Phase 22 build cannot begin until owner creates and verifies this account.

---

## §4 Architectural Decision 3 — Reversible PAM-edit contract (design shape)

**Lockout vector:** PAM change applied but cannot be undone automatically → stuck in broken-auth state until human KVM.

**Contract — 1:1 mirror of `install_with_deadman` (rawos/kernel/frontdoor.py:135):**

```python
# DESIGN ONLY — not installable code, illustrates the contract shape

@dataclass(frozen=True)
class PamSnapshot:
    pam_file: str          # e.g., "rawos-guest"
    was_absent: bool       # True if file did not exist before apply
    prior_content: bytes   # b"" if was_absent

class PamFileEdit:
    """ReversibleOperation for a non-root-critical pam.d file.

    Refused at construction if target in _SELF_PROTECTED_PAM_FILES.
    Snapshot stored on-disk at /root/.rawos-pam-backups/<uuid> (not in DB —
    must survive rawos.service being stopped or DB unreachable).
    """
    def capture(self) -> PamSnapshot: ...
    def apply(self) -> None: ...          # write to /etc/pam.d/<target>
    def verify(self) -> bool: ...        # live-auth probe (§5)
    def restore(self, snap: PamSnapshot) -> None: ...  # raw file write, no PAM

def install_pam_edit_with_deadman(
    pam_file: str,
    new_content: str,
    revert_after_s: int = 300,
    *,
    _systemd: Any = None,
) -> None:
    """
    1. snapshot — capture /etc/pam.d/<pam_file> content (or ABSENT)
    2. arm      — systemd-run --on-active revert timer (runs as root already
                  authenticated, NOT via PAM path)
    3. apply    — write new content
    4. validate — live-auth probe (separate TCP, fresh pubkey login, §5)
       * fail: disarm + restore + raise PamInstallError
    5. ARMED — caller must verify in a NEW terminal, then call commit_pam_edit()
    """

def commit_pam_edit(*, _systemd: Any = None) -> None:
    """Disarm rawos-pam-revert timer after out-of-band verification."""
```

**Timer unit name:** `rawos-pam-revert` (distinct from `rawos-frontdoor-revert`, avoids unit namespace collision).

**Snapshot storage:** `/root/.rawos-pam-backups/<uuid>` — plain file, not in `rawos.db`. Rationale: if rawos.db is inaccessible (DB corruption, service stopped), restore path must still work. Revert command reads the backup file directly.

---

## §5 Architectural Decision 4 — Validator oracle: live-auth probe (not pamtester)

**Lockout vector:** PAM config passes syntactic check (pamtester returns 0) but actual SSH pubkey auth fails (e.g., due to pam_exec or pam_access rule) → validator reports PASS, commit() disarms deadman, lockout becomes permanent.

**Why pamtester alone fails on this box:**

`pamtester sshd root auth` exercises the PAM stack with a password credential. SSH pubkey auth traverses a different set of PAM modules than password auth — specifically, `pam_unix.so` password enforcement is skipped for pubkey, but `pam_access.so`, `pam_exec.so`, `pam_limits.so` still fire. A config that breaks only the pubkey path will pass pamtester.

**Oracle design:**

```
OPERATOR SESSION (held, already authenticated)
  │
  ├── arm deadman timer
  ├── apply pam change
  └── PROBE: ssh \
          -o ControlMaster=no \
          -o ControlPath=none \
          -o BatchMode=yes \
          -o ConnectTimeout=10 \
          -i <owner_pubkey> \
          root@127.0.0.1 \
          true
      ├── exit 0  → auth still works → validate() = True → ARMED
      └── non-zero / timeout → auth broken → validate() = False
                               → disarm + restore + raise PamInstallError
```

`-o ControlMaster=no -o ControlPath=none`: ensures the probe opens a genuinely separate TCP connection and new PAM session — it cannot piggyback on the operator session's existing auth state.

`-o BatchMode=yes`: refuses password prompts — probe must succeed via pubkey only, same path as production operator access.

`root@127.0.0.1`: loopback avoids network path variations; tests the sshd daemon running on this machine.

**Lifeline invariant:** The operator session (the one calling `install_pam_edit_with_deadman`) MUST remain open during the entire call including validate(). If it closes, the probe loses its loopback reference and the deadman is the only protection. This is documented as a required operator practice, not enforced in code (code cannot prevent the operator from closing their terminal).

**pamtester role:** Pre-flight syntactic check only, run BEFORE the live-auth probe. Catches obvious parse errors in the new pam.d content. Installed as a build dependency: `apt-get install -y pamtester`. Result: pamtester fail → don't even try live-auth → raise PamInstallError immediately.

---

## §6 Architectural Decision 5 — Three-layer last-resort recovery

**Layer 1 — Deadman auto-revert (primary):**

```
systemd-run --on-active 300 --unit=rawos-pam-revert \
  -- /root/rawos/venv/bin/rawos pam _revert <snapshot_id>
```

- Runs as systemd root context, not via SSH PAM auth
- Fires automatically 300s after arm() if commit() not called
- `rawos pam _revert <id>`: reads `/root/.rawos-pam-backups/<id>`, writes back to `/etc/pam.d/<target>`, reloads sshd if needed
- Disarmed only by `commit_pam_edit()` after out-of-band verification

**Layer 1 failure modes (covered by Layer 2):**
- systemd-run fails to create timer (disk full, systemd degraded)
- Revert command path broken (Python import error, venv missing)
- snapshot file at `/root/.rawos-pam-backups/<id>` unreadable

**Layer 2 — Break-glass recovery account (§3, covers Layer 1 failures):**

```
ssh -i ~/.ssh/recovery_key rawos-recovery@167.233.44.121
sudo cp /root/.rawos-pam-backups/<snapshot_id> /etc/pam.d/<target>
sudo systemctl reload ssh
```

Account's authorized_keys not managed by rawos. sudo rule: `rawos-recovery ALL=(root) NOPASSWD: /bin/cp /root/.rawos-pam-backups/* /etc/pam.d/*, /bin/systemctl reload ssh`. Scope-limited: no unrestricted root shell.

**Layer 3 — Hetzner KVM console (unconditional):**

- Hetzner Robot → "KVM Console" for server `167.233.44.121`
- Direct serial console — bypasses SSH and all network auth entirely
- Works even if sshd is dead, PAM is broken, rawos.service is stopped
- Login as root via console (pam_unix console password auth or single-user mode if password unknown)
- CDP-recovery protocol (rawos.service SyntaxError → locked out) documented in memory `project_rawos`

**Recovery runbook (PAM lockout, strict try-in-order):**

```
1. TRY Layer 1: wait 300s — check if rawos-pam-revert timer fires automatically.
   Verify: ssh -i ~/.ssh/claude_server_key root@167.233.44.121 true → exit 0 = restored.

2. TRY Layer 2: ssh -i ~/.ssh/recovery_key rawos-recovery@167.233.44.121
   sudo cp /root/.rawos-pam-backups/<snapshot_id> /etc/pam.d/<target>
   sudo systemctl reload ssh
   Verify: ssh root@... true → exit 0 = restored.

3. TRY Layer 3: Hetzner Robot KVM console → login as root → restore file.
   cat /root/.rawos-pam-backups/<snapshot_id> > /etc/pam.d/<target>
   systemctl reload ssh
   Verify: ssh root@... true → exit 0 = restored.

4. ESCALATE: if all three fail, the box needs Hetzner support + rescue system boot.
```

---

## §7 Architectural Decision 6 — Dormant by default, earned-autonomy ceiling

**Lockout vector:** rawos autonomously modifies PAM outside operator awareness → silent authority escalation → autonomous lockout possible.

**Rules (enforced by design, not only by flag):**

| Rule | Rationale |
|------|-----------|
| `operator_pam_enabled: bool = False` (dormant) | Separate from `operator_service_enabled`; zero PAM write without explicit activation |
| No `operate_on_pam()` function | R1/R2 have autonomous paths gated by graduation. PAM has NO autonomous path. Ever. |
| No graduation threshold for PAM | Graduation implies auto-apply eligibility. PAM never reaches that eligibility. |
| Only `execute_approved_pam_edit()` exists | Requires explicit owner action; owner must be present with open session (oracle requires it) |
| No scheduled/cron trigger | Autonomous loops may never call any PAM write function |

**Why no auto-apply even after N successes:**

R1 (file edit): rollback = one file write, can be done by any agent, no session required.  
R2 (service): rollback = one systemctl call, no session required.  
PAM rollback = deadman fires + revert runs — but validate() requires a live-auth probe — which requires the operator to hold a session open. Autonomous PAM edits without an operator present = no lifeline session = no oracle = rollback cannot be *proven* to have worked. This is R3 (irreversible-floor, never auto).

---

## §8 What unlocks Phase 22 build (prerequisite checklist)

Phase 22 build CANNOT begin until ALL of the following are independently verified by the owner:

- [ ] **This document reviewed and approved** by owner (confirms lockout understanding)
- [ ] **`phase22_pam_invariants.md` reviewed and approved** by owner
- [ ] **Break-glass recovery account created**: `ssh -i recovery_key rawos-recovery@167.233.44.121 sudo true` exits 0
- [ ] **Recovery key stored off-box**: separate from `~/.ssh/claude_server_key`, copy in secure backup
- [ ] **Backup directory exists**: `/root/.rawos-pam-backups/` writable by root, confirmed on box
- [ ] **pamtester installed**: `apt-get install -y pamtester && pamtester sshd root auth` loads modules cleanly
- [ ] **KVM console accessed at least once**: operator has Hetzner Robot credentials and has verified KVM VNC session opens
- [ ] **Loopback probe works**: manual `ssh -o BatchMode=yes -o ControlMaster=no -i ~/.ssh/claude_server_key root@127.0.0.1 true` exits 0 from the box itself

Only after all boxes checked: begin `PamFileEdit` + `install_pam_edit_with_deadman` implementation in a feature branch with TDD.
