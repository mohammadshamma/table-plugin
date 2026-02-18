# Table Extension for Gemini CLI

A [Gemini CLI](https://github.com/google-gemini/gemini-cli) extension that gives Gemini structured-data superpowers through a JSON-driven SQLite interface.

## Why this exists

LLMs struggle with structured data. Ask one to compare 50 rows across three columns and you'll see numbers transposed, rows dropped, and calculations quietly drift — a problem commonly known as **context rot**. The more data the model juggles inside its context window, the worse it gets.

Table Extension side-steps this by storing data in SQLite, outside the context window. The LLM works with the data through tool calls — creating tables, inserting rows, joining, grouping, and querying — without ever needing to hold raw rows in context. The result is reliable structured-data handling with dramatically less context rot.

### Use subagents for even less context rot

For best results, populate tables inside dedicated **subagents**. When a subagent finishes, its context is discarded, so the main conversation never sees the raw data at all. The main agent can then query the finished tables cleanly, keeping its own context lean and accurate.

A typical workflow looks like this:

1. Main agent creates a table schema.
2. Main agent delegates to a subagent: *"Read data.csv and insert its rows into the `sales` table."*
3. Subagent does the heavy lifting (parsing, inserting), then exits.
4. Main agent queries the populated table — grouping, joining, aggregating — without any raw data ever touching its context.

## Prerequisites

- **Python 3** (sqlite3 is included in the standard library — no extra install needed)
- **Node.js 20+**

## Install

### From GitHub

```bash
gemini extensions install https://github.com/mohammadshamma/table-extension
```

### Local development

```bash
git clone https://github.com/mohammadshamma/table-extension
cd table-extension
npm install
gemini extensions link .
```

## Tools

| Tool | Description |
|------|-------------|
| `table_create` | Create a table with typed columns, optional PK & unique constraints |
| `table_insert` | Insert rows from a JSON array |
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

Gemini will automatically use the table tools to execute these operations.

## Architecture

```
Gemini CLI ──► MCP Server (server.js) ──► table_tool.py ──► SQLite
               Node.js + stdio            Python CLI       .db file
```

The MCP server translates tool calls into `table_tool.py` CLI invocations. All data flows as JSON.

## License

MIT
