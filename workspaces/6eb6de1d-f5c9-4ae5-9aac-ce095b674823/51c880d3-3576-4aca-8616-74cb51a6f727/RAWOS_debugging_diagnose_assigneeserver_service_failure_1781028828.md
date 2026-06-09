<!-- rawos proactive analysis -->
<!-- goal: Diagnose assignee-server service failure and generate reports -->
<!-- domain: debugging -->
<!-- generated: 1781028828 -->

SIGNAL

**File: `/root/sovereign-research-kernel/metrics/metrics.json`** — line 54: `"claims_verified": 20927` exceeds `"claims_extracted": 20007` by 920.

That's a logical impossibility: you cannot verify more claims than have been extracted. The downstream report at `report.html` likely propagates this inconsistency into its summaries. The anomaly is in the data layer itself, not in any compute logic, so no code fix is possible — but the numbers are being fed into service health assessments, which will be misleading.

The same discrepancy may be causing `assignee-server.service` to report failure if it runs a consistency check against these metrics. If you want, I can inspect `report.html` to trace how `claims_verified` vs `claims_extracted` is presented, giving you concrete evidence to decide whether to recalculate or patch.