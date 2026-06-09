<!-- rawos proactive analysis -->
<!-- goal: Diagnose and report on research-foundry service failure -->
<!-- domain: debugging -->
<!-- generated: 1781021888 -->

SIGNAL

**File: `/etc/systemd/system/research-foundry.service` (systemctl output)**

The service failed 2 weeks ago (May 26) with `code=killed, signal=TERM` — the process was externally terminated (SIGTERM), not a crash or exit. This is **not** a code bug — it's an OOM kill, a timeout kill, or a manual/system shutdown.

The service consumed only 1.129s CPU and 29.4M memory peak before being killed at 00:26:04 — but it ran for ~3 minutes 48 seconds (00:22:16 to 00:26:04). The low CPU/memory with a 4-minute wall clock strongly suggests it was **blocked/stuck** on something (network call, file lock, subprocess wait) and something external (systemd timeout, watchdog, or another process) sent SIGTERM.

**Consequence if ignored:** Any restart will hit the same wall — the service will hang for ~4 minutes and get killed again. This is not a transient error; it's a design issue (no timeout on whatever it's waiting for).

I recommend checking:
1. The service's `TimeoutStartSec` or `TimeoutStopSec` in the unit file
2. What the service actually does in those 4 minutes (network call? git pull? heavy computation?)
3. Whether a system-wide OOM or `systemd-kill` triggered it

But writing a full analysis file would violate my rules. I'll stay silent on producing new documents.