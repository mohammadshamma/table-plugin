#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
#     "anyio",
# ]
# ///
"""
Tests for the table_job_* work-queue tools (create/claim/submit/status).

Pure DB semantics — no LLMs involved, so no mocking is needed. The
completeness test simulates flaky workers over 200 rows to verify the core
guarantee: every task ends done or failed, none are lost.

Run with: uv run test_table_jobs.py
"""

import random
import sqlite3
import threading
import unittest

import server
from test_session_scoping import SessionScopingTestBase

BOOKKEEPING_COLUMNS = {
    "result",
    "_task_status",
    "_task_error",
    "_task_attempts",
    "_task_lease_expires",
    "_task_claimed_by",
}


class TableJobsTestBase(SessionScopingTestBase):
    def make_source(self, rows=None, columns=None, table="items"):
        columns = columns or {"name": "TEXT", "score": "INTEGER"}
        self.call_tool_sync("table_create", {"table": table, "columns": columns})
        if rows:
            self.call_tool_sync("table_insert", {"table": table, "rows": rows})

    def create_job(self, **overrides):
        args = {
            "table": "items",
            "template": "Summarize {name} with score {score}",
            "job_table": "items_job",
            **overrides,
        }
        return self.call_tool_sync("table_job_create", args)

    def claim(self, **overrides):
        return self.call_tool_sync("table_job_claim", {"job_table": "items_job", **overrides})

    def submit(self, task_id, **overrides):
        args = {"job_table": "items_job", "task_id": task_id, **overrides}
        return self.call_tool_sync("table_job_submit", args)

    def status(self, **overrides):
        return self.call_tool_sync("table_job_status", {"job_table": "items_job", **overrides})

    def make_scoped_source(self, conversation_id, table="items"):
        self.call_tool_sync("table_create", {
            "conversation_id": conversation_id,
            "table": table,
            "columns": {"name": "TEXT", "score": "INTEGER"},
        })
        self.call_tool_sync("table_insert", {
            "conversation_id": conversation_id,
            "table": table,
            "rows": [{"name": "a", "score": 1}],
        })

    def db_conn(self, db_path=None):
        conn = sqlite3.connect(db_path or (self.cwd_dir / "session.db"))
        conn.row_factory = sqlite3.Row
        return conn

    def job_rows(self, table="items_job"):
        conn = self.db_conn()
        try:
            return [dict(r) for r in conn.execute(f'SELECT rowid AS task_id, * FROM "{table}"')]
        finally:
            conn.close()


class TestJobCreate(TableJobsTestBase):
    def test_create_copies_all_rows_as_pending(self):
        self.make_source(rows=[{"name": "alpha", "score": 1}, {"name": "beta", "score": 2}])
        res = self.create_job()
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res["job_table"], "items_job")
        self.assertEqual(res["total_tasks"], 2)

        rows = self.job_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["name"] for r in rows}, {"alpha", "beta"})
        for r in rows:
            self.assertEqual(r["_task_status"], "pending")
            self.assertEqual(r["_task_attempts"], 0)
            self.assertIsNone(r["result"])

    def test_create_job_table_schema(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        schema = self.call_tool_sync("table_schema", {"table": "items_job"})
        cols = set(schema["columns"].keys())
        self.assertEqual(cols, {"name", "score"} | BOOKKEEPING_COLUMNS)

    def test_create_unknown_placeholder(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        res = self.create_job(template="Describe {nonexistent} and {name}")
        self.assertIn("nonexistent", res.get("error", ""))
        list_res = self.call_tool_sync("table_list", {})
        self.assertNotIn("items_job", list_res["tables"])

    def test_create_positional_placeholder_rejected(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        res = self.create_job(template="Describe {}")
        self.assertIn("Positional placeholders", res.get("error", ""))

    def test_create_reserved_source_column_rejected(self):
        self.make_source(
            rows=[{"name": "a", "result": "x"}],
            columns={"name": "TEXT", "result": "TEXT"},
        )
        res = self.create_job(template="Do {name}")
        self.assertIn("result", res.get("error", ""))
        list_res = self.call_tool_sync("table_list", {})
        self.assertNotIn("items_job", list_res["tables"])

    def test_create_existing_job_table_rejected(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.call_tool_sync("table_create", {"table": "items_job", "columns": {"x": "TEXT"}})
        res = self.create_job()
        self.assertIn("already exists", res.get("error", ""))

    def test_create_duplicate_job_rejected(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        res = self.create_job()
        self.assertIn("error", res)

    def test_create_missing_source_table(self):
        res = self.create_job(table="nope", template="Do {name}")
        self.assertIn("does not exist", res.get("error", ""))

    def test_create_empty_source(self):
        self.make_source(rows=None)
        res = self.create_job()
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res["total_tasks"], 0)
        st = self.status()
        self.assertEqual(st["total"], 0)
        self.assertTrue(st["complete"])

    def test_create_is_atomic(self):
        # A job_table name that passes pre-checks but breaks the CREATE TABLE
        # statement must roll back the _table_jobs registration too.
        self.make_source(rows=[{"name": "a", "score": 1}])
        res = self.create_job(job_table='bad"name')
        self.assertIn("error", res)
        conn = self.db_conn()
        try:
            jobs = conn.execute(f'SELECT * FROM "{server.JOBS_TABLE}"').fetchall()
        finally:
            conn.close()
        self.assertEqual(jobs, [])

    def test_create_snapshots_source(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        self.call_tool_sync("table_drop", {"table": "items"})
        res = self.claim()
        self.assertEqual(res["task"]["prompt"], "Summarize a with score 1")

    def test_invalid_lease_and_attempts(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        res = self.create_job(lease_seconds=-1)
        self.assertIn("lease_seconds", res.get("error", ""))
        res = self.create_job(max_attempts=0)
        self.assertIn("max_attempts", res.get("error", ""))


class TestJobClaim(TableJobsTestBase):
    def test_claim_renders_prompt(self):
        self.make_source(rows=[{"name": "alpha", "score": 7}])
        self.create_job()
        res = self.claim()
        self.assertEqual(res["task"]["prompt"], "Summarize alpha with score 7")
        self.assertEqual(res["remaining_pending"], 0)

    def test_sequential_claims_return_distinct_tasks_then_null(self):
        self.make_source(rows=[{"name": f"n{i}", "score": i} for i in range(3)])
        self.create_job()
        ids = set()
        for expected_remaining in (2, 1, 0):
            res = self.claim()
            self.assertIsNotNone(res["task"])
            ids.add(res["task"]["task_id"])
            self.assertEqual(res["remaining_pending"], expected_remaining)
        self.assertEqual(len(ids), 3)
        self.assertIsNone(self.claim()["task"])

    def test_claim_increments_attempts_and_records_worker(self):
        self.write_mock_transcript("root-x", "worker-1")
        self.make_scoped_source("root-x")
        # Route everything through the same root db via lineage.
        self.call_tool_sync("table_job_create", {
            "conversation_id": "root-x",
            "table": "items",
            "template": "Do {name}",
            "job_table": "items_job",
        })
        res = self.call_tool_sync("table_job_claim", {
            "conversation_id": "worker-1",
            "job_table": "items_job",
        })
        self.assertIsNotNone(res["task"])

        db = self.brain_dir / "root-x" / ".tables" / "session.db"
        conn = self.db_conn(db)
        try:
            row = dict(conn.execute('SELECT * FROM "items_job"').fetchone())
        finally:
            conn.close()
        self.assertEqual(row["_task_status"], "claimed")
        self.assertEqual(row["_task_attempts"], 1)
        self.assertEqual(row["_task_claimed_by"], "worker-1")

    def test_reclaim_updates_claimed_by(self):
        self.write_mock_transcript("root-x", "worker-1")
        self.write_mock_transcript("root-x", "worker-2")
        self.make_scoped_source("root-x")
        self.call_tool_sync("table_job_create", {
            "conversation_id": "root-x",
            "table": "items",
            "template": "Do {name}",
            "job_table": "items_job",
            "lease_seconds": 0,
        })
        self.call_tool_sync("table_job_claim", {"conversation_id": "worker-1", "job_table": "items_job"})
        res = self.call_tool_sync("table_job_claim", {"conversation_id": "worker-2", "job_table": "items_job"})
        self.assertIsNotNone(res["task"])

        db = self.brain_dir / "root-x" / ".tables" / "session.db"
        conn = self.db_conn(db)
        try:
            row = dict(conn.execute('SELECT * FROM "items_job"').fetchone())
        finally:
            conn.close()
        self.assertEqual(row["_task_claimed_by"], "worker-2")
        self.assertEqual(row["_task_attempts"], 2)

    def test_claim_unknown_job(self):
        res = self.claim(job_table="no_such_job")
        self.assertIn("No job found", res.get("error", ""))

    def test_concurrent_claims_never_share_a_task(self):
        self.make_source(rows=[{"name": f"n{i}", "score": i} for i in range(10)])
        self.create_job()

        claimed = []
        lock = threading.Lock()

        def worker():
            while True:
                res = server.dispatch("table_job_claim", {"job_table": "items_job"})
                if res["task"] is None:
                    return
                with lock:
                    claimed.append(res["task"]["task_id"])

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(claimed), 10)
        self.assertEqual(len(set(claimed)), 10)


class TestJobSubmit(TableJobsTestBase):
    def test_submit_result_marks_done(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        task = self.claim()["task"]
        res = self.submit(task["task_id"], result="the answer")
        self.assertEqual(res, {"accepted": True, "task_status": "done"})

        row = self.job_rows()[0]
        self.assertEqual(row["_task_status"], "done")
        self.assertEqual(row["result"], "the answer")
        self.assertIsNone(row["_task_error"])

    def test_submit_requires_exactly_one_of_result_error(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        task = self.claim()["task"]
        res = self.submit(task["task_id"])
        self.assertIn("exactly one", res.get("error", ""))
        res = self.submit(task["task_id"], result="x", error="y")
        self.assertIn("exactly one", res.get("error", ""))

    def test_submit_unknown_or_unclaimed_rejected(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job()
        res = self.submit(999, result="x")
        self.assertEqual(res["accepted"], False)
        self.assertIsNone(res["task_status"])
        # Task exists but is still pending.
        res = self.submit(1, result="x")
        self.assertEqual(res["accepted"], False)
        self.assertEqual(res["task_status"], "pending")
        self.assertEqual(self.job_rows()[0]["_task_status"], "pending")

    def test_submit_error_requeues_then_fails_at_max_attempts(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job(max_attempts=2)

        task = self.claim()["task"]  # attempt 1
        res = self.submit(task["task_id"], error="boom 1")
        self.assertEqual(res, {"accepted": True, "task_status": "pending"})

        task = self.claim()["task"]  # attempt 2 (== max_attempts)
        res = self.submit(task["task_id"], error="boom 2")
        self.assertEqual(res, {"accepted": True, "task_status": "failed"})

        row = self.job_rows()[0]
        self.assertEqual(row["_task_status"], "failed")
        self.assertEqual(row["_task_error"], "boom 2")
        st = self.status()
        self.assertEqual(st["failed"], 1)
        self.assertTrue(st["complete"])

    def test_late_submit_after_reclaim_first_wins(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job(lease_seconds=0)
        first = self.claim()["task"]
        second = self.claim()["task"]  # lease expired instantly; reclaimed
        self.assertEqual(first["task_id"], second["task_id"])

        res = self.submit(first["task_id"], result="from the late worker")
        self.assertEqual(res, {"accepted": True, "task_status": "done"})
        res = self.submit(second["task_id"], result="too late")
        self.assertEqual(res, {"accepted": False, "task_status": "done"})
        self.assertEqual(self.job_rows()[0]["result"], "from the late worker")


class TestJobLeasesAndStatus(TableJobsTestBase):
    def test_expired_lease_requeues_on_status(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job(lease_seconds=0)
        self.claim()
        st = self.status()
        self.assertEqual(st["pending"], 1)
        self.assertEqual(st["claimed"], 0)

    def test_abandoned_task_fails_after_max_attempts(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.create_job(lease_seconds=0, max_attempts=2)
        self.claim()  # attempt 1, abandoned
        self.claim()  # sweep requeues, attempt 2, abandoned
        st = self.status()  # sweep: attempts exhausted -> failed
        self.assertEqual(st["failed"], 1)
        self.assertTrue(st["complete"])
        row = self.job_rows()[0]
        self.assertIn("lease expired", row["_task_error"])

    def test_status_counts_through_lifecycle(self):
        self.make_source(rows=[{"name": f"n{i}", "score": i} for i in range(4)])
        self.create_job()
        st = self.status()
        self.assertEqual(
            (st["total"], st["pending"], st["claimed"], st["done"], st["failed"]),
            (4, 4, 0, 0, 0),
        )
        self.assertFalse(st["complete"])

        t1 = self.claim()["task"]
        self.claim()
        st = self.status()
        self.assertEqual((st["pending"], st["claimed"]), (2, 2))

        self.submit(t1["task_id"], result="ok")
        st = self.status()
        self.assertEqual((st["pending"], st["claimed"], st["done"]), (2, 1, 1))

    def test_status_unknown_job(self):
        res = self.status(job_table="no_such_job")
        self.assertIn("No job found", res.get("error", ""))


class TestCompleteness(TableJobsTestBase):
    def test_flaky_workers_drain_200_rows_without_losing_any(self):
        n = 200
        self.make_source(rows=[{"name": f"row{i}", "score": i} for i in range(n)])
        self.create_job(lease_seconds=0, max_attempts=3)

        rng = random.Random(42)
        abandoned_once = False
        while True:
            res = self.claim()
            if res["task"] is None:
                st = self.status()
                if st["complete"]:
                    break
                continue  # lease_seconds=0: sweep requeues on the next claim
            task = res["task"]
            if not abandoned_once:
                abandoned_once = True  # worker "dies": never submits
                continue
            if rng.random() < 0.15:
                self.submit(task["task_id"], error="flaky failure")
            else:
                self.submit(task["task_id"], result=f"answer for {task['prompt']}")

        st = self.status()
        self.assertTrue(st["complete"])
        self.assertEqual(st["total"], n)
        self.assertEqual(st["done"] + st["failed"], n)

        rows = self.job_rows()
        self.assertEqual(len(rows), n)
        self.assertEqual({r["name"] for r in rows}, {f"row{i}" for i in range(n)})
        for r in rows:
            if r["_task_status"] == "done":
                self.assertIsNotNone(r["result"])
            else:
                self.assertEqual(r["_task_status"], "failed")
                self.assertIsNotNone(r["_task_error"])


class TestJobSessionScoping(TableJobsTestBase):
    def test_siblings_share_the_root_job_queue(self):
        self.write_mock_transcript("root-s", "creator-child")
        self.write_mock_transcript("root-s", "worker-child")
        self.make_scoped_source("creator-child")

        self.call_tool_sync("table_job_create", {
            "conversation_id": "creator-child",
            "table": "items",
            "template": "Do {name}",
            "job_table": "items_job",
        })
        res = self.call_tool_sync("table_job_claim", {
            "conversation_id": "worker-child",
            "job_table": "items_job",
        })
        self.assertIsNotNone(res["task"])
        sub = self.call_tool_sync("table_job_submit", {
            "conversation_id": "worker-child",
            "job_table": "items_job",
            "task_id": res["task"]["task_id"],
            "result": "done by sibling",
        })
        self.assertEqual(sub["accepted"], True)

        st = self.call_tool_sync("table_job_status", {
            "conversation_id": "creator-child",
            "job_table": "items_job",
        })
        self.assertTrue(st["complete"])
        self.assertEqual(st["done"], 1)

        db = self.brain_dir / "root-s" / ".tables" / "session.db"
        self.assertTrue(db.exists())
        conn = self.db_conn(db)
        try:
            row = dict(conn.execute('SELECT * FROM "items_job"').fetchone())
        finally:
            conn.close()
        self.assertEqual(row["result"], "done by sibling")


if __name__ == "__main__":
    unittest.main(verbosity=2)
