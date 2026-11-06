---
description: Surface the Table Inspector auxiliary pane link so the user can browse table rows and monitor job progress in the IDE's side pane.
---

# Inspect tables in the Table Inspector

Follow these steps exactly. Antigravity provides an auxiliary pane UI extension
for inspecting tables and job progress. You do not need to call any MCP tools to
start or stop the inspector — instead, surface the `[Table Inspector](sidecar://table/inspector/)`
link to open the view in the auxiliary side pane. The web UI does all the
rendering — you never paste table rows into your context.

1. Recognize the intent: the user wants to *look at* their data — e.g. "inspect my
   tables", "let me browse the data", "show me the job that's running", "open the
   table viewer", "open inspector".

2. Provide the user with the link to open the auxiliary pane:
   `[Table Inspector](sidecar://table/inspector/)`

3. Explain what they can do in the inspector:
   - Browse every table's rows (paginated, sortable by clicking any column header).
   - Use the top switcher bar to toggle between "All tables" and individual tables or jobs.
   - For **job tables**, see a live status summary (pending/claimed/done/failed and a
     complete flag) with a filterable, per-task drill-down showing each task's error
     and result.
   - Pages auto-refresh in place (the job view every ~2s), so a running job can be
     watched live without reloading.

4. If the user asks to close or stop the inspector, inform them that they can simply
   close or collapse the Table Inspector auxiliary pane in their IDE; no MCP tool
   call or background server shutdown is needed.

**Rules:** the inspector is **read-only** — it can never modify session data, and
its `claimed` counts may lag a live `table_job_status` call because it does not
requeue expired leases. It automatically scopes to the active session database; you
don't need to pre-load or summarize anything for it. Do not attempt to call
`table_inspect_start` or `table_inspect_stop` as those tools are no longer present.
