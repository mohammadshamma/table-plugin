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
            # These legacy tests exercise multi-claim single-process flows;
            # the per-worker claim cap has its own test class below.
            "max_claims_per_worker": 0,
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

    # ─── One queue, one process, several distinct worker identities ──────────
    #
    # Distinct conversation_ids only share a database when lineage resolves them
    # to a common root, so every worker needs a transcript naming it a child of
    # ROOT. This is the world worker subagents actually run in.

    ROOT = "root-cap"

    def cap_setup(self, rows, workers, **overrides):
        self.write_mock_transcript(self.ROOT, "creator")
        for w in workers:
            self.write_mock_transcript(self.ROOT, w)
        self.call_tool_sync("table_create", {
            "conversation_id": "creator",
            "table": "items",
            "columns": {"name": "TEXT", "score": "INTEGER"},
        })
        self.call_tool_sync("table_insert", {
            "conversation_id": "creator", "table": "items", "rows": rows,
        })
        args = {
            "conversation_id": "creator",
            "table": "items",
            "template": "Summarize {name} with score {score}",
            "job_table": "items_job",
            **overrides,
        }
        return self.call_tool_sync("table_job_create", args)

    def w_claim(self, worker, **overrides):
        return self.call_tool_sync("table_job_claim", {
            "conversation_id": worker, "job_table": "items_job", **overrides,
        })

    def w_submit(self, worker, task_id, **overrides):
        return self.call_tool_sync("table_job_submit", {
            "conversation_id": worker, "job_table": "items_job",
            "task_id": task_id, **overrides,
        })

    def w_status(self):
        return self.call_tool_sync("table_job_status", {
            "conversation_id": "creator", "job_table": "items_job",
        })

    def root_db(self):
        return self.brain_dir / self.ROOT / ".tables" / "session.db"

    def root_job_rows(self):
        conn = self.db_conn(self.root_db())
        try:
            return [dict(r) for r in conn.execute('SELECT rowid AS task_id, * FROM "items_job"')]
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
            "max_claims_per_worker": 0,  # cap disabled; this test reclaims after a lapsed lease
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


class TestWorkerClaimCap(TableJobsTestBase):
    """max_claims_per_worker: default 1, a durable per-worker lifetime cap.

    Jobs here are created via raw call_tool_sync (not create_job) where the
    default matters, because the helper pins max_claims_per_worker=0 for the
    legacy multi-claim tests.

    Most of these drive several DISTINCT worker identities against ONE queue in
    ONE process (see cap_setup): the world a per-process counter mis-handles,
    and the one real worker subagents actually run in.
    """

    def raw_create(self, **overrides):
        args = {
            "table": "items",
            "template": "Summarize {name} with score {score}",
            "job_table": "items_job",
            **overrides,
        }
        return self.call_tool_sync("table_job_create", args)

    def test_distinct_workers_one_process_each_claim_one_and_drain(self):
        """The cap is per worker, not per job: N workers drain N tasks.

        Worker subagents share one MCP server process, so a cap tracked per
        process wedges the whole job after the first claim.
        """
        workers = [f"worker-{i}" for i in range(5)]
        rows = [{"name": f"n{i}", "score": i} for i in range(5)]
        self.assertEqual(self.cap_setup(rows, workers)["max_claims_per_worker"], 1)

        for w in workers:
            res = self.w_claim(w)
            self.assertIsNotNone(res["task"], f"{w} was wrongly refused: {res.get('reason')}")
            self.assertIn("last claim", res["note"])
            self.w_submit(w, res["task"]["task_id"], result=f"done-{w}")

        st = self.w_status()
        self.assertTrue(st["complete"])
        self.assertEqual(st["done"], 5)
        self.assertEqual(
            sorted(r["_task_claimed_by"] for r in self.root_job_rows()), sorted(workers)
        )

    def test_default_cap_blocks_a_workers_second_claim_but_not_a_fresh_worker(self):
        rows = [{"name": "a", "score": 1}, {"name": "b", "score": 2}]
        self.assertEqual(self.cap_setup(rows, ["w1", "w2"])["max_claims_per_worker"], 1)

        first = self.w_claim("w1")
        self.assertIsNotNone(first["task"])
        self.assertIn("last claim", first["note"])
        self.assertIn("terminate", first["note"])

        second = self.w_claim("w1")
        self.assertIsNone(second["task"])
        self.assertIn("claim limit", second["reason"])
        self.assertIn("w1", second["reason"])
        self.assertEqual(second["claim_limit"], 1)
        self.assertNotIn("remaining_pending", second)

        # The refusal never touched the database.
        statuses = sorted(r["_task_status"] for r in self.root_job_rows())
        self.assertEqual(statuses, ["claimed", "pending"])

        # A distinct worker, in this same process, still claims the leftover.
        self.assertIsNotNone(self.w_claim("w2")["task"])

    def test_submit_does_not_free_a_slot(self):
        """The cap counts a worker's lifetime claims, not its open ones."""
        rows = [{"name": "a", "score": 1}, {"name": "b", "score": 2}]
        self.cap_setup(rows, ["w1"])
        task = self.w_claim("w1")["task"]
        self.assertEqual(self.w_submit("w1", task["task_id"], result="ok")["accepted"], True)

        # The done row is still stamped w1, so w1 has spent its one claim.
        refused = self.w_claim("w1")
        self.assertIsNone(refused["task"])
        self.assertIn("claim limit", refused["reason"])

    def test_cap_requires_a_worker_identity(self):
        """A NULL stamp matches no row, so an unidentifiable worker is refused."""
        self.make_source(rows=[{"name": "a", "score": 1}])
        self.raw_create()  # default cap 1
        res = self.claim()  # no conversation_id, and the env is cleared
        self.assertIn("conversation_id", res.get("error", ""))
        self.assertEqual(self.job_rows()[0]["_task_status"], "pending")

    def test_cap_zero_is_unlimited(self):
        self.make_source(rows=[{"name": f"n{i}", "score": i} for i in range(5)])
        self.raw_create(max_claims_per_worker=0)
        for _ in range(5):
            res = self.claim()
            self.assertIsNotNone(res["task"])
            self.assertNotIn("note", res)
        drained = self.claim()
        self.assertEqual(drained, {"task": None, "remaining_pending": 0})

    def test_cap_n_allows_n_then_refuses(self):
        rows = [{"name": f"n{i}", "score": i} for i in range(3)]
        self.cap_setup(rows, ["w1"], max_claims_per_worker=2)

        first = self.w_claim("w1")
        self.assertEqual(first["note"], "Claim 1 of 2 allowed for this worker.")
        second = self.w_claim("w1")
        self.assertIn("last claim", second["note"])

        third = self.w_claim("w1")
        self.assertIsNone(third["task"])
        self.assertEqual(third["claim_limit"], 2)
        self.assertEqual(sum(r["_task_status"] == "pending" for r in self.root_job_rows()), 1)

    def test_legacy_null_cap_is_unlimited(self):
        self.make_source(rows=[{"name": "a", "score": 1}, {"name": "b", "score": 2}])
        self.raw_create()  # stores the default cap of 1
        conn = self.db_conn()
        try:
            conn.execute(f'UPDATE "{server.JOBS_TABLE}" SET max_claims_per_worker = NULL')
            conn.commit()
        finally:
            conn.close()

        # NULL = a job created before the cap existed: old behavior, no notes.
        for _ in range(2):
            res = self.claim()
            self.assertIsNotNone(res["task"])
            self.assertNotIn("note", res)
        self.assertEqual(self.claim(), {"task": None, "remaining_pending": 0})

    def test_migration_adds_column_to_legacy_meta_table(self):
        # Hand-build a session DB exactly as the pre-cap code laid it out:
        # a six-column registry and a job table with two pending tasks.
        conn = self.db_conn()
        try:
            conn.execute(
                f'CREATE TABLE "{server.JOBS_TABLE}" ('
                "job_table TEXT PRIMARY KEY, source_table TEXT, template TEXT, "
                "lease_seconds REAL, max_attempts INTEGER, created_at REAL)"
            )
            conn.execute(
                f'INSERT INTO "{server.JOBS_TABLE}" VALUES (?, ?, ?, ?, ?, ?)',
                ("legacy_job", "items", "Do {name}", 600.0, 3, 0.0),
            )
            conn.execute(
                'CREATE TABLE "legacy_job" (name TEXT, result TEXT, _task_status TEXT, '
                "_task_error TEXT, _task_attempts INTEGER, _task_lease_expires REAL, "
                "_task_claimed_by TEXT)"
            )
            for name in ("a", "b"):
                conn.execute(
                    'INSERT INTO "legacy_job" (name, _task_status, _task_attempts) '
                    "VALUES (?, 'pending', 0)",
                    (name,),
                )
            conn.commit()
        finally:
            conn.close()

        # Claiming through the tool migrates the registry in passing; the
        # legacy job's NULL cap means unlimited, so both claims succeed.
        self.assertIsNotNone(self.claim(job_table="legacy_job")["task"])
        self.assertIsNotNone(self.claim(job_table="legacy_job")["task"])

        conn = self.db_conn()
        try:
            cols = {r["name"] for r in conn.execute(f'PRAGMA table_info("{server.JOBS_TABLE}")')}
            cap = conn.execute(f'SELECT max_claims_per_worker FROM "{server.JOBS_TABLE}"').fetchone()[0]
        finally:
            conn.close()
        self.assertIn("max_claims_per_worker", cols)
        self.assertIsNone(cap)

        # The seven-column INSERT works against the migrated registry.
        self.make_source(rows=[{"name": "c", "score": 3}])
        res = self.raw_create()
        self.assertEqual(res.get("ok"), True)
        self.assertEqual(res["max_claims_per_worker"], 1)

    def test_recreated_job_resets_per_worker_counts(self):
        self.cap_setup([{"name": "a", "score": 1}], ["w1"])
        task = self.w_claim("w1")["task"]
        self.w_submit("w1", task["task_id"], result="done")
        self.assertIsNone(self.w_claim("w1")["task"])  # w1 has spent its claim

        # Dropping the job table deregisters it, so the name is reusable. The
        # rebuilt table carries no stamps, so the namesake's counts do not persist.
        dropped = self.call_tool_sync("table_drop", {
            "conversation_id": "creator", "table": "items_job",
        })
        self.assertEqual(dropped["deregistered"], True)
        recreated = self.call_tool_sync("table_job_create", {
            "conversation_id": "creator", "table": "items",
            "template": "Summarize {name} with score {score}", "job_table": "items_job",
        })
        self.assertEqual(recreated["ok"], True)

        self.assertIsNotNone(self.w_claim("w1")["task"])

    def test_dropping_a_job_table_frees_its_name(self):
        """The cycle a real agent could not complete: drop, then re-create."""
        self.cap_setup([{"name": "a", "score": 1}], ["w1"])

        # Without deregistration this fails with "already registered", and the
        # registry cannot be edited through table_run_sql — the name is burned.
        self.call_tool_sync("table_drop", {"conversation_id": "creator", "table": "items_job"})
        again = self.call_tool_sync("table_job_create", {
            "conversation_id": "creator", "table": "items",
            "template": "Summarize {name} with score {score}", "job_table": "items_job",
        })
        self.assertEqual(again.get("ok"), True, again)
        self.assertEqual(again["total_tasks"], 1)

    def test_dropping_an_ordinary_table_leaves_the_registry_alone(self):
        self.cap_setup([{"name": "a", "score": 1}], ["w1"])
        dropped = self.call_tool_sync("table_drop", {"conversation_id": "creator", "table": "items"})
        self.assertNotIn("deregistered", dropped)

        conn = self.db_conn(self.root_db())
        try:
            still = conn.execute(
                f'SELECT COUNT(*) FROM "{server.JOBS_TABLE}" WHERE job_table = ?', ("items_job",)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(still, 1)

    def test_capped_refusal_does_not_sweep_or_write(self):
        self.cap_setup([{"name": "a", "score": 1}], ["w1"], lease_seconds=0)
        self.w_claim("w1")  # lease expires instantly

        refused = self.w_claim("w1")
        self.assertIsNone(refused["task"])
        self.assertIn("reason", refused)
        # The cap is checked before the sweep, so the refusal ran none: the row
        # is still marked claimed.
        self.assertEqual(self.root_job_rows()[0]["_task_status"], "claimed")
        # Normal recovery is untouched: status sweeps it back to pending.
        self.assertEqual(self.w_status()["pending"], 1)

    def _concurrent_claims(self, worker_ids):
        results = []
        lock = threading.Lock()

        def worker(conversation_id):
            res = server.dispatch("table_job_claim", {
                "conversation_id": conversation_id, "job_table": "items_job",
            })
            with lock:
                results.append(res)

        threads = [threading.Thread(target=worker, args=(w,)) for w in worker_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_concurrent_claims_by_one_worker_respect_cap(self):
        rows = [{"name": f"n{i}", "score": i} for i in range(10)]
        self.cap_setup(rows, ["solo"])  # default cap 1

        results = self._concurrent_claims(["solo", "solo"])
        tasks = [r for r in results if r["task"] is not None]
        refusals = [r for r in results if r["task"] is None]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(refusals), 1)
        self.assertIn("claim limit", refusals[0]["reason"])

    def test_concurrent_claims_by_distinct_workers_all_succeed(self):
        """Two workers racing in one process each get their own task."""
        rows = [{"name": f"n{i}", "score": i} for i in range(4)]
        self.cap_setup(rows, ["cw0", "cw1"])  # default cap 1

        results = self._concurrent_claims(["cw0", "cw1"])
        tasks = [r["task"] for r in results if r["task"] is not None]
        self.assertEqual(len(tasks), 2, f"a worker was wrongly refused: {results}")
        self.assertEqual(len({t["task_id"] for t in tasks}), 2)

    def test_invalid_max_claims_per_worker(self):
        self.make_source(rows=[{"name": "a", "score": 1}])
        res = self.raw_create(max_claims_per_worker=-1)
        self.assertIn("max_claims_per_worker", res.get("error", ""))


class TestRunSqlQueueGuard(TableJobsTestBase):
    """table_run_sql must not be able to rewrite a job's bookkeeping.

    A job's guarantees hold only while task state moves through the table_job_*
    tools. Arbitrary SQL could otherwise forge task results, or disable the
    per-worker cap in the registry, without ever touching the DB file.
    """

    def setUp(self):
        super().setUp()
        self.cap_setup([{"name": "a", "score": 1}, {"name": "b", "score": 2}], ["w1", "w2"])

    def sql(self, statement):
        return self.call_tool_sync("table_run_sql", {
            "conversation_id": "creator", "sql": statement,
        })

    def assertDenied(self, statement):
        res = self.sql(statement)
        err = res.get("error", "")
        self.assertIn("cannot modify the job queue", err, f"not denied: {statement!r} -> {res}")
        return err

    def registry_cap(self):
        conn = self.db_conn(self.root_db())
        try:
            return conn.execute(
                f'SELECT max_claims_per_worker FROM "{server.JOBS_TABLE}" WHERE job_table = ?',
                ("items_job",),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_cannot_disable_the_claim_cap(self):
        """The exact bypass an agent used against the real server."""
        err = self.assertDenied(
            "UPDATE _table_jobs SET max_claims_per_worker = 0 WHERE job_table = 'items_job';"
        )
        self.assertIn("_table_jobs", err)
        self.assertIn("table_job_", err)  # the refusal names the sanctioned tools
        self.assertEqual(self.registry_cap(), 1)

    def test_cannot_forge_task_state(self):
        task = self.w_claim("w1")["task"]
        self.assertDenied("UPDATE items_job SET _task_status = 'done'")
        self.assertDenied("UPDATE items_job SET result = 'forged'")
        self.assertDenied("UPDATE items_job SET _task_attempts = 0")
        self.assertDenied("UPDATE items_job SET _task_claimed_by = 'someone-else'")

        row = next(r for r in self.root_job_rows() if r["task_id"] == task["task_id"])
        self.assertEqual(row["_task_status"], "claimed")
        self.assertEqual(row["_task_claimed_by"], "w1")
        self.assertIsNone(row["result"])

    def test_cannot_add_remove_or_reshape_queue_tables(self):
        for statement in (
            "INSERT INTO items_job (name, score, _task_status, _task_attempts) VALUES ('z', 9, 'pending', 0)",
            "DELETE FROM items_job",
            "DELETE FROM _table_jobs",
            "INSERT INTO _table_jobs (job_table) VALUES ('fake')",
            "DROP TABLE items_job",
            "DROP TABLE _table_jobs",
            "ALTER TABLE items_job RENAME TO stolen",
            "CREATE TRIGGER t AFTER INSERT ON items_job BEGIN SELECT 1; END",
            "PRAGMA writable_schema=ON",
        ):
            self.assertDenied(statement)

        self.assertEqual(len(self.root_job_rows()), 2)
        self.assertEqual(self.registry_cap(), 1)

    def test_reads_and_ordinary_writes_still_work(self):
        self.assertEqual(self.sql("SELECT * FROM items_job")["count"], 2)
        self.assertEqual(self.sql(f'SELECT * FROM "{server.JOBS_TABLE}"')["count"], 1)

        # A job table's SOURCE columns are the caller's own data.
        self.assertEqual(self.sql("UPDATE items_job SET name = 'renamed'")["changes"], 2)
        self.assertTrue(all(r["name"] == "renamed" for r in self.root_job_rows()))

        # Ordinary tables are untouched by the guard.
        self.call_tool_sync("table_create", {
            "conversation_id": "creator", "table": "plain", "columns": {"x": "INTEGER"},
        })
        self.call_tool_sync("table_insert", {
            "conversation_id": "creator", "table": "plain", "rows": [{"x": 1}],
        })
        self.assertEqual(self.sql("UPDATE plain SET x = 2")["changes"], 1)
        self.assertEqual(self.sql("ALTER TABLE plain ADD COLUMN y INTEGER")["ok"], True)
        self.assertEqual(self.sql("DROP TABLE plain")["ok"], True)

    def test_guard_does_not_leak_into_the_job_tools(self):
        """The table_job_* tools write _task_* freely; only run_sql is guarded."""
        task = self.w_claim("w1")["task"]
        self.assertEqual(self.w_submit("w1", task["task_id"], result="ok")["accepted"], True)
        task2 = self.w_claim("w2")["task"]
        # An error requeues the task (attempt 1 of 3), which also writes _task_*.
        self.assertEqual(self.w_submit("w2", task2["task_id"], error="boom")["accepted"], True)

        st = self.w_status()
        self.assertEqual((st["done"], st["pending"]), (1, 1))
        rows = {r["task_id"]: r for r in self.root_job_rows()}
        self.assertEqual(rows[task["task_id"]]["result"], "ok")
        self.assertEqual(rows[task2["task_id"]]["_task_error"], "boom")

    def test_deregistering_a_job_releases_protection(self):
        conn = self.db_conn(self.root_db())
        try:
            conn.execute(f'DELETE FROM "{server.JOBS_TABLE}" WHERE job_table = ?', ("items_job",))
            conn.commit()
        finally:
            conn.close()

        # No longer a registered job table, so it is ordinary data again.
        self.assertEqual(self.sql("DELETE FROM items_job")["changes"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
