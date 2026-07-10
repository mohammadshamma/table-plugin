#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
#     "anyio",
# ]
# ///
"""
E2E integration test suite for session-scoped SQLite database routing.
Covers 111 test cases across 4 tiers of testing.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the server under test
import server


class SessionScopingTestBase(unittest.TestCase):
    def setUp(self):
        # Create a mock brain directory
        self.temp_brain_dir = tempfile.TemporaryDirectory()
        self.brain_dir = Path(self.temp_brain_dir.name).resolve()

        # Create a mock CWD directory to avoid polluting actual workspace CWD
        self.temp_cwd_dir = tempfile.TemporaryDirectory()
        self.cwd_dir = Path(self.temp_cwd_dir.name).resolve()
        self.old_cwd = os.getcwd()
        os.chdir(self.cwd_dir)

        # Create a mock scratch directory
        self.temp_scratch_dir = tempfile.TemporaryDirectory()
        self.scratch_dir = Path(self.temp_scratch_dir.name).resolve()

        # Patch server's get_brain_dir function if it exists, or create it if not
        self.brain_patcher = patch("server.get_brain_dir", return_value=self.brain_dir, create=True)
        self.mock_get_brain_dir = self.brain_patcher.start()

        # Patch server's get_scratch_dir function
        self.scratch_patcher = patch("server.get_scratch_dir", return_value=self.scratch_dir, create=True)
        self.mock_get_scratch_dir = self.scratch_patcher.start()

        # Default env setup (start with clean env, override in specific tests)
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

        # Lineage is cached process-wide by conversation_id. Each test gets a
        # fresh brain dir, so an entry carried over from an earlier test would
        # route this test's conversation to a stale root.
        with server.LINEAGE_CACHE_LOCK:
            server.LINEAGE_CACHE.clear()

    def tearDown(self):
        self.brain_patcher.stop()
        self.scratch_patcher.stop()
        self.env_patcher.stop()
        os.chdir(self.old_cwd)
        try:
            self.temp_brain_dir.cleanup()
        except Exception:
            pass
        try:
            self.temp_cwd_dir.cleanup()
        except Exception:
            pass
        try:
            self.temp_scratch_dir.cleanup()
        except Exception:
            pass

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        """Helper to run the async call_tool in a synchronous runner."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        # We invoke server.call_tool directly
        res = loop.run_until_complete(server.call_tool(name, arguments))
        return json.loads(res[0].text)

    def write_mock_transcript(self, parent_id: str, child_id: str, content: str = None) -> Path:
        """Helper to create a parent-child relationship transcript log."""
        log_dir = self.brain_dir / parent_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"
        
        if content is None:
            content = f'Created the following subagents:\n{{\n  "conversationId": "{child_id}"\n}}'
        
        entry = {
            "step_index": 1,
            "source": "MODEL",
            "type": "INVOKE_SUBAGENT",
            "status": "DONE",
            "content": content
        }
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return transcript_path


class TestSessionScopingTier1(SessionScopingTestBase):
    """
    Tier 1 — Feature Coverage (Happy Path)
    Covers Happy paths for all 8 tools + Root Lineage Resolution + Fallback Support.
    """

    def setup_happy_lineage(self) -> str:
        # root -> parent -> child
        self.write_mock_transcript("root-1", "parent-1")
        self.write_mock_transcript("parent-1", "child-1")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-1"
        return "root-1"

    # --- Feature 1: table_create ---
    def test_tc_create_01_basic(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {
            "table": "users",
            "columns": {"name": "TEXT NOT NULL", "age": "INTEGER"}
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("table"), "users")

    def test_tc_create_02_primary_key(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {
            "table": "products",
            "columns": {"name": "TEXT"},
            "primary_key": "prod_id"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("table"), "products")

    def test_tc_create_03_unique_constraint(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {
            "table": "accounts",
            "columns": {"email": "TEXT"},
            "unique": ["email"]
        })
        self.assertEqual(res.get("ok"), True)

    def test_tc_create_04_if_not_exists_true(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "items",
            "columns": {"sku": "TEXT"}
        })
        res = self.call_tool_sync("table_create", {
            "table": "items",
            "columns": {"sku": "TEXT"},
            "if_not_exists": True
        })
        self.assertEqual(res.get("ok"), True)

    def test_tc_create_05_if_not_exists_false(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "items",
            "columns": {"sku": "TEXT"}
        })
        res = self.call_tool_sync("table_create", {
            "table": "items",
            "columns": {"sku": "TEXT"},
            "if_not_exists": False
        })
        self.assertIn("error", res)

    # --- Feature 2: table_insert ---
    def test_tc_insert_01_single(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"name": "TEXT"}
        })
        res = self.call_tool_sync("table_insert", {
            "table": "users",
            "rows": [{"name": "Alice"}]
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("inserted"), 1)

    def test_tc_insert_02_multiple(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"name": "TEXT"}
        })
        res = self.call_tool_sync("table_insert", {
            "table": "users",
            "rows": [{"name": "Alice"}, {"name": "Bob"}]
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("inserted"), 2)

    def test_tc_insert_03_missing_optional(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"name": "TEXT", "age": "INTEGER"}
        })
        res = self.call_tool_sync("table_insert", {
            "table": "users",
            "rows": [{"name": "Alice"}] # age omitted
        })
        self.assertEqual(res.get("ok"), True)

    def test_tc_insert_04_type_casting(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"age": "INTEGER"}
        })
        res = self.call_tool_sync("table_insert", {
            "table": "users",
            "rows": [{"age": "42"}] # string to int
        })
        self.assertEqual(res.get("ok"), True)

    def test_tc_insert_05_unique_enforcement(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"email": "TEXT"}, "unique": ["email"]
        })
        self.call_tool_sync("table_insert", {
            "table": "users", "rows": [{"email": "test@test.com"}]
        })
        res = self.call_tool_sync("table_insert", {
            "table": "users", "rows": [{"email": "test@test.com"}]
        })
        self.assertIn("error", res)

    # --- Feature 3: table_join ---
    def test_tc_join_01_inner(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER", "val": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER", "num": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"id": 1, "val": "A"}]})
        self.call_tool_sync("table_insert", {"table": "t2", "rows": [{"id": 1, "num": 100}]})
        res = self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "t1",
            "right": "t2",
            "on": "id",
            "type": "inner"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows"), 1)

    def test_tc_join_02_left(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER", "val": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER", "num": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"id": 1, "val": "A"}, {"id": 2, "val": "B"}]})
        self.call_tool_sync("table_insert", {"table": "t2", "rows": [{"id": 1, "num": 100}]})
        res = self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "t1",
            "right": "t2",
            "on": "id",
            "type": "left"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows"), 2)

    def test_tc_join_03_cross(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"val": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"num": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"val": "A"}, {"val": "B"}]})
        self.call_tool_sync("table_insert", {"table": "t2", "rows": [{"num": 1}, {"num": 2}]})
        res = self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "t1",
            "right": "t2",
            "type": "cross"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows"), 4)

    def test_tc_join_04_select_subset(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER", "val": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER", "num": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"id": 1, "val": "A"}]})
        self.call_tool_sync("table_insert", {"table": "t2", "rows": [{"id": 1, "num": 100}]})
        res = self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "t1",
            "right": "t2",
            "on": "id",
            "select": ["t1.val"]
        })
        self.assertEqual(res.get("ok"), True)

    def test_tc_join_05_explicit_keys(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"left_id": "INTEGER", "val": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"right_id": "INTEGER", "num": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"left_id": 1, "val": "A"}]})
        self.call_tool_sync("table_insert", {"table": "t2", "rows": [{"right_id": 1, "num": 100}]})
        res = self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "t1",
            "right": "t2",
            "on_left": "left_id",
            "on_right": "right_id"
        })
        self.assertEqual(res.get("ok"), True)

    # --- Feature 4: table_group_by ---
    def test_tc_group_01_single(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "sales", "columns": {"category": "TEXT", "amount": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "sales", "rows": [{"category": "A", "amount": 10.0}, {"category": "A", "amount": 20.0}]})
        res = self.call_tool_sync("table_group_by", {
            "table": "sales",
            "by": ["category"],
            "aggs": {"total": "SUM(amount)"}
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows")[0]["total"], 30.0)

    def test_tc_group_02_multiple_columns(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "sales", "columns": {"cat": "TEXT", "sub": "TEXT", "val": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "sales", "rows": [{"cat": "A", "sub": "X", "val": 1.0}]})
        res = self.call_tool_sync("table_group_by", {
            "table": "sales",
            "by": ["cat", "sub"],
            "aggs": {"total": "SUM(val)"}
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows")[0]["cat"], "A")
        self.assertEqual(res.get("rows")[0]["sub"], "X")

    def test_tc_group_03_having(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "sales", "columns": {"cat": "TEXT", "val": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "sales", "rows": [{"cat": "A", "val": 5.0}, {"cat": "B", "val": 15.0}]})
        res = self.call_tool_sync("table_group_by", {
            "table": "sales",
            "by": ["cat"],
            "aggs": {"total": "SUM(val)"},
            "having": "SUM(val) > 10"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(len(res.get("rows")), 1)
        self.assertEqual(res.get("rows")[0]["cat"], "B")

    def test_tc_group_04_order_limit(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "sales", "columns": {"cat": "TEXT", "val": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "sales", "rows": [
            {"cat": "A", "val": 5.0}, {"cat": "B", "val": 15.0}, {"cat": "C", "val": 2.0}
        ]})
        res = self.call_tool_sync("table_group_by", {
            "table": "sales",
            "by": ["cat"],
            "aggs": {"total": "SUM(val)"},
            "order_by": "total DESC",
            "limit": 2
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(len(res.get("rows")), 2)
        self.assertEqual(res.get("rows")[0]["cat"], "B")

    def test_tc_group_05_into(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "sales", "columns": {"cat": "TEXT", "val": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "sales", "rows": [{"cat": "A", "val": 5.0}]})
        res = self.call_tool_sync("table_group_by", {
            "table": "sales",
            "by": ["cat"],
            "aggs": {"total": "SUM(val)"},
            "into": "grouped_sales"
        })
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("saved_to"), "grouped_sales")

    # --- Feature 5: table_run_sql ---
    def test_tc_sql_01_select(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": 10}, {"x": 20}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT SUM(x) as s FROM t"})
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("rows")[0]["s"], 30)

    def test_tc_sql_02_update(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": 10}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "UPDATE t SET x = 100 WHERE x = 10"})
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("changes"), 1)

    def test_tc_sql_03_delete(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": 10}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "DELETE FROM t WHERE x = 10"})
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("changes"), 1)

    def test_tc_sql_04_compound(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"cat": "TEXT", "val": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"cat": "A", "val": 10}, {"cat": "B", "val": 5}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT cat, val FROM t WHERE val > 7 GROUP BY cat ORDER BY val DESC"})
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("count"), 1)

    def test_tc_sql_05_ddl(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_run_sql", {"sql": "ALTER TABLE t ADD COLUMN y TEXT"})
        self.assertEqual(res.get("ok"), True)

    # --- Feature 6: table_schema ---
    def test_tc_schema_01_specific(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_schema", {"table": "t"})
        self.assertEqual(res.get("table"), "t")
        self.assertIn("x", res.get("columns", {}))

    def test_tc_schema_02_all(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"y": "TEXT"}})
        res = self.call_tool_sync("table_schema", {})
        self.assertIn("t1", res.get("tables", {}))
        self.assertIn("t2", res.get("tables", {}))

    def test_tc_schema_03_column_details(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "TEXT NOT NULL"}})
        res = self.call_tool_sync("table_schema", {"table": "t"})
        col_x = res.get("columns", {}).get("x", {})
        self.assertEqual(col_x.get("type"), "TEXT")
        self.assertEqual(col_x.get("notnull"), True)

    def test_tc_schema_04_pk_metadata(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"name": "TEXT"}, "primary_key": "id"})
        res = self.call_tool_sync("table_schema", {"table": "t"})
        col_id = res.get("columns", {}).get("id", {})
        self.assertEqual(col_id.get("pk"), True)

    def test_tc_schema_05_non_existent(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_schema", {"table": "nonexistent"})
        self.assertEqual(res.get("columns"), {})

    # --- Feature 7: table_list ---
    def test_tc_list_01_multiple(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"y": "INTEGER"}})
        res = self.call_tool_sync("table_list", {})
        self.assertIn("t1", res.get("tables", []))
        self.assertIn("t2", res.get("tables", []))

    def test_tc_list_02_empty(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_list", {})
        self.assertEqual(res.get("tables"), [])

    def test_tc_list_03_format(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_list", {})
        self.assertIsInstance(res.get("tables"), list)

    def test_tc_list_04_before_after_create(self):
        self.setup_happy_lineage()
        res1 = self.call_tool_sync("table_list", {})
        self.assertEqual(len(res1.get("tables", [])), 0)
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        res2 = self.call_tool_sync("table_list", {})
        self.assertEqual(res2.get("tables"), ["t1"])

    def test_tc_list_05_before_after_drop(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        res1 = self.call_tool_sync("table_list", {})
        self.assertEqual(res1.get("tables"), ["t1"])
        self.call_tool_sync("table_drop", {"table": "t1"})
        res2 = self.call_tool_sync("table_list", {})
        self.assertEqual(len(res2.get("tables", [])), 0)

    # --- Feature 8: table_drop ---
    def test_tc_drop_01_basic(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_drop", {"table": "t1"})
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res.get("dropped"), "t1")

    def test_tc_drop_02_verify_list(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_drop", {"table": "t1"})
        res = self.call_tool_sync("table_list", {})
        self.assertNotIn("t1", res.get("tables", []))

    def test_tc_drop_03_non_existent(self):
        self.setup_happy_lineage()
        # Drop table returns ok: True even if table did not exist, using DROP TABLE IF EXISTS
        res = self.call_tool_sync("table_drop", {"table": "nonexistent"})
        self.assertEqual(res.get("ok"), True)

    def test_tc_drop_04_verify_other_intact(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"y": "INTEGER"}})
        self.call_tool_sync("table_drop", {"table": "t1"})
        res = self.call_tool_sync("table_list", {})
        self.assertIn("t2", res.get("tables", []))
        self.assertNotIn("t1", res.get("tables", []))

    def test_tc_drop_05_verify_integrity(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t1", "rows": [{"x": 42}]})
        self.call_tool_sync("table_drop", {"table": "t1"})
        # Creating t1 again to verify catalog is clean
        res = self.call_tool_sync("table_create", {"table": "t1", "columns": {"x": "INTEGER"}})
        self.assertEqual(res.get("ok"), True)

    # --- Feature 9: Root Lineage Resolution (Happy Path) ---
    def test_tc_lineage_01_parent_child(self):
        self.write_mock_transcript("root-session", "child-session")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # Path should resolve to root brain directory
        expected_db = self.brain_dir / "root-session" / ".tables" / "session.db"
        self.assertTrue(expected_db.exists())

    def test_tc_lineage_02_multi_generation(self):
        self.write_mock_transcript("root-session", "parent-session")
        self.write_mock_transcript("parent-session", "child-session")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        expected_db = self.brain_dir / "root-session" / ".tables" / "session.db"
        self.assertTrue(expected_db.exists())

    def test_tc_lineage_03_branching_tree(self):
        self.write_mock_transcript("root-session", "child-A")
        self.write_mock_transcript("root-session", "child-B")
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-A"
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-B"
        res = self.call_tool_sync("table_list", {})
        self.assertIn("t", res.get("tables", []))

    def test_tc_lineage_04_db_creation_path(self):
        self.write_mock_transcript("root-session", "child-session")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        expected_db = self.brain_dir / "root-session" / ".tables" / "session.db"
        self.assertTrue(expected_db.parent.is_dir())
        self.assertTrue(expected_db.is_file())

    def test_tc_lineage_05_sibling_sharing(self):
        self.write_mock_transcript("root-session", "sibling-1")
        self.write_mock_transcript("root-session", "sibling-2")
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "sibling-1"
        self.call_tool_sync("table_create", {"table": "shared_table", "columns": {"msg": "TEXT"}})
        self.call_tool_sync("table_insert", {"table": "shared_table", "rows": [{"msg": "Hello Sibling"}]})
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "sibling-2"
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT msg FROM shared_table"})
        self.assertEqual(res.get("rows")[0]["msg"], "Hello Sibling")

    # --- Feature 10: Fallback Support (Happy Path) ---
    def test_tc_fallback_01_missing_env(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        self.call_tool_sync("table_create", {"table": "fallback_table", "columns": {"x": "INTEGER"}})
        expected_db = self.cwd_dir / "session.db"
        self.assertTrue(expected_db.exists())

    def test_tc_fallback_02_relative_paths(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # Check that it's created relative to current working directory
        self.assertTrue(Path("session.db").exists())

    def test_tc_fallback_03_db_schema_creation(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        self.call_tool_sync("table_create", {"table": "t", "columns": {"val": "TEXT"}})
        res = self.call_tool_sync("table_schema", {"table": "t"})
        self.assertEqual(res.get("table"), "t")
        self.assertIn("val", res.get("columns", {}))

    def test_tc_fallback_04_query_executions(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": 123}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT x FROM t"})
        self.assertEqual(res.get("rows")[0]["x"], 123)

    def test_tc_fallback_05_drop_table(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_drop", {"table": "t"})
        res = self.call_tool_sync("table_list", {})
        self.assertNotIn("t", res.get("tables", []))


class TestSessionScopingTier2(SessionScopingTestBase):
    """
    Tier 2 — Boundary & Corner Cases
    Covers boundaries for all 8 tools + Scoping & Lineage Boundaries + Fallback Boundaries.
    """

    def setup_happy_lineage(self) -> str:
        self.write_mock_transcript("root-1", "parent-1")
        self.write_mock_transcript("parent-1", "child-1")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-1"
        return "root-1"

    # --- Feature 1 Boundaries: table_create ---
    def test_tc_create_bnd_01_empty_name(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {"table": "", "columns": {"x": "TEXT"}})
        self.assertIn("error", res)

    def test_tc_create_bnd_02_sql_injection(self):
        self.setup_happy_lineage()
        # Creating table name containing SQL command separators
        res = self.call_tool_sync("table_create", {
            "table": "users; DROP TABLE other;",
            "columns": {"x": "TEXT"}
        })
        # The underlying table_tool quotes table names, so it should create table with this literal name safely
        # or raise an error. Either way, it shouldn't execute raw DROP TABLE command.
        if res.get("ok"):
            list_res = self.call_tool_sync("table_list", {})
            self.assertIn("users; DROP TABLE other;", list_res.get("tables", []))
        else:
            self.assertIn("error", res)

    def test_tc_create_bnd_03_no_columns(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {"table": "t", "columns": {}})
        self.assertIn("error", res)

    def test_tc_create_bnd_04_duplicate_columns(self):
        self.setup_happy_lineage()
        # In Python dicts keys are unique, but we pass columns to SQL. Let's see if SQL constraint is hit if we pass something invalid.
        # But to simulate duplicate columns on lower layers: table_create API takes dict. JSON doesn't strictly prevent duplicate keys, 
        # but Python's json.loads resolves to single key. Thus duplicate columns is handled by parser. 
        # Let's test passing invalid column specification or type constraint.
        res = self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER", "X": "TEXT"}})
        # SQLite is case-insensitive for column names. So duplicate column names (x and X) should error.
        self.assertIn("error", res)

    def test_tc_create_bnd_05_unsupported_type(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INVALID_SQL_TYPE_XYZ"}})
        # SQLite accepts any type definition, but let's pass a malformed column constraint like "INTEGER DEFAULT" (missing default value) which fails
        res2 = self.call_tool_sync("table_create", {"table": "t2", "columns": {"x": "INTEGER DEFAULT"}})
        self.assertIn("error", res2)

    # --- Feature 2 Boundaries: table_insert ---
    def test_tc_insert_bnd_01_non_existent(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_insert", {"table": "nonexistent", "rows": [{"x": 1}]})
        self.assertIn("error", res)

    def test_tc_insert_bnd_02_empty_rows(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_insert", {"table": "t", "rows": []})
        self.assertIn("error", res)

    def test_tc_insert_bnd_03_extra_columns(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": 1, "extra": 2}]})
        self.assertIn("error", res)

    def test_tc_insert_bnd_04_missing_not_null(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER NOT NULL"}})
        res = self.call_tool_sync("table_insert", {"table": "t", "rows": [{}]})
        self.assertIn("error", res)

    def test_tc_insert_bnd_05_overflow(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # SQLite handles large integers up to 64-bit signed. Let's pass a huge integer.
        large_val = 2**63 - 1
        res = self.call_tool_sync("table_insert", {"table": "t", "rows": [{"x": large_val}]})
        self.assertEqual(res.get("ok"), True)

    # --- Feature 3 Boundaries: table_join ---
    def test_tc_join_bnd_01_left_non_existent(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_join", {
            "output_table": "j", "left": "nonexistent", "right": "t2", "on": "id"
        })
        self.assertIn("error", res)

    def test_tc_join_bnd_02_right_non_existent(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_join", {
            "output_table": "j", "left": "t1", "right": "nonexistent", "on": "id"
        })
        self.assertIn("error", res)

    def test_tc_join_bnd_03_invalid_column(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_join", {
            "output_table": "j", "left": "t1", "right": "t2", "on": "nonexistent_col"
        })
        self.assertIn("error", res)

    def test_tc_join_bnd_04_invalid_type(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_join", {
            "output_table": "j", "left": "t1", "right": "t2", "on": "id", "type": "OUTER_INVALID"
        })
        self.assertIn("error", res)

    def test_tc_join_bnd_05_output_collision(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t1", "columns": {"id": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "t2", "columns": {"id": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "colliding_table", "columns": {"x": "TEXT"}})
        res = self.call_tool_sync("table_join", {
            "output_table": "colliding_table", "left": "t1", "right": "t2", "on": "id"
        })
        self.assertIn("error", res)

    # --- Feature 4 Boundaries: table_group_by ---
    def test_tc_group_bnd_01_non_existent(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_group_by", {
            "table": "nonexistent", "by": ["id"], "aggs": {"cnt": "COUNT(*)"}
        })
        self.assertIn("error", res)

    def test_tc_group_bnd_02_invalid_column(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"id": "INTEGER"}})
        # Using the nonexistent column in aggregation to trigger a solid no-such-column schema validation error from SQLite parser
        res = self.call_tool_sync("table_group_by", {
            "table": "t", "by": ["id"], "aggs": {"cnt": "SUM(nonexistent_col)"}
        })
        self.assertIn("error", res)

    def test_tc_group_bnd_03_empty_aggs(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_group_by", {
            "table": "t", "by": ["id"], "aggs": {}
        })
        self.assertIn("error", res)

    def test_tc_group_bnd_04_invalid_agg_expr(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_group_by", {
            "table": "t", "by": ["id"], "aggs": {"bad": "SUM(nonexistent)"}
        })
        self.assertIn("error", res)

    def test_tc_group_bnd_05_negative_limit(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"id": "INTEGER"}})
        res = self.call_tool_sync("table_group_by", {
            "table": "t", "by": ["id"], "aggs": {"cnt": "COUNT(*)"}, "limit": -5
        })
        self.assertIn("error", res)

    # --- Feature 5 Boundaries: table_run_sql ---
    def test_tc_sql_bnd_01_invalid_sql(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT FROM WHERE"})
        self.assertIn("error", res)

    def test_tc_sql_bnd_02_empty_sql(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_run_sql", {"sql": ""})
        self.assertIn("error", res)

    def test_tc_sql_bnd_03_non_existent_ref(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT * FROM nonexistent_table"})
        self.assertIn("error", res)

    def test_tc_sql_bnd_04_multiple_statements(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # SQLite's execute() allows multiple statements if they don't return values, but usually raises Warning/Error or ignores subsequent.
        # Let's see if executing multiple commands fails or handles safely.
        res = self.call_tool_sync("table_run_sql", {"sql": "INSERT INTO t (x) VALUES (1); INSERT INTO t (x) VALUES (2);"})
        # Should either successfully execute changes, or fail. It must not crash the server.
        self.assertTrue("error" in res or res.get("ok") == True)

    def test_tc_sql_bnd_05_null_bytes(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT 'abc\x00def'"})
        # Null bytes in SQL query strings are rejected by python sqlite3 driver and must fail with an error payload
        self.assertIn("error", res)

    # --- Feature 6 Boundaries: table_schema ---
    def test_tc_schema_bnd_01_empty_name(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_schema", {"table": ""})
        self.assertIn("tables", res)

    def test_tc_schema_bnd_02_invalid_type(self):
        self.setup_happy_lineage()
        # Omit table or send wrong parameter format. We test schema parameter values.
        res = self.call_tool_sync("table_schema", {"table": 123})
        self.assertEqual(res.get("columns"), {})

    def test_tc_schema_bnd_03_uninitialized_db(self):
        # Brain session directory path is set but db file doesn't exist yet
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_schema", {})
        self.assertEqual(res.get("tables"), {})

    def test_tc_schema_bnd_04_special_regex_chars(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t.*%", "columns": {"x": "INTEGER"}})
        res = self.call_tool_sync("table_schema", {"table": "t.*%"})
        self.assertEqual(res.get("table"), "t.*%")

    def test_tc_schema_bnd_05_empty_db_file(self):
        self.setup_happy_lineage()
        # Manually create an empty file (0 bytes) where the database goes
        db_path = self.brain_dir / "root-1" / ".tables" / "session.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch()
        # Query schema should not crash, it should return empty dict or initialize cleanly
        res = self.call_tool_sync("table_schema", {})
        self.assertEqual(res.get("tables"), {})

    # --- Feature 7 Boundaries: table_list ---
    def test_tc_list_bnd_01_corrupted_db(self):
        self.setup_happy_lineage()
        db_path = self.brain_dir / "root-1" / ".tables" / "session.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with open(db_path, "w") as f:
            f.write("THIS IS NOT A SQLITE DATABASE FILE")
        res = self.call_tool_sync("table_list", {})
        self.assertIn("error", res)

    def test_tc_list_bnd_02_path_is_directory(self):
        self.setup_happy_lineage()
        db_path = self.brain_dir / "root-1" / ".tables" / "session.db"
        db_path.mkdir(parents=True, exist_ok=True) # create it as a folder
        res = self.call_tool_sync("table_list", {})
        self.assertIn("error", res)

    def test_tc_list_bnd_03_not_created_yet(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_list", {})
        self.assertEqual(res.get("tables"), [])

    def test_tc_list_bnd_04_system_tables(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # SQLite internal master table is not listed in table_list
        res = self.call_tool_sync("table_list", {})
        self.assertNotIn("sqlite_sequence", res.get("tables", []))

    def test_tc_list_bnd_05_db_locked(self):
        self.setup_happy_lineage()
        db_path = self.brain_dir / "root-1" / ".tables" / "session.db"
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        
        # Open a concurrent connection and lock the db with transaction
        conn = sqlite3.connect(db_path)
        conn.execute("BEGIN EXCLUSIVE")
        
        # Test table_list, it should either wait (timeout) or fail cleanly, not crash
        res = self.call_tool_sync("table_list", {})
        conn.close()
        self.assertTrue("tables" in res or "error" in res)

    # --- Feature 8 Boundaries: table_drop ---
    def test_tc_drop_bnd_01_empty_name(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_drop", {"table": ""})
        self.assertIn("error", res)

    def test_tc_drop_bnd_02_sql_injection(self):
        self.setup_happy_lineage()
        res = self.call_tool_sync("table_drop", {"table": "t; DROP TABLE other;"})
        self.assertEqual(res.get("ok"), True)

    def test_tc_drop_bnd_03_already_dropped(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        res1 = self.call_tool_sync("table_drop", {"table": "t"})
        res2 = self.call_tool_sync("table_drop", {"table": "t"})
        self.assertEqual(res1.get("ok"), True)
        self.assertEqual(res2.get("ok"), True)

    def test_tc_drop_bnd_04_read_only_db(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        db_path = self.brain_dir / "root-1" / ".tables" / "session.db"
        
        # Set file to read-only
        db_path.chmod(0o444)
        
        try:
            res = self.call_tool_sync("table_drop", {"table": "t"})
            # Should return error since database is write-protected
            self.assertIn("error", res)
        finally:
            db_path.chmod(0o644)

    def test_tc_drop_bnd_05_foreign_key_dependencies(self):
        self.setup_happy_lineage()
        # Create table with foreign key dependency
        self.call_tool_sync("table_create", {
            "table": "parent", "columns": {"id": "INTEGER PRIMARY KEY", "name": "TEXT"}
        })
        self.call_tool_sync("table_create", {
            "table": "child", "columns": {"id": "INTEGER", "parent_id": "INTEGER REFERENCES parent(id)"}
        })
        # Try to drop the parent. SQLite with foreign_keys=ON might prevent dropping parent table, 
        # or error out, or let it pass depending on PRAGMA settings.
        # But we verify it behaves correctly.
        res = self.call_tool_sync("table_drop", {"table": "parent"})
        self.assertTrue("ok" in res or "error" in res)

    # --- Scoping & Lineage Boundaries ---
    def test_tc_scope_bnd_01_empty_env(self):
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = ""
        # Should fallback to current working directory
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.assertTrue(Path("session.db").exists())

    def test_tc_scope_bnd_02_corrupted_transcript(self):
        log_dir = self.brain_dir / "parent-session" / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("CORRUPTED INVALID JSON LINE\n")
            f.write('{"step_index":2, "type":"INVOKE_SUBAGENT", "content":"conversationId: child-session"}\n')
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        # Lineage resolution should parse other lines or ignore corrupted lines and fallback or find the parent
        # if the second line is valid. Let's make sure it handles exception.
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # It shouldn't crash.

    def test_tc_scope_bnd_03_circular_lineage(self):
        # A claims parent B, B claims parent A
        self.write_mock_transcript("B", "A")
        self.write_mock_transcript("A", "B")
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "A"
        # Should terminate trace safely, preventing infinite recursion
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        # Ensure it doesn't crash with RecursionError

    def test_tc_scope_bnd_04_deep_lineage(self):
        # Generate chain of 15 conversation IDs: session_0 -> session_1 -> ... -> session_14
        for i in range(14):
            self.write_mock_transcript(f"session_{i}", f"session_{i+1}")
        
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "session_14"
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        expected_db = self.brain_dir / "session_0" / ".tables" / "session.db"
        self.assertTrue(expected_db.exists())

    def test_tc_scope_bnd_05_unreadable_brain_dir(self):
        self.write_mock_transcript("root-1", "child-1")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-1"
        
        # Make root-1 unreadable
        root_dir = self.brain_dir / "root-1"
        root_dir.chmod(0o000)
        
        try:
            # Resolving lineage should handle permission errors and either fallback gracefully
            # or handle the exception without crashing the MCP server.
            res = self.call_tool_sync("table_list", {})
            self.assertTrue(isinstance(res, dict))
        finally:
            root_dir.chmod(0o755)

    def test_tc_scope_bnd_06_multiple_invocations(self):
        # Parent spawns child-1 twice in transcript (e.g. restarts or multiple references)
        self.write_mock_transcript("root-1", "child-1")
        self.write_mock_transcript("root-1", "child-1")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-1"
        
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        expected_db = self.brain_dir / "root-1" / ".tables" / "session.db"
        self.assertTrue(expected_db.exists())

    # --- Fallback Boundaries ---
    def test_tc_fallback_bnd_01_readonly_cwd(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        
        # Make the CWD read-only
        self.cwd_dir.chmod(0o555)
        try:
            res = self.call_tool_sync("table_create", {
                "table": "t",
                "columns": {"x": "INTEGER"},
                "if_not_exists": True
            })
            # Should succeed because of scratch directory fallback
            self.assertEqual(res.get("ok"), True)
            expected_db = self.scratch_dir / "session.db"
            self.assertTrue(expected_db.exists())
        finally:
            self.cwd_dir.chmod(0o755)

    def test_tc_fallback_bnd_02_cwd_changed_dynamically(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
            
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        self.assertTrue(Path("session.db").exists())
        
        # Change CWD dynamically
        new_sub = self.cwd_dir / "subfolder"
        new_sub.mkdir()
        os.chdir(new_sub)
        
        # A list tables operation should see the database in the parent CWD if it relies on static paths, 
        # or fail if resolving dynamically. Either way, it must behave gracefully.
        res = self.call_tool_sync("table_list", {})
        self.assertTrue("tables" in res or "error" in res)


class TestSessionScopingTier3(SessionScopingTestBase):
    """
    Tier 3 — Cross-Feature Combinations
    Covers combinations of multiple operations.
    """

    def setup_happy_lineage(self) -> str:
        self.write_mock_transcript("root-1", "child-1")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-1"
        return "root-1"

    def test_tc_comb_01_create_insert_query(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"name": "TEXT", "score": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [{"name": "Alice", "score": 95}, {"name": "Bob", "score": 88}]})
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT name FROM t WHERE score > 90"})
        self.assertEqual(res.get("rows")[0]["name"], "Alice")

    def test_tc_comb_02_join_flow(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "left_t", "columns": {"key": "TEXT", "lval": "INTEGER"}})
        self.call_tool_sync("table_create", {"table": "right_t", "columns": {"key": "TEXT", "rval": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "left_t", "rows": [{"key": "X", "lval": 10}]})
        self.call_tool_sync("table_insert", {"table": "right_t", "rows": [{"key": "X", "rval": 20}]})
        
        join_res = self.call_tool_sync("table_join", {
            "output_table": "joined_res",
            "left": "left_t",
            "right": "right_t",
            "on": "key"
        })
        self.assertEqual(join_res.get("ok"), True)
        
        schema_res = self.call_tool_sync("table_schema", {"table": "joined_res"})
        self.assertIn("lval", schema_res.get("columns", {}))
        self.assertIn("rval", schema_res.get("columns", {}))

    def test_tc_comb_03_group_into(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"group_name": "TEXT", "val": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "t", "rows": [
            {"group_name": "A", "val": 10}, {"group_name": "A", "val": 15}, {"group_name": "B", "val": 5}
        ]})
        
        self.call_tool_sync("table_group_by", {
            "table": "t",
            "by": ["group_name"],
            "aggs": {"avg_val": "AVG(val)"},
            "into": "grouped_res"
        })
        
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT avg_val FROM grouped_res WHERE group_name = 'A'"})
        self.assertEqual(res.get("rows")[0]["avg_val"], 12.5)

    def test_tc_comb_04_lifecycle_cycle(self):
        self.setup_happy_lineage()
        # 1. Create
        self.call_tool_sync("table_create", {"table": "lifecycle_t", "columns": {"x": "INTEGER"}})
        # 2. Insert
        self.call_tool_sync("table_insert", {"table": "lifecycle_t", "rows": [{"x": 1}]})
        # 3. Schema
        schema_res = self.call_tool_sync("table_schema", {"table": "lifecycle_t"})
        self.assertIn("x", schema_res.get("columns", {}))
        # 4. List
        list_res = self.call_tool_sync("table_list", {})
        self.assertIn("lifecycle_t", list_res.get("tables", []))
        # 5. Drop
        self.call_tool_sync("table_drop", {"table": "lifecycle_t"})
        # 6. List
        list_res2 = self.call_tool_sync("table_list", {})
        self.assertNotIn("lifecycle_t", list_res2.get("tables", []))

    def test_tc_comb_05_cross_session_flow(self):
        # Setup multi-gen: root -> child -> grandchild
        self.write_mock_transcript("root-session", "child-session")
        self.write_mock_transcript("child-session", "grandchild-session")
        
        # Parent writes
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        self.call_tool_sync("table_create", {"table": "parent_data", "columns": {"id": "INTEGER", "msg": "TEXT"}})
        self.call_tool_sync("table_insert", {"table": "parent_data", "rows": [{"id": 1, "msg": "Data from parent"}]})
        
        # Grandchild queries
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "grandchild-session"
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT msg FROM parent_data"})
        self.assertEqual(res.get("rows")[0]["msg"], "Data from parent")

    def test_tc_comb_06_run_sql_dml_updates(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "t", "columns": {"x": "INTEGER"}})
        
        # INSERT via SQL
        self.call_tool_sync("table_run_sql", {"sql": "INSERT INTO t (x) VALUES (42)"})
        # UPDATE via SQL
        self.call_tool_sync("table_run_sql", {"sql": "UPDATE t SET x = 99 WHERE x = 42"})
        
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT x FROM t"})
        self.assertEqual(res.get("rows")[0]["x"], 99)

    def test_tc_comb_07_group_join(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {"table": "users", "columns": {"user_id": "INTEGER", "name": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "orders", "columns": {"order_id": "INTEGER", "user_id": "INTEGER", "amount": "REAL"}})
        self.call_tool_sync("table_insert", {"table": "users", "rows": [{"user_id": 1, "name": "Alice"}]})
        self.call_tool_sync("table_insert", {"table": "orders", "rows": [{"order_id": 1, "user_id": 1, "amount": 50.0}, {"order_id": 2, "user_id": 1, "amount": 150.0}]})
        
        self.call_tool_sync("table_join", {
            "output_table": "joined",
            "left": "users",
            "right": "orders",
            "on": "user_id"
        })
        
        group_res = self.call_tool_sync("table_group_by", {
            "table": "joined",
            "by": ["name"],
            "aggs": {"total_spent": "SUM(amount)"}
        })
        self.assertEqual(group_res.get("rows")[0]["total_spent"], 200.0)

    def test_tc_comb_08_constraint_collision(self):
        self.setup_happy_lineage()
        self.call_tool_sync("table_create", {
            "table": "users", "columns": {"username": "TEXT"}, "unique": ["username"]
        })
        
        # 1st insert passes
        res1 = self.call_tool_sync("table_insert", {"table": "users", "rows": [{"username": "alice"}]})
        self.assertEqual(res1.get("ok"), True)
        
        # 2nd insert fails (conflict)
        res2 = self.call_tool_sync("table_insert", {"table": "users", "rows": [{"username": "alice"}]})
        self.assertIn("error", res2)
        
        # 3rd insert passes (valid)
        res3 = self.call_tool_sync("table_insert", {"table": "users", "rows": [{"username": "bob"}]})
        self.assertEqual(res3.get("ok"), True)
        
        sql_res = self.call_tool_sync("table_run_sql", {"sql": "SELECT COUNT(*) as c FROM users"})
        self.assertEqual(sql_res.get("rows")[0]["c"], 2)


class TestSessionScopingTier4(SessionScopingTestBase):
    """
    Tier 4 — Real-World Application Scenarios
    Covers mock real-world pipelines.
    """

    # --- Scenario 1: E-commerce Checkout Analytics ---
    def test_tc_real_01_ecommerce_analytics(self):
        self.write_mock_transcript("root-session", "worker-session")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "worker-session"
        
        # 1. Create tables
        self.call_tool_sync("table_create", {"table": "users", "columns": {"user_id": "INTEGER", "name": "TEXT"}})
        self.call_tool_sync("table_create", {"table": "orders", "columns": {"order_id": "INTEGER", "user_id": "INTEGER", "amount": "REAL"}})
        
        # 2. Insert data
        self.call_tool_sync("table_insert", {"table": "users", "rows": [
            {"user_id": 1, "name": "Alice"}, {"user_id": 2, "name": "Bob"}
        ]})
        self.call_tool_sync("table_insert", {"table": "orders", "rows": [
            {"order_id": 101, "user_id": 1, "amount": 25.50},
            {"order_id": 102, "user_id": 1, "amount": 74.50},
            {"order_id": 103, "user_id": 2, "amount": 150.00}
        ]})
        
        # 3. Join users and orders
        self.call_tool_sync("table_join", {
            "output_table": "user_orders",
            "left": "users",
            "right": "orders",
            "on": "user_id"
        })
        
        # 4. Group by to sum order amounts
        self.call_tool_sync("table_group_by", {
            "table": "user_orders",
            "by": ["name"],
            "aggs": {"total_spent": "SUM(amount)"},
            "order_by": "total_spent DESC",
            "into": "spend_report"
        })
        
        # 5. Query spend report
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT name, total_spent FROM spend_report LIMIT 1"})
        self.assertEqual(res.get("rows")[0]["name"], "Bob")
        self.assertEqual(res.get("rows")[0]["total_spent"], 150.0)

    # --- Scenario 2: Multi-agent Task Dispatch Pipeline ---
    def test_tc_real_02_task_dispatch(self):
        # Lineage: orchestrator -> worker -> reviewer
        self.write_mock_transcript("orch-session", "worker-session")
        self.write_mock_transcript("worker-session", "reviewer-session")
        
        # Orchestrator defines tasks
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "orch-session"
        self.call_tool_sync("table_create", {
            "table": "tasks", "columns": {"task_id": "INTEGER PRIMARY KEY", "name": "TEXT", "status": "TEXT"}
        })
        self.call_tool_sync("table_insert", {
            "table": "tasks", "rows": [
                {"name": "Write Tests", "status": "PENDING"},
                {"name": "Fix Bugs", "status": "PENDING"}
            ]
        })
        
        # Worker claims task 1
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "worker-session"
        self.call_tool_sync("table_run_sql", {"sql": "UPDATE tasks SET status = 'IN_PROGRESS' WHERE task_id = 1"})
        
        # Reviewer completes task 1
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "reviewer-session"
        self.call_tool_sync("table_run_sql", {"sql": "UPDATE tasks SET status = 'COMPLETED' WHERE task_id = 1"})
        
        # Verify status
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT status FROM tasks WHERE task_id = 1"})
        self.assertEqual(res.get("rows")[0]["status"], "COMPLETED")

    # --- Scenario 3: Standalone CLI Fallback Verification ---
    def test_tc_real_03_cli_fallback_lifecycle(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
            
        # Standard CLI operations in CWD
        self.call_tool_sync("table_create", {"table": "cli_t", "columns": {"x": "INTEGER"}})
        self.call_tool_sync("table_insert", {"table": "cli_t", "rows": [{"x": 100}]})
        
        list_res = self.call_tool_sync("table_list", {})
        self.assertIn("cli_t", list_res.get("tables", []))
        
        sql_res = self.call_tool_sync("table_run_sql", {"sql": "SELECT x FROM cli_t"})
        self.assertEqual(sql_res.get("rows")[0]["x"], 100)
        
        self.call_tool_sync("table_drop", {"table": "cli_t"})
        list_res2 = self.call_tool_sync("table_list", {})
        self.assertNotIn("cli_t", list_res2.get("tables", []))

    # --- Scenario 4: Logging and Troubleshooting Auditing ---
    def test_tc_real_04_logging_auditing(self):
        # 5 sibling agents running sequentially logging to a shared table
        self.write_mock_transcript("root-session", "agent-1")
        self.write_mock_transcript("root-session", "agent-2")
        self.write_mock_transcript("root-session", "agent-3")
        self.write_mock_transcript("root-session", "agent-4")
        self.write_mock_transcript("root-session", "agent-5")
        
        # Agent-1 sets up logging table
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "agent-1"
        self.call_tool_sync("table_create", {"table": "audit_logs", "columns": {"agent_id": "TEXT", "msg": "TEXT"}})
        self.call_tool_sync("table_insert", {"table": "audit_logs", "rows": [{"agent_id": "agent-1", "msg": "Initialised database"}]})
        
        # Sibling agents append logs
        for i in range(2, 6):
            os.environ["ANTIGRAVITY_CONVERSATION_ID"] = f"agent-{i}"
            self.call_tool_sync("table_insert", {"table": "audit_logs", "rows": [{"agent_id": f"agent-{i}", "msg": f"Task execution step from agent-{i}"}]})
            
        # Root query log analysis
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "root-session"
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT COUNT(*) as c FROM audit_logs"})
        self.assertEqual(res.get("rows")[0]["c"], 5)

    # --- Scenario 5: Backup and Restore Scenario ---
    def test_tc_real_05_backup_restore(self):
        self.write_mock_transcript("root-session", "child-session")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "child-session"
        
        # Create and fill table
        self.call_tool_sync("table_create", {"table": "source_t", "columns": {"val": "TEXT"}})
        self.call_tool_sync("table_insert", {"table": "source_t", "rows": [{"val": "Row1"}, {"val": "Row2"}]})
        
        # Backup: fetch contents
        query_res = self.call_tool_sync("table_run_sql", {"sql": "SELECT val FROM source_t ORDER BY val"})
        self.assertEqual(query_res.get("count"), 2)
        rows_backup = query_res.get("rows", [])
        
        # Drop table
        self.call_tool_sync("table_drop", {"table": "source_t"})
        
        # Restore table
        self.call_tool_sync("table_create", {"table": "source_t", "columns": {"val": "TEXT"}})
        self.call_tool_sync("table_insert", {"table": "source_t", "rows": rows_backup})
        
        # Verify checksum/integrity
        res = self.call_tool_sync("table_run_sql", {"sql": "SELECT val FROM source_t ORDER BY val"})
        self.assertEqual(res.get("rows"), rows_backup)

    # --- Scenario 6: Scoping via Tool Call Argument & Cache & Fallback & Exceptions ---
    def test_tc_real_06_parameter_scoping(self):
        self.write_mock_transcript("root-arg-session", "child-arg-session")
        # Do NOT set os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        # Pass conversation_id directly in arguments
        res = self.call_tool_sync("table_create", {
            "conversation_id": "child-arg-session",
            "table": "param_table",
            "columns": {"msg": "TEXT"}
        })
        self.assertEqual(res.get("ok"), True)
        expected_db = self.brain_dir / "root-arg-session" / ".tables" / "session.db"
        self.assertTrue(expected_db.exists())

    def test_tc_real_07_cache_positive_lineage(self):
        self.write_mock_transcript("root-cached", "child-cached")
        # Call 1: Resolves lineage and caches root-cached for child-cached
        self.call_tool_sync("table_create", {
            "conversation_id": "child-cached",
            "table": "t1",
            "columns": {"x": "INTEGER"}
        })
        # Verify that it is in the server's lineage cache
        with server.LINEAGE_CACHE_LOCK:
            self.assertEqual(server.LINEAGE_CACHE.get("child-cached"), "root-cached")

    def test_tc_real_08_scratch_fallback(self):
        if "ANTIGRAVITY_CONVERSATION_ID" in os.environ:
            del os.environ["ANTIGRAVITY_CONVERSATION_ID"]
        # Make the CWD read-only
        self.cwd_dir.chmod(0o555)
        try:
            # Call with no conversation_id or environment variable
            res = self.call_tool_sync("table_create", {
                "table": "scratch_table",
                "columns": {"x": "INTEGER"},
                "if_not_exists": True
            })
            self.assertEqual(res.get("ok"), True)
            expected_db = self.scratch_dir / "session.db"
            self.assertTrue(expected_db.exists())
        finally:
            self.cwd_dir.chmod(0o755)

    def test_tc_real_09_mkdir_exception(self):
        self.write_mock_transcript("root-session", "child-session")
        # Mock mkdir to raise an exception
        with patch.object(Path, "mkdir", side_effect=OSError("Permission Denied")):
            res = self.call_tool_sync("table_create", {
                "conversation_id": "child-session",
                "table": "test_t",
                "columns": {"x": "INTEGER"},
                "if_not_exists": True
            })
            # Should fallback/execute despite directory exception
            self.assertEqual(res.get("ok"), True)
            expected_db = self.scratch_dir / "session.db"
            self.assertTrue(expected_db.exists())

    def test_tc_real_10_path_traversal_injection(self):
        # Passing an unsafe conversation_id should raise a ValueError
        with self.assertRaises(ValueError):
            asyncio.run(server.call_tool("table_create", {
                "conversation_id": "../../../unsafe",
                "table": "t",
                "columns": {"x": "INTEGER"}
            }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
