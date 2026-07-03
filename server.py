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


def get_brain_dir() -> Path:
    """Get the brain directory path where conversation sessions are stored."""
    return Path.home() / ".gemini" / "antigravity" / "brain"


def find_parent_conversation(child_id: str, brain_dir: Path) -> str | None:
    """
    Scans the directories in brain_dir for transcript.jsonl logs.
    Identifies if a parent conversation has invoked the given child_id.
    """
    if not child_id or not brain_dir.is_dir():
        return None

    # Check for word boundary only if boundary character is alphanumeric/underscore
    def matches_boundary(target_id: str, text: str) -> bool:
        escaped = re.escape(target_id)
        if target_id and (target_id[0].isalnum() or target_id[0] == '_'):
            start = r'\b'
        else:
            start = r'(?<!\w)'

        if target_id and (target_id[-1].isalnum() or target_id[-1] == '_'):
            end = r'\b'
        else:
            end = r'(?!\w)'

        pattern = start + escaped + end
        return bool(re.search(pattern, text))

    for session_dir in brain_dir.iterdir():
        try:
            if not session_dir.is_dir():
                continue
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


def get_resolved_db_path() -> str:
    """
    Resolves the SQLite database path based on environment variables and lineage tracing.
    If ANTIGRAVITY_CONVERSATION_ID is missing or empty, falls back to session.db in CWD.
    """
    conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    if not conv_id:
        return str(Path(os.getcwd()) / "session.db")

    brain_dir = get_brain_dir()
    root_id = find_root_conversation(conv_id, brain_dir)
    tables_dir = brain_dir / root_id / ".tables"
    try:
        tables_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    return str(tables_dir / "session.db")


# Schemas are declared verbatim (rather than generated from type hints) to
# stay byte-identical with the ones the Node server advertised.
TOOLS = [
    types.Tool(
        name="table_create",
        description="Create a new SQLite table. The database file is created automatically if it doesn't exist.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
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
            "properties": {},
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
                "table": {"description": "Table to drop", "type": "string"},
            },
            "required": ["table"],
        },
    ),
]


def dispatch(name: str, args: dict) -> dict:
    db_path = get_resolved_db_path()

    if name == "table_create":
        table_name = args.get("table")
        if not table_name:
            raise table_tool.TableToolError("Table name cannot be empty")
        spec = {"columns": args["columns"]}
        for key in ("primary_key", "unique", "if_not_exists"):
            if args.get(key) is not None:
                spec[key] = args[key]
        return table_tool.op_create_table(db_path, table_name, spec)

    elif name == "table_insert":
        return table_tool.op_insert(db_path, args["table"], {"rows": args["rows"]})

    elif name == "table_join":
        spec = {k: v for k, v in args.items() if k not in ("output_table",)}
        return table_tool.op_join(db_path, args["output_table"], spec)

    elif name == "table_group_by":
        limit = args.get("limit")
        if limit is not None and limit < 0:
            raise table_tool.TableToolError("Limit must be non-negative")
        spec = {k: v for k, v in args.items() if k not in ("table",)}
        return table_tool.op_group(db_path, args["table"], spec)

    elif name == "table_run_sql":
        sql = args.get("sql")
        if not sql or not sql.strip():
            raise table_tool.TableToolError("SQL query cannot be empty")
        return table_tool.op_query(db_path, sql)

    elif name == "table_schema":
        table_name = args.get("table")
        if table_name == "" or (table_name is not None and not isinstance(table_name, str)):
            return {"columns": {}}
        return table_tool.op_schema(db_path, table_name)

    elif name == "table_list":
        return table_tool.op_tables(db_path)

    elif name == "table_drop":
        # Allow empty name to proceed to SQLite drop command
        return table_tool.op_drop(db_path, args["table"])

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
    except Exception as e:
        # Same behavior as the Node server: errors come back as a
        # normal JSON payload, not a protocol-level tool error.
        result = {"error": str(e)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
