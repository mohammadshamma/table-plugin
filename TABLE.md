# sqltool — SQLite Database Tools

You have access to SQLite database tools via the `sqltool` MCP server. Use these tools to create, query, and manage SQLite databases.

## Available Tools

- **create_table** — Create a new table with typed columns, optional primary key and unique constraints.
- **insert_rows** — Insert one or more rows into a table.
- **join_tables** — Join two tables into a new table (inner, left, or cross join).
- **group_by** — Group rows with aggregation functions (COUNT, SUM, AVG, etc.). Optionally save results to a new table.
- **run_sql** — Execute arbitrary SQL for anything not covered by the structured tools.
- **get_schema** — Inspect table columns and types.
- **list_tables** — List all tables in a database.
- **drop_table** — Delete a table.

## Usage Notes

- Database files are created automatically on first use — no init step needed.
- All tools accept a `db` path. Use relative paths for project-local databases (e.g. `./data.db`).
- All output is JSON.
- For bulk operations, prefer `insert_rows` with multiple rows in a single call.
- Use `run_sql` as a fallback for complex queries, ALTER TABLE, indexes, etc.
- Python 3 must be available on the system (sqlite3 is included in Python's standard library).
