#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
table inspector — a localhost, read-only web UI for browsing a session's SQLite
tables and jobs in a browser.

Launched by server.py's table_inspect_start tool as a detached background
process (never run directly by the agent, which does not know the resolved DB
path). Serves server-side HTML with no external assets and no JavaScript
framework, binds 127.0.0.1 only, and opens the DB in read-only mode so the
browser can never mutate the session data.

Usage: inspect_server.py --db <path> --port <port>
"""

import argparse
import html
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import table_tool  # noqa: E402  (shares JOBS_TABLE / RESERVED_TASK_COLUMNS / count helpers)

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200
PREVIEW_CHARS = 120
IDLE_TIMEOUT = 1800  # self-exit after 30 min with no requests (zombie backstop)

STATUS_FILTERS = ("all",) + table_tool.TASK_STATUSES

# Mutable so the idle-watchdog thread and request handlers share it.
_STATE = {"last_activity": time.time(), "db": None}


# ─── DB access (read-only) ───────────────────────────────────────────────────


def connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the DB read-only via a file: URI. Raises sqlite3.OperationalError
    if the file does not exist (mode=ro never creates it)."""
    p = Path(db_path).resolve()
    uri = "file:" + urllib.parse.quote(str(p)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def source_columns(conn: sqlite3.Connection, name: str) -> list:
    """A job table's non-bookkeeping columns, in declared order."""
    cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{name}")')]
    return [c for c in cols if c not in table_tool.RESERVED_TASK_COLUMNS]


def clamp_page(page, size):
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = PAGE_SIZE_DEFAULT
    page = max(1, page)
    size = max(1, min(PAGE_SIZE_MAX, size))
    return page, size


# ─── HTML helpers ────────────────────────────────────────────────────────────

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
       max-width: 1100px; margin-inline: auto; }
h1 { font-size: 1.3rem; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 1.5rem 0 .5rem; }
a { color: #2563eb; text-decoration: none; } a:hover { text-decoration: underline; }
.crumbs { font-size: .85rem; margin-bottom: 1rem; opacity: .8; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { border: 1px solid #8884; padding: .35rem .5rem; text-align: left; vertical-align: top;
         max-width: 32rem; overflow-wrap: anywhere; }
th { background: #8881; position: sticky; top: 0; }
.null { opacity: .4; font-style: italic; }
.muted { opacity: .6; }
.wrap { overflow-x: auto; }
.badge { display: inline-block; padding: .05rem .5rem; border-radius: 1rem; font-size: .75rem;
         font-weight: 600; }
.badge.job { background: #7c3aed22; color: #7c3aed; }
.s-pending { background: #64748b22; color: #64748b; }
.s-claimed { background: #d9770622; color: #d97706; }
.s-done    { background: #16a34a22; color: #16a34a; }
.s-failed  { background: #dc262622; color: #dc2626; }
.cards { display: flex; gap: .75rem; flex-wrap: wrap; margin: .5rem 0; }
.card { border: 1px solid #8884; border-radius: .5rem; padding: .5rem .9rem; min-width: 5rem; }
.card .n { font-size: 1.4rem; font-weight: 700; }
.card .l { font-size: .75rem; text-transform: uppercase; opacity: .7; }
.filters a { margin-right: .6rem; }
.filters a.active { font-weight: 700; text-decoration: underline; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .25rem .9rem; }
dt { font-weight: 600; opacity: .8; }
pre { background: #8881; padding: .6rem .8rem; border-radius: .4rem; overflow-x: auto;
      white-space: pre-wrap; overflow-wrap: anywhere; margin: .3rem 0; }
pre.err { background: #dc262618; }
.note { font-size: .8rem; opacity: .7; margin-top: 1.5rem; }
.pager { margin: .8rem 0; display: flex; gap: 1rem; align-items: center; }
"""


def page(title: str, body: str, crumbs: str = "") -> str:
    crumb_html = f'<div class="crumbs">{crumbs}</div>' if crumbs else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        f"{crumb_html}{body}"
        '<div class="note">Read-only inspector. Status reflects the last agent '
        "activity; expired leases requeue on the next agent action.</div>"
        "</body></html>"
    )


def fmt(value) -> str:
    if value is None:
        return '<span class="null">∅</span>'
    return html.escape(str(value))


def preview(value) -> str:
    if value is None:
        return '<span class="null">∅</span>'
    s = str(value)
    if len(s) > PREVIEW_CHARS:
        s = s[:PREVIEW_CHARS] + "…"
    return html.escape(s)


def status_badge(status) -> str:
    cls = f"s-{status}" if status in table_tool.TASK_STATUSES else ""
    return f'<span class="badge {cls}">{html.escape(str(status))}</span>'


def pager(base_path: str, params: dict, page_num: int, pages: int) -> str:
    def link(p, label):
        q = dict(params, page=p)
        return f'<a href="{base_path}?{urllib.parse.urlencode(q)}">{label}</a>'

    parts = []
    parts.append(link(page_num - 1, "← Prev") if page_num > 1 else '<span class="muted">← Prev</span>')
    parts.append(f'<span class="muted">Page {page_num} of {pages}</span>')
    parts.append(link(page_num + 1, "Next →") if page_num < pages else '<span class="muted">Next →</span>')
    return '<div class="pager">' + "".join(parts) + "</div>"


# ─── Render functions (pure — return (status_code, html)) ────────────────────


def render_index(conn: sqlite3.Connection) -> tuple[int, str]:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    jobs = set(table_tool.list_job_tables(conn))

    if not tables:
        return 200, page("Tables", "<h1>Tables</h1><p class='muted'>No tables yet.</p>")

    rows = []
    for name in tables:
        if name == table_tool.JOBS_TABLE:
            continue  # the internal registry; surfaced via the job views instead
        count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        is_job = name in jobs
        href = ("/job?" if is_job else "/table?") + urllib.parse.urlencode({"name": name})
        badge = ' <span class="badge job">job</span>' if is_job else ""
        rows.append(
            f'<tr><td><a href="{href}">{html.escape(name)}</a>{badge}</td>'
            f"<td>{count}</td></tr>"
        )

    body = (
        "<h1>Tables</h1>"
        '<div class="wrap"><table><thead><tr><th>Table</th><th>Rows</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return 200, page("Tables", body)


def render_table(conn: sqlite3.Connection, name: str, page_num, size) -> tuple[int, str]:
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs='<a href="/">← Tables</a>')
    page_num, size = clamp_page(page_num, size)
    total = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    pages = max(1, math.ceil(total / size))
    page_num = min(page_num, pages)
    offset = (page_num - 1) * size

    cur = conn.execute(f'SELECT rowid AS rowid, * FROM "{name}" LIMIT ? OFFSET ?', (size, offset))
    cols = [d[0] for d in cur.description]
    header = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body_rows = []
    for row in cur.fetchall():
        cells = "".join(f"<td>{fmt(row[c])}</td>" for c in cols)
        body_rows.append(f"<tr>{cells}</tr>")

    body = (
        f"<h1>{html.escape(name)}</h1>"
        f'<p class="muted">{total} row(s)</p>'
        + pager("/table", {"name": name, "size": size}, page_num, pages)
        + '<div class="wrap"><table><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
        + pager("/table", {"name": name, "size": size}, page_num, pages)
    )
    return 200, page(name, body, crumbs='<a href="/">← Tables</a>')


def render_job(conn: sqlite3.Connection, name: str, status, page_num, size) -> tuple[int, str]:
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs='<a href="/">← Tables</a>')
    if name not in set(table_tool.list_job_tables(conn)):
        # Not a job table — fall back to the plain row browser.
        return render_table(conn, name, page_num, size)

    status = status if status in STATUS_FILTERS else "all"
    page_num, size = clamp_page(page_num, size)
    counts = table_tool.count_task_statuses(conn, name)
    total = sum(counts.values())
    complete = counts["pending"] + counts["claimed"] == 0

    # Status summary cards.
    cards = [f'<div class="card"><div class="n">{total}</div><div class="l">total</div></div>']
    for st in table_tool.TASK_STATUSES:
        cards.append(
            f'<div class="card"><div class="n">{counts[st]}</div>'
            f'<div class="l">{st}</div></div>'
        )
    complete_txt = ("✓ complete" if complete else "… in progress")

    # Filter bar.
    filters = []
    for st in STATUS_FILTERS:
        label = st if st == "all" else f"{st} ({counts.get(st, 0)})"
        cls = "active" if st == status else ""
        q = urllib.parse.urlencode({"name": name, "status": st, "size": size})
        filters.append(f'<a class="{cls}" href="/job?{q}">{html.escape(label)}</a>')

    # Task list.
    where = "" if status == "all" else f" WHERE _task_status = '{status}'"
    filtered_total = conn.execute(
        f'SELECT COUNT(*) FROM "{name}"{where}'
    ).fetchone()[0]
    pages = max(1, math.ceil(filtered_total / size)) if filtered_total else 1
    page_num = min(page_num, pages)
    offset = (page_num - 1) * size

    srccols = source_columns(conn, name)
    src_headers = "".join(f"<th>{html.escape(c)}</th>" for c in srccols)
    task_rows = []
    for row in conn.execute(
        f'SELECT rowid AS _task_id, * FROM "{name}"{where} ORDER BY rowid LIMIT ? OFFSET ?',
        (size, offset),
    ):
        tid = row["_task_id"]
        q = urllib.parse.urlencode({"name": name, "id": tid})
        src_cells = "".join(f"<td>{preview(row[c])}</td>" for c in srccols)
        outcome = row["_task_error"] if row["_task_status"] == "failed" else row["result"]
        task_rows.append(
            f'<tr><td><a href="/task?{q}">#{tid}</a></td>'
            f"<td>{status_badge(row['_task_status'])}</td>"
            f"<td>{row['_task_attempts']}</td>"
            f"<td>{fmt(row['_task_claimed_by'])}</td>"
            f"{src_cells}"
            f"<td>{preview(outcome)}</td></tr>"
        )

    params = {"name": name, "status": status, "size": size}
    body = (
        f"<h1>{html.escape(name)} <span class='badge job'>job</span></h1>"
        f'<p class="muted">{html.escape(complete_txt)}</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<div class="filters">{"".join(filters)}</div>'
        + pager("/job", params, page_num, pages)
        + '<div class="wrap"><table><thead><tr>'
        + "<th>Task</th><th>Status</th><th>Attempts</th><th>Claimed by</th>"
        + src_headers
        + "<th>Result / error</th></tr></thead><tbody>"
        + ("".join(task_rows) or '<tr><td colspan="99" class="muted">No tasks.</td></tr>')
        + "</tbody></table></div>"
        + pager("/job", params, page_num, pages)
    )
    return 200, page(name, body, crumbs='<a href="/">← Tables</a>')


def render_task(conn: sqlite3.Connection, name: str, rowid) -> tuple[int, str]:
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs='<a href="/">← Tables</a>')
    try:
        rowid = int(rowid)
    except (TypeError, ValueError):
        return 404, page("Not found", "<h1>Invalid task id</h1>",
                         crumbs='<a href="/">← Tables</a>')
    row = conn.execute(
        f'SELECT rowid AS _task_id, * FROM "{name}" WHERE rowid = ?', (rowid,)
    ).fetchone()
    if row is None:
        return 404, page("Not found", f"<h1>No task #{rowid}</h1>",
                         crumbs=f'<a href="/job?{urllib.parse.urlencode({"name": name})}">← {html.escape(name)}</a>')

    # Source columns as a definition list.
    src_items = "".join(
        f"<dt>{html.escape(c)}</dt><dd>{fmt(row[c])}</dd>" for c in source_columns(conn, name)
    )
    lease = row["_task_lease_expires"]
    lease_txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease)) if lease else None
    book = (
        "<dl>"
        f"<dt>status</dt><dd>{status_badge(row['_task_status'])}</dd>"
        f"<dt>attempts</dt><dd>{fmt(row['_task_attempts'])}</dd>"
        f"<dt>claimed by</dt><dd>{fmt(row['_task_claimed_by'])}</dd>"
        f"<dt>lease expires</dt><dd>{fmt(lease_txt)}</dd>"
        "</dl>"
    )
    err_html = (
        f'<h2>Error</h2><pre class="err">{html.escape(str(row["_task_error"]))}</pre>'
        if row["_task_error"] is not None else ""
    )
    result_html = (
        f"<h2>Result</h2><pre>{html.escape(str(row['result']))}</pre>"
        if row["result"] is not None
        else '<h2>Result</h2><p class="muted">No result yet.</p>'
    )

    crumbs = f'<a href="/">← Tables</a> / <a href="/job?{urllib.parse.urlencode({"name": name})}">{html.escape(name)}</a>'
    body = (
        f"<h1>Task #{rowid} <span class='muted'>in {html.escape(name)}</span></h1>"
        "<h2>Source</h2>"
        f"<dl>{src_items}</dl>"
        "<h2>Task bookkeeping</h2>"
        f"{book}{err_html}{result_html}"
    )
    return 200, page(f"Task #{rowid}", body, crumbs=crumbs)


# ─── HTTP plumbing ───────────────────────────────────────────────────────────

ROUTES = {
    "/table": lambda conn, q: render_table(conn, q.get("name", [""])[0],
                                           q.get("page", [1])[0], q.get("size", [PAGE_SIZE_DEFAULT])[0]),
    "/job": lambda conn, q: render_job(conn, q.get("name", [""])[0], q.get("status", ["all"])[0],
                                       q.get("page", [1])[0], q.get("size", [PAGE_SIZE_DEFAULT])[0]),
    "/task": lambda conn, q: render_task(conn, q.get("name", [""])[0], q.get("id", [None])[0]),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "table-inspector/0.0.4"

    def log_message(self, *args):
        pass  # stdout/stderr are /dev/null anyway; stay quiet

    def _send(self, status: int, body: str, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        _STATE["last_activity"] = time.time()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return

        try:
            conn = connect_ro(_STATE["db"])
        except sqlite3.OperationalError:
            # DB file not created yet (no tables). Present an empty state.
            self._send(200, page("Tables", "<h1>Tables</h1>"
                                 "<p class='muted'>No database yet — create a table first.</p>"))
            return

        try:
            if path == "/":
                status, body = render_index(conn)
            elif path in ROUTES:
                q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                status, body = ROUTES[path](conn, q)
            else:
                status, body = 404, page("Not found", "<h1>404</h1>"
                                         '<p><a href="/">← Tables</a></p>')
            self._send(status, body)
        except Exception as e:  # never leak a stack trace to the browser
            self._send(500, page("Error", f"<h1>Inspector error</h1><pre>{html.escape(str(e))}</pre>"))
        finally:
            conn.close()


def _idle_watchdog():
    """Exit if idle too long or the DB disappears — a zombie-server backstop."""
    while True:
        time.sleep(60)
        if time.time() - _STATE["last_activity"] > IDLE_TIMEOUT:
            os._exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only web inspector for a session's tables")
    parser.add_argument("--db", required=True, help="Path to the SQLite database")
    parser.add_argument("--port", required=True, type=int, help="TCP port to bind on 127.0.0.1")
    args = parser.parse_args(argv)

    _STATE["db"] = args.db
    _STATE["last_activity"] = time.time()

    threading.Thread(target=_idle_watchdog, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
