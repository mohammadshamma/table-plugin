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

- **Python 3** (must be available on the system path)
- **Node.js 20+**

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

## Example conversation

```
> Create a users table in project.db with name, age, and email columns

> Insert Alice (30, alice@example.com) and Bob (25, bob@example.com)

> Show me the schema of the users table

> Group users by age and count them
```

Antigravity agents will automatically use the table tools to execute these operations.

## Architecture

```
Antigravity ──► MCP Server (server.js) ──► table_tool.py ──► Database
                Node.js + stdio            Python CLI       .db file
```

The MCP server translates tool calls into `table_tool.py` CLI invocations. All data flows as JSON.

## License

MIT
