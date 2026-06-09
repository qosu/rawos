<!-- rawos proactive analysis -->
<!-- goal: Generate HTML report from JSON metrics data -->
<!-- domain: deployment -->
<!-- generated: 1781027827 -->

SIGNAL

**File:** `metrics/report.html` — timestamp line and bar width rendering

**Observation 1 — Truncated timestamp (line 28):**
The generated timestamp reads `2026-06-09 17` — the minutes and `UTC` suffix are cut off. The `<p>` tag is incomplete as rendered. This suggests the template rendering pipeline truncated the string (likely a substring operation or buffer issue in the generation script). The HTML is valid structurally, but the timestamp is broken.

**Observation 2 — Bar widths do not reflect data proportions (lines 55-59):**
```
Bar "0-2":   style="width:12px"   val=142
Bar "2-4":   style="width:177px"  val=1951
Bar "4-6":   style="width:200px"  val=2197
Bar "6-8":   style="width:131px"  val=1447
Bar "8-10":  style="width:124px"  val=1371
```

If `200px` maps to the max value 2197, then:
- 0-2 (142) should be ~13px — plausible (12px close enough)
- 2-4 (1951) should be ~178px — plausible (177px close)
- 6-8 (1447) should be ~132px — plausible (131px close)
- 8-10 (1371) should be ~125px — plausible (124px close)

So bar widths are **approximately** proportional but slightly off (0-2 is 12px vs expected ~13px, 8-10 is 124px vs expected ~125px). These are minor rounding differences — acceptable for display.

**Consequence:** The timestamp truncation on line 28 is the actionable defect. If this report is deployed to staging/production, the generated timestamp will be ambiguous and unprofessional. Bar widths are visually acceptable but worth noting if the rendering function is revisited — prefer rounding up rather than down to avoid zero-width bars for small values.