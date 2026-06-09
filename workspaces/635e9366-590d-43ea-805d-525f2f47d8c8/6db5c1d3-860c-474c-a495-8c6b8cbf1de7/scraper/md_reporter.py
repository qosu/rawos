#!/usr/bin/env python3
"""
Markdown Test Reporter — Generate structured Markdown reports from test results.

Usage:
    from md_reporter import write_test_report
    write_test_report(results, 'test_report.md')
"""

import json
import os
import time
from datetime import datetime


def format_duration(seconds):
    """Format a duration in seconds to a human-readable string."""
    if seconds < 0:
        return f"-{format_duration(-seconds)}"
    if seconds < 0.001:
        return f"{seconds * 1000:.1f}ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


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


if __name__ == '__main__':
    # Demo: generate a sample report
    sample_results = {
        'total': 18,
        'passed': 16,
        'failures': 1,
        'errors': 1,
        'skipped': 0,
        'duration': 0.345,
        'tests': [
            {
                'name': 'test_csv_creates_file (test_scraper.TestDataExporter)',
                'status': 'PASS'
            },
            {
                'name': 'test_rate_limit_enforces_minimum_delay (test_scraper.TestRateLimit)',
                'status': 'FAIL',
                'traceback': 'AssertionError: 0.089 < 0.08'
            },
            {
                'name': 'test_json_creates_valid_file (test_scraper.TestDataExporter)',
                'status': 'ERROR',
                'traceback': 'json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)'
            },
        ]
    }

    write_test_report(sample_results, 'demo_test_report.md', config={
        'version': '1.0.0',
        'suite': 'scraper tests',
    })
    write_coverage_report({'statement': 87.5, 'branch': 71.2}, 'demo_coverage_report.md')
    print("\n✅ Demo reports generated. Open the .md files to view.")
