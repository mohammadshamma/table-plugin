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
import sqlite3
import string
import sys
import time
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
    types.Tool(
        name="table_job_create",
        description=(
            "Create a per-row LLM job: every row of the source table becomes one task in a durable "
            "work queue (the job table). Worker subagents drain it via table_job_claim/table_job_submit."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Source table; every row becomes exactly one task", "type": "string"},
                "template": {
                    "description": 'Prompt template with {column} placeholders filled from each row, e.g. "Summarize: {text}"',
                    "type": "string",
                },
                "job_table": {
                    "description": "Name for the job table (copy of source rows + bookkeeping + results); also the job's identifier",
                    "type": "string",
                },
                "lease_seconds": {
                    "description": "Seconds a claimed task is reserved for its worker before being requeued (default: 600)",
                    "type": "number",
                },
                "max_attempts": {
                    "description": "Times a task may be handed out before it is marked failed (default: 3)",
                    "type": "number",
                },
            },
            "required": ["table", "template", "job_table"],
        },
    ),
    types.Tool(
        name="table_job_claim",
        description=(
            "Claim the next pending task of a job. Returns the task id and its rendered prompt, "
            "or a null task when no work remains. Called by worker subagents."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "job_table": {"description": "Job table to draw work from", "type": "string"},
            },
            "required": ["job_table"],
        },
    ),
    types.Tool(
        name="table_job_submit",
        description=(
            "Submit the outcome of a claimed task: a result (task done) or an error "
            "(task requeued, or failed once attempts are exhausted). Called by worker subagents."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "job_table": {"description": "Job table the task belongs to", "type": "string"},
                "task_id": {"description": "Task id returned by table_job_claim", "type": "number"},
                "result": {"description": "The completed answer (provide exactly one of result/error)", "type": "string"},
                "error": {"description": "Why the task could not be completed (provide exactly one of result/error)", "type": "string"},
            },
            "required": ["job_table", "task_id"],
        },
    ),
    types.Tool(
        name="table_job_status",
        description=(
            "Report a job's task counts (total/pending/claimed/done/failed) and whether it is complete. "
            "The only loop condition the orchestrating agent needs."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "job_table": {"description": "Job table to report on", "type": "string"},
            },
            "required": ["job_table"],
        },
    ),
]


# ─── Per-row job queue (table_job_* tools) ───────────────────────────────────
#
# A "job" turns every row of a source table into one task in a durable SQLite
# work queue, drained by worker subagents through claim/submit. All bookkeeping
# lives in SQL so LLM nondeterminism can only cost retries, never missed rows.

JOBS_TABLE = "_table_jobs"

# Columns the job table adds on top of the source columns. Source tables must
# not use these names.
RESERVED_TASK_COLUMNS = (
    "result",
    "_task_status",
    "_task_error",
    "_task_attempts",
    "_task_lease_expires",
    "_task_claimed_by",
)


def job_connect(db: str) -> sqlite3.Connection:
    """Connection with explicit transaction control for atomic claims.

    Worker subagents may each run their own server process against the shared
    db, so job operations use BEGIN IMMEDIATE and need a busy timeout.
    """
    conn = table_tool.connect(db)
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def extract_template_placeholders(template: str) -> set[str]:
    """Column names referenced by {placeholder} fields in a template."""
    placeholders = set()
    for _literal, field, _spec, _conv in string.Formatter().parse(template):
        if field is None:
            continue
        name = field.split(".")[0].split("[")[0]
        if not name:
            raise table_tool.TableToolError(
                "Positional placeholders like '{}' are not supported; use named {column} placeholders"
            )
        placeholders.add(name)
    return placeholders


def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{JOBS_TABLE}" (\n'
        "  job_table TEXT PRIMARY KEY,\n"
        "  source_table TEXT,\n"
        "  template TEXT,\n"
        "  lease_seconds REAL,\n"
        "  max_attempts INTEGER,\n"
        "  created_at REAL\n"
        ")"
    )


def get_job(conn: sqlite3.Connection, job_table: str) -> dict:
    if not job_table or not job_table.strip():
        raise table_tool.TableToolError("Job table name cannot be empty")
    ensure_jobs_table(conn)
    row = conn.execute(
        f'SELECT * FROM "{JOBS_TABLE}" WHERE job_table = ?', (job_table,)
    ).fetchone()
    if row is None:
        raise table_tool.TableToolError(f"No job found for table '{job_table}'")
    return dict(row)


def sweep_expired_leases(conn: sqlite3.Connection, job: dict) -> None:
    """Requeue claimed tasks whose lease expired; fail those out of attempts.

    Recovery is lazy: this runs at the start of every claim and status call,
    inside the caller's transaction. There is no background process.
    """
    conn.execute(
        f'UPDATE "{job["job_table"]}" SET '
        "  _task_status = CASE WHEN _task_attempts >= ? THEN 'failed' ELSE 'pending' END, "
        "  _task_error = CASE WHEN _task_attempts >= ? "
        "    THEN 'lease expired after ' || _task_attempts || ' attempt(s)' ELSE _task_error END "
        "WHERE _task_status = 'claimed' AND _task_lease_expires < ?",
        (job["max_attempts"], job["max_attempts"], time.time()),
    )


def op_job_create(db: str, args: dict) -> dict:
    source = args.get("table")
    template = args.get("template")
    job_table = args.get("job_table")
    lease_raw = args.get("lease_seconds")
    lease_seconds = 600.0 if lease_raw is None else float(lease_raw)
    attempts_raw = args.get("max_attempts")
    max_attempts = 3 if attempts_raw is None else int(attempts_raw)

    if not source or not source.strip():
        raise table_tool.TableToolError("Table name cannot be empty")
    if not template or not template.strip():
        raise table_tool.TableToolError("'template' is required and cannot be empty")
    if not job_table or not job_table.strip():
        raise table_tool.TableToolError("Job table name cannot be empty")
    if lease_seconds < 0:
        raise table_tool.TableToolError("'lease_seconds' must be non-negative")
    if max_attempts < 1:
        raise table_tool.TableToolError("'max_attempts' must be at least 1")

    conn = job_connect(db)
    try:
        ensure_jobs_table(conn)

        source_columns = [r["name"] for r in conn.execute(f'PRAGMA table_info("{source}")')]
        if not source_columns:
            raise table_tool.TableToolError(f"Table '{source}' does not exist")

        unknown = sorted(extract_template_placeholders(template) - set(source_columns))
        if unknown:
            raise table_tool.TableToolError(
                f"Template references unknown column(s): {', '.join(unknown)}. "
                f"Available columns: {', '.join(source_columns)}"
            )
        collisions = [c for c in source_columns if c in RESERVED_TASK_COLUMNS]
        if collisions:
            raise table_tool.TableToolError(
                f"Source column(s) collide with job bookkeeping columns: {', '.join(collisions)}. "
                "Rename them or pre-project the source into a new table first."
            )

        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (job_table,)
        ).fetchone()
        if exists:
            raise table_tool.TableToolError(f"Table '{job_table}' already exists")
        registered = conn.execute(
            f'SELECT job_table FROM "{JOBS_TABLE}" WHERE job_table = ?', (job_table,)
        ).fetchone()
        if registered:
            raise table_tool.TableToolError(f"A job is already registered for table '{job_table}'")

        # Job row, job table, and the row copy are one atomic unit.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f'INSERT INTO "{JOBS_TABLE}" '
                "(job_table, source_table, template, lease_seconds, max_attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_table, source, template, lease_seconds, max_attempts, time.time()),
            )
            conn.execute(f'CREATE TABLE "{job_table}" AS SELECT * FROM "{source}"')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "result" TEXT')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "_task_status" TEXT DEFAULT \'pending\'')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "_task_error" TEXT')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "_task_attempts" INTEGER DEFAULT 0')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "_task_lease_expires" REAL')
            conn.execute(f'ALTER TABLE "{job_table}" ADD COLUMN "_task_claimed_by" TEXT')
            total = conn.execute(f'SELECT COUNT(*) FROM "{job_table}"').fetchone()[0]
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            raise table_tool.TableToolError(f"SQLite error: {e}") from e

        return {"ok": True, "job_table": job_table, "total_tasks": total}
    finally:
        conn.close()


def op_job_claim(db: str, args: dict) -> dict:
    worker_id = args.get("conversation_id") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")

    conn = job_connect(db)
    try:
        job = get_job(conn, args.get("job_table"))
        job_table = job["job_table"]

        conn.execute("BEGIN IMMEDIATE")
        try:
            sweep_expired_leases(conn, job)
            row = conn.execute(
                f'SELECT rowid AS _task_id, * FROM "{job_table}" '
                "WHERE _task_status = 'pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return {"task": None, "remaining_pending": 0}

            task_id = row["_task_id"]
            conn.execute(
                f'UPDATE "{job_table}" SET '
                "  _task_status = 'claimed', "
                "  _task_attempts = _task_attempts + 1, "
                "  _task_lease_expires = ?, "
                "  _task_claimed_by = ? "
                "WHERE rowid = ?",
                (time.time() + job["lease_seconds"], worker_id, task_id),
            )
            remaining = conn.execute(
                f'SELECT COUNT(*) FROM "{job_table}" WHERE _task_status = \'pending\''
            ).fetchone()[0]
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            raise table_tool.TableToolError(f"SQLite error: {e}") from e

        row_values = {
            k: v for k, v in dict(row).items()
            if k != "_task_id" and k not in RESERVED_TASK_COLUMNS
        }
        try:
            prompt = job["template"].format(**row_values)
        except Exception as e:
            raise table_tool.TableToolError(f"Failed to render template for task {task_id}: {e}") from e

        return {"task": {"task_id": task_id, "prompt": prompt}, "remaining_pending": remaining}
    finally:
        conn.close()


def op_job_submit(db: str, args: dict) -> dict:
    result = args.get("result")
    error = args.get("error")
    if (result is None) == (error is None):
        raise table_tool.TableToolError("Provide exactly one of 'result' or 'error'")
    task_id_raw = args.get("task_id")
    if task_id_raw is None:
        raise table_tool.TableToolError("'task_id' is required")
    task_id = int(task_id_raw)

    conn = job_connect(db)
    try:
        job = get_job(conn, args.get("job_table"))
        job_table = job["job_table"]

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f'SELECT _task_status, _task_attempts FROM "{job_table}" WHERE rowid = ?',
                (task_id,),
            ).fetchone()
            # Only claimed -> done/pending/failed transitions are accepted, so
            # stale or duplicate submits can never overwrite finished work.
            if row is None or row["_task_status"] != "claimed":
                conn.execute("COMMIT")
                return {"accepted": False, "task_status": row["_task_status"] if row else None}

            if result is not None:
                new_status = "done"
                conn.execute(
                    f'UPDATE "{job_table}" SET _task_status = \'done\', result = ?, '
                    "_task_error = NULL, _task_lease_expires = NULL WHERE rowid = ?",
                    (result, task_id),
                )
            else:
                new_status = "failed" if row["_task_attempts"] >= job["max_attempts"] else "pending"
                conn.execute(
                    f'UPDATE "{job_table}" SET _task_status = ?, _task_error = ?, '
                    "_task_lease_expires = NULL WHERE rowid = ?",
                    (new_status, error, task_id),
                )
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            raise table_tool.TableToolError(f"SQLite error: {e}") from e

        return {"accepted": True, "task_status": new_status}
    finally:
        conn.close()


def op_job_status(db: str, args: dict) -> dict:
    conn = job_connect(db)
    try:
        job = get_job(conn, args.get("job_table"))
        job_table = job["job_table"]

        conn.execute("BEGIN IMMEDIATE")
        try:
            sweep_expired_leases(conn, job)
            counts = {"pending": 0, "claimed": 0, "done": 0, "failed": 0}
            for row in conn.execute(
                f'SELECT _task_status, COUNT(*) AS n FROM "{job_table}" GROUP BY _task_status'
            ):
                if row["_task_status"] in counts:
                    counts[row["_task_status"]] = row["n"]
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK")
            raise table_tool.TableToolError(f"SQLite error: {e}") from e

        total = sum(counts.values())
        return {
            "total": total,
            **counts,
            "complete": counts["pending"] + counts["claimed"] == 0,
        }
    finally:
        conn.close()


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

    elif name == "table_job_create":
        return op_job_create(db_path, args)

    elif name == "table_job_claim":
        return op_job_claim(db_path, args)

    elif name == "table_job_submit":
        return op_job_submit(db_path, args)

    elif name == "table_job_status":
        return op_job_status(db_path, args)

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
