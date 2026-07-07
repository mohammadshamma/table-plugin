# Table Plugin for Antigravity

An Antigravity plugin that gives agents database capabilities through structured tools.

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
| `table_job_create` | Turn every row of a table into one task in a durable work queue |
| `table_job_claim` | Claim the next pending task (id + rendered prompt) |
| `table_job_submit` | Submit a claimed task's result or error |
| `table_job_status` | Task counts and a `complete` flag |
| `table_inspect_start` | Launch a localhost, read-only web UI to browse tables and jobs; returns a URL |
| `table_inspect_stop` | Shut down the inspector web UI |

## Example conversation

```
> Create a users table with name, age, and email columns

> Insert Alice (30, alice@example.com) and Bob (25, bob@example.com)

> Show me the schema of the users table

> Group users by age and count them
```

Antigravity agents will automatically use the table tools to execute these operations.

## Row-level fan-out with subagents

The `table_job_*` tools run a templated LLM task over **every row** of a table
using parallel subagents, without trusting the model to enumerate rows.
`table_job_create` copies each source row into a *job table* as a `pending`
task (pure SQL, so nothing is missed); worker subagents then pump
`table_job_claim` → `table_job_submit`, and the orchestrating agent loops on
`table_job_status` until `complete`. Crashed workers are recovered via claim
leases, poison rows are capped by a retry limit and marked `failed`, and
finished work can't be overwritten — LLM nondeterminism can cost retries, but
never rows.

For a one-command entry point, copy the bundled workflow into your project
(or your global workflows directory) once:

```bash
cp workflows/process_table.md /path/to/your/project/.agent/workflows/
```

Then invoke `/process_table` in Antigravity, e.g.:

```
/process_table summarize each row of the reviews table into reviews_summary
```

## Inspecting your data in a browser

Ask the agent to "inspect my tables" and it calls `table_inspect_start`, which
launches a small localhost web UI (`inspect_server.py`, stdlib-only) and hands
you a URL. You can page through every row of any table, and **job tables get a
dedicated view**: a status summary (pending/claimed/done/failed + a `complete`
flag) with a filterable, per-task drill-down showing each task's error and
result. The server binds `127.0.0.1` and opens the database **read-only**, so
browsing can never mutate your session data; `table_inspect_stop` shuts it down.

Copy the workflow in for a one-command entry point:

```bash
cp workflows/inspect.md /path/to/your/project/.agent/workflows/
```

## Architecture

```
Antigravity ──► MCP Server (server.py) ──► table_tool.py ──► Database
                Python + stdio (uv run)    imported module   .db file
```

`server.py` is a single-file [PEP 723](https://peps.python.org/pep-0723/)
script: `uv run` reads its inline metadata, provisions Python and the pinned
`mcp` SDK, and starts the server. Tool calls dispatch directly to functions
imported from `table_tool.py` — no subprocess per call. `table_tool.py` also
remains usable standalone as a CLI.

## Development

Run the test suites:

```bash
uv run test_session_scoping.py         # server + session-scoped routing
uv run test_table_jobs.py              # table_job_* work-queue tools
python3 -m unittest test_table_tool -v # table_tool CLI (pure stdlib)
```

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
