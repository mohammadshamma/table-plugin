#!/usr/bin/env python3
"""
table_tool - A JSON-driven SQLite CLI designed for LLM integration.

Usage:
    table_tool create-table <db> <table> <json>
    table_tool insert <db> <table> <json>
    table_tool import-csv <db> <table> <json>
    table_tool join <db> <output_table> <json>
    table_tool group <db> <table> <json>
    table_tool query <db> <sql>
    table_tool schema <db> [<table>]
    table_tool tables <db>
    table_tool drop <db> <table>

All data exchange uses JSON for easy LLM generation and parsing.
Output is always JSON to stdout for easy consumption.

The operations are also importable (see op_* functions), which is how
server.py exposes them as MCP tools without a subprocess round-trip.
"""

import argparse
import csv
import json
import re
import sqlite3
import sys


class TableToolError(Exception):
    """Raised by op_* functions on invalid input or SQLite errors."""


def connect(db_path: str) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e


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


# ─── Operations (importable) ─────────────────────────────────────────────────


def op_create_table(db: str, table: str, spec: dict) -> dict:
    """
    Create a table.

    Spec format:
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
    if not table or not table.strip():
        raise TableToolError("Table name cannot be empty")
    columns = spec.get("columns")
    if not columns or not isinstance(columns, dict):
        raise TableToolError("'columns' is required and must be an object mapping column names to types")

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
    sql = f'CREATE TABLE {exists_clause}"{table}" (\n  {",".join(col_defs)}\n)'

    conn = connect(db)
    try:
        conn.execute(sql)
        conn.commit()
        return {"ok": True, "table": table, "sql": sql}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_insert(db: str, table: str, spec: dict) -> dict:
    """
    Insert rows into a table.

    Spec format:
    {
        "rows": [
            {"name": "Alice", "age": 30, "email": "alice@example.com"},
            {"name": "Bob", "age": 25, "email": "bob@example.com"}
        ]
    }
    """
    if not table or not table.strip():
        raise TableToolError("Table name cannot be empty")
    rows = spec.get("rows")
    if not rows or not isinstance(rows, list):
        raise TableToolError("'rows' is required and must be a list of objects")

    conn = connect(db)
    try:
        inserted = 0
        for row in rows:
            cols = ", ".join(f'"{k}"' for k in row.keys())
            placeholders = ", ".join("?" for _ in row)
            sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders})'
            conn.execute(sql, list(row.values()))
            inserted += 1
        conn.commit()
        return {"ok": True, "table": table, "inserted": inserted}
    except sqlite3.Error as e:
        conn.rollback()
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


_INT_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_TYPE_RANK = {"INTEGER": 0, "REAL": 1, "TEXT": 2}
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _classify_value(value: str) -> str:
    """Classify one trimmed, non-empty string as INTEGER, REAL, or TEXT.

    Regex-gated rather than int()/float() tries: Python's parsers accept
    underscores ("1_000") and "nan"/"inf", which must stay TEXT. A value
    matching the integer pattern is decided here and never falls through to
    the REAL check — "007" also matches _REAL_RE and would round-trip as 7.0.
    """
    if _INT_RE.match(value):
        digits = value.lstrip("+-")
        if len(digits) > 1 and digits[0] == "0":
            return "TEXT"  # leading zeros carry meaning (zip codes, IDs)
        if not _SQLITE_INT_MIN <= int(value) <= _SQLITE_INT_MAX:
            return "TEXT"  # beyond SQLite's 64-bit INTEGER range
        return "INTEGER"
    if _REAL_RE.match(value):
        return "REAL"
    return "TEXT"


def infer_column_types(header: list, rows: list) -> dict:
    """Deterministically infer an SQL type per column by scanning ALL rows.

    A column is INTEGER if every non-empty trimmed value classifies as
    INTEGER, REAL if every value classifies as INTEGER or REAL, else TEXT.
    Empty values contribute nothing; a column with no non-empty values (or
    zero data rows) is TEXT. Returns {column: type} in header order.
    """
    types = {}
    for i, name in enumerate(header):
        rank = -1
        for row in rows:
            value = row[i].strip()
            if not value:
                continue
            rank = max(rank, _TYPE_RANK[_classify_value(value)])
            if rank == _TYPE_RANK["TEXT"]:
                break
        types[name] = ("INTEGER", "REAL", "TEXT")[rank] if rank >= 0 else "TEXT"
    return types


def _convert_value(value: str, col_type: str):
    """Convert one CSV field for storage. Empty (after trim) is NULL for
    every column type; TEXT keeps the raw untrimmed string."""
    if not value.strip():
        return None
    if col_type == "INTEGER":
        return int(value.strip())
    if col_type == "REAL":
        return float(value.strip())
    return value


def op_import_csv(db: str, table: str, spec: dict) -> dict:
    """
    Create and populate a new table from a local CSV file.

    Spec format:
    {
        "file_path": "/abs/path/data.csv",   // required
        "delimiter": ";"                      // optional, default ","
    }

    Column names come from the required header row; types are inferred by
    infer_column_types(). Errors if the table already exists. The exists
    check, CREATE TABLE, and all inserts run in one transaction, so a failed
    import leaves nothing behind. The file is read fully into memory —
    streaming in two passes is the future path for huge files.
    """
    if not table or not table.strip():
        raise TableToolError("Table name cannot be empty")
    if '"' in table:
        raise TableToolError('Table name cannot contain a double quote (")')
    file_path = spec.get("file_path")
    if not file_path:
        raise TableToolError("'file_path' is required")
    delimiter = spec.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise TableToolError("'delimiter' must be a single character")

    try:
        # newline="" is required for embedded newlines in quoted fields;
        # utf-8-sig transparently strips the BOM Excel puts on exports.
        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter=delimiter)
            try:
                all_rows = list(reader)
            except csv.Error as e:
                raise TableToolError(f"CSV parse error at line {reader.line_num}: {e}") from e
    except (FileNotFoundError, IsADirectoryError) as e:
        raise TableToolError(f"CSV file not found: {file_path}") from e
    except UnicodeDecodeError as e:
        raise TableToolError(f"CSV file is not valid UTF-8: {file_path}") from e

    if not all_rows or not all_rows[0]:
        raise TableToolError("CSV file is empty (no header row)")

    header = [name.strip() for name in all_rows[0]]
    rows = all_rows[1:]

    seen = set()
    for i, name in enumerate(header, start=1):
        if not name:
            raise TableToolError(f"CSV header has an empty column name (column {i})")
        if '"' in name:
            raise TableToolError(f"CSV column name cannot contain a double quote: {name}")
        key = name.casefold()  # SQLite column names are case-insensitive
        if key in seen:
            raise TableToolError(f"Duplicate CSV column name: {name}")
        seen.add(key)

    for n, row in enumerate(rows, start=1):
        if len(row) != len(header):
            raise TableToolError(f"CSV row {n} has {len(row)} fields, expected {len(header)}")

    columns = infer_column_types(header, rows)
    converted = [
        [_convert_value(value, columns[name]) for name, value in zip(header, row)]
        for row in rows
    ]

    col_defs = ",".join(f'"{name}" {col_type}' for name, col_type in columns.items())
    create_sql = f'CREATE TABLE "{table}" (\n  {col_defs}\n)'
    col_list = ", ".join(f'"{name}"' for name in header)
    placeholders = ", ".join("?" for _ in header)
    insert_sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'

    conn = connect(db)
    try:
        # sqlite3 only auto-begins a transaction before DML, so BEGIN
        # explicitly to keep the exists check, CREATE TABLE, and inserts
        # atomic (same pattern as the job queue in server.py).
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            raise TableToolError(f'Table "{table}" already exists; use table_drop first to replace it')
        conn.execute(create_sql)
        conn.executemany(insert_sql, converted)
        conn.commit()
        return {
            "ok": True,
            "table": table,
            "inserted": len(converted),
            "columns": columns,
            "sql": create_sql,
        }
    except sqlite3.Error as e:
        conn.rollback()
        raise TableToolError(f"SQLite error: {e}") from e
    except TableToolError:
        conn.rollback()
        raise
    finally:
        conn.close()


def op_join(db: str, output_table: str, spec: dict) -> dict:
    """
    Join two tables and store the result in a new table.

    Spec format:
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
    if not output_table or not output_table.strip():
        raise TableToolError("Output table name cannot be empty")
    left = spec.get("left")
    right = spec.get("right")
    if not left or not right:
        raise TableToolError("'left' and 'right' table names are required")

    join_type = spec.get("type", "inner").upper()
    if join_type not in ("INNER", "LEFT", "CROSS"):
        raise TableToolError("'type' must be one of: inner, left, cross")

    # Build ON clause
    if "on" in spec:
        on_clause = f'"{left}"."{spec["on"]}" = "{right}"."{spec["on"]}"'
    elif "on_left" in spec and "on_right" in spec:
        on_clause = f'"{left}"."{spec["on_left"]}" = "{right}"."{spec["on_right"]}"'
    else:
        if join_type == "CROSS":
            on_clause = None
        else:
            raise TableToolError("'on' or both 'on_left'/'on_right' are required (except for cross joins)")

    select_cols = ", ".join(spec.get("select", ["*"]))
    exists_clause = "IF NOT EXISTS " if spec.get("if_not_exists") else ""

    sql = f'CREATE TABLE {exists_clause}"{output_table}" AS SELECT {select_cols} FROM "{left}" {join_type} JOIN "{right}"'
    if on_clause:
        sql += f" ON {on_clause}"

    conn = connect(db)
    try:
        conn.execute(sql)
        conn.commit()
        count = conn.execute(f'SELECT COUNT(*) FROM "{output_table}"').fetchone()[0]
        return {"ok": True, "table": output_table, "rows": count, "sql": sql}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_group(db: str, table: str, spec: dict) -> dict:
    """
    Group by columns with aggregations. Returns results as JSON.

    Spec format:
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
    if not table or not table.strip():
        raise TableToolError("Table name cannot be empty")
    by = spec.get("by")
    aggs = spec.get("aggs")
    if not by or not isinstance(by, list):
        raise TableToolError("'by' is required and must be a list of column names")
    if not aggs or not isinstance(aggs, dict):
        raise TableToolError("'aggs' is required and must be an object mapping alias to aggregate expression")

    limit = spec.get("limit")
    if limit is not None and limit < 0:
        raise TableToolError("Limit must be non-negative")

    select_parts = [f'"{col}"' for col in by]
    select_parts += [f'{expr} AS "{alias}"' for alias, expr in aggs.items()]
    select_clause = ", ".join(select_parts)
    group_clause = ", ".join(f'"{col}"' for col in by)

    sql = f'SELECT {select_clause} FROM "{table}" GROUP BY {group_clause}'

    if "having" in spec:
        sql += f' HAVING {spec["having"]}'
    if "order_by" in spec:
        sql += f' ORDER BY {spec["order_by"]}'
    if "limit" in spec:
        sql += f' LIMIT {int(spec["limit"])}'

    conn = connect(db)
    try:
        # Optionally save to a new table
        into = spec.get("into")
        if into:
            create_sql = f'CREATE TABLE "{into}" AS {sql}'
            conn.execute(create_sql)
            conn.commit()

        rows = conn.execute(sql).fetchall()
        result = [dict(r) for r in rows]
        out = {"ok": True, "table": table, "grouped_by": by, "count": len(result), "rows": result}
        if into:
            out["saved_to"] = into
        return out
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_query(db: str, sql: str) -> dict:
    """Run arbitrary SQL and return results as JSON."""
    if not sql or not sql.strip():
        raise TableToolError("SQL query cannot be empty")
    conn = connect(db)
    try:
        cursor = conn.execute(sql)
        if cursor.description:
            rows = [dict(r) for r in cursor.fetchall()]
            return {"ok": True, "count": len(rows), "rows": rows}
        else:
            conn.commit()
            return {"ok": True, "changes": conn.total_changes}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_schema(db: str, table: str | None = None) -> dict:
    """Show schema for a table or all tables."""
    conn = connect(db)
    try:
        if table:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = {r["name"]: {"type": r["type"], "notnull": bool(r["notnull"]), "pk": bool(r["pk"]), "default": r["dflt_value"]} for r in rows}
            return {"table": table, "columns": columns}
        else:
            tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            return {"tables": {r["name"]: r["sql"] for r in tables}}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_tables(db: str) -> dict:
    """List all tables."""
    conn = connect(db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {"tables": [r["name"] for r in rows]}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


def op_drop(db: str, table: str) -> dict:
    """Drop a table."""
    if not table or not table.strip():
        raise TableToolError("Table name cannot be empty")
    conn = connect(db)
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.commit()
        return {"ok": True, "dropped": table}
    except sqlite3.Error as e:
        raise TableToolError(f"SQLite error: {e}") from e
    finally:
        conn.close()


# ─── Shared job-queue read helpers ───────────────────────────────────────────
#
# These live here (rather than in server.py) because both the MCP server and
# the read-only web inspector (inspect_server.py) import table_tool but not each
# other. Keeping the job registry name, reserved columns, and status counting in
# one place guarantees the two processes never disagree about a job's state.

# The registry table listing every job (see server.py's ensure_jobs_table).
JOBS_TABLE = "_table_jobs"

# Columns a job table adds on top of its source columns. Source tables must not
# use these names.
RESERVED_TASK_COLUMNS = (
    "result",
    "_task_status",
    "_task_error",
    "_task_attempts",
    "_task_lease_expires",
    "_task_claimed_by",
)

# The task lifecycle states, in the order a status view should present them.
TASK_STATUSES = ("pending", "claimed", "done", "failed")


def count_task_statuses(conn: sqlite3.Connection, job_table: str) -> dict:
    """Count a job table's rows by _task_status.

    Returns a dict with a zero-filled key for every state in TASK_STATUSES so
    callers can rely on all four being present. The caller owns the transaction
    (and any lease sweep); this is a pure read.
    """
    counts = {status: 0 for status in TASK_STATUSES}
    for row in conn.execute(
        f'SELECT _task_status, COUNT(*) AS n FROM "{job_table}" GROUP BY _task_status'
    ):
        if row["_task_status"] in counts:
            counts[row["_task_status"]] = row["n"]
    return counts


def list_job_tables(conn: sqlite3.Connection) -> list:
    """Names of all registered job tables, or [] if no jobs exist yet.

    Guarded against the registry table not existing (a database that has never
    created a job), so read-only callers need no setup.
    """
    try:
        rows = conn.execute(
            f'SELECT job_table FROM "{JOBS_TABLE}" ORDER BY job_table'
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # registry table absent → no jobs
    return [r["job_table"] for r in rows]


# ─── CLI Commands ────────────────────────────────────────────────────────────


def run_op(fn, *op_args):
    try:
        output(fn(*op_args))
    except TableToolError as e:
        error(str(e))


def cmd_create_table(args):
    run_op(op_create_table, args.db, args.table, parse_json_arg(args.json))


def cmd_insert(args):
    run_op(op_insert, args.db, args.table, parse_json_arg(args.json))


def cmd_import_csv(args):
    run_op(op_import_csv, args.db, args.table, parse_json_arg(args.json))


def cmd_join(args):
    run_op(op_join, args.db, args.output_table, parse_json_arg(args.json))


def cmd_group(args):
    run_op(op_group, args.db, args.table, parse_json_arg(args.json))


def cmd_query(args):
    run_op(op_query, args.db, args.sql)


def cmd_schema(args):
    run_op(op_schema, args.db, args.table)


def cmd_tables(args):
    run_op(op_tables, args.db)


def cmd_drop(args):
    run_op(op_drop, args.db, args.table)


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

    # import-csv
    p = sub.add_parser("import-csv", help="Create and populate a table from a CSV file")
    p.add_argument("db", help="Path to SQLite database")
    p.add_argument("table", help="Table name")
    p.add_argument("json", help='JSON spec (or \'-\' for stdin), e.g. {"file_path": "/data/x.csv"}')
    p.set_defaults(func=cmd_import_csv)

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
