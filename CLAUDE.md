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
uv run test_csv_import.py              # CSV type inference + import-csv CLI + MCP wiring
uv run test_inspect.py                 # inspector start/stop lifecycle + web-UI render functions
python3 -m unittest test_table_tool -v # table_tool CLI (pure stdlib, subprocess-driven)

# Single test (uv-run suites accept unittest CLI args)
uv run test_table_jobs.py TestClassName.test_name
python3 -m unittest test_table_tool.TestClassName.test_name -v

# Run the MCP server manually (normally launched by Antigravity)
uv run server.py
```

`DEVELOPMENT.md` is the contributor-facing guide (local test loop, symlinking a checkout into `$HOME/.gemini/config/plugins/table` to test inside Antigravity) — keep it in sync when commands or the install flow change.

## Architecture

```
Antigravity ──► MCP server (server.py) ──► table_tool.py ──► SQLite .db file
                stdio, uv run             imported module    session-scoped path
```

- **server.py** — declares the 15 MCP tools (`table_create/insert/join/group_by/run_sql/schema/list/drop/import_csv` + `table_job_create/claim/submit/status` + `table_inspect_start/stop`) and dispatches them to functions imported from `table_tool.py` (no subprocess per call). Also owns three subsystems that live only here:
  - **Session scoping**: every tool takes an optional `conversation_id` (or `ANTIGRAVITY_CONVERSATION_ID` env). The server traces parent→child conversation lineage by scanning `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl`, resolves the *root* session, and stores the DB at `<brain>/<root_id>/.tables/session.db` so a main agent and all its subagents share one database. Falls back to `~/.gemini/antigravity/scratch/session.db`. Lineage results are cached in-process (`LINEAGE_CACHE`, thread-safe). `conversation_id` is validated against `^[a-zA-Z0-9_-]+$` to block path traversal.
  - **Durable work queue** (`table_job_*`): turns every row of a source table into one task in a job table, for per-row subagent fan-out. Guarantees: every row becomes exactly one task (pure SQL copy — the model never enumerates rows); expired claim leases are lazily requeued; retries are capped by `max_attempts` (default 3), then the task is marked `failed` (never dropped); finished work cannot be overwritten; each worker may claim at most `max_claims_per_worker` tasks over its lifetime (default 1 — claim, submit, terminate, so every row gets a fresh context; 0 = unlimited), enforced durably in SQL: a claim counts the rows already stamped with the worker's `conversation_id` in `_task_claimed_by`, inside its own `BEGIN IMMEDIATE` transaction. Worker subagents may share one server process or run separate ones — a SQL count holds either way, and survives a server restart. A capped claim that cannot identify its worker (no `conversation_id`, no `ANTIGRAVITY_CONVERSATION_ID`) is refused, since a NULL stamp matches no row and would go uncounted. Job tables reserve the columns `result`, `_task_status`, `_task_error`, `_task_attempts`, `_task_lease_expires`, `_task_claimed_by` — source tables must not use these names. Concurrency uses WAL mode, `BEGIN IMMEDIATE`, and `busy_timeout=10000`, since parallel workers race for the same DB from threads of one server process or from separate processes.
  - **Web inspector** (`table_inspect_*`): `op_inspect_start` resolves the session DB path and spawns `inspect_server.py` as a **detached** background subprocess (`start_new_session=True`, stdio → `DEVNULL` so it can't corrupt the MCP JSON-RPC stream), then waits until its `/healthz` responds. It picks a free port starting at 8760 and records `{pid, port, url, started_at}` in a pidfile `<db_dir>/.inspect.json`; start is idempotent (reuses a live instance, clears a stale pidfile). `op_inspect_stop` SIGTERMs the process group and removes the pidfile.
- **table_tool.py** — the actual SQLite operations (`op_create_table`, `op_insert`, `op_import_csv`, `op_join`, `op_group_by`, `op_query`, `op_schema`, `op_tables`, `op_drop`), all returning JSON. Also works standalone as a CLI (`table_tool.py create-table <db> <table> <json>` …), which is how `test_table_tool.py` exercises it. `op_import_csv` infers INTEGER/REAL/TEXT column types deterministically in code (`infer_column_types` — whole-data scan, regex-gated; never the LLM) and runs the exists-check + CREATE + inserts in one transaction. Also owns the read-only job helpers shared with the inspector — `JOBS_TABLE`, `RESERVED_TASK_COLUMNS`, `count_task_statuses`, `list_job_tables` (server.py re-imports the first two) — so the web view and `table_job_status` can never disagree about a job's counts.
- **inspect_server.py** — a PEP 723, **stdlib-only** (`http.server`) web UI spawned by `table_inspect_start`. Binds `127.0.0.1` only and opens the DB read-only (`file:…?mode=ro`). Routes: `/` (tables + row counts, job tables flagged), `/table` (paginated rows of any table), `/job` (status summary + filterable per-task list), `/task` (one task's source columns + `_task_*` bookkeeping), `/healthz` (liveness probe). `/table` and `/job` accept `sort`/`dir` params — the column is validated against `PRAGMA table_info` + `rowid` and the direction against asc/desc before interpolation, because ORDER BY identifiers can't be `?`-bound. Every page embeds a small inline script that re-fetches the current URL and swaps `#main` in place (2s on `/job`, 5s elsewhere; pauses when the tab is hidden or the toggle unchecked) — note this keeps `_STATE["last_activity"]` fresh, so an open tab holds the server past the 30-min idle backstop. Each route's HTML is a pure `render_*(conn, …)` function so tests call them without a socket. Self-exits after 30 min idle (zombie backstop). Imports `table_tool` — so it must be installed alongside `server.py` (see Constraints).
- **skills/table/SKILL.md** — tool documentation surfaced to agents; keep it in sync when tool schemas change.
- **workflows/process_table.md** — the `/process_table` orchestration recipe (create job → keep a rolling pool of ~5 one-task workers topped up as each finishes → loop on status until `complete`).
- **workflows/import_csv.md** — the `/import_csv` recipe (resolve file path + table name from the user's request → `table_import_csv` → report row count and inferred schema).
- **workflows/inspect.md** — the `/inspect` recipe (recognize a "let me look at my data" ask → `table_inspect_start` → hand the user the URL → `table_inspect_stop` when done).

**User-interaction convention**: every user-initiated operation gets a workflow recipe in `workflows/` — it tells the agent how to extract parameters from the user's request (asking when unclear), pass `conversation_id`, invoke the tools, and report back — while `SKILL.md` documents the tools themselves and points to the workflow. Keep both in sync when adding tools.

## Constraints

- **Absolute install path**: `mcp_config.json` hardcodes `$HOME/.gemini/config/plugins/table/server.py` because Antigravity does not substitute variables like `${extensionPath}` and resolves relative paths against the session cwd. Don't "clean this up" into a relative path.
- **`inspect_server.py` ships alongside `server.py`**: `op_inspect_start` locates it via `Path(__file__).parent / "inspect_server.py"`, and the script itself does `import table_tool`. `agy plugin install` git-clones the whole repo into the install dir, so any committed file ships automatically — just keep `inspect_server.py` at the repo root next to `server.py`/`table_tool.py`. It is spawned with the MCP server's own interpreter (`sys.executable`, stdlib-only script), so it needs no `uv` on PATH and no network at start time.
- Tool JSON schemas in `server.py` were kept byte-identical to the original Node implementation; treat schema changes as breaking.
