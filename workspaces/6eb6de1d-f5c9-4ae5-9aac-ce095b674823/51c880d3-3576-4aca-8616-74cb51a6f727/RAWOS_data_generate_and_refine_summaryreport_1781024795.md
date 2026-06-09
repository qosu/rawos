<!-- rawos proactive analysis -->
<!-- goal: Generate and refine summary/report data files -->
<!-- domain: data -->
<!-- generated: 1781024795 -->

SIGNAL

**File: `metrics/metrics.json` — `claims_verified` (20893) exceeds `claims_extracted` (19995) by 898**

This is a logical impossibility: verified claims cannot outnumber extracted claims unless the pipeline allows verification of claims that were never recorded as extracted in this dataset. This inconsistency will cascade into any derived reports or dashboards that compute verification rates or pipeline health metrics.

**Consequence:** If `report.html` (edited 8 times in the same session) references these values — e.g., to show `% of claims verified` or pipeline completeness — it will silently report >100% verification coverage, misleading any human or automated consumer of this report.

This should be investigated at the source: either `claims_extracted` is underreported (missing the 898 surplus verifications) or `claims_verified` is overcounted (possibly double-counting re-verifications from an upstream tribunal step). The fix is not in metrics.json itself — that's just the symptom — but in the pipeline step(s) that populate these two counters.