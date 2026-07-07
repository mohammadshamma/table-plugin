# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An Antigravity plugin that gives agents SQLite database capabilities via an MCP server, to keep structured data out of the LLM context window ("context rot" avoidance). Installed with `agy plugin install`; launched by Antigravity via `mcp_config.json`.

## Commands

There is no build step, package manager config, or lock file — `server.py` and the uv-run tests are PEP 723 single-file scripts (`uv run` provisions Python ≥3.10 and the pinned `mcp==1.28.1` automatically).

```bash
# Run the test suites
uv run test_session_scoping.py         # server + session-scoped DB routing (uses unittest + mock)
uv run test_table_jobs.py              # table_job_* work-queue semantics
python3 -m unittest test_table_tool -v # table_tool CLI (pure stdlib, subprocess-driven)

# Single test (uv-run suites accept unittest CLI args)
uv run test_table_jobs.py TestClassName.test_name
python3 -m unittest test_table_tool.TestClassName.test_name -v

# Run the MCP server manually (normally launched by Antigravity)
uv run server.py
```

## Architecture

```
Antigravity ──► MCP server (server.py) ──► table_tool.py ──► SQLite .db file
                stdio, uv run             imported module    session-scoped path
```

- **server.py** — declares the 12 MCP tools (`table_create/insert/join/group_by/run_sql/schema/list/drop` + `table_job_create/claim/submit/status`) and dispatches them to functions imported from `table_tool.py` (no subprocess per call). Also owns two subsystems that live only here:
  - **Session scoping**: every tool takes an optional `conversation_id` (or `ANTIGRAVITY_CONVERSATION_ID` env). The server traces parent→child conversation lineage by scanning `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl`, resolves the *root* session, and stores the DB at `<brain>/<root_id>/.tables/session.db` so a main agent and all its subagents share one database. Falls back to `~/.gemini/antigravity/scratch/session.db`. Lineage results are cached in-process (`LINEAGE_CACHE`, thread-safe). `conversation_id` is validated against `^[a-zA-Z0-9_-]+$` to block path traversal.
  - **Durable work queue** (`table_job_*`): turns every row of a source table into one task in a job table, for per-row subagent fan-out. Guarantees: every row becomes exactly one task (pure SQL copy — the model never enumerates rows); expired claim leases are lazily requeued; retries are capped by `max_attempts` (default 3), then the task is marked `failed` (never dropped); finished work cannot be overwritten. Job tables reserve the columns `result`, `_task_status`, `_task_error`, `_task_attempts`, `_task_lease_expires`, `_task_claimed_by` — source tables must not use these names. Concurrency uses WAL mode, `BEGIN IMMEDIATE`, and `busy_timeout=10000` since parallel workers run separate server processes against the same DB.
- **table_tool.py** — the actual SQLite operations (`op_create_table`, `op_insert`, `op_join`, `op_group_by`, `op_query`, `op_schema`, `op_tables`, `op_drop`), all returning JSON. Also works standalone as a CLI (`table_tool.py create-table <db> <table> <json>` …), which is how `test_table_tool.py` exercises it.
- **skills/table/SKILL.md** — tool documentation surfaced to agents; keep it in sync when tool schemas change.
- **workflows/process_table.md** — the `/process_table` orchestration recipe (create job → spawn parallel workers pumping claim→submit → loop on status until `complete`).

## Constraints

- **Absolute install path**: `mcp_config.json` hardcodes `$HOME/.gemini/config/plugins/table/server.py` because Antigravity does not substitute variables like `${extensionPath}` and resolves relative paths against the session cwd. Don't "clean this up" into a relative path.
- Tool JSON schemas in `server.py` were kept byte-identical to the original Node implementation; treat schema changes as breaking.
