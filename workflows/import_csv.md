---
description: Import a local CSV file into a database table — column names from the header row, column types inferred deterministically, file contents never entering model context.
---

# Import a CSV file into a table

Follow these steps exactly. The `table_import_csv` MCP tool does all the work server-side (parsing, type inference, table creation, inserts); never read the CSV contents yourself.

1. Determine from the user's request:
   - **file path** — the CSV file to import; must be a local file (expand `~` to an absolute path)
   - **table name** — where the data goes (default: the CSV filename stem, lowercased, e.g. `~/data/Sales.csv` → `sales`)
   - **delimiter** — only if the user indicated a non-comma format (e.g. `;` or tab)

   Ask the user for anything you cannot determine.

2. Read your conversationId from your User Information/Metadata and pass it as `conversation_id` in every table tool call in this workflow.

3. Call `table_import_csv` with `table` and `file_path` (and `delimiter` if needed).

4. If it errors with "already exists": ask the user whether to replace the table. Only after they confirm, call `table_drop` and retry the import — never drop silently.

5. Report to the user: the table name, the `inserted` row count, and the inferred column types from the `columns` field of the response.

**Rules:** never read or paste the CSV's rows yourself — the whole point is that the data flows file → SQLite without entering context. If the import fails validation (ragged rows, duplicate headers), report the tool's error message; nothing was created.
