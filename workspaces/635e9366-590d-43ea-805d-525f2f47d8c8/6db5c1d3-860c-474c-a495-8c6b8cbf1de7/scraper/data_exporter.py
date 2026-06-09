#!/usr/bin/env python3
"""
DataExporter — Export scraped data to CSV, JSON, Markdown, or SQLite.

Pure stdlib, no external dependencies. Can be imported independently
of WebScraper (which requires requests + BeautifulSoup).
"""

import csv
import json
import sqlite3


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
