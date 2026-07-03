#!/usr/bin/env python3
"""
table_tool - A JSON-driven SQLite CLI designed for LLM integration.

Usage:
    table_tool create-table <db> <table> <json>
    table_tool insert <db> <table> <json>
    table_tool join <db> <output_table> <json>
    table_tool group <db> <table> <json>
    table_tool query <db> <sql>
    table_tool schema <db> [<table>]
    table_tool tables <db>
    table_tool drop <db> <table>

All data exchange uses JSON for easy LLM generation and parsing.
Output is always JSON to stdout for easy consumption.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def output(data: dict | list) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    print()


def error(msg: str, code: int = 1) -> None:
    output({"error": msg})
    sys.exit(code)


def parse_json_arg(raw: str) -> dict:
    """Parse JSON from argument or stdin if '-'."""
    if raw == "-":
        raw = sys.stdin.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON: {e}")


# ─── Commands ────────────────────────────────────────────────────────────────


def cmd_create_table(args):
    """
    Create a table.

    JSON format:
    {
        "columns": {
            "name": "TEXT",
            "age": "INTEGER",
            "email": "TEXT NOT NULL"
        },
        "primary_key": "id",          // optional, auto-creates INTEGER PRIMARY KEY
        "unique": ["email"],           // optional
        "if_not_exists": true          // optional, default false
    }
    """
    spec = parse_json_arg(args.json)
    columns = spec.get("columns")
    if not columns or not isinstance(columns, dict):
        error("'columns' is required and must be an object mapping column names to types")

    col_defs = []

    # Optional auto primary key
    pk = spec.get("primary_key")
    if pk:
        col_defs.append(f'"{pk}" INTEGER PRIMARY KEY AUTOINCREMENT')

    for col_name, col_type in columns.items():
        if col_name == pk:
            continue  # already handled
        col_defs.append(f'"{col_name}" {col_type}')

    # Unique constraints
    for ucol in spec.get("unique", []):
        col_defs.append(f"UNIQUE({ucol})")

    exists_clause = "IF NOT EXISTS " if spec.get("if_not_exists") else ""
    sql = f'CREATE TABLE {exists_clause}"{args.table}" (\n  {",".join(col_defs)}\n)'

    conn = connect(args.db)
    try:
        conn.execute(sql)
        conn.commit()
        output({"ok": True, "table": args.table, "sql": sql})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_insert(args):
    """
    Insert rows into a table.

    JSON format:
    {
        "rows": [
            {"name": "Alice", "age": 30, "email": "alice@example.com"},
            {"name": "Bob", "age": 25, "email": "bob@example.com"}
        ]
    }
    """
    spec = parse_json_arg(args.json)
    rows = spec.get("rows")
    if not rows or not isinstance(rows, list):
        error("'rows' is required and must be a list of objects")

    conn = connect(args.db)
    try:
        inserted = 0
        for row in rows:
            cols = ", ".join(f'"{k}"' for k in row.keys())
            placeholders = ", ".join("?" for _ in row)
            sql = f'INSERT INTO "{args.table}" ({cols}) VALUES ({placeholders})'
            conn.execute(sql, list(row.values()))
            inserted += 1
        conn.commit()
        output({"ok": True, "table": args.table, "inserted": inserted})
    except sqlite3.Error as e:
        conn.rollback()
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_join(args):
    """
    Join two tables and store the result in a new table.

    JSON format:
    {
        "left": "users",
        "right": "orders",
        "on": "user_id",                        // simple key (same name in both)
        "on_left": "id", "on_right": "user_id", // or explicit left/right keys
        "type": "inner",                         // inner | left | cross
        "select": ["users.name", "orders.total"],// optional, default *
        "if_not_exists": true                    // optional
    }
    """
    spec = parse_json_arg(args.json)
    left = spec.get("left")
    right = spec.get("right")
    if not left or not right:
        error("'left' and 'right' table names are required")

    join_type = spec.get("type", "inner").upper()
    if join_type not in ("INNER", "LEFT", "CROSS"):
        error("'type' must be one of: inner, left, cross")

    # Build ON clause
    if "on" in spec:
        on_clause = f'"{left}"."{spec["on"]}" = "{right}"."{spec["on"]}"'
    elif "on_left" in spec and "on_right" in spec:
        on_clause = f'"{left}"."{spec["on_left"]}" = "{right}"."{spec["on_right"]}"'
    else:
        if join_type == "CROSS":
            on_clause = None
        else:
            error("'on' or both 'on_left'/'on_right' are required (except for cross joins)")

    select_cols = ", ".join(spec.get("select", ["*"]))
    exists_clause = "IF NOT EXISTS " if spec.get("if_not_exists") else ""

    sql = f'CREATE TABLE {exists_clause}"{args.output_table}" AS SELECT {select_cols} FROM "{left}" {join_type} JOIN "{right}"'
    if on_clause:
        sql += f" ON {on_clause}"

    conn = connect(args.db)
    try:
        conn.execute(sql)
        conn.commit()
        count = conn.execute(f'SELECT COUNT(*) FROM "{args.output_table}"').fetchone()[0]
        output({"ok": True, "table": args.output_table, "rows": count, "sql": sql})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_group(args):
    """
    Group by columns with aggregations. Returns results as JSON.

    JSON format:
    {
        "by": ["department"],
        "aggs": {
            "headcount": "COUNT(*)",
            "avg_salary": "AVG(salary)",
            "max_salary": "MAX(salary)"
        },
        "having": "COUNT(*) > 5",       // optional
        "order_by": "headcount DESC",    // optional
        "limit": 10,                     // optional
        "into": "summary_table"          // optional, saves result to new table
    }
    """
    spec = parse_json_arg(args.json)
    by = spec.get("by")
    aggs = spec.get("aggs")
    if not by or not isinstance(by, list):
        error("'by' is required and must be a list of column names")
    if not aggs or not isinstance(aggs, dict):
        error("'aggs' is required and must be an object mapping alias to aggregate expression")

    select_parts = [f'"{col}"' for col in by]
    select_parts += [f'{expr} AS "{alias}"' for alias, expr in aggs.items()]
    select_clause = ", ".join(select_parts)
    group_clause = ", ".join(f'"{col}"' for col in by)

    sql = f'SELECT {select_clause} FROM "{args.table}" GROUP BY {group_clause}'

    if "having" in spec:
        sql += f' HAVING {spec["having"]}'
    if "order_by" in spec:
        sql += f' ORDER BY {spec["order_by"]}'
    if "limit" in spec:
        sql += f' LIMIT {int(spec["limit"])}'

    conn = connect(args.db)
    try:
        # Optionally save to a new table
        into = spec.get("into")
        if into:
            create_sql = f'CREATE TABLE "{into}" AS {sql}'
            conn.execute(create_sql)
            conn.commit()

        rows = conn.execute(sql).fetchall()
        result = [dict(r) for r in rows]
        out = {"ok": True, "table": args.table, "grouped_by": by, "count": len(result), "rows": result}
        if into:
            out["saved_to"] = into
        output(out)
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_query(args):
    """Run arbitrary SQL and return results as JSON."""
    conn = connect(args.db)
    try:
        cursor = conn.execute(args.sql)
        if cursor.description:
            rows = [dict(r) for r in cursor.fetchall()]
            output({"ok": True, "count": len(rows), "rows": rows})
        else:
            conn.commit()
            output({"ok": True, "changes": conn.total_changes})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_schema(args):
    """Show schema for a table or all tables."""
    conn = connect(args.db)
    try:
        if args.table:
            rows = conn.execute(f'PRAGMA table_info("{args.table}")').fetchall()
            columns = {r["name"]: {"type": r["type"], "notnull": bool(r["notnull"]), "pk": bool(r["pk"]), "default": r["dflt_value"]} for r in rows}
            output({"table": args.table, "columns": columns})
        else:
            tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            output({"tables": {r["name"]: r["sql"] for r in tables}})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_tables(args):
    """List all tables."""
    conn = connect(args.db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        output({"tables": [r["name"] for r in rows]})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


def cmd_drop(args):
    """Drop a table."""
    conn = connect(args.db)
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{args.table}"')
        conn.commit()
        output({"ok": True, "dropped": args.table})
    except sqlite3.Error as e:
        error(f"SQLite error: {e}")
    finally:
        conn.close()


# ─── CLI Setup ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="table_tool",
        description="JSON-driven SQLite CLI for LLM integration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # create-table
    p = sub.add_parser("create-table", help="Create a table")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", help="Table name")
    p.add_argument("json", help="JSON spec (or '-' for stdin)")
    p.set_defaults(func=cmd_create_table)

    # insert
    p = sub.add_parser("insert", help="Insert rows")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", help="Table name")
    p.add_argument("json", help="JSON spec (or '-' for stdin)")
    p.set_defaults(func=cmd_insert)

    # join
    p = sub.add_parser("join", help="Join two tables into a new one")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("output_table", help="Name for the output table")
    p.add_argument("json", help="JSON spec (or '-' for stdin)")
    p.set_defaults(func=cmd_join)

    # group
    p = sub.add_parser("group", help="Group by with aggregations")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", help="Table name")
    p.add_argument("json", help="JSON spec (or '-' for stdin)")
    p.set_defaults(func=cmd_group)

    # query
    p = sub.add_parser("query", help="Run arbitrary SQL")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("sql", help="SQL statement")
    p.set_defaults(func=cmd_query)

    # schema
    p = sub.add_parser("schema", help="Show table schema")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", nargs="?", help="Table name (omit for all)")
    p.set_defaults(func=cmd_schema)

    # tables
    p = sub.add_parser("tables", help="List all tables")
    p.add_argument("db", help="Path to SQLite database")
    p.set_defaults(func=cmd_tables)

    # drop
    p = sub.add_parser("drop", help="Drop a table")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", help="Table name")
    p.set_defaults(func=cmd_drop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
