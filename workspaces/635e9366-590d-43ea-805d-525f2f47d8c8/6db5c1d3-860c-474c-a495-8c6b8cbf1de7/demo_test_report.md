# Test Report — 2026-06-09 16:37:14

## Configuration

| Key | Value |
| --- | --- |
| version | 1.0.0 |
| suite | scraper tests |

## Summary

- **Total tests**: 18
- **Passed**: 16 (88.9%)
- **Failed**: 1
- **Errors**: 1
- **Skipped**: 0
- **Duration**: 345ms

![Status](https://img.shields.io/badge/status-2_failing-red)

## Test Details

| # | Test | Status | Detail |
| --- | --- | --- | --- |
| 1 | `test_csv_creates_file (test_scraper.TestDataExporter)` | ✅ PASS |  |
| 2 | `test_rate_limit_enforces_minimum_delay (test_scraper.TestRateLimit)` | ❌ FAIL | AssertionError: 0.089 < 0.08 |
| 3 | `test_json_creates_valid_file (test_scraper.TestDataExporter)` | 💥 ERROR | json.JSONDecodeError: Expecting value: line 1 column 1 (char 0) |

## Failure Details

<details>
<summary><strong>FAIL</strong>: test_rate_limit_enforces_minimum_delay (test_scraper.TestRateLimit)</summary>

```
AssertionError: 0.089 < 0.08
```

</details>

<details>
<summary><strong>ERROR</strong>: test_json_creates_valid_file (test_scraper.TestDataExporter)</summary>

```
json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

</details>

---
*Report generated: 2026-06-09T16:37:14.948149*
