# Table Extension

A JSON-driven SQLite interface designed for LLM workflows. Create tables, insert rows, join tables, group by with aggregations — all through structured MCP tools.

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

The LLM will automatically use the table MCP tools to execute these operations.

## Architecture

```
LLM CLI ──► MCP Server (server.js) ──► table_tool.py ──► SQLite
             Node.js + stdio            Python CLI       .db file
```

The MCP server translates tool calls into table_tool.py CLI invocations. All data flows as JSON.

## License

MIT
