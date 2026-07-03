#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
#     "anyio",
# ]
# ///
"""
Adversarial integration and unit test suite targeting untested code paths,
error handling blocks, exception handlers, and edge cases in server.py.
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import server
import table_tool


class AdversarialServerTest(unittest.TestCase):
    def setUp(self):
        # Create a mock brain directory
        self.temp_brain_dir = tempfile.TemporaryDirectory()
        self.brain_dir = Path(self.temp_brain_dir.name).resolve()

        # Create a mock CWD directory to avoid polluting actual workspace CWD
        self.temp_cwd_dir = tempfile.TemporaryDirectory()
        self.cwd_dir = Path(self.temp_cwd_dir.name).resolve()
        self.old_cwd = os.getcwd()
        os.chdir(self.cwd_dir)

        # Default env setup (start with clean env, override in specific tests)
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
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

    def call_tool_sync(self, name: str, arguments: dict) -> dict:
        """Helper to run the async call_tool in a synchronous runner."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        res = loop.run_until_complete(server.call_tool(name, arguments))
        return json.loads(res[0].text)

    def write_mock_transcript_raw(self, parent_id: str, lines: list[str]) -> Path:
        """Helper to create a raw transcript file with specific lines."""
        log_dir = self.brain_dir / parent_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return transcript_path

    def test_get_brain_dir(self):
        """Verify the unpatched get_brain_dir() function returns the correct home path structure."""
        res = server.get_brain_dir()
        self.assertTrue(isinstance(res, Path))
        self.assertTrue(res.parts[-3:] == (".gemini", "antigravity", "brain"))

    def test_find_parent_conversation_empty_child_or_invalid_brain(self):
        """Verify find_parent_conversation handles empty IDs or non-existent brain directories gracefully."""
        # Empty child_id
        res = server.find_parent_conversation("", self.brain_dir)
        self.assertIsNone(res)
        
        # brain_dir is not a directory
        non_existent_dir = self.brain_dir / "does_not_exist"
        res = server.find_parent_conversation("child-1", non_existent_dir)
        self.assertIsNone(res)

    def test_find_parent_conversation_non_directory_or_no_transcript(self):
        """Verify find_parent_conversation ignores files in brain_dir or directories lacking transcripts."""
        # Create a file in brain_dir (should be skipped since it's not a directory)
        some_file = self.brain_dir / "some_file.txt"
        some_file.write_text("hello", encoding="utf-8")
        
        # Create a directory lacking transcript.jsonl
        empty_session_dir = self.brain_dir / "empty_session"
        empty_session_dir.mkdir()
        
        res = server.find_parent_conversation("child-1", self.brain_dir)
        self.assertIsNone(res)

    def test_find_parent_conversation_invalid_json_types(self):
        """Verify find_parent_conversation skips non-dict JSON entries or incorrect event type steps."""
        lines = [
            "[1, 2, 3]", # JSON array (non-dict)
            '{"step_index": 1}', # missing type
            '{"step_index": 2, "type": null, "content": "child-1"}', # null type
            '{"step_index": 3, "type": 123, "content": "child-1"}', # int type
            '{"step_index": 4, "type": "OTHER_STEP", "content": "child-1"}' # wrong type
        ]
        self.write_mock_transcript_raw("parent-session", lines)
        res = server.find_parent_conversation("child-1", self.brain_dir)
        self.assertIsNone(res)

    def test_find_parent_conversation_non_string_content(self):
        """Verify find_parent_conversation handles non-string content values by converting them to JSON string."""
        lines = [
            '{"step_index": 1, "type": "INVOKE_SUBAGENT", "content": {"conversationId": "child-1"}}',
            '{"step_index": 2, "type": "INVOKE_SUBAGENT", "content": ["child-1"]}',
            '{"step_index": 3, "type": "INVOKE_SUBAGENT", "content": null}'
        ]
        self.write_mock_transcript_raw("parent-session", lines)
        
        # With list/dict serialization, child-1 is extracted and matched
        res = server.find_parent_conversation("child-1", self.brain_dir)
        self.assertEqual(res, "parent-session")

    def test_find_parent_conversation_robust_fallback_strip(self):
        """Verify the robust fallback handles exact substring match with whitespace strip."""
        lines = [
            '{"step_index": 1, "type": "INVOKE_SUBAGENT", "content": " $child "}'
        ]
        self.write_mock_transcript_raw("parent-session", lines)
        res = server.find_parent_conversation("$child", self.brain_dir)
        self.assertEqual(res, "parent-session")

    def test_find_parent_conversation_robust_fallback_quotes(self):
        """Verify the robust fallback handles exact substring match wrapped in quotes."""
        lines = [
            '{"step_index": 1, "type": "INVOKE_SUBAGENT", "content": "\\"$child\\""}'
        ]
        self.write_mock_transcript_raw("parent-session-2", lines)
        res = server.find_parent_conversation("$child", self.brain_dir)
        self.assertEqual(res, "parent-session-2")

    @patch("pathlib.Path.mkdir")
    def test_get_resolved_db_path_mkdir_exception(self, mock_mkdir):
        """Verify get_resolved_db_path handles directory creation exceptions gracefully."""
        mock_mkdir.side_effect = PermissionError("Permission denied")
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "session-1"
        with patch("server.get_brain_dir", return_value=self.brain_dir):
            db_path = server.get_resolved_db_path()
            # It should not crash, and return the path inside the target tables directory anyway
            self.assertEqual(db_path, str(self.brain_dir / "session-1" / ".tables" / "session.db"))

    def test_dispatch_unknown_tool_and_key_errors(self):
        """Verify dispatch throws ValueError for unknown tools and handles missing required argument keys."""
        with patch("server.get_resolved_db_path", return_value=str(self.cwd_dir / "session.db")):
            # Unknown tool
            with self.assertRaises(ValueError):
                server.dispatch("unknown_tool", {})
                
            # Key errors (missing columns in table_create)
            with self.assertRaises(KeyError):
                server.dispatch("table_create", {"table": "t"})

    def test_call_tool_unknown_tool_returns_error(self):
        """Verify call_tool intercepts dispatch errors and returns them as an error dict payload."""
        res = self.call_tool_sync("unknown_tool", {})
        self.assertIn("error", res)
        self.assertIn("Unknown tool", res["error"])

    @patch("server.stdio_server")
    @patch("server.server.run")
    def test_main_startup(self, mock_server_run, mock_stdio_server):
        """Verify main() sets up stdio streams and runs the MCP server."""
        mock_read = MagicMock()
        mock_write = MagicMock()
        mock_stdio_server.return_value.__aenter__.return_value = (mock_read, mock_write)
        
        asyncio.run(server.main())
        mock_server_run.assert_called_once_with(mock_read, mock_write, server.server.create_initialization_options())


if __name__ == "__main__":
    unittest.main(verbosity=2)
