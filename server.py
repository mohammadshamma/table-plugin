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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import table_tool

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

DB_ARG = {"description": "Path to the SQLite database file", "type": "string"}

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
                "db": DB_ARG,
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
            "required": ["db", "table", "columns"],
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
                "db": DB_ARG,
                "table": {"description": "Table to insert into", "type": "string"},
                "rows": {
                    "description": 'Array of row objects, e.g. [{"name": "Alice", "age": 30}]',
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": {}},
                },
            },
            "required": ["db", "table", "rows"],
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
                "db": DB_ARG,
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
            "required": ["db", "output_table", "left", "right"],
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
                "db": DB_ARG,
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
            "required": ["db", "table", "by", "aggs"],
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
                "db": DB_ARG,
                "sql": {"description": "SQL statement to execute", "type": "string"},
            },
            "required": ["db", "sql"],
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
                "db": DB_ARG,
                "table": {"description": "Table name (omit to get all tables)", "type": "string"},
            },
            "required": ["db"],
        },
    ),
    types.Tool(
        name="table_list",
        description="List all tables in a SQLite database.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {"db": DB_ARG},
            "required": ["db"],
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
                "db": DB_ARG,
                "table": {"description": "Table to drop", "type": "string"},
            },
            "required": ["db", "table"],
        },
    ),
]


def dispatch(name: str, args: dict) -> dict:
    if name == "table_create":
        spec = {"columns": args["columns"]}
        for key in ("primary_key", "unique", "if_not_exists"):
            if args.get(key):
                spec[key] = args[key]
        return table_tool.op_create_table(args["db"], args["table"], spec)
    if name == "table_insert":
        return table_tool.op_insert(args["db"], args["table"], {"rows": args["rows"]})
    if name == "table_join":
        spec = {k: v for k, v in args.items() if k not in ("db", "output_table")}
        return table_tool.op_join(args["db"], args["output_table"], spec)
    if name == "table_group_by":
        spec = {k: v for k, v in args.items() if k not in ("db", "table")}
        return table_tool.op_group(args["db"], args["table"], spec)
    if name == "table_run_sql":
        return table_tool.op_query(args["db"], args["sql"])
    if name == "table_schema":
        return table_tool.op_schema(args["db"], args.get("table"))
    if name == "table_list":
        return table_tool.op_tables(args["db"])
    if name == "table_drop":
        return table_tool.op_drop(args["db"], args["table"])
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
        # Same behavior as the Node server: table_tool errors come back as a
        # normal JSON payload, not a protocol-level tool error.
        result = {"error": str(e)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
