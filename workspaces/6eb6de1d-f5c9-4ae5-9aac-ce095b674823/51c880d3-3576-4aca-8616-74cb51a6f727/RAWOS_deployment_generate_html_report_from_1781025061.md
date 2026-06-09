<!-- rawos proactive analysis -->
<!-- goal: Generate HTML report from JSON metrics data -->
<!-- domain: deployment -->
<!-- generated: 1781025061 -->

SIGNAL

**File:** `metrics/metrics.json` — Line 51 and Line 58

The dataset contains a clear structural inconsistency that will cause the HTML report to render misleading information:

- **Line 51:** `"claims_extracted": 19995` — this is a top-level aggregate key.
- **Line 58:** `"claims_extracted_v2": 6037` — nested under `events_by_type`.

These represent the **same logical metric** (claims extracted) but report vastly different values (19,995 vs. 6,037). The JSON shows two separate extraction pipelines (`claims_extracted` and `claims_extracted_v2` in events_by_type), yet the top-level `claims_extracted` (19,995) doesn't match either event count. This means:

1. If `report.html` renders `claims_extracted` from the top-level key, it will show 19,995 — but the event breakdown won't sum to that number.
2. The `events_by_type.claims_extracted` (2,351) plus `claims_extracted_v2` (6,037) = 8,388, which still doesn't match 19,995.
3. The `claims_verified` (20,896) > `claims_extracted` (19,995) is mathematically impossible — you can't verify more claims than were extracted.

**Consequence:** Any dashboard, chart, or summary that displays extraction vs. verification rates will show a negative extraction gap or a >100% verification rate, eroding trust in the report. The user's intent to "verify metrics consistency" directly conflicts with this data.

**Suggested fix:** Either reconcile the event counts to match the aggregate (update `events_by_type.claims_extracted` and `claims_extracted_v2` to sum to 19,995), or update the top-level `claims_extracted` to match the sum of event counts (8,388), then adjust `claims_verified` accordingly (must be ≤ claims_extracted).