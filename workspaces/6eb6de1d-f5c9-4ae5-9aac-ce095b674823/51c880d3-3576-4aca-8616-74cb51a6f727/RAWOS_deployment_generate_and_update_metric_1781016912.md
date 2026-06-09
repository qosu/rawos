<!-- rawos proactive analysis -->
<!-- goal: Generate and update metric reports in HTML and JSON -->
<!-- domain: deployment -->
<!-- generated: 1781016912 -->

SIGNAL

**File: `metrics/report.html:46`** — The "Avg Tribunal Score" value shown is `5.5/10`, but the histogram data below it sums to **6984 tribunal reviews** (138+1891+2147+1437+1371), which matches the "Tribunal Reviews" count of **6984**. The issue is that the average score cannot be verified with the provided histogram buckets — the midpoint-weighted average of `(1*138)+(3*1891)+(5*2147)+(7*1437)+(9*1371) = 37,174` divided by `6984` gives approximately **5.32**, not **5.5**. This discrepancy of ~0.18 suggests either:
1. The average is computed from raw per-review scores while the histogram uses bucketed ranges that introduce rounding error, OR
2. The average is stale relative to the histogram data

**Consequence:** If stakeholders rely on dashboard metrics to assess tribunal calibration, a 0.18-point drift could misrepresent whether the current threshold of 6.5 for approval is appropriate. The `5.5` average is already well below the `6.5` threshold, making precision important for any threshold-tuning decision.

**Recommendation:** Either (a) recompute the average from the raw scores that generated the histogram, or (b) add a note that the displayed average is approximate due to bucketing. This would normally be a CONTRIBUTE action, but I lack access to the raw per-review scores to make the calculation definitive.