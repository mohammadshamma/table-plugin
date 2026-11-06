#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp==2.3.0",
#     "anyio",
# ]
# ///
"""
Tests for the Table Inspector auxiliary pane web UI (inspect_server.py) and
sidecar integration (sidecars/inspector/sidecar.json).

Covers:
- Sidecar configuration manifest schema and validation
- Standalone mode fallback: CLI port precedence, --db fallback, and watchdog guard
- Session scoping: conversationId validation, root resolution, non-mutating empty state
- Token authentication: ?token= param, X-Sidecar-Token header, /healthz exemption
- Query parameter propagation: conversationId and token threaded through all routes
- Switcher bar and theme sync: pill navigation tabs, sticky headers, postMessage listener
- HTML rendering: table browser, job status, pagination, sorting, HTML escaping, read-only guarantees
- End-to-end HTTP smoke tests over localhost sockets

Run with: uv run test_inspect.py
"""

import html
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import inspect_server
import server
import table_tool
from test_session_scoping import SessionScopingTestBase


# ─── 1. Sidecar Configuration Manifest Tests ─────────────────────────────────


class SidecarConfigTest(unittest.TestCase):
    """Validates the Antigravity sidecar configuration manifest for the inspector."""

    def setUp(self):
        self.plugin_dir = Path(__file__).resolve().parent
        self.sidecar_path = self.plugin_dir / "sidecars" / "inspector" / "sidecar.json"

    def test_sidecar_json_exists_and_valid_json(self):
        self.assertTrue(self.sidecar_path.is_file(), f"Manifest missing at {self.sidecar_path}")
        content = self.sidecar_path.read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertIsInstance(data, dict)

    def test_sidecar_execution_config(self):
        data = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("command"), "python3")
        self.assertEqual(data.get("args"), ["../../inspect_server.py"])
        self.assertEqual(data.get("restart_policy"), "always")
        self.assertIs(data.get("has_web_ui"), True)

        target_script = (self.sidecar_path.parent / data["args"][0]).resolve()
        self.assertTrue(target_script.is_file(), f"Target script {target_script} not found on disk")
        self.assertEqual(target_script.name, "inspect_server.py")

    def test_sidecar_ui_config_views(self):
        data = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
        self.assertIn("ui_config", data)
        self.assertIsInstance(data["ui_config"], dict)
        views = data["ui_config"].get("views")
        self.assertIsInstance(views, list)
        self.assertGreaterEqual(len(views), 1)

        aux_view = next((v for v in views if v.get("entrypoint") == "SIDECAR_UI_ENTRYPOINT_AUX_PANE"), None)
        self.assertIsNotNone(aux_view, "View with SIDECAR_UI_ENTRYPOINT_AUX_PANE not found")
        self.assertEqual(aux_view.get("path"), "/")
        self.assertEqual(aux_view.get("title"), "Table Inspector")

    def test_sidecar_description_present(self):
        data = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
        self.assertIn("description", data)
        self.assertIsInstance(data["description"], str)
        self.assertTrue(len(data["description"].strip()) > 0)


# ─── 2. Standalone Fallback & Lifecycle Tests ────────────────────────────────


class InspectStandaloneFallbackTest(SessionScopingTestBase):
    """Validates CLI port precedence, idle watchdog lifecycle, and --db fallback behavior."""

    def setUp(self):
        super().setUp()
        self._orig_state_db = inspect_server._STATE.get("db")
        self._orig_last_activity = inspect_server._STATE.get("last_activity")
        inspect_server._STATE["db"] = None

    def tearDown(self):
        inspect_server._STATE["db"] = self._orig_state_db
        inspect_server._STATE["last_activity"] = self._orig_last_activity
        super().tearDown()

    def test_port_precedence_cli_explicit(self):
        self.assertEqual(inspect_server.resolve_port(8080), 8080)
        self.assertEqual(inspect_server.resolve_port(4242), 4242)

    def test_port_precedence_cli_preserves_port_zero(self):
        # Port 0 is dynamically assigned by OS; must not be treated as falsy
        self.assertEqual(inspect_server.resolve_port(0), 0)

    def test_port_env_var_and_whitespace_stripping(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "9090"}):
            self.assertEqual(inspect_server.resolve_port(None), 9090)

        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "  9191 \n\t"}):
            self.assertEqual(inspect_server.resolve_port(None), 9191)

    def test_port_fallback_to_default_8760(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(inspect_server.resolve_port(None), inspect_server.DEFAULT_PORT)
            self.assertEqual(inspect_server.resolve_port(None), 8760)

        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": ""}):
            self.assertEqual(inspect_server.resolve_port(None), 8760)

        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "   "}):
            self.assertEqual(inspect_server.resolve_port(None), 8760)

        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "not_a_number"}):
            self.assertEqual(inspect_server.resolve_port(None), 8760)

    def test_port_cli_takes_precedence_over_env_var(self):
        with patch.dict(os.environ, {"ANTIGRAVITY_SIDECAR_WEB_PORT": "9999"}):
            self.assertEqual(inspect_server.resolve_port(7777), 7777)
            self.assertEqual(inspect_server.resolve_port(0), 0)

    def test_watchdog_spawned_only_in_db_mode(self):
        dummy_db = str(self.cwd_dir / "test.db")
        with patch.object(inspect_server.ThreadingHTTPServer, "serve_forever"):
            inspect_server.main(["--db", dummy_db, "--port", "0"])

        w_threads = [t for t in threading.enumerate() if getattr(t, "_target", None) == inspect_server._idle_watchdog]
        self.assertEqual(len(w_threads), 1)
        self.assertTrue(w_threads[0].daemon)
        self.assertEqual(inspect_server._STATE["db"], dummy_db)

    def test_watchdog_disabled_in_sidecar_mode(self):
        # In sidecar mode (--db omitted), watchdog thread must not be launched
        with patch.object(inspect_server.ThreadingHTTPServer, "serve_forever"):
            inspect_server.main(["--port", "0"])
        self.assertIsNone(inspect_server._STATE["db"])

    def test_watchdog_disabled_when_db_arg_empty(self):
        with patch.object(inspect_server.ThreadingHTTPServer, "serve_forever"):
            inspect_server.main(["--db", "", "--port", "0"])
        self.assertFalse(bool(inspect_server._STATE["db"]))

    def test_cleanup_pidfile_matching_pid_unlinked(self):
        db_path = self.cwd_dir / "session.db"
        pidfile = self.cwd_dir / inspect_server.INSPECT_PIDFILE_NAME
        pidfile.write_text(json.dumps({"pid": os.getpid(), "port": 8760}))

        inspect_server._cleanup_pidfile(str(db_path))
        self.assertFalse(pidfile.exists())

    def test_cleanup_pidfile_foreign_pid_preserved(self):
        db_path = self.cwd_dir / "session.db"
        pidfile = self.cwd_dir / inspect_server.INSPECT_PIDFILE_NAME
        pidfile.write_text(json.dumps({"pid": 999999, "port": 8760}))

        inspect_server._cleanup_pidfile(str(db_path))
        self.assertTrue(pidfile.exists())

    def test_db_fallback_when_conversation_id_missing_unit(self):
        standalone_db = self.cwd_dir / "standalone.db"
        inspect_server._STATE["db"] = str(standalone_db)

        self.assertEqual(inspect_server.resolve_request_db(None), standalone_db)
        self.assertEqual(inspect_server.resolve_request_db(""), standalone_db)
        self.assertEqual(inspect_server.resolve_request_db("_new"), standalone_db)

    def test_db_fallback_used_in_http_request_without_conversation_id(self):
        standalone_db = self.cwd_dir / "standalone.db"
        conn = sqlite3.connect(str(standalone_db))
        conn.execute("CREATE TABLE standalone_items (id INT, label TEXT)")
        conn.commit()
        conn.close()

        inspect_server._STATE["db"] = str(standalone_db)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("standalone_items", body)
                self.assertNotIn("No database yet", body)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_conversation_id_takes_precedence_over_standalone_db_http(self):
        standalone_db = self.cwd_dir / "standalone.db"
        conn = sqlite3.connect(str(standalone_db))
        conn.execute("CREATE TABLE standalone_items (id INT)")
        conn.commit()
        conn.close()

        conv_id = "scoped-session-test"
        session_db = self.brain_dir / conv_id / ".tables" / "session.db"
        session_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(session_db))
        conn.execute("CREATE TABLE scoped_items (id INT)")
        conn.commit()
        conn.close()

        inspect_server._STATE["db"] = str(standalone_db)

        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/?conversationId={conv_id}", timeout=2) as resp:
                    self.assertEqual(resp.status, 200)
                    body = resp.read().decode("utf-8")
                    self.assertIn("scoped_items", body)
                    self.assertNotIn("standalone_items", body)
            finally:
                httpd.shutdown()
                httpd.server_close()


# ─── 3. Session Scoping & Lineage Tests ──────────────────────────────────────


class InspectSessionScopingTest(SessionScopingTestBase):
    """Validates conversation ID regex validation, non-mutating empty state, and lineage scoping."""

    def setUp(self):
        super().setUp()
        self._orig_state_db = inspect_server._STATE.get("db")
        inspect_server._STATE["db"] = None
        with table_tool.LINEAGE_CACHE_LOCK:
            table_tool.LINEAGE_CACHE.clear()

    def tearDown(self):
        inspect_server._STATE["db"] = self._orig_state_db
        with table_tool.LINEAGE_CACHE_LOCK:
            table_tool.LINEAGE_CACHE.clear()
        super().tearDown()

    def test_regex_rejection_invalid_ids_raise_value_error(self):
        invalid_ids = [
            "../traversal",
            "..\\win32",
            "nested/dir",
            "session;DROP TABLE",
            "id with spaces",
            "   leading_space",
            "trailing_space   ",
            "id@domain",
            "id#fragment",
            "id$variable",
            "id%20encoded",
            "id!bang",
            "id*asterisk",
            "conv_🔑_emoji",
            "جلسة_عربية",
            "conv\x00null",
            "conv\nnewline",
            "conv\rcarriage",
        ]
        for bad_id in invalid_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    inspect_server.resolve_request_db(bad_id)

    def test_regex_allowed_characters_accepted(self):
        valid_ids = [
            "conv-123",
            "CONV_456",
            "session-abc_DEF-09",
            "0",
            "a",
            "Z",
            "conv_with_underscores_and-dashes",
        ]
        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            for valid_id in valid_ids:
                with self.subTest(valid_id=valid_id):
                    res = inspect_server.resolve_request_db(valid_id)
                    self.assertEqual(res, self.brain_dir / valid_id / ".tables" / "session.db")

    def test_regex_rejection_http_400_with_zero_io(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with patch("table_tool.find_root_conversation") as mock_find_root, \
                 patch("table_tool.find_parent_conversation") as mock_find_parent:
                before_files = set(self.brain_dir.rglob("*"))
                bad_urls = [
                    f"http://127.0.0.1:{port}/?conversationId=../escape",
                    f"http://127.0.0.1:{port}/?conversationId=foo/bar",
                    f"http://127.0.0.1:{port}/?conversationId=bad;injection",
                    f"http://127.0.0.1:{port}/?conversationId=bad%20space",
                    f"http://127.0.0.1:{port}/?conversationId=id@special",
                ]
                for url in bad_urls:
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(url, timeout=2)
                    self.assertEqual(ctx.exception.code, 400)
                    body = ctx.exception.read().decode("utf-8")
                    ctx.exception.close()
                    self.assertIn("Invalid conversation ID", body)

                mock_find_root.assert_not_called()
                mock_find_parent.assert_not_called()
                after_files = set(self.brain_dir.rglob("*"))
                self.assertEqual(before_files, after_files)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_empty_state_resolution_returns_none(self):
        self.assertIsNone(inspect_server.resolve_request_db(None))
        self.assertIsNone(inspect_server.resolve_request_db(""))
        self.assertIsNone(inspect_server.resolve_request_db("_new"))

    def test_empty_state_returns_200_without_filesystem_mutations(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
                before_files = set(self.brain_dir.rglob("*"))
                empty_state_urls = [
                    f"http://127.0.0.1:{port}/",
                    f"http://127.0.0.1:{port}/?conversationId=",
                    f"http://127.0.0.1:{port}/?conversationId=_new",
                    f"http://127.0.0.1:{port}/?conversationId=uncreated-session-id",
                ]
                for url in empty_state_urls:
                    with urllib.request.urlopen(url, timeout=2) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(resp.headers.get("Refresh"), "5")
                        body = resp.read().decode("utf-8")
                        self.assertIn("No database yet — create a table first.", body)
                        self.assertIn('meta http-equiv="refresh" content="5"', body)

                after_files = set(self.brain_dir.rglob("*"))
                self.assertEqual(before_files, after_files)
                self.assertFalse((self.brain_dir / "uncreated-session-id").exists())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_empty_state_when_db_has_zero_tables(self):
        conv_id = "empty-tables-session"
        db_file = self.brain_dir / conv_id / ".tables" / "session.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        conn.close()

        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/?conversationId={conv_id}", timeout=2) as resp:
                    self.assertEqual(resp.status, 200)
                    body = resp.read().decode("utf-8")
                    self.assertIn("No tables yet.", body)
                    self.assertEqual(resp.headers.get("Refresh"), "5")
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_scoping_resolves_to_root_conversation_db(self):
        # Multi-tier subagent lineage: root -> parent -> leaf
        self.write_mock_transcript("root-sess", "parent-sess")
        self.write_mock_transcript("parent-sess", "leaf-sess")

        root_db = self.brain_dir / "root-sess" / ".tables" / "session.db"
        root_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(root_db))
        conn.execute("CREATE TABLE root_items (id INT, name TEXT)")
        conn.commit()
        conn.close()

        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            resolved = inspect_server.resolve_request_db("leaf-sess")
            self.assertEqual(resolved, root_db)

            with table_tool.LINEAGE_CACHE_LOCK:
                self.assertEqual(table_tool.LINEAGE_CACHE.get("leaf-sess"), "root-sess")

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/?conversationId=leaf-sess", timeout=2) as resp:
                    self.assertEqual(resp.status, 200)
                    body = resp.read().decode("utf-8")
                    self.assertIn("root_items", body)
                    self.assertIn("/table?name=root_items&conversationId=leaf-sess", body)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_scoping_lineage_cache_acceleration(self):
        self.write_mock_transcript("cached-root", "cached-child")
        root_db = self.brain_dir / "cached-root" / ".tables" / "session.db"

        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            # First resolution reads transcripts and populates cache
            res1 = inspect_server.resolve_request_db("cached-child")
            self.assertEqual(res1, root_db)
            with table_tool.LINEAGE_CACHE_LOCK:
                self.assertEqual(table_tool.LINEAGE_CACHE.get("cached-child"), "cached-root")

            # Remove transcripts from disk completely
            transcript_file = self.brain_dir / "cached-root" / ".system_generated" / "logs" / "transcript.jsonl"
            transcript_file.unlink()

            # Second resolution uses cache, still resolves to cached-root
            res2 = inspect_server.resolve_request_db("cached-child")
            self.assertEqual(res2, root_db)

    def test_scoping_unrooted_orphan_conversation(self):
        orphan_id = "orphan-session-999"
        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            resolved = inspect_server.resolve_request_db(orphan_id)
            self.assertEqual(resolved, self.brain_dir / orphan_id / ".tables" / "session.db")


# ─── 4. Token Authentication Tests ───────────────────────────────────────────


class InspectTokenAuthTest(SessionScopingTestBase):
    """Verifies token-based authentication and exemption rules for inspect_server.py."""

    def setUp(self):
        super().setUp()
        self.orig_token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN")
        self.orig_db = inspect_server._STATE.get("db")
        inspect_server._STATE["db"] = None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.orig_token is not None:
            os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = self.orig_token
        else:
            os.environ.pop("ANTIGRAVITY_SIDECAR_UI_TOKEN", None)
        inspect_server._STATE["db"] = self.orig_db
        super().tearDown()

    def test_healthz_unauthenticated_without_token_env(self):
        os.environ.pop("ANTIGRAVITY_SIDECAR_UI_TOKEN", None)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"ok")

    def test_healthz_unauthenticated_when_token_env_is_set(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        # 1. No token provided
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"ok")
        # 2. Invalid token in query
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz?token=wrong", timeout=2) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"ok")
        # 3. Invalid token in header
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/healthz", headers={"X-Sidecar-Token": "wrong"}
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"ok")

    def test_missing_token_returns_403(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2)
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn("Forbidden", ctx.exception.read().decode())
        ctx.exception.close()

    def test_invalid_token_query_returns_403(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/?token=wrong-token", timeout=2)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_invalid_token_header_returns_403(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", headers={"X-Sidecar-Token": "wrong-token"}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_valid_token_query_returns_200(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/?token=secret-123", timeout=2) as r:
            self.assertEqual(r.status, 200)

    def test_valid_token_header_returns_200(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", headers={"X-Sidecar-Token": "secret-123"}
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)

    def test_token_query_param_precedence(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        # Query correct, header wrong -> 200
        req1 = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/?token=secret-123",
            headers={"X-Sidecar-Token": "wrong"},
        )
        with urllib.request.urlopen(req1, timeout=2) as r:
            self.assertEqual(r.status, 200)

        # Query wrong, header correct -> 403 (query param takes precedence)
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/?token=wrong",
            headers={"X-Sidecar-Token": "secret-123"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req2, timeout=2)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_token_empty_query_param_falls_back_to_header(self):
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = "secret-123"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/?token=", headers={"X-Sidecar-Token": "secret-123"}
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)

    def test_token_constant_time_comparison_utf8_emojis(self):
        token = "tok_🔑_rocket_🚀_euro_€_123"
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = token
        # Valid emoji token in query
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote(token)}", timeout=2
        ) as r:
            self.assertEqual(r.status, 200)
        # Valid emoji token in header
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", headers={"X-Sidecar-Token": token.encode("utf-8")}
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)
        # Invalid emoji token
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote('tok_🔑_wrong_❌')}",
                timeout=2,
            )
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_token_constant_time_comparison_latin1_and_symbols(self):
        token = "tok_café_über_ñ_!@#$%^&*()_+="
        os.environ["ANTIGRAVITY_SIDECAR_UI_TOKEN"] = token
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote(token)}", timeout=2
        ) as r:
            self.assertEqual(r.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote('tok_café_wrong')}",
                timeout=2,
            )
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_token_auth_disabled_when_env_unset(self):
        os.environ.pop("ANTIGRAVITY_SIDECAR_UI_TOKEN", None)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2) as r:
            self.assertEqual(r.status, 200)


# ─── 5. Parameter Propagation Tests ──────────────────────────────────────────


class InspectParamPropagationTest(SessionScopingTestBase):
    """Verifies that conversationId and token parameters thread through all internal links."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.execute("CREATE TABLE users (id INT, name TEXT, score INT)")
        for i in range(120):
            cls.conn.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"user_{i}", i * 10))
        cls.conn.execute(
            f'CREATE TABLE "{table_tool.JOBS_TABLE}" (job_table TEXT PRIMARY KEY, '
            f"source_table TEXT, template TEXT, model TEXT, status TEXT, created_at REAL, completed_at REAL)"
        )
        cls.conn.execute(
            f'INSERT INTO "{table_tool.JOBS_TABLE}" VALUES ("users_job", "users", "tmpl", "gpt", "active", 1.0, NULL)'
        )
        cls.conn.execute(
            'CREATE TABLE users_job (id INT, name TEXT, score INT, _task_status TEXT, '
            '_task_claimed_by TEXT, _task_claimed_at REAL, _task_lease_expires REAL, '
            '_task_attempts INT, _task_error TEXT, result TEXT)'
        )
        for i in range(25):
            st = "done" if i % 2 == 0 else "failed"
            cls.conn.execute(
                "INSERT INTO users_job VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    i,
                    f"u_{i}",
                    i,
                    st,
                    "worker_1",
                    10.0,
                    20.0,
                    1,
                    "err" if st == "failed" else None,
                    "res" if st == "done" else None,
                ),
            )
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        super().tearDownClass()

    def test_build_url_comprehensive(self):
        bq = {"conversationId": "conv-123", "token": "tok-456"}
        self.assertEqual(
            inspect_server.build_url("/", None, bq),
            "/?conversationId=conv-123&token=tok-456",
        )
        self.assertEqual(
            inspect_server.build_url("/table", {"name": "users"}, bq),
            "/table?name=users&conversationId=conv-123&token=tok-456",
        )
        self.assertEqual(
            inspect_server.build_url(
                "/job", {"name": "users_job", "status": "failed", "page": 2}, bq
            ),
            "/job?name=users_job&status=failed&page=2&conversationId=conv-123&token=tok-456",
        )
        self.assertEqual(
            inspect_server.build_url("/task", {"name": "users_job", "id": 5}, bq),
            "/task?name=users_job&id=5&conversationId=conv-123&token=tok-456",
        )
        # string base_query format
        self.assertEqual(
            inspect_server.build_url(
                "/table", {"name": "users"}, "conversationId=c_str&token=t_str"
            ),
            "/table?name=users&conversationId=c_str&token=t_str",
        )

    def test_build_url_empty_base(self):
        self.assertEqual(inspect_server.build_url("/", None, None), "/")
        self.assertEqual(inspect_server.build_url("/table", {"name": "users"}, {}), "/table?name=users")

    def test_index_route_propagates_params(self):
        bq = {"conversationId": "conv-idx", "token": "tok-idx"}
        status, htm = inspect_server.render_index(self.conn, base_query=bq)
        self.assertEqual(status, 200)
        hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', htm)
        self.assertGreater(len(hrefs), 0)
        for h in hrefs:
            self.assertIn("conversationId=conv-idx", h)
            self.assertIn("token=tok-idx", h)

    def test_table_route_propagates_params(self):
        bq = {"conversationId": "conv-tbl", "token": "tok-tbl"}
        status, htm = inspect_server.render_table(
            self.conn, "users", 1, 50, sort="score", direction="desc", base_query=bq
        )
        self.assertEqual(status, 200)
        hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', htm)
        self.assertGreater(len(hrefs), 0)
        for h in hrefs:
            self.assertIn("conversationId=conv-tbl", h)
            self.assertIn("token=tok-tbl", h)

    def test_job_route_propagates_params(self):
        bq = {"conversationId": "conv-job", "token": "tok-job"}
        status, htm = inspect_server.render_job(
            self.conn,
            "users_job",
            "all",
            1,
            10,
            sort="_task_status",
            direction="asc",
            base_query=bq,
        )
        self.assertEqual(status, 200)
        hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', htm)
        self.assertGreater(len(hrefs), 0)
        for h in hrefs:
            self.assertIn("conversationId=conv-job", h)
            self.assertIn("token=tok-job", h)

    def test_task_route_propagates_params(self):
        bq = {"conversationId": "conv-task", "token": "tok-task"}
        status, htm = inspect_server.render_task(self.conn, "users_job", 1, base_query=bq)
        self.assertEqual(status, 200)
        hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', htm)
        self.assertGreater(len(hrefs), 0)
        for h in hrefs:
            self.assertIn("conversationId=conv-task", h)
            self.assertIn("token=tok-task", h)

    def test_404_pages_propagate_params(self):
        bq = {"conversationId": "conv-404", "token": "tok-404"}
        for render in [
            inspect_server.render_table(self.conn, "nonexistent", 1, 50, base_query=bq),
            inspect_server.render_job(self.conn, "nonexistent", "all", 1, 50, base_query=bq),
            inspect_server.render_task(self.conn, "users_job", 99999, base_query=bq),
        ]:
            status, htm = render
            self.assertEqual(status, 404)
            hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', htm)
            for h in hrefs:
                self.assertIn("conversationId=conv-404", h)
                self.assertIn("token=tok-404", h)

    def test_http_index_links_carry_parameters(self):
        conv_id = "test-index-params"
        token = "tok-index-params"
        db_file = self.brain_dir / conv_id / ".tables" / "session.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE demo (id INT)")
        conn.commit()
        conn.close()

        orig_db = inspect_server._STATE.get("db")
        inspect_server._STATE["db"] = None
        with patch("table_tool.get_brain_dir", return_value=self.brain_dir):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                url = f"http://127.0.0.1:{port}/?conversationId={conv_id}&token={token}"
                with urllib.request.urlopen(url, timeout=2) as r:
                    body = r.read().decode("utf-8")
                    hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', body)
                    self.assertGreater(len(hrefs), 0)
                    for h in hrefs:
                        self.assertIn(f"conversationId={conv_id}", h)
                        self.assertIn(f"token={token}", h)
            finally:
                httpd.shutdown()
                httpd.server_close()
                inspect_server._STATE["db"] = orig_db

    def test_http_all_routes_link_crawler(self):
        conv_id = "crawl-conv-1"
        tok = "crawl-tok-1"
        session_db = self.brain_dir / conv_id / ".tables" / "session.db"
        session_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(session_db))
        try:
            conn.execute("CREATE TABLE users (id INT, name TEXT, score INT)")
            for i in range(60):
                conn.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"u_{i}", i))
            conn.execute(
                f'CREATE TABLE "{table_tool.JOBS_TABLE}" (job_table TEXT PRIMARY KEY, source_table TEXT, template TEXT, model TEXT, status TEXT, created_at REAL, completed_at REAL)'
            )
            conn.execute(
                f'INSERT INTO "{table_tool.JOBS_TABLE}" VALUES ("users_job", "users", "tmpl", "gpt", "active", 1.0, NULL)'
            )
            conn.execute(
                "CREATE TABLE users_job (id INT, name TEXT, score INT, _task_status TEXT, _task_claimed_by TEXT, _task_claimed_at REAL, _task_lease_expires REAL, _task_attempts INT, _task_error TEXT, result TEXT)"
            )
            conn.execute(
                "INSERT INTO users_job VALUES (1, 'u_1', 1, 'done', 'worker_1', 10.0, 20.0, 1, NULL, 'res')"
            )
            conn.commit()
        finally:
            conn.close()

        orig_db = inspect_server._STATE.get("db")
        inspect_server._STATE["db"] = None

        with patch("table_tool.get_brain_dir", return_value=self.brain_dir), patch.dict(
            os.environ, {"ANTIGRAVITY_SIDECAR_UI_TOKEN": tok}
        ):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                endpoints = [
                    f"/?conversationId={conv_id}&token={tok}",
                    f"/table?name=users&conversationId={conv_id}&token={tok}",
                    f"/table?name=users&page=2&size=50&conversationId={conv_id}&token={tok}",
                    f"/table?name=users&sort=score&dir=desc&conversationId={conv_id}&token={tok}",
                    f"/job?name=users_job&conversationId={conv_id}&token={tok}",
                    f"/task?name=users_job&id=1&conversationId={conv_id}&token={tok}",
                    f"/table?name=missing&conversationId={conv_id}&token={tok}",
                    f"/job?name=missing&conversationId={conv_id}&token={tok}",
                    f"/task?name=users_job&id=99999&conversationId={conv_id}&token={tok}",
                    f"/unregistered_url_path?conversationId={conv_id}&token={tok}",
                ]
                for ep in endpoints:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}{ep}", timeout=2) as r:
                            body = r.read().decode("utf-8")
                    except urllib.error.HTTPError as e:
                        body = e.read().decode("utf-8")
                        e.close()
                    hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', body)
                    self.assertGreater(len(hrefs), 0, f"No links found on {ep}")
                    for h in hrefs:
                        self.assertIn(
                            f"conversationId={conv_id}", h, f"Missing convId in link '{h}' on {ep}"
                        )
                        self.assertIn(
                            f"token={tok}", h, f"Missing token in link '{h}' on {ep}"
                        )
                    self.assertNotIn(
                        '<a href="/">', body, f"Bare root href='/' found in body on {ep}"
                    )
            finally:
                httpd.shutdown()
                httpd.server_close()
                inspect_server._STATE["db"] = orig_db


# ─── 6. Top Switcher Bar & Theme Sync Tests ──────────────────────────────────


class InspectSwitcherBarAndThemeTest(unittest.TestCase):
    """Verifies switcher bar rendering, sticky table CSS, and theme synchronization."""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        cls.conn.execute("CREATE TABLE authors (id INT, name TEXT)")
        cls.conn.execute("CREATE TABLE books (id INT, title TEXT)")
        cls.conn.execute(
            f'CREATE TABLE "{table_tool.JOBS_TABLE}" (job_table TEXT PRIMARY KEY, source_table TEXT, template TEXT, model TEXT, status TEXT, created_at REAL, completed_at REAL)'
        )
        cls.conn.execute(
            f'INSERT INTO "{table_tool.JOBS_TABLE}" VALUES ("books_job", "books", "t", "m", "active", 1.0, NULL)'
        )
        cls.conn.execute(
            "CREATE TABLE books_job (id INT, _task_status TEXT, _task_claimed_by TEXT, _task_claimed_at REAL, _task_lease_expires REAL, _task_attempts INT, _task_error TEXT, result TEXT)"
        )
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_switcher_pills_order_and_badges(self):
        sw = inspect_server.render_switcher(
            self.conn, active_route="/", base_query={"conversationId": "c1", "token": "t1"}
        )
        self.assertTrue(
            sw.startswith(
                '<nav class="switcher"><a class="pill active" aria-current="page" href="/?conversationId=c1&token=t1">All tables</a>'
            )
        )
        self.assertIn(
            '<a class="pill" href="/table?name=authors&conversationId=c1&token=t1">authors</a>', sw
        )
        self.assertIn(
            '<a class="pill" href="/table?name=books&conversationId=c1&token=t1">books</a>', sw
        )
        self.assertIn(
            '<a class="pill job-pill" href="/job?name=books_job&conversationId=c1&token=t1">books_job <span class="badge job">job</span></a>',
            sw,
        )
        self.assertNotIn(table_tool.JOBS_TABLE, sw)

    def test_switcher_active_states_per_route(self):
        # Index route
        sw_idx = inspect_server.render_switcher(self.conn, active_route="/")
        self.assertIn('<a class="pill active" aria-current="page" href="/">All tables</a>', sw_idx)
        self.assertNotIn('active" aria-current="page" href="/table?name=books"', sw_idx)

        # Table route
        sw_tbl = inspect_server.render_switcher(self.conn, active_route="/table", active_name="books")
        self.assertIn('<a class="pill" href="/">All tables</a>', sw_tbl)
        self.assertIn(
            '<a class="pill active" aria-current="page" href="/table?name=books">books</a>', sw_tbl
        )
        self.assertNotIn(
            '<a class="pill active" aria-current="page" href="/table?name=authors">authors</a>', sw_tbl
        )

        # Job route
        sw_job = inspect_server.render_switcher(self.conn, active_route="/job", active_name="books_job")
        self.assertIn(
            '<a class="pill job-pill active" aria-current="page" href="/job?name=books_job">books_job <span class="badge job">job</span></a>',
            sw_job,
        )

        # Task route
        sw_task = inspect_server.render_switcher(self.conn, active_route="/task", active_name="books_job")
        self.assertIn(
            '<a class="pill job-pill active" aria-current="page" href="/job?name=books_job">books_job <span class="badge job">job</span></a>',
            sw_task,
        )

    def test_switcher_empty_state_none_conn(self):
        sw = inspect_server.render_switcher(None, active_route="/", base_query={"conversationId": "c1"})
        self.assertEqual(
            sw,
            '<nav class="switcher"><a class="pill active" aria-current="page" href="/?conversationId=c1">All tables</a></nav>',
        )

    def test_sticky_headers_css_rules(self):
        style = inspect_server.STYLE
        self.assertIn("position: sticky;", style)
        self.assertIn("top: 0;", style)
        self.assertIn("z-index: 10;", style)
        self.assertIn("--th-bg", style)

    def test_horizontal_scroll_css_and_wrap_container(self):
        style = inspect_server.STYLE
        self.assertIn("overflow-x: auto;", style)
        self.assertIn(".wrap", style)
        self.assertIn(".switcher", style)
        self.assertIn("white-space: nowrap;", style)

        # Check render functions include wrap container
        status, htm_tbl = inspect_server.render_table(self.conn, "books", 1, 50)
        self.assertEqual(status, 200)
        self.assertIn('<div class="wrap"><table>', htm_tbl)

        status, htm_job = inspect_server.render_job(self.conn, "books_job", "all", 1, 50)
        self.assertEqual(status, 200)
        self.assertIn('<div class="wrap"><table>', htm_job)

    def test_theme_change_postmessage_listener(self):
        script = inspect_server.THEME_SCRIPT
        self.assertTrue(
            "addEventListener('message'" in script or 'addEventListener("message"' in script
        )
        self.assertIn("theme-change", script)
        self.assertIn("setProperty", script)
        self.assertIn("data-theme", script)
        self.assertIn("colorScheme", script)

        status, htm = inspect_server.render_index(self.conn)
        self.assertIn(script, htm)

    def test_compact_aux_pane_styling(self):
        style = inspect_server.STYLE
        self.assertIn("padding: 0.5rem 0.75rem;", style)
        self.assertIn("font: 12px/1.4", style)


# ─── 7. HTML Rendering Tests ─────────────────────────────────────────────────


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

    def test_table_sort_desc_orders_rows(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_table(conn, "items", 1, 50, "score", "desc")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn(">n119<", htm)                              # highest score first
        self.assertLess(htm.index(">n119<"), htm.index(">n118<"))
        self.assertNotIn(">n69<", htm)                            # page 1 desc = 119–70
        self.assertIn("▼", htm)

    def test_table_sort_toggle_link(self):
        conn = self.ro()
        try:
            _, htm = inspect_server.render_table(conn, "items", 1, 50, "score", "desc")
        finally:
            conn.close()
        self.assertIn("sort=score&dir=asc", htm)   # active column link toggles direction
        self.assertIn("sort=name&dir=asc", htm)    # inactive column links start asc
        self.assertNotIn("▲", htm)                 # only the active direction is marked

    def test_table_invalid_sort_falls_back_to_rowid(self):
        conn = self.ro()
        try:
            status, htm = inspect_server.render_table(
                conn, "items", 1, 50, 'score"; DROP TABLE items--', "up")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn(">n0<", htm)      # default rowid order, page 1
        self.assertNotIn(">n50<", htm)

    def test_sort_composes_with_pager(self):
        conn = self.ro()
        try:
            _, htm = inspect_server.render_table(conn, "items", 1, 50, "score", "desc")
        finally:
            conn.close()
        self.assertIn("sort=score&dir=desc&page=2", htm)  # Next link keeps the sort

    def test_job_sort_by_status(self):
        conn = self.ro()
        try:
            _, asc = inspect_server.render_job(conn, "items_job", "all", 1, 50,
                                               "_task_status", "asc")
            _, desc = inspect_server.render_job(conn, "items_job", "all", 1, 50,
                                                "_task_status", "desc")
        finally:
            conn.close()
        # asc: 'done' < 'failed' < 'pending' (the closing quote pins the id).
        self.assertLess(asc.index(f'id={self.done_id}"'), asc.index(f'id={self.failed_id}"'))
        # desc: page 1 is all pending tasks.
        self.assertNotIn(f'id={self.done_id}"', desc)

    def test_job_filter_links_carry_sort(self):
        conn = self.ro()
        try:
            _, htm = inspect_server.render_job(conn, "items_job", "all", 1, 50,
                                               "_task_attempts", "desc")
        finally:
            conn.close()
        self.assertIn("status=failed&size=50&sort=_task_attempts&dir=desc", htm)

    def test_refresh_script_on_live_pages(self):
        conn = self.ro()
        try:
            _, job = inspect_server.render_job(conn, "items_job", "all", 1, 50)
            _, index = inspect_server.render_index(conn)
            _, missing = inspect_server.render_table(conn, "nope", 1, 50)
        finally:
            conn.close()
        for htm in (job, index):
            self.assertIn('id="main"', htm)
            self.assertIn("visibilityState", htm)
            self.assertIn('id="auto"', htm)
        # Error pages never refresh.
        self.assertNotIn("visibilityState", missing)
        self.assertNotIn('id="auto"', missing)

    def test_default_urls_have_no_sort_params(self):
        conn = self.ro()
        try:
            _, table = inspect_server.render_table(conn, "items", 1, 50)
            _, job = inspect_server.render_job(conn, "items_job", "all", 1, 50)
        finally:
            conn.close()
        # Pager and filter links stay byte-identical to before sorting existed.
        self.assertIn('href="/table?name=items&size=50&page=2"', table)
        self.assertIn('href="/job?name=items_job&status=failed&size=50"', job)

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

    def test_build_url_preserves_conversation_and_token(self):
        base_query = {"conversationId": "conv-test-123", "token": "tok-test-456"}
        self.assertEqual(
            inspect_server.build_url("/", None, base_query),
            "/?conversationId=conv-test-123&token=tok-test-456",
        )
        self.assertEqual(
            inspect_server.build_url("/table", {"name": "items"}, base_query),
            "/table?name=items&conversationId=conv-test-123&token=tok-test-456",
        )
        self.assertEqual(
            inspect_server.build_url("/job", {"name": "items_job", "status": "failed"}, base_query),
            "/job?name=items_job&status=failed&conversationId=conv-test-123&token=tok-test-456",
        )


# ─── 8. HTTP Smoke Tests ─────────────────────────────────────────────────────


class InspectHttpSmokeTest(SessionScopingTestBase):
    """One real HTTP round-trip to confirm the server wiring."""

    def test_healthz_and_index_over_http(self):
        db = str(self.cwd_dir / "session.db")
        self.call_tool_sync("table_create", {"table": "t", "columns": {"a": "TEXT"}})

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

    def test_unrecognized_route_404_propagates_parameters(self):
        conv_id = "test-conv-404"
        token = "tok-secret-404"
        db_file = self.brain_dir / conv_id / ".tables" / "session.db"
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute("CREATE TABLE users (id INT, name TEXT)")
            conn.commit()
        finally:
            conn.close()

        orig_db = inspect_server._STATE.get("db")
        inspect_server._STATE["db"] = None
        with patch("table_tool.get_brain_dir", return_value=self.brain_dir), \
             patch.dict("os.environ", {"ANTIGRAVITY_SIDECAR_UI_TOKEN": token}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), inspect_server.Handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{port}/unrecognized_path?conversationId={conv_id}&token={token}"
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url, timeout=2)
                err = ctx.exception
                self.assertEqual(err.code, 404)
                body = err.read().decode("utf-8")
                err.close()
                self.assertIn("404", body)
                self.assertIn("Not found", body)

                hrefs = re.findall(r'<a\s+[^>]*href=["\']([^"\']*)["\']', body)
                self.assertGreaterEqual(len(hrefs), 2, "Expected multiple navigation links on 404 page")

                for href in hrefs:
                    self.assertIn(
                        f"conversationId={conv_id}",
                        href,
                        f"Link '{href}' on 404 page is missing conversationId",
                    )
                    self.assertIn(
                        f"token={token}",
                        href,
                        f"Link '{href}' on 404 page is missing token",
                    )

                self.assertNotIn('<a href="/">', body, "404 page contains hardcoded unparameterized href='/' link")
            finally:
                httpd.shutdown()
                httpd.server_close()
                inspect_server._STATE["db"] = orig_db


if __name__ == "__main__":
    unittest.main()
