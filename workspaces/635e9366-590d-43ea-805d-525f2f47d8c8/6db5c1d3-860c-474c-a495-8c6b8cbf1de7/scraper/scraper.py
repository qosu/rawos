#!/usr/bin/env python3
"""
Web Scraper - Extract data from websites using requests + BeautifulSoup
"""

import requests
from bs4 import BeautifulSoup
import csv
import json
import re
import sqlite3
import time
import sys
from urllib.parse import urljoin, urlparse


class WebScraper:
    """A simple but powerful web scraper"""

    def __init__(self, user_agent=None, delay=0.5, timeout=10):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': user_agent or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        self.delay = delay
        self.timeout = timeout
        self.last_request_time = 0

    def _rate_limit(self):
        """Respectful delay between requests"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def fetch(self, url):
        """Fetch HTML content from a URL"""
        self._rate_limit()
        print(f"  🌐 Fetching: {url}")
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or 'utf-8'
            self.last_request_time = time.time()
            return resp.text
        except requests.exceptions.RequestException as e:
            print(f"  ❌ Error: {e}")
            return None

    def fetch_json(self, url):
        """Fetch JSON data from an API endpoint"""
        self._rate_limit()
        print(f"  🌐 Fetching JSON: {url}")
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            self.last_request_time = time.time()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  ❌ Error: {e}")
            return None

    def parse(self, html, selectors):
        """
        Parse HTML with CSS selectors and extract data.
        
        Args:
            html: Raw HTML string
            selectors: List of dicts with keys:
                - name: field name
                - selector: CSS selector string
                - type: 'text' (default), 'attr', 'html', 'link', or 'count'
                - attr: attribute name (if type='attr')
                - multiple: True to always return list
        
        Returns:
            dict with extracted data
        """
        soup = BeautifulSoup(html, 'html.parser')
        results = {}

        for sel in selectors:
            name = sel['name']
            css_sel = sel['selector']
            extract_type = sel.get('type', 'text')
            attr_name = sel.get('attr', None)
            multiple = sel.get('multiple', False)

            elements = soup.select(css_sel)
            values = []

            for el in elements:
                if extract_type == 'text':
                    val = el.get_text(strip=True)
                elif extract_type == 'html':
                    val = str(el)
                elif extract_type == 'attr':
                    val = el.get(attr_name, '')
                elif extract_type == 'link':
                    val = {
                        'text': el.get_text(strip=True),
                        'href': el.get('href', '')
                    }
                elif extract_type == 'count':
                    val = len(elements)
                    values = [val]
                    break
                else:
                    val = el.get_text(strip=True)

                if val and isinstance(val, str):
                    val = val.strip()
                values.append(val)

            # Clean up: remove empties, deduplicate links
            if extract_type == 'link':
                values = [v for v in values if v.get('text') or v.get('href')]
            else:
                values = [v for v in values if v]

            # Return string if single value, unless multiple=True
            if len(values) == 1 and not multiple:
                results[name] = values[0]
            elif len(values) == 0:
                results[name] = None if not multiple else []
            else:
                results[name] = values

            count = len(values) if isinstance(values, list) else 1
            print(f"  ✓ '{name}': {count} item(s) extracted")

        return results

    def scrape(self, url, selectors):
        """Fetch and parse a URL in one step"""
        html = self.fetch(url)
        if not html:
            return None
        return self.parse(html, selectors)

    def scrape_pages(self, urls, selectors):
        """Scrape multiple URLs"""
        results = []
        for url in urls:
            data = self.scrape(url, selectors)
            results.append({'url': url, 'data': data})
        return results


class DataExporter:
    """Export scraped data to various formats"""

    @staticmethod
    def to_csv(data, filename, fieldnames=None):
        """Export list of dicts to CSV"""
        if not data:
            print("No data to export.")
            return

        if fieldnames is None:
            fieldnames = data[0].keys() if isinstance(data[0], dict) else []

        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in data:
                # Flatten nested dicts/arrays for CSV
                flat = {}
                for k, v in row.items():
                    if isinstance(v, (dict, list)):
                        flat[k] = json.dumps(v, ensure_ascii=False)
                    else:
                        flat[k] = v
                writer.writerow(flat)

        print(f"  ✅ Exported to {filename} ({len(data)} rows)")

    @staticmethod
    def to_json(data, filename, pretty=True):
        """Export to JSON"""
        with open(filename, 'w', encoding='utf-8') as f:
            if pretty:
                json.dump(data, f, indent=2, ensure_ascii=False)
            else:
                json.dump(data, f, ensure_ascii=False)
        print(f"  ✅ Exported to {filename}")

    @staticmethod
    def to_markdown(data, filename):
        """Export to simple Markdown table"""
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
        print(f"  ✅ Exported Markdown table to {filename}")

    @staticmethod
    def to_sqlite(data, filename, table_name='scraped_data', if_exists='replace'):
        """
        Export list of dicts to a SQLite database table.

        Args:
            data: List of dicts to export
            filename: Path to SQLite database file
            table_name: Name of the table to create/insert into
            if_exists: 'replace' (default), 'append', 'fail' — same as pandas

        Returns:
            Number of rows inserted, or None on failure
        """
        if not data or not isinstance(data, list):
            print("No data to export.")
            return None

        if not isinstance(data[0], dict):
            print("Data must be a list of dicts.")
            return None

        # Infer column names and types from first row
        fieldnames = list(data[0].keys())
        if not fieldnames:
            print("Data dicts are empty.")
            return None

        conn = sqlite3.connect(filename)
        try:
            cursor = conn.cursor()

            # Handle if_exists logic
            if if_exists == 'replace':
                cursor.execute(f"DROP TABLE IF EXISTS [{table_name}]")
            elif if_exists == 'fail':
                cursor.execute(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                )
                if cursor.fetchone():
                    print(f"  ❌ Table '{table_name}' already exists (if_exists='fail')")
                    return None

            # Build CREATE TABLE with TEXT columns (most flexible for scraped data)
            col_defs = ', '.join(f'[{k}] TEXT' for k in fieldnames)
            cursor.execute(f"CREATE TABLE IF NOT EXISTS [{table_name}] ({col_defs})")

            # Insert rows
            placeholders = ', '.join(['?' for _ in fieldnames])
            insert_sql = f"INSERT INTO [{table_name}] ({', '.join(f'[{k}]' for k in fieldnames)}) VALUES ({placeholders})"

            rows_inserted = 0
            for row in data:
                values = []
                for k in fieldnames:
                    v = row.get(k)
                    if isinstance(v, (dict, list)):
                        values.append(json.dumps(v, ensure_ascii=False))
                    elif v is None:
                        values.append(None)
                    else:
                        values.append(str(v))
                cursor.execute(insert_sql, values)
                rows_inserted += 1

            conn.commit()
            print(f"  ✅ Exported {rows_inserted} rows to SQLite [{table_name}] in {filename}")
            return rows_inserted

        except sqlite3.Error as e:
            print(f"  ❌ SQLite error: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()


def demo_hacker_news():
    """Demo: Scrape Hacker News front page"""
    print("\n" + "=" * 60)
    print("📰 DEMO: Hacker News Headlines")
    print("=" * 60)

    scraper = WebScraper(delay=0.2)
    
    selectors = [
        {'name': 'stories', 'selector': '.athing .title a', 'type': 'link'},
        {'name': 'scores', 'selector': '.subtext .score', 'type': 'text'},
        {'name': 'total_stories', 'selector': '.athing', 'type': 'count'}
    ]

    data = scraper.scrape('https://news.ycombinator.com/', selectors)
    if not data:
        return

    print(f"\n📋 Top {data.get('total_stories', 'N/A')} stories:")
    stories = data.get('stories', [])
    scores = data.get('scores', [])
    if not isinstance(stories, list):
        stories = [stories]
    if not isinstance(scores, list):
        scores = [scores]

    for i, story in enumerate(stories[:10]):
        title = story.get('text', '') if isinstance(story, dict) else str(story)
        url = story.get('href', '') if isinstance(story, dict) else ''
        score = scores[i] if i < len(scores) else 'N/A'
        print(f"  {i+1}. {title[:70]}")
        print(f"     ⭐ {score}  🔗 {url[:50]}")
    
    return data


def demo_page_metadata(url=None):
    """Demo: Extract metadata from any page"""
    if not url:
        url = 'https://example.com'
    
    print(f"\n" + "=" * 60)
    print(f"📄 DEMO: Page Metadata - {url}")
    print("=" * 60)

    scraper = WebScraper(delay=0.2)
    
    selectors = [
        {'name': 'title', 'selector': 'title', 'type': 'text'},
        {'name': 'description', 'selector': 'meta[name="description"]', 'type': 'attr', 'attr': 'content'},
        {'name': 'headings_h1', 'selector': 'h1', 'type': 'text'},
        {'name': 'headings_h2', 'selector': 'h2', 'type': 'text'},
        {'name': 'links', 'selector': 'a[href]', 'type': 'link'},
        {'name': 'images', 'selector': 'img[src]', 'type': 'attr', 'attr': 'src'},
        {'name': 'paragraphs', 'selector': 'p', 'type': 'text'},
    ]

    data = scraper.scrape(url, selectors)
    if not data:
        return

    print(f"\n📌 Title: {data.get('title', 'N/A')}")
    print(f"📝 Description: {data.get('description', 'N/A')}")

    h1 = data.get('headings_h1', [])
    if isinstance(h1, str): h1 = [h1]
    print(f"\n🔤 H1 Headings ({len(h1)}):")
    for h in h1[:5]:
        print(f"   - {h[:70]}")

    links = data.get('links', [])
    if isinstance(links, dict): links = [links]
    print(f"\n🔗 Links ({len(links)} total):")
    for link in links[:8]:
        if isinstance(link, dict):
            print(f"   - '{link.get('text', '')[:40] or '(no text)'}' -> {link.get('href', '')[:50]}")

    images = data.get('images', [])
    if isinstance(images, str): images = [images]
    print(f"\n🖼️  Images ({len(images)} total)")
    for img in images[:5]:
        print(f"   - {str(img)[:60]}")

    return data


def demo_custom_site():
    """Demo: Scrape quotes from quotes.toscrape.com"""
    print("\n" + "=" * 60)
    print("💬 DEMO: Quotes from quotes.toscrape.com")
    print("=" * 60)

    scraper = WebScraper(delay=0.3)
    
    selectors = [
        {'name': 'quotes', 'selector': '.quote .text', 'type': 'text'},
        {'name': 'authors', 'selector': '.quote .author', 'type': 'text'},
        {'name': 'tags', 'selector': '.quote .tags .tag', 'type': 'text'},
    ]

    data = scraper.scrape('https://quotes.toscrape.com/', selectors)
    if not data:
        return

    quotes = data.get('quotes', [])
    authors = data.get('authors', [])
    
    if isinstance(quotes, str): quotes = [quotes]
    if isinstance(authors, str): authors = [authors]

    print(f"\n📖 {len(quotes)} quotes found:\n")
    for i in range(min(len(quotes), 5)):
        q = quotes[i][:80] if isinstance(quotes[i], str) else str(quotes[i])[:80]
        a = authors[i] if i < len(authors) else 'Unknown'
        print(f"  💭 \"{q}\"")
        print(f"     — {a}\n")

    # Export to JSON
    rows = []
    for i in range(len(quotes)):
        rows.append({
            'quote': quotes[i] if isinstance(quotes[i], str) else str(quotes[i]),
            'author': authors[i] if i < len(authors) else 'Unknown'
        })
    
    exporter = DataExporter()
    exporter.to_json(rows, 'quotes.json')
    exporter.to_csv(rows, 'quotes.csv')

    return data


def main():
    """Main entry point"""
    args = sys.argv[1:]
    
    if len(args) >= 2 and args[0] == '--url':
        # Scrape a custom URL
        url = args[1]
        demo_page_metadata(url)
    elif len(args) >= 2 and args[0] == '--json':
        # Fetch JSON API
        url = args[1]
        print(f"\n🔌 Fetching JSON API: {url}")
        scraper = WebScraper()
        data = scraper.fetch_json(url)
        if data:
            print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
    else:
        # Run all demos
        demo_hacker_news()
        print()
        demo_page_metadata()
        print()
        demo_custom_site()

    print("\n" + "=" * 60)
    print("✅ Done! Usage:")
    print("   python3 scraper.py                      # Run demos")
    print("   python3 scraper.py --url https://...    # Scrape any URL")
    print("   python3 scraper.py --json https://api.. # Fetch JSON API")
    print("=" * 60)


if __name__ == '__main__':
    main()
