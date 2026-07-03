---
name: table
description: Database operations for LLM workflows. Create tables, insert rows, join, group by, and run arbitrary SQL.
---

# Table Extension — Database Tools

You have access to database tools via the `table` MCP server. Use these tools to create, query, and manage database tables.

## Available Tools

- **table_create** — Create a new table with typed columns, optional primary key and unique constraints.
- **table_insert** — Insert one or more rows into a table.
- **table_join** — Join two tables into a new table (inner, left, or cross join).
- **table_group_by** — Group rows with aggregation functions (COUNT, SUM, AVG, etc.). Optionally save results to a new table.
- **table_run_sql** — Execute arbitrary SQL for anything not covered by the structured tools.
- **table_schema** — Inspect table columns and types.
- **table_list** — List all tables in a database.
- **table_drop** — Delete a table.

## Usage Notes

- Database tables are automatically local to your agent session and all descendants.
- Always retrieve your current `conversationId` from your User Information/Metadata (e.g. `d6528b8e-...`) and pass it as the `conversation_id` parameter to all tools. This ensures session isolation and sharing with subagents.
- All output is JSON.
- For bulk operations, prefer `table_insert` with multiple rows in a single call.
- Use `table_run_sql` as a fallback for complex queries, ALTER TABLE, indexes, etc.
- Python 3 must be available on the system.
