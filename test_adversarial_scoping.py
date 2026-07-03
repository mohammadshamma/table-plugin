#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
#     "anyio",
# ]
# ///
"""
Adversarial test suite targeting coverage gaps and boundary edge cases in server.py.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import server
import table_tool


class TestAdversarialScoping(unittest.TestCase):
    def setUp(self):
        # Create a mock brain directory
        self.temp_brain_dir = tempfile.TemporaryDirectory()
        self.brain_dir = Path(self.temp_brain_dir.name).resolve()

        # Patch server's get_brain_dir function
        self.brain_patcher = patch("server.get_brain_dir", return_value=self.brain_dir, create=True)
        self.mock_get_brain_dir = self.brain_patcher.start()

        # Clean env setup
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()

    def tearDown(self):
        self.brain_patcher.stop()
        self.env_patcher.stop()
        try:
            self.temp_brain_dir.cleanup()
        except Exception:
            pass

    def test_find_parent_invalid_inputs(self):
        # Line 208: Empty child_id or nonexistent brain_dir
        self.assertIsNone(server.find_parent_conversation("", self.brain_dir))
        self.assertIsNone(server.find_parent_conversation("child", Path("/nonexistent/dir/xyz")))

    def test_find_parent_non_directory_child(self):
        # Line 215: Non-directory child under brain_dir should be skipped
        file_path = self.brain_dir / "regular_file.txt"
        file_path.write_text("not a directory")
        self.assertIsNone(server.find_parent_conversation("child", self.brain_dir))

    def test_find_parent_missing_transcript(self):
        # Line 219: Directory with missing transcript file should be skipped
        empty_session = self.brain_dir / "empty-session"
        empty_session.mkdir()
        self.assertIsNone(server.find_parent_conversation("child", self.brain_dir))

    def test_find_parent_corrupt_entries(self):
        # Lines 226 & 234: Empty lines and non-dict JSON entries
        parent_dir = self.brain_dir / "parent-session"
        log_dir = parent_dir / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"

        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write("\n")  # Empty line (Line 226)
            f.write("[1, 2, 3]\n")  # Non-dict JSON list (Line 234)
            f.write('"just a string"\n')  # Non-dict JSON string
            f.write('{"type": "INVOKE_SUBAGENT", "content": "child-session"}\n')  # Valid match

        res = server.find_parent_conversation("child-session", self.brain_dir)
        self.assertEqual(res, "parent-session")

    def test_find_parent_non_string_content(self):
        # Line 243: content is not a string (e.g. structured dict/list)
        parent_dir = self.brain_dir / "parent-session"
        log_dir = parent_dir / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"

        entry = {
            "type": "INVOKE_SUBAGENT",
            "content": {
                "subagents": ["child-session"]
            }
        }
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        res = server.find_parent_conversation("child-session", self.brain_dir)
        self.assertEqual(res, "parent-session")

    def test_find_parent_robust_fallback(self):
        # Lines 249-250: leading/trailing non-word characters in child_id (triggering robust fallback check)
        child_id = "-child-session-"
        parent_dir = self.brain_dir / "parent-session"
        log_dir = parent_dir / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = log_dir / "transcript.jsonl"

        # The pattern search (regex) might fail because child_id starts/ends with a non-word char,
        # but the fallback check `f'"{child_id}"' in content` will succeed.
        entry = {
            "type": "INVOKE_SUBAGENT",
            "content": f'Invoked subagent with ID "{child_id}"'
        }
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        res = server.find_parent_conversation(child_id, self.brain_dir)
        self.assertEqual(res, "parent-session")

    def test_get_resolved_db_path_mkdir_exception(self):
        # Lines 290-292: exception handling in mkdir
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "session_1"
        
        # Mock mkdir to raise PermissionError
        with patch.object(Path, "mkdir", side_effect=PermissionError("Permission denied")):
            db_path = server.get_resolved_db_path()
            # It should catch the exception, not crash, and return the resolved path
            self.assertEqual(db_path, str(self.brain_dir / "session_1" / ".tables" / "session.db"))

    def test_list_tools_coverage(self):
        # Line 346: call list_tools directly
        res = asyncio.run(server.list_tools())
        self.assertEqual(res, server.TOOLS)
        self.assertTrue(any(t.name == "table_create" for t in res))


if __name__ == "__main__":
    unittest.main()
