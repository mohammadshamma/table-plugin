#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
#     "anyio",
# ]
# ///
"""
Tests for the web inspector: the table_inspect_start/stop lifecycle (server.py)
and inspect_server.py's pure render functions.

The lifecycle tests mock subprocess.Popen / os.kill so no real web server is
spawned. The render tests build a real temp database through the MCP tools and
call the render_* functions directly (no socket), plus one end-to-end smoke test
that actually boots the HTTP server on an OS-assigned port.

Run with: uv run test_inspect.py
"""

import json
import socket
import sqlite3
import sys
import threading
import unittest
import urllib.request
from unittest.mock import MagicMock, patch

import server
import inspect_server
import table_tool
from test_session_scoping import SessionScopingTestBase


def _fake_proc(pid=4242):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None  # still running
    proc.returncode = None
    return proc


class InspectLifecycleTests(SessionScopingTestBase):
    """table_inspect_start / table_inspect_stop — subprocess mocked out."""

    def setUp(self):
        super().setUp()
        # Lineage cache is module-global; keep tests independent.
        with server.LINEAGE_CACHE_LOCK:
            server.LINEAGE_CACHE.clear()
        self.pidfile = self.cwd_dir / server.INSPECT_PIDFILE_NAME

    def test_start_writes_pidfile(self):
        proc = _fake_proc(4242)
        with patch("server.subprocess.Popen", return_value=proc) as popen, \
             patch("server._inspector_healthy", return_value=True):
            res = self.call_tool_sync("table_inspect_start", {})

        self.assertTrue(res["ok"])
        self.assertTrue(res["url"].startswith("http://127.0.0.1:"))
        self.assertNotIn("already_running", res)

        record = json.loads(self.pidfile.read_text())
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["port"], res["port"])

        # Spawned detached, with the resolved DB path and chosen port. The
        # child runs under this server's own interpreter (stdlib-only script).
        args, kwargs = popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("inspect_server.py"))
        self.assertIn("--db", cmd)
        self.assertEqual(cmd[cmd.index("--db") + 1], str(self.cwd_dir / "session.db"))
        self.assertEqual(cmd[cmd.index("--port") + 1], str(res["port"]))
        self.assertTrue(kwargs.get("start_new_session"))

    def test_start_idempotent_when_alive(self):
        self.pidfile.write_text(json.dumps(
            {"pid": 99999, "port": 8760, "url": "http://127.0.0.1:8760/", "started_at": 0}))
        with patch("server._pid_alive", return_value=True), \
             patch("server.subprocess.Popen") as popen:
            res = self.call_tool_sync("table_inspect_start", {})

        self.assertTrue(res["ok"])
        self.assertTrue(res["already_running"])
        self.assertEqual(res["url"], "http://127.0.0.1:8760/")
        popen.assert_not_called()

    def test_start_respawns_on_stale_pidfile(self):
        self.pidfile.write_text(json.dumps(
            {"pid": 99999, "port": 8760, "url": "http://127.0.0.1:8760/", "started_at": 0}))
        proc = _fake_proc(555)
        with patch("server._pid_alive", return_value=False), \
             patch("server.subprocess.Popen", return_value=proc) as popen, \
             patch("server._inspector_healthy", return_value=True):
            res = self.call_tool_sync("table_inspect_start", {})

        popen.assert_called_once()
        self.assertNotIn("already_running", res)
        self.assertEqual(json.loads(self.pidfile.read_text())["pid"], 555)

    def test_start_errors_if_process_exits_immediately(self):
        proc = _fake_proc(7)
        proc.poll.return_value = 1  # exited before becoming healthy
        proc.returncode = 1
        with patch("server.subprocess.Popen", return_value=proc), \
             patch("server._inspector_healthy", return_value=False):
            res = self.call_tool_sync("table_inspect_start", {})
        self.assertIn("error", res)
        self.assertFalse(self.pidfile.exists())

    def test_stop_kills_and_removes_pidfile(self):
        self.pidfile.write_text(json.dumps(
            {"pid": 4242, "port": 8760, "url": "http://127.0.0.1:8760/", "started_at": 0}))
        with patch("server._pid_alive", return_value=True), \
             patch("server.os.getpgid", return_value=4242) as getpgid, \
             patch("server.os.killpg") as killpg:
            res = self.call_tool_sync("table_inspect_stop", {})

        self.assertTrue(res["ok"])
        self.assertTrue(res["was_running"])
        self.assertEqual(res["stopped_pid"], 4242)
        getpgid.assert_called_once_with(4242)
        killpg.assert_called_once_with(4242, server.signal.SIGTERM)
        self.assertFalse(self.pidfile.exists())

    def test_stop_when_not_running(self):
        with patch("server.os.killpg") as killpg:
            res = self.call_tool_sync("table_inspect_stop", {})
        self.assertTrue(res["ok"])
        self.assertFalse(res["was_running"])
        killpg.assert_not_called()

    def test_port_fallback_when_default_occupied(self):
        # Hold the default port so _pick_free_port must move past it.
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", server.DEFAULT_INSPECT_PORT))
        try:
            port = server._pick_free_port(server.DEFAULT_INSPECT_PORT)
            self.assertNotEqual(port, server.DEFAULT_INSPECT_PORT)
            self.assertGreater(port, server.DEFAULT_INSPECT_PORT)
        finally:
            held.close()

    def test_resolved_db_path_passed_to_child(self):
        # root-1 invoked child-1; the DB must live under the root session.
        self.write_mock_transcript("root-1", "child-1")
        proc = _fake_proc(11)
        with patch("server.subprocess.Popen", return_value=proc) as popen, \
             patch("server._inspector_healthy", return_value=True):
            self.call_tool_sync("table_inspect_start", {"conversation_id": "child-1"})

        cmd = popen.call_args[0][0]
        expected = str(self.brain_dir / "root-1" / ".tables" / "session.db")
        self.assertEqual(cmd[cmd.index("--db") + 1], expected)


class InspectRenderTests(SessionScopingTestBase):
    """inspect_server.py render_* functions against a real temp database."""

    def setUp(self):
        super().setUp()
        self.db = str(self.cwd_dir / "session.db")
        # A plain table with 120 rows.
        self.call_tool_sync("table_create",
                            {"table": "items", "columns": {"name": "TEXT", "score": "INTEGER"}})
        self.call_tool_sync("table_insert", {
            "table": "items",
            "rows": [{"name": f"n{i}", "score": i} for i in range(120)],
        })
        # A job over it with max_attempts=1 so a single error → failed, and no
        # per-worker claim cap: this fixture claims twice from one process.
        self.call_tool_sync("table_job_create", {
            "table": "items", "template": "Do {name}", "job_table": "items_job",
            "max_attempts": 1, "max_claims_per_worker": 0,
        })
        c1 = self.call_tool_sync("table_job_claim", {"job_table": "items_job"})
        self.done_id = c1["task"]["task_id"]
        self.call_tool_sync("table_job_submit",
                            {"job_table": "items_job", "task_id": self.done_id, "result": "<b>done</b>"})
        c2 = self.call_tool_sync("table_job_claim", {"job_table": "items_job"})
        self.failed_id = c2["task"]["task_id"]
        self.call_tool_sync("table_job_submit",
                            {"job_table": "items_job", "task_id": self.failed_id, "error": "boom <x>"})

    def ro(self):
        return inspect_server.connect_ro(self.db)

    def test_index_lists_and_flags_jobs(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_index(conn)
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn("/table?name=items", htm)       # plain table → row browser
        self.assertIn("/job?name=items_job", htm)      # job table → job view
        self.assertIn('badge job', htm)
        self.assertNotIn(table_tool.JOBS_TABLE, htm)   # internal registry hidden

    def test_table_pagination(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_table(conn, "items", 2, 50)
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn(">n50<", htm)          # first row of page 2 (rows 51–100)
        self.assertIn(">n99<", htm)          # last row of page 2
        self.assertNotIn(">n49<", htm)       # page 1
        self.assertNotIn(">n100<", htm)      # page 3
        self.assertIn("Page 2 of 3", htm)

    def test_job_counts_match_tool(self):
        tool = self.call_tool_sync("table_job_status", {"job_table": "items_job"})
        conn = self.ro()
        try:
            web = table_tool.count_task_statuses(conn, "items_job")
        finally:
            conn.close()
        for st in table_tool.TASK_STATUSES:
            self.assertEqual(web[st], tool[st], f"{st} count drifted")
        self.assertEqual(web["done"], 1)
        self.assertEqual(web["failed"], 1)

    def test_job_view_renders_summary_and_filter(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_job(conn, "items_job", "all", 1, 50)
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn("job", htm)
        self.assertIn("failed", htm)
        # filter links present
        self.assertIn("status=failed", htm)
        self.assertIn("status=done", htm)

    def test_job_status_filter_narrows_tasks(self):
        conn = self.ro()
        try:
            _, htm = inspect_server.render_job(conn, "items_job", "failed", 1, 50)
        finally:
            conn.close()
        # Only the failed task links should appear.
        self.assertIn(f"/task?name=items_job&id={self.failed_id}", htm)
        self.assertNotIn(f"/task?name=items_job&id={self.done_id}", htm)

    def test_task_drilldown_failed_shows_error(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_task(conn, "items_job", self.failed_id)
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn("Error", htm)
        self.assertIn("boom &lt;x&gt;", htm)  # escaped

    def test_task_drilldown_done_shows_result_escaped(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_task(conn, "items_job", self.done_id)
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn("Result", htm)
        self.assertIn("&lt;b&gt;done&lt;/b&gt;", htm)
        self.assertNotIn("<b>done</b>", htm)

    def test_unknown_table_is_404(self):
        conn = self.ro()
        try:
            for render in (
                lambda c: inspect_server.render_table(c, "nope", 1, 50),
                lambda c: inspect_server.render_job(c, "nope", "all", 1, 50),
                lambda c: inspect_server.render_task(c, "nope", 1),
            ):
                self.assertEqual(render(conn)[0], 404)
        finally:
            conn.close()

    def test_html_escaping_in_cells(self):
        self.call_tool_sync("table_create", {"table": "evil", "columns": {"payload": "TEXT"}})
        self.call_tool_sync("table_insert",
                            {"table": "evil", "rows": [{"payload": "<script>alert(1)</script>"}]})
        conn = self.ro()
        try:
            _, htm = inspect_server.render_table(conn, "evil", 1, 50)
        finally:
            conn.close()
        self.assertNotIn("<script>alert", htm)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", htm)

    def test_connection_is_readonly(self):
        conn = self.ro()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO items(name, score) VALUES ('x', 1)")
        finally:
            conn.close()

    def test_missing_db_connect_raises(self):
        with self.assertRaises(sqlite3.OperationalError):
            inspect_server.connect_ro(str(self.cwd_dir / "does_not_exist.db"))


class InspectHttpSmokeTest(SessionScopingTestBase):
    """One real HTTP round-trip to confirm the server wiring."""

    def test_healthz_and_index_over_http(self):
        db = str(self.cwd_dir / "session.db")
        self.call_tool_sync("table_create", {"table": "t", "columns": {"a": "TEXT"}})

        from http.server import ThreadingHTTPServer
        inspect_server._STATE["db"] = db
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                self.assertEqual(r.read().decode(), "ok")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                self.assertIn("Tables", r.read().decode())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
