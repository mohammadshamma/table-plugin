# Table Plugin for Antigravity

An Antigravity plugin that gives agents database capabilities through structured tools — including a **durable job queue** that fans a templated task out over every row of a table using parallel subagents.

> This repository is a read-only mirror, published automatically from the maintainer's development repo. Issues are welcome; pull requests are applied upstream and then appear here.

## Why this exists

LLMs struggle with structured data. Ask one to compare 50 rows across three columns and you'll see numbers transposed, rows dropped, and calculations quietly drift — a problem commonly known as **context rot**. The more data the model juggles inside its context window, the worse it gets.

This plugin side-steps this by storing data in a database outside the context window. The agent works with the data through tool calls — creating tables, inserting rows, joining, grouping, and querying — without ever needing to hold raw rows in context. The result is reliable structured-data handling with dramatically less context rot.

### Use subagents for even less context rot

For best results, populate tables inside dedicated **subagents**. When a subagent finishes, its context is discarded, so the main conversation never sees the raw data at all. The main agent can then query the finished tables cleanly, keeping its own context lean and accurate.

A typical workflow looks like this:

1. Main agent creates a table schema.
2. Main agent delegates to a subagent: *"Read data.csv and insert its rows into the `sales` table."*
3. Subagent does the heavy lifting (parsing, inserting), then exits.
4. Main agent queries the populated table — grouping, joining, aggregating — without any raw data ever touching its context.

The [job queue](#job-processing-run-a-task-over-every-row) takes this pattern further: instead of one subagent loading a table, a pool of parallel subagents processes every row of one, with durable bookkeeping that guarantees no row is skipped, dropped, or double-processed.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — that's it. uv provisions Python and the
  MCP SDK automatically, so you don't need Python or Node installed.

The first server launch needs network access to download the pinned
dependencies; uv caches them, so every launch after that is offline and fast.

## Install

To install the plugin using the Antigravity CLI:

```bash
agy plugin install https://github.com/mohammadshamma/table-plugin
```

## Tools

| Tool | Description |
|------|-------------|
| `table_create` | Create a table with typed columns, optional PK & unique constraints |
| `table_insert` | Insert rows from a list |
| `table_join` | Join two tables into a new table (inner/left/cross) |
| `table_group_by` | Group by with aggregations, optionally save to a new table |
| `table_run_sql` | Execute arbitrary SQL |
| `table_schema` | Inspect table columns and types |
| `table_list` | List all tables in a database |
| `table_drop` | Drop a table |
| `table_import_csv` | Import a CSV file into a new table with inferred column types |
| `table_job_create` | Turn every row of a table into one task in a durable work queue |
| `table_job_claim` | Claim the next pending task (id + rendered prompt) |
| `table_job_submit` | Submit a claimed task's result or error |
| `table_job_status` | Task counts and a `complete` flag |
| `table_version` | Inspect plugin version, build hash, loaded status, and file paths |

## Example conversation

```
> Create a users table with name, age, and email columns

> Insert Alice (30, alice@example.com) and Bob (25, bob@example.com)

> Show me the schema of the users table

> Group users by age and count them
```

Antigravity agents will automatically use the table tools to execute these operations.

## Job processing: run a task over every row

The `table_job_*` tools are a **durable work queue** that runs a templated LLM
task over **every row** of a table using parallel subagents, without trusting
the model to enumerate rows. `table_job_create` copies each source row into a
*job table* as a `pending` task (pure SQL, so nothing is missed); worker
subagents then pump `table_job_claim` → `table_job_submit`, and the
orchestrating agent keeps a rolling pool of ~5 single-task workers topped up —
checking `table_job_status` as each finishes — until `complete`.

The queue is built so that LLM nondeterminism can cost retries, but never rows:

- **Every row becomes exactly one task** — job creation is a pure SQL copy;
  the model never lists rows.
- **Crashed workers can't strand work** — claims carry a lease, and expired
  leases are automatically requeued.
- **Poison rows can't loop forever** — retries are capped (`max_attempts`,
  default 3), after which the task is marked `failed` rather than dropped.
- **Finished work can't be overwritten** — a `done` task rejects further
  submissions.
- **Each worker gets a fresh context** — workers claim one task by default
  (`max_claims_per_worker`), so no row is processed by a context polluted by
  previous rows.
- **Safe under real parallelism** — SQLite WAL mode, immediate transactions,
  and busy timeouts let separate worker processes hammer the same queue.

For a one-command entry point, copy the bundled workflow into your project
(or your global workflows directory) once:

```bash
cp workflows/process_table.md /path/to/your/project/.agent/workflows/
```

Then invoke `/process_table` in Antigravity, e.g.:

```
/process_table summarize each row of the reviews table into reviews_summary
```

### Permissions

Under Antigravity's default review policy, every `table_job_claim` /
`table_job_submit` call from every worker subagent triggers a permission
prompt, which makes a parallel job impractical to babysit. Before running a
job, relax the review policy:

- **IDE**: Settings → Antigravity → Advanced Settings → Review policy →
  "Agent Decides" or "Always Proceed".
- **CLI**: use a `/permissions` preset, or `agy --dangerously-skip-permissions`
  for headless runs.

Note that per-tool MCP allowlisting (`"alwaysAllow"` in `mcp_config.json`) is
[currently ignored by Antigravity](https://discuss.ai.google.dev/t/how-to-auto-approve-specific-local-mcp-tools-bypass-accept-prompt-in-antigravity/135984),
so the policy can only be relaxed globally. A relaxed policy auto-approves
**all** agent actions, not just the table tools — restore your usual setting
after the job finishes.

## Inspecting your data in the Auxiliary Pane

Ask the agent to "inspect my tables" (or open the Table Inspector after
creating/importing a table or starting a job), and the agent provides a link to
the inspector:

[Table Inspector](sidecar://table/inspector/)

Clicking the link opens the Table Inspector directly in Antigravity's auxiliary
side pane. It runs as a lightweight, stdlib-only sidecar daemon
(`inspect_server.py`, registered via `sidecars/inspector/sidecar.json`), scoped
automatically per request to the active conversation's SQLite database.

Features:
- **Per-request conversation scoping**: automatically connects to the current
  session database (`<brain>/<root_id>/.tables/session.db`) read-only.
- **Top switcher bar**: quickly navigate between "All tables" and dedicated
  tabs for individual tables and jobs.
- **Dedicated job view**: status summary (pending/claimed/done/failed + a
  `complete` flag) with a filterable, per-task drill-down showing error and
  result columns.
- **Theme synchronization**: dynamically matches Antigravity's light and dark
  editor themes via host `theme-change` events.
- **Zero mutation**: opens databases with `mode=ro` so inspecting never
  modifies session data.

Copy the workflow in for a one-command entry point:

```bash
cp workflows/inspect.md /path/to/your/project/.agent/workflows/
```

## Architecture

```
Antigravity Host ──► MCP Server (server.py) ───────► table_tool.py ──► Session Database
  (stdio, uv run)    14 tools                        imported module   (.tables/session.db)
                                                               ▲
Auxiliary Pane   ──► Sidecar (inspect_server.py) ───────────┘
  (sidecar://)       HTTP, stdlib-only, per-request scoping
```

`server.py` is a single-file [PEP 723](https://peps.python.org/pep-0723/)
script: `uv run` reads its inline metadata, provisions Python and the pinned
`mcp` SDK, and starts the server. Tool calls dispatch directly to functions
imported from `table_tool.py` — no subprocess per call. `table_tool.py` also
hosts lineage resolution helpers and remains usable standalone as a CLI.

`inspect_server.py` is a stdlib-only HTTP server running as an Antigravity
sidecar (`table/inspector`), configured in `sidecars/inspector/sidecar.json`
with `SIDECAR_UI_ENTRYPOINT_AUX_PANE`. It authenticates requests via
`ANTIGRAVITY_SIDECAR_UI_TOKEN`, resolves the active session database per
request from `?conversationId=<id>`, and serves the compact auxiliary pane UI.

## Development

Run the test suites:

```bash
uv run test_session_scoping.py         # server + session-scoped routing
uv run test_table_jobs.py              # table_job_* work-queue tools
uv run test_csv_import.py              # CSV type inference + import wiring
uv run test_inspect.py                 # sidecar config, scoping, auth, UI & theme rendering
python3 -m unittest test_table_tool -v # table_tool CLI (pure stdlib)
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for the full development guide,
including how to test a local checkout inside Antigravity.

## Troubleshooting

### The `table` MCP server fails to start

- **`uv: command not found`** — the launcher runs `uv` through `/bin/sh`, which
  may not have your interactive shell's PATH. Either install uv system-wide or
  edit the installed `mcp_config.json` to use the absolute path of `uv`
  (usually `$HOME/.local/bin/uv`).
- **`No such file or directory ... server.py`** — the config launches the
  server from its standard install location,
  `$HOME/.gemini/config/plugins/table/server.py` (hard-coded because
  Antigravity doesn't substitute variables like `${extensionPath}` and
  resolves relative paths against the session cwd, not the plugin directory).
  If your plugin is installed elsewhere, edit `mcp_config.json` inside the
  plugin directory to point at the absolute path of `server.py`.
- **First launch hangs or fails offline** — the first run downloads the `mcp`
  SDK; make sure you're online once. Subsequent launches use uv's cache.
- **Windows** — the `/bin/sh` launcher assumes a POSIX shell. Edit the
  installed `mcp_config.json` to invoke `uv` directly:
  `{"command": "uv", "args": ["run", "C:\\Users\\<you>\\.gemini\\config\\plugins\\table\\server.py"]}`.

## License

MIT
