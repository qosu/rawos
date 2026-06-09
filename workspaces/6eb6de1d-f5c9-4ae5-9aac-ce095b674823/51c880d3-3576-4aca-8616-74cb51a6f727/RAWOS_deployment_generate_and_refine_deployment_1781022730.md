<!-- rawos proactive analysis -->
<!-- goal: Generate and refine deployment reports in JSON and HTML -->
<!-- domain: deployment -->
<!-- generated: 1781022730 -->

Confirmed. The three critical deployment report files (`summary.json`, `report.html`, `metrics.json`) do not exist anywhere in the repo. There are 41 `RAWOS_*` debris files cluttering the root instead — analysis notes and outdated fix artifacts that should have been cleaned up.

**Specific concrete finding:** `summary.json` (line 1 equivalent — missing file) — This file is the structured deployment summary. Without it, any deployment reporting pipeline relying on it will produce no output, and any CI/CD or monitoring that reads it will fail silently or error.

**Consequence:** The deployment reporting system is in a broken empty state — the outputs were lost, leaving only stale intermediate analysis files with no actionable deployment data.