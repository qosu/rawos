<!-- rawos proactive analysis -->
<!-- goal: debug assignee-server service via report/metrics -->
<!-- domain: debugging -->
<!-- generated: 1781027300 -->

SIGNAL

**File: `/root/sovereign-research-kernel/metrics/report.html`, line 15** — The generated timestamp shows `2026-06-09 17:47 UTC` but the histogram bar widths are clearly unrealistically small relative to their values.

```
<div class="bar" style="width:12px"></div><span class="bar-val">142</span>   (0-2 bin: 142 items → 12px)
<div class="bar" style="width:177px"></div><span class="bar-val">1947</span>  (2-4 bin: 1947 items → 177px)
<div class="bar" style="width:200px"></div><span class="bar-val">2195</span>  (4-6 bin: 2195 items → 200px)
```

The ratio is not linear: 142 items → 12px (11.8 items/px) but 1947 items → 177px (11.0 items/px) — approximately consistent. However, the 0-2 bin **bar is nearly invisible** (12px wide) while representing 142 items. The `min-width:2px` fallback is being hit instead of scaling properly. A bar representing 142 items out of a max of 2195 should be `(142/2195)*200 = ~13px` — which matches, so the scaling _is_ correct. So no data inconsistency there.

**Actual issue:** The `report.html` shows `Tasks Total: 18068` but the `assignee-server` service status is unknown. The most actionable concrete observation:

**File: `metrics/report.html` line 50** — `Owner Queue Pending: 5045` represents **28% of all tasks stuck in owner queue**. If the assignee-server is supposed to drain this queue, 5045 pending items with no trend data (no delta from previous report, no rate shown) means we cannot determine if assignee-server is healthy or stalled. The report needs a `Queue drain rate (tasks/min)` metric or the assignee-server should expose a `/health` or `/metrics` endpoint to verify processing is active.

**Consequence:** Without queue drain rate or assignee-server health data, operator cannot distinguish between "assignee-server is backlogged but processing" vs "assignee-server is crashed/stopped." A silent failure would go undetected until the queue grows unbounded.

**Recommendation specific fix:** Add a `Queue Δ` or drain rate derived from consecutive metrics.json snapshots, or add an assignee-server health check block to report.html.