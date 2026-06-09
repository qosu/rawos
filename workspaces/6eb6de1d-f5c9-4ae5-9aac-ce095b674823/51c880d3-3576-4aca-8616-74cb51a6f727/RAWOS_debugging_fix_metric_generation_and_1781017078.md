<!-- rawos proactive analysis -->
<!-- goal: Fix metric generation and update deployment pipeline -->
<!-- domain: debugging -->
<!-- generated: 1781017078 -->

**Here is the concrete problem:**

1. **`metrics/metrics.json` line 11-12**: `"claims_extracted": 2349` is the *old* `claims_extracted` event count, but there's also `"claims_extracted_v2": 5897` (line 13). The report.html shows `Claims Verified: 20830`, which comes from the v2+legacy sum (2349 + 5897 = 8246 total extracted events, but verified is 20830). These numbers are inconsistent — if `claims_verified: 20830` is correct, then the individual counts should sum coherently.

2. **`metrics/metrics.json` line 17**: `"SOFT_APPROVE": 144` exists in the data but is **never displayed** in `report.html`. This is a data loss issue — the report only shows AUTO_APPROVE, ESCALATE_TO_OWNER, and AUTO_REJECT (line 0). The 144 SOFT_APPROVE decisions are silently dropped.

3. **No `metrics/summary.json` exists** — the deployment pipeline was supposed to create it according to the `RAWOS_deployment_generate_and_update_metric_1781016912.md` plan, but it's absent.

**Consequence if ignored:** The deployment pipeline is producing incomplete reports that silently drop certain verdict categories (SOFT_APPROVE), and the summary.json target is missing, meaning any monitoring or CI that depends on it will fail. The metrics.json also has contradictory extraction counts (2349 vs 5897 vs 20830) that will cause confusion when debugging verification pipeline throughput.