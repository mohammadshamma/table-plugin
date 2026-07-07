#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
# ]
# ///
"""
Tests for CSV import: type inference (unit), the import-csv CLI (subprocess,
end to end), and the table_import_csv MCP tool wiring.

Run with: python3 -m unittest test_csv_import -v   (MCP test skipped if mcp
is not installed) or: uv run test_csv_import.py    (runs everything)
"""

import asyncio
import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import table_tool

try:
    import server
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

HERE = Path(__file__).resolve().parent
TABLE_TOOL = HERE / "table_tool.py"


def run(*args) -> dict:
    result = subprocess.run(
        [sys.executable, str(TABLE_TOOL), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


class InferenceTest(unittest.TestCase):
    """Unit tests for the deterministic type-inference rules."""

    def infer_one(self, values) -> str:
        return table_tool.infer_column_types(["col"], [[v] for v in values])["col"]

    def test_integers(self):
        self.assertEqual(self.infer_one(["5", "-3", "+7", " 42 ", "0"]), "INTEGER")

    def test_leading_zeros_are_text_not_real(self):
        # "007" matches both the int and real patterns; it must be decided in
        # the INTEGER branch (-> TEXT), never fall through to REAL (7.0).
        self.assertEqual(table_tool._classify_value("007"), "TEXT")
        self.assertEqual(table_tool._classify_value("-01"), "TEXT")
        self.assertEqual(self.infer_one(["007", "12"]), "TEXT")

    def test_int_range_bounds(self):
        self.assertEqual(self.infer_one([str(2**63 - 1)]), "INTEGER")
        self.assertEqual(self.infer_one([str(-(2**63))]), "INTEGER")
        self.assertEqual(self.infer_one([str(2**63)]), "TEXT")
        self.assertEqual(self.infer_one([str(-(2**63) - 1)]), "TEXT")

    def test_reals(self):
        self.assertEqual(self.infer_one(["3.14", "-0.5"]), "REAL")
        self.assertEqual(self.infer_one([".5", "3.", "1e5", "2E-3", "+3.0"]), "REAL")

    def test_mixed_int_and_real_is_real(self):
        self.assertEqual(self.infer_one(["1", "2.5", "3"]), "REAL")

    def test_specials_are_text(self):
        for v in ("NaN", "nan", "inf", "Infinity", "1_000", "1,000", "0x1F",
                  "true", "false", "2024-01-01"):
            self.assertEqual(self.infer_one([v]), "TEXT", v)

    def test_empty_values_ignored(self):
        self.assertEqual(self.infer_one(["", "5", "  "]), "INTEGER")

    def test_all_empty_column_is_text(self):
        self.assertEqual(self.infer_one(["", ""]), "TEXT")

    def test_no_data_rows_is_text(self):
        self.assertEqual(table_tool.infer_column_types(["col"], [])["col"], "TEXT")

    def test_one_text_value_demotes_column(self):
        self.assertEqual(self.infer_one(["1", "2", "x", "4"]), "TEXT")


class ImportCsvTest(unittest.TestCase):
    """End-to-end tests of the import-csv CLI subcommand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def write_csv(self, content: str, name: str = "data.csv", encoding: str = "utf-8") -> str:
        path = self.dir / name
        path.write_text(content, encoding=encoding, newline="")
        return str(path)

    def import_csv(self, table: str, path: str, **spec) -> dict:
        return run("import-csv", self.db, table, json.dumps({"file_path": path, **spec}))

    def query(self, sql: str) -> dict:
        return run("query", self.db, sql)

    def test_happy_path_types_and_roundtrip(self):
        path = self.write_csv("name,age,score\nAlice,30,9.5\nBob,25,8\n")
        result = self.import_csv("people", path)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(
            result["columns"], {"name": "TEXT", "age": "INTEGER", "score": "REAL"}
        )
        rows = self.query("SELECT SUM(age) AS total, SUM(score) AS s FROM people")["rows"]
        self.assertEqual(rows[0]["total"], 55)
        self.assertAlmostEqual(rows[0]["s"], 17.5)

    def test_empty_fields_become_null(self):
        path = self.write_csv("name,age\nAlice,\n,30\n")
        result = self.import_csv("t", path)
        self.assertEqual(result["ok"], True)
        rows = self.query(
            "SELECT COUNT(*) AS n FROM t WHERE age IS NULL OR name IS NULL"
        )["rows"]
        self.assertEqual(rows[0]["n"], 2)

    def test_quoted_fields_survive(self):
        path = self.write_csv(
            'name,notes\n"Smith, John","said ""hi""\nand left"\n'
        )
        result = self.import_csv("t", path)
        self.assertEqual(result["inserted"], 1)
        rows = self.query("SELECT name, notes FROM t")["rows"]
        self.assertEqual(rows[0]["name"], "Smith, John")
        self.assertEqual(rows[0]["notes"], 'said "hi"\nand left')

    def test_unicode_and_bom(self):
        path = self.write_csv("naïve,数量\nmüller,42\n", encoding="utf-8-sig")
        result = self.import_csv("t", path)
        self.assertEqual(result["columns"], {"naïve": "TEXT", "数量": "INTEGER"})
        rows = self.query('SELECT "naïve" AS n FROM t')["rows"]
        self.assertEqual(rows[0]["n"], "müller")

    def test_existing_table_errors_and_is_untouched(self):
        path = self.write_csv("a\n1\n2\n")
        self.assertEqual(self.import_csv("t", path)["ok"], True)
        result = self.import_csv("t", path)
        self.assertIn("already exists", result["error"])
        rows = self.query("SELECT COUNT(*) AS n FROM t")["rows"]
        self.assertEqual(rows[0]["n"], 2)

    def test_missing_file(self):
        result = self.import_csv("t", str(self.dir / "nope.csv"))
        self.assertIn("not found", result["error"])

    def test_empty_file(self):
        path = self.write_csv("")
        result = self.import_csv("t", path)
        self.assertIn("empty", result["error"])

    def test_header_only_creates_empty_table(self):
        path = self.write_csv("a,b\n")
        result = self.import_csv("t", path)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["columns"], {"a": "TEXT", "b": "TEXT"})

    def test_bad_headers(self):
        cases = [
            ("id,name,id\n1,x,2\n", "Duplicate"),
            ("ID,id\n1,2\n", "Duplicate"),  # SQLite columns are case-insensitive
            ("a,,c\n1,2,3\n", "empty column name"),
            ('a"b,c\n1,2\n', "double quote"),
        ]
        for i, (content, expected) in enumerate(cases):
            path = self.write_csv(content, name=f"h{i}.csv")
            result = self.import_csv(f"t{i}", path)
            self.assertIn(expected, result["error"], content)

    def test_ragged_row_errors_atomically(self):
        path = self.write_csv("a,b\n1,2\n3\n")
        result = self.import_csv("t", path)
        self.assertIn("expected 2", result["error"])
        tables = run("tables", self.db)
        self.assertNotIn("t", tables.get("tables", []))

    def test_custom_delimiters(self):
        path = self.write_csv("a;b\n1;x\n")
        result = self.import_csv("t1", path, delimiter=";")
        self.assertEqual(result["columns"], {"a": "INTEGER", "b": "TEXT"})
        path = self.write_csv("a\tb\n1\tx\n", name="tab.csv")
        result = self.import_csv("t2", path, delimiter="\t")
        self.assertEqual(result["inserted"], 1)

    def test_bad_delimiter(self):
        path = self.write_csv("a\n1\n")
        result = self.import_csv("t", path, delimiter=";;")
        self.assertIn("single character", result["error"])

    def test_bad_table_name(self):
        path = self.write_csv("a\n1\n")
        result = self.import_csv('x"y', path)
        self.assertIn("double quote", result["error"])

    def test_larger_file_sanity(self):
        path = self.dir / "big.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "value"])
            for i in range(5000):
                writer.writerow([i, f"row {i}"])
        result = self.import_csv("big", str(path))
        self.assertEqual(result["inserted"], 5000)
        rows = self.query("SELECT COUNT(*) AS n, SUM(id) AS s FROM big")["rows"]
        self.assertEqual(rows[0]["n"], 5000)
        self.assertEqual(rows[0]["s"], sum(range(5000)))


@unittest.skipUnless(HAS_MCP, "mcp package not installed (run via: uv run test_csv_import.py)")
class McpWiringTest(unittest.TestCase):
    """table_import_csv dispatches through server.call_tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "session.db")
        self.db_patcher = patch("server.get_resolved_db_path", return_value=self.db)
        self.db_patcher.start()

    def tearDown(self):
        self.db_patcher.stop()
        self.tmp.cleanup()

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        res = loop.run_until_complete(server.call_tool(name, arguments))
        return json.loads(res[0].text)

    def test_import_and_already_exists(self):
        path = self.dir / "data.csv"
        path.write_text("name,age\nAlice,30\n", encoding="utf-8", newline="")
        result = self.call_tool_sync(
            "table_import_csv", {"table": "people", "file_path": str(path)}
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["columns"], {"name": "TEXT", "age": "INTEGER"})
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT age FROM people").fetchone()[0], 30
            )
        finally:
            conn.close()
        result = self.call_tool_sync(
            "table_import_csv", {"table": "people", "file_path": str(path)}
        )
        self.assertIn("already exists", result["error"])

    def test_tool_is_advertised(self):
        names = [t.name for t in server.TOOLS]
        self.assertIn("table_import_csv", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
