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
import signal
import socket
import sqlite3
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
    # The probe filename must be unique per process AND thread: concurrent
    # workers racing a shared probe file delete it out from under each other,
    # making the loser silently fall back to the scratch DB (wrong database).
    test_db = path / f".test_write-{os.getpid()}-{threading.get_ident()}.db"
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
                "max_claims_per_worker": {
                    "description": (
                        "Max tasks one worker agent may claim in its lifetime (default: 1 — "
                        "claim, submit, terminate; 0 = unlimited). Keeps each row in a fresh "
                        "worker context."
                    ),
                    "type": "number",
                },
            },
            "required": ["table", "template", "job_table"],
        },
    ),
    types.Tool(
        name="table_job_claim",
        description=(
            "Claim the next pending task of a job. Returns the task id and its rendered prompt; "
            "or a null task with remaining_pending 0 when no work remains; or a null task with a "
            "'reason' when this worker has hit the job's per-worker claim limit and must terminate "
            "so fresh workers continue. Called by worker subagents — orchestrators check progress "
            "with table_job_status, never by claiming."
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
    types.Tool(
        name="table_import_csv",
        description=(
            "Create and populate a new table from a local CSV file. Column names come from "
            "the header row; column types (INTEGER/REAL/TEXT) are inferred deterministically "
            "from the data. Errors if the table already exists (use table_drop first)."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "table": {"description": "Name of the table to create", "type": "string"},
                "file_path": {
                    "description": "Absolute path to a CSV file on the local filesystem",
                    "type": "string",
                },
                "delimiter": {
                    "description": 'Field delimiter, a single character (default ",")',
                    "type": "string",
                },
            },
            "required": ["table", "file_path"],
        },
    ),
    types.Tool(
        name="table_inspect_start",
        description=(
            "Start a local, read-only web UI for browsing this session's tables in a browser: "
            "every row of any table (paginated), plus a status/drill-down view for job tables. "
            "Returns a localhost URL to give the user. Idempotent — returns the existing URL if "
            "already running. Stop it with table_inspect_stop."
        ),
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
                "port": {
                    "description": "Preferred TCP port (default 8760); falls back to the next free port if occupied",
                    "type": "number",
                },
            },
        },
    ),
    types.Tool(
        name="table_inspect_stop",
        description="Stop the local table-inspector web UI started by table_inspect_start.",
        inputSchema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": False,
            "type": "object",
            "properties": {
                "conversation_id": CONV_ID_ARG,
            },
        },
    ),
]


# ─── Per-row job queue (table_job_* tools) ───────────────────────────────────
#
# A "job" turns every row of a source table into one task in a durable SQLite
# work queue, drained by worker subagents through claim/submit. All bookkeeping
# lives in SQL so LLM nondeterminism can only cost retries, never missed rows.

# The registry table name and reserved column tuple live in table_tool so the
# read-only web inspector shares the exact same definitions (see table_tool.py).
JOBS_TABLE = table_tool.JOBS_TABLE
RESERVED_TASK_COLUMNS = table_tool.RESERVED_TASK_COLUMNS

# Claims made by THIS process, keyed by (resolved db path, job_table). The
# `max_claims_per_worker` cap cannot key on _task_claimed_by (every worker
# passes the parent conversation's id), but each worker subagent runs its own
# server process, so an in-process counter is a per-worker counter. Same
# module-level dict + lock idiom as LINEAGE_CACHE.
WORKER_CLAIM_COUNTS: dict = {}
WORKER_CLAIM_COUNTS_LOCK = threading.Lock()


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
        "  max_claims_per_worker INTEGER,\n"
        "  created_at REAL\n"
        ")"
    )
    cols = {r["name"] for r in conn.execute(f'PRAGMA table_info("{JOBS_TABLE}")')}
    if "max_claims_per_worker" not in cols:
        # Registry created before the claim cap existed. NULL = unlimited (the
        # old behavior for existing jobs); op_job_create stores a resolved value.
        try:
            conn.execute(f'ALTER TABLE "{JOBS_TABLE}" ADD COLUMN max_claims_per_worker INTEGER')
        except sqlite3.OperationalError:
            pass  # another worker process won the migration race


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
    claims_raw = args.get("max_claims_per_worker")
    max_claims_per_worker = 1 if claims_raw is None else int(claims_raw)

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
    if max_claims_per_worker < 0:
        raise table_tool.TableToolError("'max_claims_per_worker' must be non-negative (0 = unlimited)")

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
                "(job_table, source_table, template, lease_seconds, max_attempts, "
                "max_claims_per_worker, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_table, source, template, lease_seconds, max_attempts,
                 max_claims_per_worker, time.time()),
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

        # A job name can be re-registered after manual deregistration; the new
        # job must not inherit claim counts from an older namesake.
        with WORKER_CLAIM_COUNTS_LOCK:
            WORKER_CLAIM_COUNTS.pop((db, job_table), None)

        return {
            "ok": True,
            "job_table": job_table,
            "total_tasks": total,
            "max_claims_per_worker": max_claims_per_worker,
        }
    finally:
        conn.close()


def op_job_claim(db: str, args: dict) -> dict:
    worker_id = args.get("conversation_id") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")

    conn = job_connect(db)
    try:
        job = get_job(conn, args.get("job_table"))
        job_table = job["job_table"]

        # Per-worker claim cap. NULL (job predates the cap) and 0 both mean
        # unlimited. Refusal happens before the transaction: a capped worker
        # does no DB work at all, and its null task is structurally distinct
        # from a drained queue (reason/claim_limit vs remaining_pending).
        cap_raw = job.get("max_claims_per_worker")
        cap = int(cap_raw) if cap_raw else 0
        counter_key = (db, job_table)
        if cap:
            with WORKER_CLAIM_COUNTS_LOCK:
                made = WORKER_CLAIM_COUNTS.get(counter_key, 0)
                if made >= cap:
                    return {
                        "task": None,
                        "reason": (
                            f"worker claim limit reached ({cap} per worker) — submit any "
                            "outstanding result, then terminate; fresh workers will "
                            "continue this job"
                        ),
                        "claim_limit": cap,
                    }
                # Reserve the slot; released below if no task is actually
                # claimed. Never hold the lock across SQLite I/O.
                WORKER_CLAIM_COUNTS[counter_key] = made + 1

        claimed = False
        try:
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
            claimed = True
        finally:
            if cap and not claimed:
                with WORKER_CLAIM_COUNTS_LOCK:
                    WORKER_CLAIM_COUNTS[counter_key] -= 1

        row_values = {
            k: v for k, v in dict(row).items()
            if k != "_task_id" and k not in RESERVED_TASK_COLUMNS
        }
        try:
            # A render failure keeps the reserved slot: the claim is already
            # committed (it also consumed a _task_attempts); the lease sweep
            # recovers the task.
            prompt = job["template"].format(**row_values)
        except Exception as e:
            raise table_tool.TableToolError(f"Failed to render template for task {task_id}: {e}") from e

        result = {"task": {"task_id": task_id, "prompt": prompt}, "remaining_pending": remaining}
        if cap:
            with WORKER_CLAIM_COUNTS_LOCK:
                made = WORKER_CLAIM_COUNTS.get(counter_key, 0)
            if made >= cap:
                result["note"] = (
                    f"This is your last claim for this job (limit {cap} per worker). "
                    "Submit the result with table_job_submit, then terminate — do not "
                    "call table_job_claim again; fresh workers handle the remaining tasks."
                )
            else:
                result["note"] = f"Claim {made} of {cap} allowed for this worker."
        return result
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
            counts = table_tool.count_task_statuses(conn, job_table)
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


# ─── Web inspector (table_inspect_* tools) ───────────────────────────────────
#
# A localhost, read-only web UI for browsing the session's tables and jobs in a
# browser. The agent turns it on/off via table_inspect_start/stop. The launcher
# must live here because only this process can resolve the session DB path (via
# get_resolved_db_path); it hands that path to inspect_server.py, spawned as a
# detached background subprocess so it outlives this stdio server.

DEFAULT_INSPECT_PORT = 8760
INSPECT_PIDFILE_NAME = ".inspect.json"
INSPECT_PORT_ATTEMPTS = 20


def inspect_paths(db_path: str) -> tuple[Path, Path]:
    """(pidfile, web-server script) for a given resolved DB path.

    The pidfile sits next to the DB so inspector state is per-session and gets
    cleaned up with the session; the script ships alongside server.py.
    """
    pidfile = Path(db_path).parent / INSPECT_PIDFILE_NAME
    script = Path(__file__).resolve().parent / "inspect_server.py"
    return pidfile, script


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists and is signalable."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _read_inspect_pidfile(pidfile: Path) -> dict | None:
    try:
        return json.loads(pidfile.read_text())
    except (OSError, ValueError):
        return None


def _pick_free_port(preferred: int) -> int:
    """First free localhost port at or above `preferred` (wrapping stays in range)."""
    for offset in range(INSPECT_PORT_ATTEMPTS):
        port = preferred + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise table_tool.TableToolError(
        f"No free port found in range {preferred}-{preferred + INSPECT_PORT_ATTEMPTS - 1}"
    )


def _inspector_healthy(port: int, timeout: float = 0.3) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def op_inspect_start(db: str, args: dict) -> dict:
    pidfile, script = inspect_paths(db)

    # Idempotent: reuse a live instance rather than stacking servers.
    existing = _read_inspect_pidfile(pidfile)
    if existing and _pid_alive(existing.get("pid", 0)):
        return {
            "ok": True,
            "already_running": True,
            "url": existing.get("url"),
            "port": existing.get("port"),
            "db": db,
        }
    if existing:
        pidfile.unlink(missing_ok=True)  # stale (dead pid)

    if not script.is_file():
        raise table_tool.TableToolError(f"Inspector script not found: {script}")

    port_raw = args.get("port")
    preferred = DEFAULT_INSPECT_PORT if port_raw is None else int(port_raw)
    if not (1024 <= preferred <= 65535):
        raise table_tool.TableToolError("'port' must be between 1024 and 65535")
    port = _pick_free_port(preferred)

    # Run the (stdlib-only) inspector with this server's own interpreter rather
    # than `uv run`: sys.executable is guaranteed to exist and satisfy the
    # >=3.10 requirement, with no PATH lookup, uv resolution, or network access.
    # Detached so it survives this stdio server; DEVNULL so it never writes to
    # the MCP stdout stream (which would corrupt the JSON-RPC protocol).
    proc = subprocess.Popen(
        [sys.executable, str(script), "--db", db, "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise table_tool.TableToolError(
                f"Inspector exited immediately (code {proc.returncode})"
            )
        if _inspector_healthy(port):
            break
        time.sleep(0.15)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        raise table_tool.TableToolError("Inspector did not become healthy in time")

    url = f"http://127.0.0.1:{port}/"
    record = {"pid": proc.pid, "port": port, "url": url, "started_at": time.time()}
    try:
        pidfile.write_text(json.dumps(record))
    except OSError as e:
        raise table_tool.TableToolError(f"Could not write inspector pidfile: {e}") from e

    return {"ok": True, "url": url, "port": port, "db": db}


def op_inspect_stop(db: str, args: dict) -> dict:
    pidfile, _ = inspect_paths(db)
    record = _read_inspect_pidfile(pidfile)
    if not record:
        return {"ok": True, "was_running": False}

    pid = record.get("pid", 0)
    stopped = False
    if _pid_alive(pid):
        # Signal the whole session group (start_new_session made pid the leader).
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except (ProcessLookupError, OSError):
                stopped = False
    pidfile.unlink(missing_ok=True)
    return {"ok": True, "was_running": stopped, "stopped_pid": pid if stopped else None}


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

    elif name == "table_import_csv":
        spec = {"file_path": args.get("file_path")}
        if args.get("delimiter") is not None:
            spec["delimiter"] = args["delimiter"]
        return table_tool.op_import_csv(db_path, args.get("table"), spec)

    elif name == "table_job_create":
        return op_job_create(db_path, args)

    elif name == "table_job_claim":
        return op_job_claim(db_path, args)

    elif name == "table_job_submit":
        return op_job_submit(db_path, args)

    elif name == "table_job_status":
        return op_job_status(db_path, args)

    elif name == "table_inspect_start":
        return op_inspect_start(db_path, args)

    elif name == "table_inspect_stop":
        return op_inspect_stop(db_path, args)

    else:
        raise ValueError(f"Unknown tool: {name}")


server = Server("table", version="0.0.4")


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
