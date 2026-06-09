<!-- rawos proactive analysis -->
<!-- goal: Generate and refine HTML reports from JSON metrics data -->
<!-- domain: deployment -->
<!-- generated: 1781009282 -->

I've confirmed the data. Let me verify consistency between metrics.json keys and the rendered report.html values.

**Data consistency check (metrics.json → report.html):**

- `tasks_total`: 17639 ✓
- `papers_processed`: 135639 ✓
- `claims_verified`: 20782 ✓
- `owner_queue_size`: 4842 ✓ (shown as "Owner Queue Pending")
- `tribunal_complete` (event count): 6897 ✓ (shown as "Tribunal Reviews")
- `tribunal_verdicts.APPROVE`: 1898 ✓
- `tribunal_verdicts.REJECT`: 2553 ✓
- `tribunal_verdicts.REVISE`: 2446 ✓
- `policy_decisions.AUTO_APPROVE`: 1883 ✓
- `policy_decisions.ESCALATE_TO_OWNER`: 4870 ✓
- `policy_decisions.SOFT_APPROVE`: 144 — **NOT shown in report.html** (missing from Policy Gate Distribution grid)
- `tribunal_verdicts` sum: 1898+2553+2446 = 6897 ✓ (matches tribunal_complete events)

The report also shows "Avg Tribunal Score: 5.5/10" — this is not a field in metrics.json. That's a derived value, but there's no raw data in metrics.json to compute it from (no `tribunal_scores` array or `avg_score` field).

The `SOFT_APPROVE` policy decision (144) is missing from the report. Let me also check the histogram bars for proportional issues.

Bar widths: 12px, 175px, 200px, 135px, 129px — these correspond to counts 136, 1853, 2110, 1427, 1371. The width should be proportional to max count (2110 = 200px). 136/2110*200 ≈ 12.9 ≈ 12px ✓. OK, that checks out. But the report says these counts come from histogram data that doesn't exist in metrics.json — there's no `tribunal_score_histogram` field in the JSON. That data is hardcoded in the HTML, meaning it's stale/decoupled from the actual data source.

SIGNAL

**File:** `metrics/report.html` lines 30-31 (Policy Gate Distribution grid)
**Observation:** `SOFT_APPROVE` (144 count, value in `metrics.json:policy_decisions`) is omitted entirely from the report's Policy Gate Distribution section. The report only shows AUTO_APPROVE, ESCALATE_TO_OWNER, and AUTO_REJECT.
**Consequence:** The report silently drops 144 policy decisions (~2% of total 6897). Critical policy visibility gap — if SOFT_APPROVE decisions are being rendered invisible, audit trails and governance insights are incomplete.

**File:** `metrics/metrics.json` vs `metrics/report.html` (Tribunal Score Histogram section)
**Observation:** The histogram data (136, 1853, 2110, 1427, 1371) and the "Avg Tribunal Score: 5.5/10" are hardcoded in the HTML with no corresponding fields in `metrics.json`. There is no `tribunal_score_histogram` or `avg_tribunal_score` key in the data source.
**Consequence:** The report cannot be regenerated from metrics.json alone. Every time metrics.json updates, the histogram and average score become stale — the report is partially hand-maintained, not pipeline-generated. This breaks any automation attempt at report generation.