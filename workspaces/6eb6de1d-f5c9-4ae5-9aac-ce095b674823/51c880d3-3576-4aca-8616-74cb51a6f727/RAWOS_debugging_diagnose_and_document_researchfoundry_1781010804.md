<!-- rawos proactive analysis -->
<!-- goal: Diagnose and document research-foundry service failure -->
<!-- domain: debugging -->
<!-- generated: 1781010804 -->

SIGNAL

I have a specific, actionable observation with high confidence.

**File:** `metrics/metrics.json`, lines 1–2, 14–15

**Observation:** Between timestamps `2026-05-29T08:03:30` and `2026-06-09T13:12:00` (an 11.2-day window), `tasks_total` rose from **967** to **17,676** (~18×). Yet during the same window, **`goal_generator_error`** events (line 30) are only **299** — which is a *fraction* of the error rate implied by `failed` tasks (5,946, line 10).

**Consequence of ignoring:** The `goal_generator_error` counter (299) and `failed` task count (5,946) are misaligned by a factor of ~20×. Either:
1. The `failed` counter is wrong (inflated), or
2. `goal_generator_error` is not the primary failure mode, or
3. There is an *unobserved failure path* in the pipeline that does not emit `goal_generator_error` events.

If (3) is true, `research-foundry.service` failure diagnosis will be blind to the actual error path — you'll chase `goal_generator` while the real bug lives in `claims_extracted_v2` or `archaeology` stages (which show only 5,806 and 92–104 events respectively, against 17,676 tasks created).

**Recommended next action:** Check journal for non-`goal_generator` error patterns in the research-foundry service, specifically around claims extraction and archaeology stages where event counts drop off sharply.