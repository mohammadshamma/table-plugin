---
description: Turn on a local, read-only web UI so the user can browse any row of any table in the session — plus a status and per-task drill-down view for job tables — in their browser.
---

# Inspect tables in a browser

Follow these steps exactly. The `table_inspect_start` MCP tool launches a
localhost, read-only web server against this session's database and returns a URL;
`table_inspect_stop` shuts it down. The web UI does all the rendering — you never
paste table rows into your context.

1. Recognize the intent: the user wants to *look at* their data — e.g. "inspect my
   tables", "let me browse the data", "show me the job that's running", "open the
   table viewer".

2. Read your conversationId from your User Information/Metadata and pass it as
   `conversation_id` in every table tool call in this workflow (so the inspector
   points at the same session database as your other table tools).

3. Call `table_inspect_start`. Pass `port` only if the user asked for a specific
   one. The tool is idempotent — if an inspector is already running it just returns
   the existing URL (`already_running: true`); do not try to start a second one.

4. Give the user the returned `url` and tell them to open it in a browser. Mention
   that they can browse every table's rows (paginated, and sortable by clicking any
   column header) and that **job tables get a special view**: a status summary
   (pending/claimed/done/failed and a complete flag) with a filterable, per-task
   drill-down showing each task's error and result. Pages auto-refresh in place
   (the job view every ~2s), so a running job can be watched live without reloading.

5. When the user is done — or says "stop" / "turn it off" / "close the inspector" —
   call `table_inspect_stop`.

**Rules:** the inspector is localhost-only and **read-only** — it can never modify
the data, and its `claimed` counts may lag a live `table_job_status` call because it
does not requeue expired leases. It shows whatever is in the session database right
now; you don't need to pre-load or summarize anything for it.
