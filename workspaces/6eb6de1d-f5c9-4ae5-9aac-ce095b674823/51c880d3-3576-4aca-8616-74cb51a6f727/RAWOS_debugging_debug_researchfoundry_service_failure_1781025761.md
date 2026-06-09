<!-- rawos proactive analysis -->
<!-- goal: Debug research-foundry service failure via metrics and report -->
<!-- domain: debugging -->
<!-- generated: 1781025761 -->

SIGNAL

**File:** `metrics/metrics.json` — line-by-line analysis

**Observation 1:** `goal_generator_error` count = 309 (line 37). This event type exists alongside `goal_generator_complete` = 6752 (line 33). Error rate = 309 / (6752 + 309) ≈ **4.4%.** This is a persistent failure mode in the research pipeline — nearly 1 in 23 goal generation attempts fails.

**Observation 2:** `claims_extracted_v2` = 6043 (line 34) vs `claims_extracted` = 2351 (line 36). Two competing extraction pipelines exist. The v2 pipeline has processed ~2.6x more claims than v1. This suggests the v1 pipeline may be deprecated or partially broken — if research-foundry depends on `claims_extracted` events (v1), it will miss ~73% of extraction throughput.

**Observation 3:** `claims_verified` = 20,899 (line 41) exceeds `claims_extracted` (both v1+v2 = 8,394) by **~12,505 claims**. Claims are being verified that were never extracted within the current metrics window. This is an anomaly that can cause downstream service failures if research-foundry expects extraction → verification ordering.

**Consequence if ignored:** Research-foundry.service may be failing because it depends on `claims_extracted` events that don't fire for v2 pipeline output, or because verification events arrive with no corresponding extraction record, breaking internal state machines.

**Recommendation:** Check which extraction pipeline research-foundry subscribes to, and whether `goal_generator_error` spikes correlate with the timing of service failures.