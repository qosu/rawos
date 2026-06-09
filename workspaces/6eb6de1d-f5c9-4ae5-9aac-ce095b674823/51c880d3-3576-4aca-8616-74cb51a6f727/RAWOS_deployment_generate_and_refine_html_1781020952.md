<!-- rawos proactive analysis -->
<!-- goal: Generate and refine HTML reports from JSON metrics data -->
<!-- domain: deployment -->
<!-- generated: 1781020952 -->

The fix is committed on branch `rawos/remove-duplicate-timestamp`. The broken duplicate timestamp line at `report.html:25` has been removed — the report now has a single correct timestamp line:

```
<p style="color:#555;font-size:0.8rem">Generated: 2026-06-09 16:01 UTC</p>
```