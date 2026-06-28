# Security Policy

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: **macrohardrrrrrrr@gmail.com**

Include:
- Description of the vulnerability and its impact
- Steps to reproduce
- Affected version(s) or commit range
- Any suggested fix

You will receive an acknowledgement within 48 hours and a resolution timeline
within 7 days. We will credit you in the changelog unless you prefer otherwise.

## Threat model

rawos operates under an explicit threat model with seven categories (T1–T7):

| ID | Threat |
|---|---|
| T1 | Remote attacker (no credentials) — network exposure on public host |
| T2 | Malicious authenticated user — agent tool execution → sandbox escape |
| T3 | Compromised LLM / MITM — AI reasoning hijacked via poisoned inference |
| T4 | Poisoned perception / stored injection — attacker plants data the AI reads later |
| T5 | Supply chain — poisoned dependency or forged self-reload artifact |
| T6 | Credential theft — secrets extracted from process environment or filesystem |
| T7 | AI malfunction (non-malicious) — incorrect reasoning causes harmful action |

## Security invariants

The codebase enforces twelve invariants (I-SEC1–I-SEC12):

- **I-SEC1** — No action can produce a state from which the substrate cannot be
  recovered (zero-lockout floor). All enforcement changes use deadman auto-revert.
- **I-SEC2** — Untrusted user code never runs host-direct or as root inside container.
- **I-SEC3** — High-value secrets (API keys, JWT) are not present in the environment
  of any sandboxed process.
- **I-SEC4** — All memory/context queries are scoped to ;
  no cross-tenant path exists.
- **I-SEC5** — Untrusted content (user chat, tool output, fetched URLs) is structurally
  separated from trusted content (AI constitution) in context assembly.
- **I-SEC6** — All privileged and irreversible actions pass through a single capability
  gate (). Untrusted reasoning cannot escalate capability level.
- **I-SEC7** — All privileged actions are logged to an append-only, hash-chained,
  ECDSA-signed audit chain, mirrored off-box.
- **I-SEC8** — URL fetch is default-deny for RFC1918, link-local (169.254.x.x),
  loopback, and non-HTTP(S) schemes.
- **I-SEC9** — Self-reload and dep installs verify integrity (hash check) before
  executing any artifact.
- **I-SEC10** — Any secret ever stored in plaintext is treated as compromised and
  must be rotated.
- **I-SEC11** — New enforcement is shipped dormant or in audit mode first; only
  promoted to enforce after an observation period shows no false positives.
- **I-SEC12** — No commit may reduce the passing test count. All new modules follow
  TDD (RED → GREEN before any production code).

## Security architecture

rawos implements the Security Hardening Program (SHP) across seven phases:

- **SHP.0** — Threat model and verification harness
- **SHP.1** — Host perimeter (nftables, SSH hardening, fail2ban)
- **SHP.2** — Secrets management (systemd LoadCredential, rotation protocol)
- **SHP.3** — Sandbox / multi-tenant execution isolation
- **SHP.4** — Cognitive security (provenance tagging, capability mediation, output guard)
- **SHP.5** — Hybrid trust anchor (tamper-evident audit log, off-box mirror)
- **SHP.6** — BPF LSM audit → enforce flip (machine-wide, human-gated)
- **SHP.7** — Supply chain integrity (signed self-reload, dep pinning, SBOM)

## Container deployments

The Docker image runs the API tier only. Substrate features — BPF LSM enforcement,
Landlock self-MAC, systemd unit authorship, deadman heartbeat, and self-reload — require
bare-metal or VM Linux with direct kernel access. These features are absent in the container
image by design, not by oversight.

## Supported versions

Only the latest commit on the  branch is supported. There are no versioned
releases with security backports at this time.
