"""
Unit tests for md_reporter.py — the Markdown report generation module.

Tests cover:
  - format_duration: formatting second values to human-readable strings
  - collect_test_results: converting TestResult objects to structured dicts
  - write_test_report: generating full test report Markdown files
  - write_coverage_report: generating coverage Markdown files
  - Edge cases: empty results, 0% / 100% coverage, missing tracebacks
"""
import unittest
import unittest.mock
import tempfile
import os
import json
import re

from md_reporter import (
    format_duration,
    collect_test_results,
    write_test_report,
    write_coverage_report,
)


class TestFormatDuration(unittest.TestCase):
    """format_duration converts seconds to human-readable strings"""

    def test_microseconds(self):
        """< 0.001s → expressed in ms with 1 decimal"""
        result = format_duration(0.0005)
        self.assertEqual(result, "0.5ms")

    def test_milliseconds(self):
        """0.001–1s → expressed in ms as integer"""
        result = format_duration(0.150)
        self.assertEqual(result, "150ms")

    def test_seconds(self):
        """1–60s → expressed in s with 2 decimals"""
        result = format_duration(2.5)
        self.assertEqual(result, "2.50s")

    def test_minutes_and_seconds(self):
        """>=60s → expressed in minutes and seconds"""
        result = format_duration(125)
        self.assertEqual(result, "2m 5s")

    def test_exact_zero(self):
        """0 seconds → 0.0ms"""
        result = format_duration(0)
        self.assertEqual(result, "0.0ms")

    def test_negative_value(self):
        """Negative duration (shouldn't happen, but won't crash)"""
        result = format_duration(-0.5)
        self.assertEqual(result, "-500ms")


class TestCollectTestResults(unittest.TestCase):
    """collect_test_results converts TestResult to structured dict"""

    def setUp(self):
        self.runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)

    def test_all_passing(self):
        """All tests passing → correct counts"""
        suite = unittest.TestSuite()
        suite.addTest(PassingTest('test_method'))
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertEqual(collected['total'], 1)
        self.assertEqual(collected['passed'], 1)
        self.assertEqual(collected['failures'], 0)
        self.assertEqual(collected['errors'], 0)
        self.assertEqual(collected['skipped'], 0)

    def test_with_failure(self):
        """Failing test → failure captured in tests list"""
        suite = unittest.TestSuite()
        suite.addTest(FailingTest('test_fails'))
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertEqual(collected['failures'], 1)
        self.assertEqual(len(collected['tests']), 1)
        self.assertEqual(collected['tests'][0]['status'], 'FAIL')

    def test_with_error(self):
        """Errored test → error captured"""
        suite = unittest.TestSuite()
        suite.addTest(ErroringTest('test_errors'))
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertEqual(collected['errors'], 1)
        self.assertEqual(len(collected['tests']), 1)
        self.assertEqual(collected['tests'][0]['status'], 'ERROR')

    def test_with_skip(self):
        """Skipped test → captured in skipped count and tests list"""
        suite = unittest.TestSuite()
        suite.addTest(SkippingTest('test_skip'))
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertEqual(collected['skipped'], 1)
        self.assertEqual(len(collected['tests']), 1)
        self.assertEqual(collected['tests'][0]['status'], 'SKIPPED')

    def test_tuple_result_handling(self):
        """(result, output) tuple → extracts result from first element"""
        suite = unittest.TestSuite()
        suite.addTest(PassingTest('test_method'))
        result = self.runner.run(suite)
        collected = collect_test_results((result, "some output"))
        self.assertEqual(collected['total'], 1)
        self.assertEqual(collected['passed'], 1)

    def test_empty_suite(self):
        """No tests → zero counts"""
        suite = unittest.TestSuite()
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertEqual(collected['total'], 0)
        self.assertEqual(collected['passed'], 0)

    def test_failure_traceback_included(self):
        """Failure traceback is captured in test entry"""
        suite = unittest.TestSuite()
        suite.addTest(FailingTest('test_fails'))
        result = self.runner.run(suite)
        collected = collect_test_results(result)
        self.assertIn('AssertionError', collected['tests'][0].get('traceback', ''))


class TestWriteTestReport(unittest.TestCase):
    """write_test_report generates valid Markdown test reports"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _make_path(self, name):
        return os.path.join(self.tmpdir, name)

    def test_basic_report_creates_file(self):
        """Basic report → file exists and is non-empty"""
        results = {
            'total': 10, 'passed': 8, 'failures': 1, 'errors': 1,
            'skipped': 0, 'duration': 0.5,
            'tests': [
                {'name': 'test_a', 'status': 'PASS'},
                {'name': 'test_b', 'status': 'FAIL', 'traceback': 'AssertionError'},
                {'name': 'test_c', 'status': 'ERROR', 'traceback': 'KeyError'},
            ]
        }
        path = self._make_path('report.md')
        result = write_test_report(results, path)
        self.assertEqual(result, path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 50)

    def test_report_contains_summary_stats(self):
        """Report contains total, passed, failed, error, skipped counts"""
        results = {
            'total': 5, 'passed': 3, 'failures': 1, 'errors': 1,
            'skipped': 0, 'duration': 0.3,
            'tests': [],
        }
        path = self._make_path('summary.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('5', content)
        self.assertIn('3', content)
        self.assertIn('1', content)  # failures
        self.assertIn('1', content)  # errors

    def test_report_includes_table(self):
        """Report has test details table with status column"""
        results = {
            'total': 3, 'passed': 1, 'failures': 1, 'errors': 1,
            'skipped': 0, 'duration': 0.3,
            'tests': [
                {'name': 'test_x', 'status': 'PASS'},
                {'name': 'test_y', 'status': 'FAIL', 'traceback': 'oops'},
                {'name': 'test_z', 'status': 'ERROR', 'traceback': 'boom'},
            ]
        }
        path = self._make_path('table.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('| # | Test | Status', content)
        self.assertIn('PASS', content)
        self.assertIn('FAIL', content)
        self.assertIn('ERROR', content)

    def test_report_failure_details_collapsible(self):
        """Failures rendered as <details> sections"""
        results = {
            'total': 2, 'passed': 0, 'failures': 2, 'errors': 0,
            'skipped': 0, 'duration': 0.1,
            'tests': [
                {'name': 'test_fail', 'status': 'FAIL', 'traceback': 'ValueError: x'},
                {'name': 'test_fail2', 'status': 'FAIL', 'traceback': 'TypeError: y'},
            ]
        }
        path = self._make_path('failures.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('<details>', content)
        self.assertIn('</details>', content)
        self.assertIn('ValueError', content)

    def test_report_with_custom_title(self):
        """Custom title appears in report"""
        results = {'total': 1, 'passed': 1, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        path = self._make_path('custom.md')
        write_test_report(results, path, title='Custom Title Here')
        with open(path) as f:
            content = f.read()
        self.assertIn('Custom Title Here', content)
        self.assertIn('# Custom Title Here', content)

    def test_report_with_config_metadata(self):
        """Config metadata renders as a table"""
        results = {'total': 1, 'passed': 1, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        path = self._make_path('config.md')
        write_test_report(results, path, config={'version': '2.0', 'suite': 'all'})
        with open(path) as f:
            content = f.read()
        self.assertIn('version', content)
        self.assertIn('2.0', content)
        self.assertIn('suite', content)

    def test_report_accepts_raw_testresult(self):
        """Accepts TestResult directly (not just dict)"""
        import io
        runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
        suite = unittest.TestSuite()
        suite.addTest(PassingTest('test_method'))
        result = runner.run(suite)
        path = self._make_path('raw.md')
        write_test_report(result, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('Test Report', content)
        self.assertIn('1', content)  # total tests = 1

    def test_report_no_tests(self):
        """Empty test list doesn't crash"""
        results = {'total': 0, 'passed': 0, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.0, 'tests': []}
        path = self._make_path('empty.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('0', content)

    def test_report_timestamp_footer(self):
        """Report has ISO timestamp footer"""
        results = {'total': 1, 'passed': 1, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        path = self._make_path('timestamp.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('Report generated:', content)
        # ISO format has T separator
        date_pattern = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        self.assertRegex(content, date_pattern)

    def test_report_creates_parent_directory(self):
        """Creates parent directory if it doesn't exist"""
        results = {'total': 1, 'passed': 1, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        deep_path = os.path.join(self.tmpdir, 'sub', 'deep', 'report.md')
        result = write_test_report(results, deep_path)
        self.assertEqual(result, deep_path)
        self.assertTrue(os.path.exists(deep_path))

    def test_skipped_test_has_reason(self):
        """Skipped test reason shown in detail column"""
        results = {
            'total': 1, 'passed': 0, 'failures': 0, 'errors': 0,
            'skipped': 1, 'duration': 0.0,
            'tests': [
                {'name': 'test_skip_me', 'status': 'SKIPPED', 'reason': 'not implemented yet'},
            ]
        }
        path = self._make_path('skip.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('not implemented yet', content)

    def test_status_badge_passing(self):
        """All passing → green badge"""
        results = {'total': 5, 'passed': 5, 'failures': 0, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        path = self._make_path('badge_pass.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('brightgreen', content)

    def test_status_badge_failing(self):
        """Some failures → red badge"""
        results = {'total': 5, 'passed': 3, 'failures': 2, 'errors': 0,
                   'skipped': 0, 'duration': 0.1, 'tests': []}
        path = self._make_path('badge_fail.md')
        write_test_report(results, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('red', content)


class TestWriteCoverageReport(unittest.TestCase):
    """write_coverage_report generates valid Markdown coverage reports"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _make_path(self, name):
        return os.path.join(self.tmpdir, name)

    def test_basic_coverage_creates_file(self):
        """Basic coverage → file exists and has header"""
        data = {'statement': 85.0, 'branch': 72.5}
        path = self._make_path('coverage.md')
        result = write_coverage_report(data, path)
        self.assertEqual(result, path)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        self.assertIn('# Coverage Report', content)

    def test_coverage_percentage_bars(self):
        """Visual bar chart with filled/empty blocks"""
        data = {'lines': 50.0}
        path = self._make_path('bars.md')
        write_coverage_report(data, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('█', content)
        self.assertIn('░', content)

    def test_coverage_status_indicators(self):
        """✅ for >=80%, ⚠️ for 50-79%, ❌ for <50%"""
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False) as f:
            path = f.name
        try:
            write_coverage_report({'high': 95.0, 'medium': 65.0, 'low': 30.0}, path)
            with open(path) as fh:
                content = fh.read()
            self.assertIn('✅', content)
            self.assertIn('⚠️', content)
            self.assertIn('❌', content)
        finally:
            os.unlink(path)

    def test_coverage_100_percent(self):
        """100% coverage renders correctly"""
        data = {'all': 100.0}
        path = self._make_path('100.md')
        write_coverage_report(data, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('100.0%', content)
        self.assertIn('✅', content)

    def test_coverage_0_percent(self):
        """0% coverage renders without division errors"""
        data = {'none': 0.0}
        path = self._make_path('0.md')
        write_coverage_report(data, path)
        with open(path) as f:
            content = f.read()
        self.assertIn('0.0%', content)
        self.assertIn('❌', content)

    def test_coverage_large_dict(self):
        """Many metrics all render in the table"""
        data = {f'metric_{i}': float(i * 10) for i in range(11)}
        path = self._make_path('large.md')
        write_coverage_report(data, path)
        with open(path) as f:
            content = f.read()
        for i in range(11):
            self.assertIn(f'metric_{i}', content)

    def test_coverage_creates_parent_directory(self):
        """Creates parent directory if it doesn't exist"""
        data = {'stmt': 88.0}
        deep_path = os.path.join(self.tmpdir, 'cov', 'sub', 'report.md')
        result = write_coverage_report(data, deep_path)
        self.assertEqual(result, deep_path)
        self.assertTrue(os.path.exists(deep_path))


# ── Helper test classes ─────────────────────────────────────────────

import io


class PassingTest(unittest.TestCase):
    def test_method(self):
        pass


class FailingTest(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)


class ErroringTest(unittest.TestCase):
    def test_errors(self):
        raise KeyError('missing')


class SkippingTest(unittest.TestCase):
    @unittest.skip('reason to skip')
    def test_skip(self):
        pass


if __name__ == '__main__':
    unittest.main()
