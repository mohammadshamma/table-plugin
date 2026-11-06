# Development guide

How to hack on the plugin and test your changes — first in isolation, then
inside a real Antigravity session.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — provisions Python and the pinned
  `mcp` SDK automatically (`server.py` and most tests are
  [PEP 723](https://peps.python.org/pep-0723/) single-file scripts). The
  first `uv run` needs network access once; everything after is cached.
- **Antigravity** (the IDE and/or the `agy` CLI) — only needed for the
  end-to-end steps below. The test suites and the MCP server itself run
  without it.
- A git checkout of this repo.

## Fast inner loop (no Antigravity needed)

### Run the tests

```bash
uv run test_session_scoping.py         # server + session-scoped DB routing
uv run test_table_jobs.py              # table_job_* work-queue semantics
uv run test_csv_import.py              # CSV type inference + import wiring
uv run test_inspect.py                 # sidecar config, scoping, auth, UI & theme rendering
python3 -m unittest test_table_tool -v # table_tool CLI (pure stdlib)
```

Run a single test the same way:

```bash
uv run test_table_jobs.py TestClassName.test_name
python3 -m unittest test_table_tool.TestClassName.test_name -v
```

> **Note:** run the suites as a regular user, not root. A few
> `test_session_scoping.py` cases verify behavior against read-only files and
> directories, and root bypasses file permissions, so those cases fail
> spuriously under root.

### Exercise the SQLite layer directly

`table_tool.py` is also a standalone CLI, handy for poking at the data layer
without any MCP plumbing:

```bash
python3 table_tool.py create-table /tmp/dev.db users '{"columns": {"name": "TEXT NOT NULL", "age": "INTEGER"}}'
python3 table_tool.py insert /tmp/dev.db users '{"rows": [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]}'
python3 table_tool.py query /tmp/dev.db 'SELECT age, COUNT(*) AS n FROM users GROUP BY age'
```

Every subcommand prints JSON; run `python3 table_tool.py -h` for the full list.

### Smoke-test the MCP server over stdio

The server speaks JSON-RPC on stdin/stdout, so you can drive a full MCP
handshake from the shell without Antigravity:

```bash
{ printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  sleep 2
} | uv run server.py
```

The response to `id: 2` should list all 14 `table_*` tools. (The `sleep`
keeps stdin open long enough for the server to answer before the pipe
closes.)

When run this way, with no `ANTIGRAVITY_CONVERSATION_ID` in the environment,
tool calls write to `./session.db` in the current directory (gitignored) —
convenient for inspecting results afterwards with `sqlite3`.

## Testing your checkout inside Antigravity

Antigravity launches the server from the hard-coded install path
`$HOME/.gemini/config/plugins/table/server.py` (see `mcp_config.json`; the
path is absolute because Antigravity doesn't substitute variables like
`${extensionPath}` and resolves relative paths against the session cwd). So
the trick to a live-editable setup is making that path *be* your checkout.

### Recommended: symlink your checkout over the install dir

Install the plugin once so Antigravity registers it, then replace the
installed copy with a symlink to your working tree:

```bash
agy plugin install https://github.com/mohammadshamma/table-plugin   # once
rm -rf "$HOME/.gemini/config/plugins/table"
ln -s /path/to/your/checkout "$HOME/.gemini/config/plugins/table"
```

Because `agy plugin install` git-clones the whole repo into the install dir,
the installed layout is identical to a checkout — so the symlink swaps in
everything at once: `server.py`, `table_tool.py`, `inspect_server.py`,
`skills/`, and `workflows/`. Edits in your checkout take effect the next time
the server process starts (see below); no reinstall step in the loop.

To go back to the released version, remove the symlink and reinstall:

```bash
rm "$HOME/.gemini/config/plugins/table"
agy plugin install https://github.com/mohammadshamma/table-plugin
```

### Alternative: install straight from the local directory

```bash
agy plugin install /path/to/your/checkout
```

You can validate the plugin structure before installing:
```bash
agy plugin validate table-plugin
```
This confirms that `plugin.json`, skills, rules, MCP servers, and sidecars (`sidecars 1`) are recognized. Note that installing straight from a directory copies a snapshot of your checkout, so you must re-run the install after every change — fine for a one-off test, tedious as a loop. The symlink
approach avoids it.

### Picking up changes

- **`server.py` / `table_tool.py`** — loaded when the MCP server process
  starts, so restart it: start a new Antigravity conversation, or reload the
  MCP servers from the IDE's MCP panel.
- **`skills/table/SKILL.md` / `workflows/*.md`** — read per conversation;
  start a new conversation to pick up edits.
- **`inspect_server.py` / `sidecar.json`** — managed as a persistent sidecar
  daemon by Antigravity (`table/inspector`). In development, reload the
  sidecar or restart Antigravity to pick up sidecar edits. You can also run it
  standalone for rapid UI iteration:
  `python3 inspect_server.py --port 8760 --db /tmp/dev.db`

### Verify end-to-end

1. Open Antigravity and confirm the `table` MCP server is up with all 14
   tools (the IDE's MCP panel shows the tool list and any startup errors).
2. Run the smoke conversation from the README:
   ```
   > Create a users table with name, age, and email columns
   > Insert Alice (30, alice@example.com) and Bob (25, bob@example.com)
   > Group users by age and count them
   ```
3. Ask the agent to *"inspect my tables"* — the agent will provide a
   `[Table Inspector](sidecar://table/inspector/)` link that opens the
   session-scoped Table Inspector in Antigravity's auxiliary side pane.

### Where the data lands

Inside Antigravity, each session's database lives at

```
~/.gemini/antigravity/brain/<root_conversation_id>/.tables/session.db
```

(the *root* id, so a main agent and its subagents share one DB; the fallback
when no brain directory is writable is
`~/.gemini/antigravity/scratch/session.db`). You can open it directly:

```bash
sqlite3 ~/.gemini/antigravity/brain/<id>/.tables/session.db '.tables'
```

To reproduce the session routing outside Antigravity, export
`ANTIGRAVITY_CONVERSATION_ID=<id>` before driving the server over stdio.

## Debugging tips

- **Server won't start in Antigravity** — see the
  [Troubleshooting](README.md#troubleshooting) section of the README (PATH
  issues with `uv`, install-path mismatches, first-launch network).
- **Sidecar inspector** — Antigravity discovers the sidecar at
  `sidecars/inspector/sidecar.json` and runs it as an auxiliary pane UI
  extension (`table/inspector`). The sidecar dynamically resolves the SQLite
  DB for the active conversation from `?conversationId=<id>` using
  `table_tool.find_root_conversation()`, authenticates via
  `ANTIGRAVITY_SIDECAR_UI_TOKEN`, and handles light/dark themes via
  host `theme-change` postMessages.
- **Job bookkeeping** — a job table carries its queue state in the
  `_task_status`, `_task_error`, `_task_attempts`, `_task_lease_expires`, and
  `_task_claimed_by` columns; query them with `table_run_sql` or `sqlite3`
  when a job misbehaves, or use the inspector's per-task drill-down.

## Before you ship

- Run all five test suites (above).
- Tool JSON schemas in `server.py` are frozen — treat any schema change as a
  breaking change.
- If you changed tool behavior or schemas, update `skills/table/SKILL.md`
  and the relevant `workflows/*.md` recipe to match; every user-initiated
  operation gets a workflow recipe.
- Keep `inspect_server.py` at the repo root next to `server.py` — it is
  invoked by `sidecars/inspector/sidecar.json` (`../../inspect_server.py`) and
  imports `table_tool`.
- Bump `version` in `plugin.json` and `PLUGIN_VERSION` in `server.py`.
