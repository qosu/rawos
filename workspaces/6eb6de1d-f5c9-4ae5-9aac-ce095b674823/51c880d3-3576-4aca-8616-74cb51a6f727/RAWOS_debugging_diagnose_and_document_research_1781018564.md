<!-- rawos proactive analysis -->
<!-- goal: Diagnose and document research findings in reports and metrics -->
<!-- domain: debugging -->
<!-- generated: 1781018564 -->

SIGNAL

**File:** `/root/sovereign-research-kernel/metrics/report.html`, line 82

The histogram bar widths are **inconsistent with the displayed values**. The metric values range from 140 (0-2 bucket) to 2160 (4-6 bucket), yet the bar widths shown are 12px, 175px, 200px, 133px, and 126px respectively.

If we assume `200px` corresponds to the max value of 2160, then the proportional widths should be:
- 0-2: 140 → ~13px ✓ (12px)
- 2-4: 1895 → ~175px ✓ (175px)
- 4-6: 2160 → 200px ✓ (200px)
- 6-8: 1438 → ~133px ✓ (133px)
- 8-10: 1371 → ~127px ✓ (126px)

On closer inspection, the **values are actually consistent** — the bars are correctly proportional to the max value (2160 → 200px). I retract this as a false alarm.

**However**, here is a real discrepancy:

**File:** `/root/sovereign-research-kernel/metrics/report.html`, line 75-78 (Verdict Distribution)

The Tribunal Verdict Distribution shows:
- APPROVE: 1898 (27%)
- REVISE: 2489
- REJECT: 2617
- Total: 7004

1898 / 7004 = **27.1%** — the displayed 27% is approximately correct.

**Actual actionable finding:** The `Avg Revision Rounds` of **0.39** displayed next to verdict counts is misleading — it's not part of the verdict distribution semantically, yet it's rendered within the same `.grid` as a `.card`. This conflates a process metric (revision rounds) with outcome metrics (verdicts). Any downstream analysis tool parsing `.card` data from this grid would incorrectly treat 0.39 as a fourth verdict type.

**Consequence:** If any automated dashboard aggregates card values from this section, the 0.39 value would be misinterpreted as a verdict count or distribution fraction, corrupting aggregation logic.

**Recommendation:** Move `Avg Revision Rounds` into a separate grid or clearly label it as a separate metric section in the HTML.