<!-- rawos proactive analysis -->
<!-- goal: Generate and refine deployment reports in HTML and JSON -->
<!-- domain: deployment -->
<!-- generated: 1781022889 -->

SIGNAL

Three findings, all concrete:

**1. `metrics/summary.json` is missing** — `read_file` returned "file not found". The deployment report set is incomplete. The 7 edits to `summary.json` in the recent activity log may have renamed, moved, or deleted it. Any consumer expecting all three files (report.html, metrics.json, summary.json) will fail.

**2. `report.html` vs `metrics.json` data mismatch — Tribunal verdicts**

| Source | APPROVE | REVISE | REJECT | Total |
|---|---|---|---|---|
| `report.html` | 1899 | 2498 | 2661 | **7058** |
| `metrics.json` tribunal_verdicts | 1899 | 2498 | 2661 | **7058** |

These match — good.

**3. `report.html` vs `metrics.json` data mismatch — Policy Gate Distribution**

| Source | AUTO_APPROVE | ESCALATE_TO_OWNER | AUTO_REJECT |
|---|---|---|---|
| `report.html` | 1884 | 5030 | **0** |
| `metrics.json` policy_decisions | 1884 | 5030 | **—** (but has `SOFT_APPROVE: 144`) |

`report.html` shows `AUTO_REJECT: 0` but `metrics.json` has `SOFT_APPROVE: 144` — a distinct category that is **silently dropped** in the HTML. This means 144 policy decisions (2% of all decisions) are invisible in the report. A stakeholder reading only the HTML would not know SOFT_APPROVE decisions exist.

**Consequence of ignoring:** Missing `summary.json` breaks automated deployment pipelines. Missing `SOFT_APPROVE` in the HTML misrepresents policy outcomes — any analysis of owner-vs-automated throughput is off by 144 records.

**Action needed:** Either restore `summary.json` from git history or regenerate it from `metrics.json`. Add `SOFT_APPROVE` row to the Policy Gate Distribution section in `report.html`.