<!-- rawos proactive analysis -->
<!-- goal: Debug assignee-server service failure and generate reports -->
<!-- domain: debugging -->
<!-- generated: 1781030695 -->

SIGNAL

**File: `/root/sovereign-research-kernel/metrics/metrics.json`, line 17**

The `events_by_type.tribunal_complete` count (7,154) exactly matches `policy_evaluated` (7,154), and `papers_processed` (143,259) is suspiciously close to `events_by_type.research_fetch` (143,413) — only 154 apart. But the critical mismatch is:

- `claims_verified` (20,944) exceeds `claims_extracted` (20,007) by 937. This is logically impossible — claims cannot be verified before they are extracted. This is not a transient race condition; these are cumulative counters in a single snapshot.

**Consequence:** Either the extraction counter is undercounting by ~4.7%, or the verification counter is double-counting. Any report relying on these metrics (e.g., report.html) will show a verification rate >100%, which will mislead diagnosis of the assignee-server failure. The number of claims verified being higher than extracted suggests a bug in the claims pipeline — possibly verification events firing without corresponding extraction events being logged.