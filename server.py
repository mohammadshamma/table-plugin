#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp==1.28.1",
# ]
# ///
"""
table MCP Server
Exposes SQLite operations as MCP tools over stdio.
Imports table_tool.py directly for the actual database operations.

Run with: uv run server.py  (uv provisions Python and the mcp SDK)
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import table_tool

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


import threading

# Thread-safe in-memory cache for conversation_id -> root_conversation_id
LINEAGE_CACHE = {}
LINEAGE_CACHE_LOCK = threading.Lock()


def get_brain_dir() -> Path:
    """Get the brain directory path where conversation sessions are stored."""
    return Path.home() / ".gemini" / "antigravity" / "brain"


def get_scratch_dir() -> Path:
    """Get the scratch directory path."""
    return Path.home() / ".gemini" / "antigravity" / "scratch"


def find_parent_conversation(child_id: str, brain_dir: Path) -> str | None:
    """
    Scans the directories in brain_dir for transcript.jsonl logs.
    Identifies if a parent conversation has invoked the given child_id.
    """
    if not child_id or not brain_dir.is_dir():
        return None

    # Check for exact UUID boundary (UUIDs consist of hex chars and dashes)
    def matches_boundary(target_id: str, text: str) -> bool:
        escaped = re.escape(target_id)
        pattern = rf"(?<![a-f0-9-])" + escaped + rf"(?![a-f0-9-])"
        return bool(re.search(pattern, text, re.IGNORECASE))

    # Sort brain_dir subdirectories by modification time (newest first) for determinism
    try:
        subdirs = sorted(
            [d for d in brain_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True
        )
    except Exception:
        subdirs = [d for d in brain_dir.iterdir() if d.is_dir()]

    for session_dir in subdirs:
        try:
            transcript_path = session_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not transcript_path.is_file():
                continue
        except Exception:
            continue

        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    if not isinstance(entry, dict):
                        continue

                    if entry.get("type") != "INVOKE_SUBAGENT":
                        continue

                    content = entry.get("content")
                    if isinstance(content, (dict, list)):
                        content_str = json.dumps(content)
                    elif content is None:
                        content_str = "null"
                    else:
                        content_str = str(content)

                    if matches_boundary(child_id, content_str):
                        return session_dir.name
        except Exception:
            continue

    return None


def find_root_conversation(current_id: str, brain_dir: Path) -> str:
    """Recursively traces parent conversation IDs up to the ultimate root conversation."""
    visited = set()
    curr = current_id
    while curr:
        if curr in visited:
            break
        visited.add(curr)
        parent = find_parent_conversation(curr, brain_dir)
        if not parent:
            break
        curr = parent
    return curr


def is_writable(path: Path) -> bool:
    """Helper to check if a directory is writable for SQLite databases."""
    import sqlite3
    test_db = path / ".test_write.db"
    try:
        conn = sqlite3.connect(test_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        test_db.unlink(missing_ok=True)
        # Also clean up WAL files if any
        Path(str(test_db) + "-wal").unlink(missing_ok=True)
        Path(str(test_db) + "-shm").unlink(missing_ok=True)
        return True
    except Exception:
        try:
            test_db.unlink(missing_ok=True)
            Path(str(test_db) + "-wal").unlink(missing_ok=True)
            Path(str(test_db) + "-shm").unlink(missing_ok=True)
        except Exception:
            pass
        return False


def get_resolved_db_path(args: dict) -> str:
    """
    Resolves the SQLite database path based on the passed conversation_id,
    falling back to environment variables, and tracing back to the root session.
    If no ID is available, falls back to a writable scratch directory.
    """
    conv_id = args.get("conversation_id")
    if not conv_id:
        conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")

    if conv_id:
        # Validate conversation_id to prevent path traversal / prompt injection
        if not re.match(r"^[a-zA-Z0-9_-]+$", conv_id):
            raise ValueError("Invalid conversation_id format")

    if not conv_id:
        cwd = Path(os.getcwd())
        if is_writable(cwd) and cwd != Path("/"):
            return str(cwd / "session.db")
        else:
            # Fallback to a guaranteed writable path in the user's home scratch dir
            scratch_dir = get_scratch_dir()
            try:
                scratch_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            return str(scratch_dir / "session.db")

    brain_dir = get_brain_dir()

    # Check cache
    with LINEAGE_CACHE_LOCK:
        cached_root = LINEAGE_CACHE.get(conv_id)

    if cached_root:
        root_id = cached_root
    else:
        root_id = find_root_conversation(conv_id, brain_dir)
        # Cache only positive resolutions (where root is different from the request ID)
        # to avoid freezing a race-condition first-call miss
        if root_id and root_id != conv_id:
            with LINEAGE_CACHE_LOCK:
                LINEAGE_CACHE[conv_id] = root_id

    tables_dir = brain_dir / root_id / ".tables"
    try:
        tables_dir.mkdir(parents=True, exist_ok=True)
        db_path = tables_dir / "session.db"
    except Exception:
        # Fallback to scratch_dir if mkdir fails
        scratch_dir = get_scratch_dir()
        try:
            scratch_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        db_path = scratch_dir / "session.db"

    return str(db_path)


# Schemas are declared verbatim (rather than generated from type hints) to
# stay byte-identical with the ones the Node server advertised.
CONV_ID_ARG = {"description": "Optional conversation ID to scope the tables", "type": "string"}

TOOLS = [
    types.Tool(
        name="table_create",
        description="Create a new SQLite table. The database file is created automatically if it doesn't exist.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Name of the table to create", "type": "string"},
                "columns": {
                    "description": 'Object mapping column names to SQL types, e.g. {"name": "TEXT NOT NULL", "age": "INTEGER"}',
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "primary_key": {
                    "description": "Column name for an auto-incrementing INTEGER PRIMARY KEY (optional)",
                    "type": "string",
                },
                "unique": {
                    "description": "List of columns that should have UNIQUE constraints",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "if_not_exists": {
                    "description": "If true, don't error if the table already exists",
                    "type": "boolean",
                },
            },
            "required": ["table", "columns"],
        },
    ),
    types.Tool(
        name="table_insert",
        description="Insert one or more rows into an existing SQLite table.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Table to insert into", "type": "string"},
                "rows": {
                    "description": 'Array of row objects, e.g. [{"name": "Alice", "age": 30}]',
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": {}},
                },
            },
            "required": ["table", "rows"],
        },
    ),
    types.Tool(
        name="table_join",
        description="Join two SQLite tables and store the result in a new table.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "output_table": {"description": "Name for the new joined table", "type": "string"},
                "left": {"description": "Left table name", "type": "string"},
                "right": {"description": "Right table name", "type": "string"},
                "on": {
                    "description": "Join key if the column name is the same in both tables",
                    "type": "string",
                },
                "on_left": {
                    "description": "Left table join column (use with on_right)",
                    "type": "string",
                },
                "on_right": {
                    "description": "Right table join column (use with on_left)",
                    "type": "string",
                },
                "type": {
                    "description": "Join type (default: inner)",
                    "type": "string",
                    "enum": ["inner", "left", "cross"],
                },
                "select": {
                    "description": 'Columns to select, e.g. ["users.name", "orders.total"]',
                    "type": "array",
                    "items": {"type": "string"},
                },
                "if_not_exists": {"type": "boolean"},
            },
            "required": ["output_table", "left", "right"],
        },
    ),
    types.Tool(
        name="table_group_by",
        description="Group rows by one or more columns with aggregation functions. Optionally save results to a new table.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Table to group", "type": "string"},
                "by": {
                    "description": "Columns to group by",
                    "type": "array",
                    "items": {"type": "string"},
                },
                "aggs": {
                    "description": 'Aggregations: {"alias": "SQL_EXPR"}, e.g. {"count": "COUNT(*)", "total": "SUM(amount)"}',
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "having": {"description": "HAVING clause, e.g. COUNT(*) > 5", "type": "string"},
                "order_by": {"description": "ORDER BY clause, e.g. count DESC", "type": "string"},
                "limit": {"description": "Max rows to return", "type": "number"},
                "into": {"description": "If set, save results into this new table", "type": "string"},
            },
            "required": ["table", "by", "aggs"],
        },
    ),
    types.Tool(
        name="table_run_sql",
        description="Run an arbitrary SQL query and return results as JSON. Use for SELECT, UPDATE, DELETE, or any SQL not covered by other tools.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "sql": {"description": "SQL statement to execute", "type": "string"},
            },
            "required": ["sql"],
        },
    ),
    types.Tool(
        name="table_schema",
        description="Get the schema of a specific table or all tables in the database.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Table name (omit to get all tables)", "type": "string"},
            },
        },
    ),
    types.Tool(
        name="table_list",
        description="List all tables in a SQLite database.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
            },
        },
    ),
    types.Tool(
        name="table_drop",
        description="Drop (delete) a table from the database.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Table to drop", "type": "string"},
            },
            "required": ["table"],
        },
    ),
]


def dispatch(name: str, args: dict) -> dict:
    db_path = get_resolved_db_path(args)

    if name == "table_create":
        spec = {"columns": args["columns"]}
        for key in ("primary_key", "unique", "if_not_exists"):
            if args.get(key) is not None:
                spec[key] = args[key]
        return table_tool.op_create_table(db_path, args.get("table"), spec)

    elif name == "table_insert":
        return table_tool.op_insert(db_path, args.get("table"), {"rows": args["rows"]})

    elif name == "table_join":
        spec = {k: v for k, v in args.items() if k not in ("output_table", "conversation_id")}
        return table_tool.op_join(db_path, args.get("output_table"), spec)

    elif name == "table_group_by":
        spec = {k: v for k, v in args.items() if k not in ("table", "conversation_id")}
        return table_tool.op_group(db_path, args.get("table"), spec)

    elif name == "table_run_sql":
        return table_tool.op_query(db_path, args.get("sql"))

    elif name == "table_schema":
        table_name = args.get("table")
        if table_name == "":
            return table_tool.op_schema(db_path, None)
        return table_tool.op_schema(db_path, table_name)

    elif name == "table_list":
        return table_tool.op_tables(db_path)

    elif name == "table_drop":
        return table_tool.op_drop(db_path, args.get("table"))

    else:
        raise ValueError(f"Unknown tool: {name}")


server = Server("table", version="0.0.3")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = dispatch(name, arguments)
    except table_tool.TableToolError as e:
        # Expected errors from table_tool operations
        result = {"error": str(e)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
