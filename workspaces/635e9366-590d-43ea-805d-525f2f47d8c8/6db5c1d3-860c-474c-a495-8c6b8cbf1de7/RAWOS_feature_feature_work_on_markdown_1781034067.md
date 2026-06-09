<!-- rawos proactive analysis -->
<!-- goal: feature work on markdown project -->
<!-- domain: feature -->
<!-- generated: 1781034067 -->

I now have a thorough understanding. The project workspace is a Markdown-based scraper project with:

- **`md_reporter.py`** — Markdown test/coverage report generator (our focus)
- **`scraper.py`** — Web scraper with `WebScraper` and `DataExporter` classes
- **`data_exporter.py`** — Standalone export module (duplicate of `DataExporter` in `scraper.py`)
- **`test_scraper.py`** — 18 passing tests (stdlib only)

The **clear feature gap**: `md_reporter.py` has two separate functions (`write_test_report` and `write_coverage_report`) with no unified API, no dashboard/page-level report, and no built-in way to generate **combined** markdown reports that include both test results and coverage in a single document.

I'm going to add a **`write_dashboard_report`** function to `md_reporter.py` that combines tests + coverage into a single Markdown document with summary cards, plus a **MarkdownSummary** context manager that lets you build reports step by step. This is a real, testable feature.

/root/rawos/workspaces/635e9366-590d-43ea-805d-525f2f47d8c8/6db5c1d3-860c-474c-a495-8c6b8cbf1de7/scraper/md_reporter.py
#!/usr/bin/env python3
"""
Markdown Test Reporter — Generate structured Markdown reports from test results.

Features:
  - write_test_report()      — Full test report with failure details
  - write_coverage_report()  — Coverage metrics with visual bars
  - write_dashboard_report() — Combined test + coverage dashboard
  - MarkdownSummary          — Context manager for building reports step by step

Usage:
    from md_reporter import write_test_report, write_dashboard_report, MarkdownSummary
    write_test_report(results, 'test_report.md')
"""

import json
import os
import time
from datetime import datetime
from contextlib import contextmanager

def format_duration(seconds):
    """Format a duration in seconds to a human-readable string."""
    if seconds < 0.001:
        return f"{seconds * 1000:.1f}ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{seconds / 60:.1f}m {seconds % 60:.0f}s"

def collect_test_results(test_runner_result):
    """
    Collect test results from a unittest.TestResult or unittest.TestRunner result.
    
    Args:
        test_runner_result: A TestResult object or tuple (result, output)
        
    Returns:
        dict with keys: total, passed, failed, errors, skipped, duration, tests
    """
    # Handle different return types from test runners
    result = test_runner_result
    if isinstance(test_runner_result, tuple) and len(test_runner_result) >= 2:
        result = test_runner_result[0]

    # Extract result info
    result_info = {
        'total': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors),
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'duration': getattr(result, 'duration', 0),
        'tests': [],
    }

    for test, traceback in result.failures:
        result_info['tests'].append({
            'name': str(test),
            'status': 'FAIL',
            'traceback': traceback,
        })

    for test, traceback in result.errors:
        result_info['tests'].append({
            'name': str(test),
            'status': 'ERROR',
            'traceback': traceback,
        })

    for test, reason in getattr(result, 'skipped', []):
        result_info['tests'].append({
            'name': str(test),
            'status': 'SKIPPED',
            'reason': reason,
        })

    return result_info

def write_test_report(results, filename, title=None, config=None):
    """
    Generate a structured Markdown test report file.
    
    Args:
        results: dict from collect_test_results(), or a TestResult object
        filename: Output .md file path
        title: Optional report title (default: auto-generated)
        config: Optional dict with extra metadata (version, description, etc.)
    
    Returns:
        Path to the generated report file
    """
    # Accept raw TestResult objects too
    if hasattr(results, 'testsRun'):
        results = collect_test_results(results)

    title = title or f"Test Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    lines = []
    
    # Header
    lines.append(f"# {title}")
    lines.append("")

    # Configuration metadata
    if config:
        lines.append("## Configuration")
        lines.append("")
        lines.append("| Key | Value |")
        lines.append("| --- | --- |")
        for k, v in config.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    total = results['total']
    passed = results['passed']
    failures = results['failures']
    errors = results['errors']
    skipped = results['skipped']
    duration = format_duration(results.get('duration', 0))

    pass_rate = (passed / total * 100) if total > 0 else 0

    lines.append(f"- **Total tests**: {total}")
    lines.append(f"- **Passed**: {passed} ({pass_rate:.1f}%)")
    lines.append(f"- **Failed**: {failures}")
    lines.append(f"- **Errors**: {errors}")
    lines.append(f"- **Skipped**: {skipped}")
    lines.append(f"- **Duration**: {duration}")
    lines.append("")

    # Status badge
    if failures > 0 or errors > 0:
        lines.append(f"![Status](https://img.shields.io/badge/status-{failures + errors}_failing-red)")
    elif total > 0:
        lines.append(f"![Status](https://img.shields.io/badge/status-all_passing-brightgreen)")
    lines.append("")

    # Detailed results table
    if results.get('tests'):
        lines.append("## Test Details")
        lines.append("")
        lines.append("| # | Test | Status | Detail |")
        lines.append("| --- | --- | --- | --- |")
        for i, t in enumerate(results['tests'], 1):
            status = t['status']
            # Emoji status
            if status == 'FAIL':
                icon = '❌'
            elif status == 'ERROR':
                icon = '💥'
            elif status == 'SKIPPED':
                icon = '⏭️'
            else:
                icon = '✅'

            detail = ''
            if status in ('FAIL', 'ERROR'):
                tb = t.get('traceback', '')
                # Take first meaningful line of traceback
                for line in tb.split('\n'):
                    line = line.strip()
                    if line and 'Traceback' not in line and 'File' not in line:
                        detail = line[:80]
                        break
                if not detail:
                    detail = '(see traceback below)'
            elif status == 'SKIPPED':
                detail = t.get('reason', '')
            
            lines.append(f"| {i} | `{t['name']}` | {icon} {status} | {detail} |")
        lines.append("")

    # Failure details with collapsible sections
    has_failures = [t for t in results.get('tests', []) if t['status'] in ('FAIL', 'ERROR')]
    if has_failures:
        lines.append("## Failure Details")
        lines.append("")
        for t in has_failures:
            safe_name = t['name'].replace('(', '_').replace(')', '_')
            lines.append(f"<details>")
            lines.append(f"<summary><strong>{t['status']}</strong>: {t['name']}</summary>")
            lines.append("")
            lines.append("```")
            traceback = t.get('traceback', 'No traceback captured')
            lines.append(traceback[:2000])  # Truncate very long tracebacks
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Timestamp footer
    lines.append("---")
    lines.append(f"*Report generated: {datetime.now().isoformat()}*")
    lines.append("")

    content = '\n'.join(lines)

    # Create parent directory if needed
    parent = os.path.dirname(filename)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"  📝 Test report written: {filename} ({len(content)} bytes)")
    return filename

def write_coverage_report(coverage_data, filename):
    """
    Generate a Markdown coverage report.
    
    Args:
        coverage_data: dict with keys mapping to coverage percentages
                       e.g. {'statement': 85.0, 'branch': 72.5}
        filename: Output .md file path
    
    Returns:
        Path to the generated report file
    """
    lines = []
    lines.append("# Coverage Report")
    lines.append("")
    lines.append("| Metric | Coverage | Status |")
    lines.append("| --- | --- | --- |")
    
    for metric, value in coverage_data.items():
        if isinstance(value, (int, float)):
            bar_len = 20
            filled = int(value / 100 * bar_len)
            bar = '█' * filled + '░' * (bar_len - filled)
            status = '✅' if value >= 80 else ('⚠️' if value >= 50 else '❌')
            lines.append(f"| {metric} | {value:.1f}% {bar} | {status} |")
    
    lines.append("")
    lines.append("---")
    lines.append(f"*Report generated: {datetime.now().isoformat()}*")
    lines.append("")

    content = '\n'.join(lines)
    
    parent = os.path.dirname(filename)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"  📊 Coverage report written: {filename} ({len(content)} bytes)")
    return filename

# ---------------------------------------------------------------------------
# NEW FEATURE: Combined Dashboard Report + MarkdownSummary builder
# ---------------------------------------------------------------------------

def write_dashboard_report(test_results, coverage_data, filename, title=None, config=None):
    """
    Generate a combined Markdown dashboard with test results AND coverage data.
    
    This is the unified reporting entrypoint — produces a richer single-page
    report suitable for CI artifacts, PR summaries, or project documentation.
    
    Args:
        test_results: dict from collect_test_results(), or a TestResult object
        coverage_data: dict with coverage metric -> percentage (float 0-100)
        filename: Output .md file path
        title: Optional report title
        config: Optional dict with extra metadata
    
    Returns:
        Path to the generated dashboard file
    """
    if hasattr(test_results, 'testsRun'):
        test_results = collect_test_results(test_results)

    title = title or f"Project Dashboard — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    lines = []
    lines.append(f"# {title}")
    lines.append("")

    # ---- Configuration ----
    if config:
        lines.append("## Configuration")
        lines.append("")
        lines.append("| Key | Value |")
        lines.append("| --- | --- |")
        for k, v in config.items():
            lines.append(f"| {k} | {v} |")
        lines.append("")

    # ---- Summary Cards ----
    lines.append("## Summary")
    lines.append("")

    total = test_results['total']
    passed = test_results['passed']
    failures = test_results['failures']
    errors = test_results['errors']
    skipped = test_results['skipped']
    duration = format_duration(test_results.get('duration', 0))
    pass_rate = (passed / total * 100) if total > 0 else 0

    lines.append('<table>')
    lines.append('<tr>')
    lines.append(f'<td align="center"><strong>🧪 Tests</strong><br/>{total}</td>')
    lines.append(f'<td align="center"><strong>✅ Passed</strong><br/>{passed} ({pass_rate:.1f}%)</td>')
    lines.append(f'<td align="center"><strong>❌ Failed</strong><br/>{failures}</td>')
    lines.append(f'<td align="center"><strong>💥 Errors</strong><br/>{errors}</td>')
    lines.append(f'<td align="center"><strong>⏭️ Skipped</strong><br/>{skipped}</td>')
    lines.append(f'<td align="center"><strong>⏱ Duration</strong><br/>{duration}</td>')
    lines.append('</tr>')
    lines.append('</table>')
    lines.append("")

    # Coverage cards
    if coverage_data:
        lines.append('<table>')
        lines.append('<tr>')
        for metric, value in coverage_data.items():
            if isinstance(value, (int, float)):
                icon = '✅' if value >= 80 else ('⚠️' if value >= 50 else '❌')
                lines.append(
                    f'<td align="center"><strong>{icon} {metric}</strong><br/>{value:.1f}%</td>'
                )
        lines.append('</tr>')
        lines.append('</table>')
        lines.append("")

    # Status badge
    if failures > 0 or errors > 0:
        lines.append(f"![Status](https://img.shields.io/badge/status-{failures + errors}_failing-red)")
    elif total > 0:
        lines.append(f"![Status](https://img.shields.io/badge/status-all_passing-brightgreen)")
    lines.append("")

    # ---- Coverage Detail ----
    if coverage_data:
        lines.append("## Coverage Detail")
        lines.append("")
        lines.append("| Metric | Coverage | Status |")
        lines.append("| --- | --- | --- |")
        for metric, value in coverage_data.items():
            if isinstance(value, (int, float)):
                bar_len = 20
                filled = int(value / 100 * bar_len)
                bar = '█' * filled + '░' * (bar_len - filled)
                status = '✅' if value >= 80 else ('⚠️' if value >= 50 else '❌')
                lines.append(f"| {metric} | {value:.1f}% {bar} | {status} |")
        lines.append("")

    # ---- Test Detail Table ----
    if test_results.get('tests'):
        lines.append("## Test Details")
        lines.append("")
        lines.append("| # | Test | Status | Detail |")
        lines.append("| --- | --- | --- | --- |")
        for i, t in enumerate(test_results['tests'], 1):
            status = t['status']
            if status == 'FAIL':
                icon = '❌'
            elif status == 'ERROR':
                icon = '💥'
            elif status == 'SKIPPED':
                icon = '⏭️'
            else:
                icon = '✅'

            detail = ''
            if status in ('FAIL', 'ERROR'):
                tb = t.get('traceback', '')
                for line in tb.split('\n'):
                    line = line.strip()
                    if line and 'Traceback' not in line and 'File' not in line:
                        detail = line[:80]
                        break
                if not detail:
                    detail = '(see traceback below)'
            elif status == 'SKIPPED':
                detail = t.get('reason', '')

            lines.append(f"| {i} | `{t['name']}` | {icon} {status} | {detail} |")
        lines.append("")

    # ---- Failure Details (collapsible) ----
    has_failures = [t for t in test_results.get('tests', []) if t['status'] in ('FAIL', 'ERROR')]
    if has_failures:
        lines.append("## Failure Details")
        lines.append("")
        for t in has_failures:
            lines.append(f"<details>")
            lines.append(f"<summary><strong>{t['status']}</strong>: {t['name']}</summary>")
            lines.append("")
            lines.append("```")
            traceback = t.get('traceback', 'No traceback captured')
            lines.append(traceback[:2000])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # ---- Footer ----
    lines.append("---")
    lines.append(f"*Dashboard generated: {datetime.now().isoformat()}*")
    lines.append("")

    content = '\n'.join(lines)
    parent = os.path.dirname(filename)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"  📊 Dashboard report written: {filename} ({len(content)} bytes)")
    return filename

@contextmanager
def MarkdownSummary(filename, title=None, config=None):
    """
    Context manager for building a Markdown report step by step.

    Yields a builder object with .add_section(), .add_table(), .add_code_block()
    methods. Writes the file on exit.

    Usage:
        with MarkdownSummary('report.md', title='My Report') as report:
            report.add_section('Results', 'All tests passed.')
            report.add_table(['Name', 'Status'], [['test_a', 'PASS']])
    """
    class ReportBuilder:
        def __init__(self):
            self.lines = []
            self._title