#!/usr/bin/env python3
"""
Integration tests for table_tool.py.
Exercises the CLI via subprocess, mirroring how the MCP server's operations
are driven end to end. Run with: python3 -m unittest -v
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE_TOOL = HERE / "table_tool.py"
DB = HERE / "test_temp.db"


def run(*args) -> dict:
    result = subprocess.run(
        [sys.executable, str(TABLE_TOOL), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


class TableToolTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{DB}{suffix}").unlink(missing_ok=True)

    def test_01_create_table(self):
        result = run(
            "create-table",
            str(DB),
            "users",
            json.dumps({
                "columns": {"name": "TEXT NOT NULL", "age": "INTEGER"},
                "primary_key": "id",
            }),
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["table"], "users")

    def test_02_insert(self):
        result = run(
            "insert",
            str(DB),
            "users",
            json.dumps({
                "rows": [
                    {"name": "Alice", "age": 30},
                    {"name": "Bob", "age": 25},
                ],
            }),
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["inserted"], 2)

    def test_03_query(self):
        result = run("query", str(DB), "SELECT * FROM users ORDER BY name")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["rows"][0]["name"], "Alice")
        self.assertEqual(result["rows"][1]["name"], "Bob")

    def test_04_schema(self):
        result = run("schema", str(DB), "users")
        self.assertEqual(result["table"], "users")
        self.assertIn("name", result["columns"])
        self.assertEqual(result["columns"]["name"]["type"], "TEXT")
        self.assertEqual(result["columns"]["name"]["notnull"], True)
        self.assertIn("age", result["columns"])
        self.assertEqual(result["columns"]["age"]["type"], "INTEGER")

    def test_05_tables(self):
        result = run("tables", str(DB))
        self.assertIn("users", result["tables"])

    def test_06_group(self):
        result = run(
            "group",
            str(DB),
            "users",
            json.dumps({
                "by": ["age"],
                "aggs": {"count": "COUNT(*)"},
                "order_by": "age",
            }),
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["rows"][0]["age"], 25)
        self.assertEqual(result["rows"][0]["count"], 1)

    def test_07_join(self):
        # Create a second table for joining
        run(
            "create-table",
            str(DB),
            "orders",
            json.dumps({"columns": {"user_name": "TEXT", "amount": "REAL"}}),
        )
        run(
            "insert",
            str(DB),
            "orders",
            json.dumps({
                "rows": [
                    {"user_name": "Alice", "amount": 99.5},
                    {"user_name": "Bob", "amount": 45.0},
                ],
            }),
        )

        result = run(
            "join",
            str(DB),
            "user_orders",
            json.dumps({
                "left": "users",
                "right": "orders",
                "on_left": "name",
                "on_right": "user_name",
                "type": "inner",
                "select": ["users.name", "orders.amount"],
            }),
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["table"], "user_orders")

    def test_08_drop(self):
        result = run("drop", str(DB), "user_orders")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dropped"], "user_orders")

        # Verify it's gone
        tables = run("tables", str(DB))
        self.assertNotIn("user_orders", tables["tables"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
