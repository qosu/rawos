<!-- rawos proactive analysis -->
<!-- goal: Generate HTML report from JSON metrics data -->
<!-- domain: deployment -->
<!-- generated: 1781029096 -->

SIGNAL

**File: `metrics/metrics.json` — `claims_verified` (20938) exceeds `claims_extracted` (20007).**

This is a logical impossibility: claims cannot be verified before they are extracted. The numbers imply `claims_verified` > `claims_extracted` by about 4.5%. Either:

1. The extraction and verification pipelines are counting from overlapping but different event populations (e.g., verifications include legacy claims not captured in the extraction counter), or
2. The counters are drifting due to a race condition or incorrect accumulation logic in whichever process writes to `metrics.json`.

**Consequence:** Any downstream report or dashboard that renders "verification rate" (verified ÷ extracted) will show >100%, which breaks trust in the metrics pipeline. If these numbers feed into deployment health checks, they could trigger false-all-clear or false-alert states depending on the direction of the threshold.