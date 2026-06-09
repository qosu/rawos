<!-- rawos proactive analysis -->
<!-- goal: Debug assignee-server service failure and generate reports -->
<!-- domain: debugging -->
<!-- generated: 1781015902 -->

SIGNAL

Two concrete observations with specific evidence:

**1. Report timestamp drift (report.html:55)**

The report shows `Generated: 2026-06-09 14:36 UTC` but the actual file modification history from the diff shows repeated edits stretching back to `2026-05-29 08:03 UTC`. After 30 edits in ~30 minutes, the report only has its timestamp updated — not the actual metrics data. The `2377 Tasks Total` referenced in earlier edits now shows `17793` — these are hardcoded values being manually toggled, not sourced from the failed service.

**2. The assignee-server.service failure is invisible in the report**

The trigger clearly states `service_failed:assignee-server.service` as the root event. Yet report.html contains zero references to service health, last heartbeat, failed services, or any operational metrics. The report shows tribunal/policy/paper metrics which are downstream artifacts at best. If the assignee server is down, none of those numbers are current — they're stale data from before the failure.

**Consequence**: The report is being treated as a dashboard of real-time system health, but it contains no service-liveness data and its data provenance is unknown (manual edits vs. live polling). Anyone relying on this report to diagnose the assignee-server outage will miss the root cause entirely because the report doesn't surface it. The 30 edits in 30 minutes are churn, not progress — metrics values are being rewritten without a data pipeline backing them.

I am confident in both observations (0.92). I will not create a branch for this because the issue is structural (report lacks service health data, data source is manual edits) and fixing it would require defining a data pipeline contract, which is outside the scope of debugging a single service failure.