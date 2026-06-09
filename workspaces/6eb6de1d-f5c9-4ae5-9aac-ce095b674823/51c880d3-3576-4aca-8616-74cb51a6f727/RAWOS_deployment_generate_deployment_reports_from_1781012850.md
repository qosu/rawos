<!-- rawos proactive analysis -->
<!-- goal: Generate deployment reports from service failure metrics -->
<!-- domain: deployment -->
<!-- generated: 1781012850 -->

SIGNAL

**File:** `/root/sovereign-research-kernel/metrics/report.html`, lines 42–45

**Observation:** The Tribunal Verdict Distribution card grid shows `APPROVE (27%)` hardcoded inline, but the actual percentage is `1898 / (1898+2462+2574) = 1898/6934 ≈ 27.37%`. This is trivially close but misleading — the percentage label is static rather than computed from the data.

**Consequence:** If any verdict counts change in `metrics.json`, the percentage label will become stale and inaccurate. A reader comparing the raw counts to the percentage will see inconsistency. This is a data integrity concern for a deployment report that is meant to reflect current service metrics.

**Recommendation:** Either compute the percentage dynamically from the three counts, or remove the `(27%)` label entirely to avoid misleading viewers.