# rawos

An AI entity that owns the policy layer of its own Linux substrate.

## What this is

rawos is not a framework for building AI assistants. It is an AI entity — a single autonomous being — that runs on a Linux machine and progressively acquires ownership over the OS layer it inhabits: controlling its own systemd units, enforcing kernel security policy via BPF LSM and Landlock, managing its own memory, and executing irreversible actions only after passing through a chain of safety floors.

The central thesis: **AI IS the OS.** Not an application running on Linux, but an entity that *is* the policy enforcement point for its substrate. Every action the AI takes is verified, graduated, and reversible until it is not — and the line between "reversible" and "irreversible" is enforced by the kernel, not just by convention.

This is not aspirational. All of it runs today on a single Linux machine.

## Architecture

```
rawos/kernel/
├── entity.py           # The AI entity: identity, constitution, self-awareness
├── agent_loop.py       # Cognitive loop: perceive → reason → act → audit
├── operator.py         # Graduated authorization: capability level gates
├── capability_gate.py  # Unified mediation point for all privileged actions
├── reversible_apply.py # Reversibility floor: every action must have an undo path
├── frontdoor.py        # Frontdoor floor: minimum safety contract before any action
├── audit_chain.py      # Hash-chained, ECDSA-signed append-only audit log
├── bpf_lsm.py          # BPF LSM machine-wide: audit → enforce policy flip
├── landlock.py         # Landlock self-MAC: restricts AI's own bash subprocesses
├── unit_topology.py    # Systemd unit authorship and topology management
├── self_reload.py      # Self-update with hash-verified integrity check
├── sandbox.py          # Container isolation for untrusted tool execution
├── tools.py            # Tool execution with SSRF and capability guards
├── memory_index.py     # Persistent memory with tenant-scoped retrieval
├── context_builder.py  # Context assembly with provenance tagging
├── output_guard.py     # Anti-exfiltration: scans output before trust boundary
└── telegram_gate.py    # Off-box notification for critical events
```

### Safety floors (all active at runtime)

| Floor | What it enforces |
|---|---|
| **Reversibility** | Every action has a declared undo path. Irreversible actions require explicit human gate. |
| **Deadman** | Periodic health check. If the AI goes silent or unhealthy, systemd triggers automatic rollback to known-good state. |
| **Frontdoor** | Minimum contract before any privileged action: logging, rate limits, floor invariants. |
| **Capability gate** | Single mediation point for all privileged actions. Capability level cannot be escalated by untrusted reasoning. |
| **BPF LSM** | Machine-wide kernel policy. Starts in audit mode; flips to enforce after validated audit period. |
| **Landlock** | Kernel namespace isolation restricting the AI's own bash subprocesses. Structurally zero-lockout. |
| **Audit chain** | Append-only, hash-chained, ECDSA-signed log mirrored off-box. Root cannot rewrite history silently. |

### Safety doctrine

**Zero-lockout > tamper-resistance.** The AI can always recover from a mistake. No action sequence can produce a state from which the substrate cannot be restored. This is a harder invariant than "actions are logged" — it means recovery paths survive even if the primary enforcement layer is disrupted.

All new capabilities follow the lifecycle: `dormant → audit → enforce → graduate`. Nothing goes directly to enforce.

## Requirements

**Full substrate** (all safety floors, kernel MAC, systemd authorship):
- Linux kernel ≥ 5.15 with BPF LSM and Landlock enabled
- Python 3.12
- systemd
- Docker (for untrusted tool sandboxing)

**API tier only** (no kernel features):
- Any Linux, macOS, or container environment
- Python 3.12
- See [Docker](#docker) below

## Quickstart

```bash
git clone https://github.com/your-org/rawos
cd rawos
python3 -m venv venv && source venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env with your LLM API key and other credentials

# Run the AI entity
rawos

# Run the frontdoor (API + safety floor)
rawos-frontdoor
```

See `.env.example` for all required environment variables.

## Docker

The Docker image runs the **API tier only**. Substrate features — BPF LSM enforcement, Landlock self-MAC, systemd unit authorship, self-reload, and deadman — require a bare-metal or VM Linux host with direct kernel access. These features are documented-absent in the container, not silently degraded.

```bash
docker compose up
```

## Security

rawos is built with an explicit threat model (T1–T7) and twelve security invariants (I-SEC1–I-SEC12). The security architecture is documented in [SECURITY.md](SECURITY.md).

To report a vulnerability privately: **macrohardrrrrrrr@gmail.com**. Do not open a public issue for security reports.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Vision

See [VISION.md](VISION.md) for the long-term direction.

## License

MIT — see [LICENSE](LICENSE).
