<!-- rawos proactive analysis -->
<!-- goal: Debug research-foundry service failure and generate reports -->
<!-- domain: debugging -->
<!-- generated: 1781022015 -->

SIGNAL

**Observation:** The user has been rapidly cycling through edits to `report.html`, `metrics.json`, and `summary.json` — multiple times each — but there is no evidence of a structured debugging process (no service logs checked, no unit tests run, no git history of branch creation or commits).

**Evidence:** The activity log shows no `bash` commands for `journalctl`, `systemctl status`, or test execution. Without service logs, the root cause of "research-foundry service failure" cannot be determined. Editing the output artifacts (`.html`, `.json`) in isolation is treating symptoms, not the underlying fault.

**Consequence:** If the service failure is caused by bad input, broken schema, or a runtime crash, no amount of regenerating reports will fix it. The reports will be rebuilt and then immediately overwritten again on the next service run. The user is stuck in an edit loop with no diagnostic signal.

**Recommending:** Before any further edits, run `journalctl -u research-foundry.service --no-pager -n 100` to get actual failure data. Then decide whether the fix is in code, config, or data — and create a `rawos/debug-research-foundry` branch for the fix.