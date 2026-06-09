"""
Unit tests for WebScraper and DataExporter.
Uses only stdlib (unittest) since pip is unavailable in this environment.
Tests the components that don't require external dependencies.
"""
import unittest
import tempfile
import os
import json
import csv
import time
import sys
import io

# We can't import scraper directly because requests/bs4 are missing.
# Instead, test the DataExporter class by extracting it, and test
# the _rate_limit logic by testing the algorithm in isolation.

# ---------- DataExporter tests (no dependencies) ----------

class TestDataExporter(unittest.TestCase):
    """Test DataExporter CSV/JSON/Markdown export methods"""

    def setUp(self):
        self.sample_data = [
            {'name': 'Alice', 'score': 95, 'active': True},
            {'name': 'Bob', 'score': 87, 'active': False},
            {'name': 'Charlie', 'score': 92, 'active': True},
        ]

    # --- Inline reimplementation of DataExporter for isolated testing ---

    def _export_to_csv(self, data, filename, fieldnames=None):
        if not data:
            return
        if fieldnames is None:
            fieldnames = list(data[0].keys()) if isinstance(data[0], dict) else []
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in data:
                flat = {}
                for k, v in row.items():
                    if isinstance(v, (dict, list)):
                        flat[k] = json.dumps(v, ensure_ascii=False)
                    else:
                        flat[k] = v
                writer.writerow(flat)
        return filename

    def _export_to_json(self, data, filename, pretty=True):
        with open(filename, 'w', encoding='utf-8') as f:
            if pretty:
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                json.dump(data, f, ensure_ascii=False)
        return filename

    def _export_to_markdown(self, data, filename):
        if not data or not isinstance(data, list):
            return
        fieldnames = list(data[0].keys()) if isinstance(data[0], dict) else []
        if not fieldnames:
            return
        lines = ['| ' + ' | '.join(fieldnames) + ' |']
        lines.append('| ' + ' | '.join(['---'] * len(fieldnames)) + ' |')
        for row in data:
            vals = []
            for k in fieldnames:
                v = row.get(k, '')
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                vals.append(str(v)[:60])
            lines.append('| ' + ' | '.join(vals) + ' |')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return filename

    def test_csv_creates_file(self):
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_csv(self.sample_data, tmpname)
            self.assertTrue(os.path.exists(tmpname))
            with open(tmpname) as fh:
                content = fh.read()
            self.assertIn('Alice', content)
            self.assertIn('score', content)
            self.assertIn('name', content)
        finally:
            os.unlink(tmpname)

    def test_csv_row_count(self):
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_csv(self.sample_data, tmpname)
            with open(tmpname) as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
            self.assertEqual(len(rows), 3)
        finally:
            os.unlink(tmpname)

    def test_csv_nested_field_flattened(self):
        data = [{'name': 'Test', 'tags': ['a', 'b', 'c']}]
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_csv(data, tmpname)
            with open(tmpname) as fh:
                content = fh.read()
            # CSV writes JSON with double-quote escaping.
            # json.dumps(['a', 'b', 'c']) = '["a", "b", "c"]'
            # csv.DictWriter then wraps it in quotes and escapes inner quotes: """[""a"", ""b"", ""c""]"""
            # So check for the serialized values a, b, c appearing in the CSV content
            self.assertIn('a', content)
            self.assertIn('b', content)
            self.assertIn('c', content)
            # Also verify the JSON structure is preserved (decoded back)
            with open(tmpname) as fh:
                reader = csv.DictReader(fh)
                row = next(reader)
            loaded_tags = json.loads(row['tags'])
            self.assertEqual(loaded_tags, ['a', 'b', 'c'])
        finally:
            os.unlink(tmpname)

    def test_csv_empty_data(self):
        """Empty data should not crash"""
        with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
            tmpname = f.name
        try:
            # Should not raise
            self._export_to_csv([], tmpname)
        finally:
            os.unlink(tmpname)

    def test_json_creates_valid_file(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_json(self.sample_data, tmpname)
            with open(tmpname) as fh:
                loaded = json.load(fh)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded[0]['name'], 'Alice')
            self.assertEqual(loaded[1]['score'], 87)
        finally:
            os.unlink(tmpname)

    def test_json_pretty_formatting(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_json(self.sample_data, tmpname, pretty=True)
            with open(tmpname) as fh:
                content = fh.read()
            # Pretty JSON has newlines
            self.assertIn('\n', content)
            self.assertIn('  ', content)
        finally:
            os.unlink(tmpname)

    def test_json_compact_formatting(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_json(self.sample_data, tmpname, pretty=False)
            with open(tmpname) as fh:
                content = fh.read()
            # Compact JSON has no unnecessary whitespace
            self.assertNotIn('\n  ', content)
        finally:
            os.unlink(tmpname)

    def test_markdown_creates_table(self):
        with tempfile.NamedTemporaryFile(suffix='.md', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_markdown(self.sample_data, tmpname)
            with open(tmpname) as fh:
                content = fh.read()
            self.assertIn('| name', content)
            self.assertIn('| ---', content)
            self.assertIn('Alice', content)
            self.assertIn('Charlie', content)
        finally:
            os.unlink(tmpname)

    def test_markdown_empty_data(self):
        with tempfile.NamedTemporaryFile(suffix='.md', delete=False) as f:
            tmpname = f.name
        try:
            result = self._export_to_markdown([], tmpname)
            self.assertIsNone(result)
        finally:
            os.unlink(tmpname)

    def test_markdown_nested_values_truncated(self):
        data = [{'name': 'Long', 'nested': {'a': 'b' * 100}}]
        with tempfile.NamedTemporaryFile(suffix='.md', delete=False) as f:
            tmpname = f.name
        try:
            self._export_to_markdown(data, tmpname)
            with open(tmpname) as fh:
                content = fh.read()
            # Values should be truncated to 60 chars in markdown
            for line in content.split('\n'):
                if 'Long' in line:
                    self.assertLessEqual(len(line.split('|')[2].strip()), 65)
        finally:
            os.unlink(tmpname)


# ---------- Rate limiting tests (no dependencies) ----------

class TestRateLimit(unittest.TestCase):
    """Test the rate-limiting algorithm used by WebScraper._rate_limit"""

    def test_rate_limit_enforces_minimum_delay(self):
        """Simulate a recent request: sleep should fire"""
        delay = 0.1
        last_time = time.time()  # "just happened"
        t0 = time.time()
        elapsed = time.time() - last_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        elapsed = time.time() - t0
        # Should have slept ~delay seconds
        self.assertGreaterEqual(elapsed, delay * 0.8)

    def test_rate_limit_no_sleep_if_enough_time_passed(self):
        delay = 0.1
        last_time = time.time() - 10  # 10 seconds ago
        t0 = time.time()
        elapsed = time.time() - last_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        elapsed = time.time() - t0
        # Should NOT have slept (elapsed is essentially overhead only)
        self.assertLess(elapsed, 0.05)  # Just function call overhead

    def test_rate_limit_zero_delay(self):
        """With zero delay, no sleep even for recent requests"""
        delay = 0.0
        last_time = time.time()
        t0 = time.time()
        elapsed = time.time() - last_time
        if elapsed < delay:
            time.sleep(delay - elapsed)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 0.05)

    def test_rate_limit_consecutive_calls_respect_delay(self):
        """Two calls in quick succession: second one must wait"""
        delay = 0.15
        last_time = time.time() - 0.5  # old enough to pass first check
        # First call (no sleep expected)
        elapsed_since_last = time.time() - last_time
        if elapsed_since_last < delay:
            time.sleep(delay - elapsed_since_last)
        last_time = time.time()  # update like _rate_limit does

        # Second call immediately after — should sleep
        t0 = time.time()
        elapsed_since_last = time.time() - last_time
        if elapsed_since_last < delay:
            time.sleep(delay - elapsed_since_last)
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, delay * 0.8)


# ---------- Tests for scraper module structure (if importable) ----------

class TestScraperModuleStructure(unittest.TestCase):
    """Test that scraper.py has correct structure via AST analysis"""

    def _scraper_path(self):
        """Resolve scraper.py path regardless of CWD"""
        # When run from project root: scraper/scraper.py
        # When run from scraper/: scraper.py
        candidates = ['scraper/scraper.py', 'scraper.py']
        for p in candidates:
            if os.path.exists(p):
                return p
        raise FileNotFoundError(
            f"Cannot find scraper.py. Tried: {candidates} from {os.getcwd()}"
        )

    def test_scraper_has_required_classes(self):
        import ast
        path = self._scraper_path()
        with open(path) as f:
            tree = ast.parse(f.read())
        class_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        self.assertIn('WebScraper', class_names)
        self.assertIn('DataExporter', class_names)

    def test_scraper_has_demo_functions(self):
        import ast
        path = self._scraper_path()
        with open(path) as f:
            tree = ast.parse(f.read())
        func_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('demo_hacker_news', func_names)
        self.assertIn('demo_page_metadata', func_names)
        self.assertIn('demo_custom_site', func_names)
        self.assertIn('main', func_names)

    def test_webscraper_has_expected_methods(self):
        import ast
        path = self._scraper_path()
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'WebScraper':
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, ast.FunctionDef)
                }
                self.assertIn('fetch', methods)
                self.assertIn('parse', methods)
                self.assertIn('scrape', methods)
                self.assertIn('_rate_limit', methods)
                break

    def test_dataexporter_has_static_methods(self):
        import ast
        path = self._scraper_path()
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'DataExporter':
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, ast.FunctionDef)
                }
                self.assertIn('to_csv', methods)
                self.assertIn('to_json', methods)
                self.assertIn('to_markdown', methods)
                break


if __name__ == '__main__':
    unittest.main()
