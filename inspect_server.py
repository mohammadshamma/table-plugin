#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
table inspector — a localhost, read-only web UI for browsing a session's SQLite
tables and jobs in an Antigravity auxiliary pane or browser.

Runs as an Antigravity sidecar daemon registered via sidecars/inspector/sidecar.json
(or standalone with --db). In sidecar mode, dynamically resolves the active session
database per request from ?conversationId=<id> and authenticates requests via
ANTIGRAVITY_SIDECAR_UI_TOKEN. Serves server-side HTML with no external assets and
no JavaScript framework, binds 127.0.0.1 only, and opens the DB in read-only mode so
the UI can never mutate session data. A small inline script handles live auto-refresh
and Antigravity theme synchronization.

Usage:
  inspect_server.py [--port <port>]                     # sidecar mode (scoped per request)
  inspect_server.py --db <path> [--port <port>]         # standalone mode
"""

import argparse
import html
import hmac
import json
import math
import os
import re
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
REFRESH_JOB_SECONDS = 2  # job pages track live worker progress
REFRESH_SECONDS = 5  # other pages: row counts and results still change mid-job
INSPECT_PIDFILE_NAME = ".inspect.json"
DEFAULT_PORT = 8760

STATUS_FILTERS = ("all",) + table_tool.TASK_STATUSES

CONV_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
THREADED_PARAMS = ("conversationId", "token")

# Mutable so the idle-watchdog thread and request handlers share it.
_STATE = {"last_activity": time.time(), "db": None}


# ─── DB access (read-only) ───────────────────────────────────────────────────


def connect_ro(db_path: str | None) -> sqlite3.Connection:
    """Open the DB read-only via a file: URI. Raises sqlite3.OperationalError
    if the file does not exist or db_path is None (mode=ro never creates it)."""
    if not db_path:
        raise sqlite3.OperationalError("No database configured")
    p = Path(db_path).resolve()
    uri = "file:" + urllib.parse.quote(str(p)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_request_db(conv_id: str | None) -> Path | None:
    """Resolve the SQLite database path for a request.

    Returns:
        Path: when a valid database path is determined (may or may not exist on disk).
        None: when the request represents an empty state (missing, empty, or '_new').

    Raises:
        ValueError: if conv_id contains invalid characters.
    """
    if conv_id is None:
        if _STATE.get("db"):
            return Path(_STATE["db"])
        return None

    if conv_id == "" or conv_id == "_new":
        if _STATE.get("db"):
            return Path(_STATE["db"])
        return None

    if not CONV_ID_RE.match(conv_id):
        raise ValueError("Invalid conversation ID")

    brain_dir = table_tool.get_brain_dir()

    with table_tool.LINEAGE_CACHE_LOCK:
        root_id = table_tool.LINEAGE_CACHE.get(conv_id)

    if not root_id:
        direct_db = brain_dir / conv_id / ".tables" / "session.db"
        if direct_db.is_file():
            root_id = conv_id
            with table_tool.LINEAGE_CACHE_LOCK:
                table_tool.LINEAGE_CACHE[conv_id] = root_id
        else:
            root_id = table_tool.find_root_conversation(conv_id, brain_dir)
            if root_id and root_id != conv_id:
                with table_tool.LINEAGE_CACHE_LOCK:
                    table_tool.LINEAGE_CACHE[conv_id] = root_id
            elif not root_id:
                root_id = conv_id

    return brain_dir / root_id / ".tables" / "session.db"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def source_columns(conn: sqlite3.Connection, name: str) -> list:
    """A job table's non-bookkeeping columns, in declared order."""
    cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{name}")')]
    return [c for c in cols if table_tool._fold(c) not in table_tool._RESERVED_FOLDED]


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


SORT_DIRS = ("asc", "desc")


def sort_key(conn, name, sort, direction):
    """Validate a requested sort against the table's real columns (+ rowid) —
    ORDER BY identifiers cannot be ?-bound, so only whitelisted names may be
    interpolated. Returns (col, direction): col is None when no/invalid sort
    was requested (the caller keeps its default order); direction is always
    asc|desc. Call only after the table_exists gate."""
    direction = direction if direction in SORT_DIRS else "asc"
    if not sort:
        return None, direction
    cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{name}")')]
    if sort != "rowid" and sort not in cols:
        return None, direction
    return sort, direction


def order_clause(col, direction, default=""):
    """' ORDER BY "col" ASC|DESC, rowid' — the rowid tiebreaker keeps rows with
    equal sort keys stable across pages and refreshes."""
    if col is None:
        return default
    quoted = col.replace('"', '""')
    return f' ORDER BY "{quoted}" {direction.upper()}, rowid'


# ─── URL building & Navigation ───────────────────────────────────────────────

def build_url(
    path: str,
    params: dict | None = None,
    base_query: dict | str | None = None,
) -> str:
    """Build an internal URL preserving threaded query parameters (conversationId, token).

    Route-specific parameters from `params` are placed first to preserve deterministic
    URL prefixes and backward compatibility, followed by threaded parameters from `base_query`.
    """
    parsed = urllib.parse.urlparse(path)
    base_path = parsed.path

    threaded: dict[str, str] = {}
    if base_query:
        if isinstance(base_query, str):
            bq = urllib.parse.parse_qs(base_query, keep_blank_values=True)
        else:
            bq = base_query
        for key in THREADED_PARAMS:
            if key in bq:
                val = bq[key]
                if isinstance(val, list):
                    if val and val[0]:
                        threaded[key] = val[0]
                elif val:
                    threaded[key] = str(val)

    combined: dict[str, str] = {}
    if parsed.query:
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            combined[k] = v

    if params:
        for k, v in params.items():
            if v is not None:
                combined[k] = str(v)

    for k, v in threaded.items():
        if k not in combined:
            combined[k] = v

    if combined:
        return f"{base_path}?{urllib.parse.urlencode(combined)}"
    return base_path


def discover_tables_and_jobs(conn: sqlite3.Connection | None) -> tuple[list[str], list[str]]:
    """Discover user tables and job tables from the SQLite database.
    Returns (user_tables, job_tables) sorted alphabetically.
    """
    if conn is None:
        return [], []
    try:
        raw_tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        jobs = set(table_tool.list_job_tables(conn))
        user_tables = [t for t in raw_tables if t != table_tool.JOBS_TABLE and t not in jobs]
        job_tables = [t for t in raw_tables if t in jobs]
        return user_tables, job_tables
    except Exception:
        return [], []


def render_switcher(
    conn: sqlite3.Connection | None,
    active_route: str = "/",
    active_name: str | None = None,
    base_query: dict | str | None = None,
) -> str:
    """Render the compact horizontal top switcher pill bar across all views."""
    user_tables, job_tables = discover_tables_and_jobs(conn)

    pills = []
    # 1. "All tables" pill
    all_active = ' active" aria-current="page' if active_route == "/" else ""
    all_url = build_url("/", None, base_query)
    pills.append(f'<a class="pill{all_active}" href="{all_url}">All tables</a>')

    # 2. User table pills
    for t in user_tables:
        is_active = (
            ' active" aria-current="page'
            if (active_route == "/table" and active_name == t)
            else ""
        )
        url = build_url("/table", {"name": t}, base_query)
        pills.append(f'<a class="pill{is_active}" href="{url}">{html.escape(t)}</a>')

    # 3. Job table pills
    for j in job_tables:
        is_active = (
            ' active" aria-current="page'
            if (active_route in ("/job", "/task") and active_name == j)
            else ""
        )
        url = build_url("/job", {"name": j}, base_query)
        pills.append(
            f'<a class="pill job-pill{is_active}" href="{url}">'
            f'{html.escape(j)} <span class="badge job">job</span></a>'
        )

    return f'<nav class="switcher">{"".join(pills)}</nav>'


# ─── HTML helpers ────────────────────────────────────────────────────────────

STYLE = """
:root {
  color-scheme: light dark;
  --bg: var(--background, var(--vscode-editor-background, #ffffff));
  --fg: var(--foreground, var(--vscode-editor-foreground, #1f2328));
  --fg-muted: var(--muted, var(--vscode-descriptionForeground, #656d76));
  --link: var(--link-color, var(--vscode-textLink-foreground, #0969da));
  --border: var(--border-color, var(--vscode-panel-border, #d0d7de));
  --th-bg: var(--table-header-bg, var(--vscode-editorGroupHeader-tabsBackground, #f6f8fa));
  --card-bg: var(--card-background, var(--vscode-sideBar-background, #f6f8fa));
  --pre-bg: var(--code-background, var(--vscode-textCodeBlock-background, #f6f8fa));
  --pill-bg: var(--pill-background, #eaeef2);
  --pill-hover-bg: var(--pill-hover-background, #dbe2e8);
  --pill-active-bg: var(--accent, var(--vscode-button-background, #0969da));
  --pill-active-fg: var(--accent-foreground, var(--vscode-button-foreground, #ffffff));
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: var(--background, var(--vscode-editor-background, #0d1117));
    --fg: var(--foreground, var(--vscode-editor-foreground, #e6edf3));
    --fg-muted: var(--muted, var(--vscode-descriptionForeground, #848d97));
    --link: var(--link-color, var(--vscode-textLink-foreground, #4493f8));
    --border: var(--border-color, var(--vscode-panel-border, #30363d));
    --th-bg: var(--table-header-bg, var(--vscode-editorGroupHeader-tabsBackground, #161b22));
    --card-bg: var(--card-background, var(--vscode-sideBar-background, #161b22));
    --pre-bg: var(--code-background, var(--vscode-textCodeBlock-background, #161b22));
    --pill-bg: var(--pill-background, #21262d);
    --pill-hover-bg: var(--pill-hover-background, #30363d);
    --pill-active-bg: var(--accent, var(--vscode-button-background, #1f6feb));
    --pill-active-fg: var(--accent-foreground, var(--vscode-button-foreground, #ffffff));
  }
}

:root[data-theme="light"] {
  --bg: var(--background, #ffffff);
  --fg: var(--foreground, #1f2328);
  --fg-muted: var(--muted, #656d76);
  --link: var(--link-color, #0969da);
  --border: var(--border-color, #d0d7de);
  --th-bg: var(--table-header-bg, #f6f8fa);
  --card-bg: var(--card-background, #f6f8fa);
  --pre-bg: var(--code-background, #f6f8fa);
  --pill-bg: var(--pill-background, #eaeef2);
  --pill-active-bg: var(--accent, #0969da);
  --pill-active-fg: #ffffff;
}

:root[data-theme="dark"] {
  --bg: var(--background, #0d1117);
  --fg: var(--foreground, #e6edf3);
  --fg-muted: var(--muted, #848d97);
  --link: var(--link-color, #4493f8);
  --border: var(--border-color, #30363d);
  --th-bg: var(--table-header-bg, #161b22);
  --card-bg: var(--card-background, #161b22);
  --pre-bg: var(--code-background, #161b22);
  --pill-bg: var(--pill-background, #21262d);
  --pill-active-bg: var(--accent, #1f6feb);
  --pill-active-fg: #ffffff;
}

* { box-sizing: border-box; }
body {
  font: 12px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
  margin: 0;
  padding: 0.5rem 0.75rem;
  max-width: 100%;
  background: var(--bg);
  color: var(--fg);
}
h1 { font-size: 1.05rem; margin: 0 0 0.4rem; }
h2 { font-size: 0.95rem; margin: 0.8rem 0 0.3rem; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.crumbs { font-size: 0.75rem; margin-bottom: 0.4rem; color: var(--fg-muted); }

/* Switcher bar */
.switcher {
  display: flex;
  gap: 0.35rem;
  overflow-x: auto;
  padding: 0.2rem 0 0.4rem;
  margin-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  scrollbar-width: thin;
  -webkit-overflow-scrolling: touch;
}
.switcher::-webkit-scrollbar { height: 3px; }
.switcher::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.15rem 0.5rem;
  border-radius: 9999px;
  font-size: 0.72rem;
  text-decoration: none;
  background: var(--pill-bg);
  color: var(--fg);
  border: 1px solid var(--border);
  flex-shrink: 0;
}
.pill:hover { background: var(--pill-hover-bg); text-decoration: none; }
.pill.active {
  background: var(--pill-active-bg);
  color: var(--pill-active-fg);
  border-color: var(--pill-active-bg);
  font-weight: 600;
}
.pill.job-pill .badge.job {
  font-size: 0.65rem;
  padding: 0.05rem 0.35rem;
}
.pill.active .badge.job {
  background: #ffffff33;
  color: #fff;
}

/* Tables, headers & scrolling */
.wrap {
  overflow-x: auto;
  width: 100%;
  max-width: 100%;
  -webkit-overflow-scrolling: touch;
  margin: 0.4rem 0;
}
table { border-collapse: collapse; width: 100%; font-size: 0.78rem; }
th, td {
  border: 1px solid var(--border);
  padding: 0.25rem 0.4rem;
  text-align: left;
  vertical-align: top;
}
th {
  background: var(--th-bg);
  position: sticky;
  top: 0;
  z-index: 10;
  white-space: nowrap;
}
td { max-width: 20rem; overflow-wrap: break-word; }

.null { opacity: .4; font-style: italic; }
.muted { color: var(--fg-muted); }

/* Badges & cards */
.badge { display: inline-block; padding: .05rem .45rem; border-radius: 1rem; font-size: .7rem; font-weight: 600; }
.badge.job { background: #7c3aed22; color: #7c3aed; }
.s-pending { background: #64748b22; color: #64748b; }
.s-claimed { background: #d9770622; color: #d97706; }
.s-done    { background: #16a34a22; color: #16a34a; }
.s-failed  { background: #dc262622; color: #dc2626; }
.cards { display: flex; gap: 0.35rem; flex-wrap: wrap; margin: 0.35rem 0; }
.card {
  border: 1px solid var(--border);
  background: var(--card-bg);
  border-radius: 0.35rem;
  padding: 0.3rem 0.5rem;
  min-width: 3.5rem;
}
.card .n { font-size: 1.1rem; font-weight: 700; }
.card .l { font-size: 0.65rem; text-transform: uppercase; color: var(--fg-muted); }

.filters { margin: 0.4rem 0; font-size: 0.75rem; }
.filters a { margin-right: 0.4rem; }
.filters a.active { font-weight: 700; text-decoration: underline; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .2rem .6rem; font-size: 0.78rem; }
dt { font-weight: 600; color: var(--fg-muted); }
pre {
  background: var(--pre-bg);
  padding: 0.4rem 0.6rem;
  border-radius: 0.3rem;
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 0.25rem 0;
  font-size: 0.75rem;
  border: 1px solid var(--border);
}
pre.err { background: #dc262618; border-color: #dc262644; }
.note { font-size: 0.7rem; color: var(--fg-muted); margin-top: 0.8rem; }
.pager { margin: 0.5rem 0; display: flex; gap: 0.5rem; align-items: center; font-size: 0.75rem; }
.auto { font-size: 0.7rem; color: var(--fg-muted); display: inline-block; margin-top: 0.4rem; }
"""

THEME_SCRIPT = """
(() => {
  window.addEventListener('message', (event) => {
    if (event.data && (event.data.type === 'theme-change' || event.data.event === 'theme-change')) {
      const payload = event.data.payload || event.data;
      const vars = payload.cssVariables || payload.variables || {};
      for (const [key, val] of Object.entries(vars)) {
        if (typeof key === 'string' && key.startsWith('--')) {
          document.documentElement.style.setProperty(key, val);
        }
      }
      const theme = payload.theme || event.data.theme;
      if (theme) {
        document.documentElement.setAttribute('data-theme', theme);
        if (theme === 'dark' || theme === 'light') {
          document.documentElement.style.colorScheme = theme;
        }
      }
    }
  });
})();
"""

# In-place auto-refresh: re-fetch the current URL and swap #main's children, so
# scroll position and the #auto toggle (outside #main) survive and nothing
# flashes. %d is the interval in ms.
SCRIPT = """
(() => {
  let inflight = false;
  async function tick() {
    if (inflight || document.visibilityState !== "visible"
        || !document.getElementById("auto").checked) return;
    inflight = true;
    try {
      const r = await fetch(location.href, {cache: "no-store"});
      if (!r.ok) return;  // e.g. table dropped mid-watch: keep the last page
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const fresh = doc.getElementById("main");
      if (fresh) document.getElementById("main").innerHTML = fresh.innerHTML;
    } catch (e) { /* server gone (idle-exit/stop): keep the last page */ }
    finally { inflight = false; }
  }
  setInterval(tick, %d);
})();
"""


def page(
    title: str,
    body: str,
    crumbs: str = "",
    refresh: int = 0,
    switcher: str = "",
    meta_refresh: bool = False,
) -> str:
    crumb_html = f'<div class="crumbs">{crumbs}</div>' if crumbs else ""
    meta_tag = f'<meta http-equiv="refresh" content="{refresh}">' if (meta_refresh and refresh) else ""
    live = ""
    if refresh:
        live = (
            f'<label class="auto"><input type="checkbox" id="auto" checked> '
            f"auto-refresh ({refresh}s)</label>"
            f"<script>{SCRIPT % (refresh * 1000)}</script>"
        )
    theme_script = f"<script>{THEME_SCRIPT}</script>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'>"
        f"{meta_tag}"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
        f'<main id="main">{switcher}{crumb_html}{body}</main>'
        '<div class="note">Read-only inspector. Status reflects the last agent '
        "activity; expired leases requeue on the next agent action.</div>"
        f"{live}{theme_script}</body></html>"
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


def pager(base_path: str, params: dict, page_num: int, pages: int,
          base_query: dict | str | None = None) -> str:
    def link(p, label):
        url = build_url(base_path, dict(params, page=p), base_query)
        return f'<a href="{url}">{label}</a>'

    parts = []
    parts.append(link(page_num - 1, "← Prev") if page_num > 1 else '<span class="muted">← Prev</span>')
    parts.append(f'<span class="muted">Page {page_num} of {pages}</span>')
    parts.append(link(page_num + 1, "Next →") if page_num < pages else '<span class="muted">Next →</span>')
    return '<div class="pager">' + "".join(parts) + "</div>"


def sort_headers(base_path: str, params: dict, cols, sort, direction,
                 base_query: dict | str | None = None) -> str:
    """<th> cells as sort links. cols is [(label, key)]; the active column's
    link toggles direction and shows a ▲/▼ marker. Links omit `page` so a
    sort change starts back at page 1."""
    cells = []
    for label, key in cols:
        active = key == sort
        nxt = "desc" if active and direction == "asc" else "asc"
        url = build_url(base_path, dict(params, sort=key, dir=nxt), base_query)
        marker = (" ▲" if direction == "asc" else " ▼") if active else ""
        cells.append(
            f'<th><a href="{url}">{html.escape(label)}</a>{marker}</th>'
        )
    return "".join(cells)


# ─── Render functions (pure — return (status_code, html)) ────────────────────


def render_index(conn: sqlite3.Connection,
                 base_query: dict | str | None = None) -> tuple[int, str]:
    user_tables, job_tables = discover_tables_and_jobs(conn)
    switcher = render_switcher(conn, active_route="/", base_query=base_query)

    if not user_tables and not job_tables:
        return 200, page("Tables", "<h1>Tables</h1><p class='muted'>No tables yet.</p>",
                         refresh=REFRESH_SECONDS, switcher=switcher, meta_refresh=True)

    rows = []
    all_display = sorted(user_tables + job_tables)
    jobs_set = set(job_tables)

    for name in all_display:
        count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        is_job = name in jobs_set
        href = build_url("/job" if is_job else "/table", {"name": name}, base_query)
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
    return 200, page("Tables", body, refresh=REFRESH_SECONDS, switcher=switcher)


def render_table(conn: sqlite3.Connection, name: str, page_num, size,
                 sort=None, direction=None,
                 base_query: dict | str | None = None) -> tuple[int, str]:
    switcher = render_switcher(conn, active_route="/table", active_name=name, base_query=base_query)
    crumbs = f'<a href="{build_url("/", None, base_query)}">← Tables</a>'
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs=crumbs, switcher=switcher)
    page_num, size = clamp_page(page_num, size)
    col, direction = sort_key(conn, name, sort, direction)
    total = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    pages = max(1, math.ceil(total / size))
    page_num = min(page_num, pages)
    offset = (page_num - 1) * size

    cur = conn.execute(
        f'SELECT rowid AS rowid, * FROM "{name}"{order_clause(col, direction)} LIMIT ? OFFSET ?',
        (size, offset),
    )
    cols = [d[0] for d in cur.description]
    body_rows = []
    for row in cur.fetchall():
        cells = "".join(f"<td>{fmt(row[c])}</td>" for c in cols)
        body_rows.append(f"<tr>{cells}</tr>")

    # sort/dir only when active, so default URLs stay identical to before.
    params = {"name": name, "size": size}
    if col is not None:
        params.update(sort=col, dir=direction)
    header = sort_headers("/table", params, [(c, c) for c in cols], col, direction, base_query=base_query)

    body = (
        f"<h1>{html.escape(name)}</h1>"
        f'<p class="muted">{total} row(s)</p>'
        + pager("/table", params, page_num, pages, base_query=base_query)
        + '<div class="wrap"><table><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
        + pager("/table", params, page_num, pages, base_query=base_query)
    )
    return 200, page(name, body, crumbs=crumbs, refresh=REFRESH_SECONDS, switcher=switcher)


def render_job(conn: sqlite3.Connection, name: str, status, page_num, size,
               sort=None, direction=None,
               base_query: dict | str | None = None) -> tuple[int, str]:
    switcher = render_switcher(conn, active_route="/job", active_name=name, base_query=base_query)
    crumbs = f'<a href="{build_url("/", None, base_query)}">← Tables</a>'
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs=crumbs, switcher=switcher)
    if name not in set(table_tool.list_job_tables(conn)):
        # Not a job table — fall back to the plain row browser.
        return render_table(conn, name, page_num, size, sort, direction, base_query=base_query)

    status = status if status in STATUS_FILTERS else "all"
    page_num, size = clamp_page(page_num, size)
    col, direction = sort_key(conn, name, sort, direction)
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

    # Filter bar. Filter links carry an active sort so the two compose.
    filters = []
    for st in STATUS_FILTERS:
        label = st if st == "all" else f"{st} ({counts.get(st, 0)})"
        cls = "active" if st == status else ""
        fq = {"name": name, "status": st, "size": size}
        if col is not None:
            fq.update(sort=col, dir=direction)
        q_url = build_url("/job", fq, base_query)
        filters.append(f'<a class="{cls}" href="{q_url}">{html.escape(label)}</a>')

    # Task list.
    where = "" if status == "all" else f" WHERE _task_status = '{status}'"
    filtered_total = conn.execute(
        f'SELECT COUNT(*) FROM "{name}"{where}'
    ).fetchone()[0]
    pages = max(1, math.ceil(filtered_total / size)) if filtered_total else 1
    page_num = min(page_num, pages)
    offset = (page_num - 1) * size

    srccols = source_columns(conn, name)
    task_rows = []
    order = order_clause(col, direction, default=" ORDER BY rowid")
    for row in conn.execute(
        f'SELECT rowid AS _task_id, * FROM "{name}"{where}{order} LIMIT ? OFFSET ?',
        (size, offset),
    ):
        tid = row["_task_id"]
        t_url = build_url("/task", {"name": name, "id": tid}, base_query)
        src_cells = "".join(f"<td>{preview(row[c])}</td>" for c in srccols)
        outcome = row["_task_error"] if row["_task_status"] == "failed" else row["result"]
        task_rows.append(
            f'<tr><td><a href="{t_url}">#{tid}</a></td>'
            f"<td>{status_badge(row['_task_status'])}</td>"
            f"<td>{row['_task_attempts']}</td>"
            f"<td>{fmt(row['_task_claimed_by'])}</td>"
            f"{src_cells}"
            f"<td>{preview(outcome)}</td></tr>"
        )

    # sort/dir only when active, so default URLs stay identical to before.
    params = {"name": name, "status": status, "size": size}
    if col is not None:
        params.update(sort=col, dir=direction)
    # The Result / error cell is computed, so it sorts by the real `result`
    # column (failed rows have a NULL result and group together).
    header_cols = (
        [("Task", "rowid"), ("Status", "_task_status"),
         ("Attempts", "_task_attempts"), ("Claimed by", "_task_claimed_by")]
        + [(c, c) for c in srccols]
        + [("Result / error", "result")]
    )
    body = (
        f"<h1>{html.escape(name)} <span class='badge job'>job</span></h1>"
        f'<p class="muted">{html.escape(complete_txt)}</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<div class="filters">{"".join(filters)}</div>'
        + pager("/job", params, page_num, pages, base_query=base_query)
        + '<div class="wrap"><table><thead><tr>'
        + sort_headers("/job", params, header_cols, col, direction, base_query=base_query)
        + "</tr></thead><tbody>"
        + ("".join(task_rows) or '<tr><td colspan="99" class="muted">No tasks.</td></tr>')
        + "</tbody></table></div>"
        + pager("/job", params, page_num, pages, base_query=base_query)
    )
    return 200, page(name, body, crumbs=crumbs, refresh=REFRESH_JOB_SECONDS, switcher=switcher)


def render_task(conn: sqlite3.Connection, name: str, rowid,
                base_query: dict | str | None = None) -> tuple[int, str]:
    switcher = render_switcher(conn, active_route="/task", active_name=name, base_query=base_query)
    crumbs = f'<a href="{build_url("/", None, base_query)}">← Tables</a> / <a href="{build_url("/job", {"name": name}, base_query)}">{html.escape(name)}</a>'
    if not name or not table_exists(conn, name):
        return 404, page("Not found", f"<h1>No such table</h1><p>{fmt(name)}</p>",
                         crumbs=crumbs, switcher=switcher)
    try:
        rowid = int(rowid)
    except (TypeError, ValueError):
        return 404, page("Not found", "<h1>Invalid task id</h1>",
                         crumbs=crumbs, switcher=switcher)
    row = conn.execute(
        f'SELECT rowid AS _task_id, * FROM "{name}" WHERE rowid = ?', (rowid,)
    ).fetchone()
    if row is None:
        return 404, page("Not found", f"<h1>No task #{rowid}</h1>",
                         crumbs=crumbs, switcher=switcher)

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

    body = (
        f"<h1>Task #{rowid} <span class='muted'>in {html.escape(name)}</span></h1>"
        "<h2>Source</h2>"
        f"<dl>{src_items}</dl>"
        "<h2>Task bookkeeping</h2>"
        f"{book}{err_html}{result_html}"
    )
    return 200, page(f"Task #{rowid}", body, crumbs=crumbs, refresh=REFRESH_SECONDS, switcher=switcher)


# ─── HTTP plumbing ───────────────────────────────────────────────────────────

ROUTES = {
    "/table": lambda conn, q, bq=None: render_table(conn, q.get("name", [""])[0],
                                                    q.get("page", [1])[0], q.get("size", [PAGE_SIZE_DEFAULT])[0],
                                                    q.get("sort", [None])[0], q.get("dir", [None])[0],
                                                    base_query=bq),
    "/job": lambda conn, q, bq=None: render_job(conn, q.get("name", [""])[0] or q.get("id", [""])[0],
                                                q.get("status", ["all"])[0],
                                                q.get("page", [1])[0], q.get("size", [PAGE_SIZE_DEFAULT])[0],
                                                q.get("sort", [None])[0], q.get("dir", [None])[0],
                                                base_query=bq),
    "/task": lambda conn, q, bq=None: render_task(conn, q.get("name", [""])[0], q.get("id", [None])[0],
                                                  base_query=bq),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "table-inspector/0.0.5"

    def log_message(self, *args):
        pass  # stdout/stderr are /dev/null anyway; stay quiet

    def _send(self, status: int, body: str, content_type="text/html; charset=utf-8", headers: dict | None = None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        _STATE["last_activity"] = time.time()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return

        expected_token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN")
        if expected_token:
            q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            header_token = self.headers.get("X-Sidecar-Token") if self.headers else None
            if header_token is not None:
                header_token = header_token.strip()
            candidate = q.get("token", [None])[0] or header_token
            try:
                expected_bytes = expected_token.encode("utf-8", errors="surrogateescape")
                cand_utf8 = candidate.encode("utf-8", errors="surrogateescape") if candidate else b""
                cand_latin1 = candidate.encode("latin-1", errors="ignore") if candidate else b""
                valid = bool(candidate and (
                    hmac.compare_digest(cand_utf8, expected_bytes)
                    or hmac.compare_digest(cand_latin1, expected_bytes)
                ))
            except Exception:
                valid = False
            if not valid:
                self._send(403, "Forbidden: invalid or missing token", "text/plain; charset=utf-8")
                return

        # Parse query parameters
        q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        conv_id_vals = q.get("conversationId") if "conversationId" in q else q.get("conversation_id")
        conv_id = conv_id_vals[0] if conv_id_vals else None

        # Build threaded query parameters dictionary
        base_query: dict[str, str] = {}
        if conv_id:
            base_query["conversationId"] = conv_id
        token_cand = q.get("token", [None])[0] or (
            self.headers.get("X-Sidecar-Token").strip()
            if (self.headers and self.headers.get("X-Sidecar-Token"))
            else None
        )
        if token_cand:
            base_query["token"] = token_cand

        # 1. Validation & Resolution (Zero-I/O Failure Gate)
        try:
            db_path = resolve_request_db(conv_id)
        except ValueError:
            self._send(400, "Invalid conversation ID", "text/plain; charset=utf-8")
            return

        # 2. Non-mutating existence check (empty state if None or file does not exist)
        if db_path is None or not db_path.is_file():
            self._send(
                200,
                page(
                    "Tables",
                    "<h1>Tables</h1><p class='muted'>No database yet — create a table first.</p>",
                    refresh=REFRESH_SECONDS,
                    switcher=render_switcher(None, active_route=path, base_query=base_query),
                    meta_refresh=True,
                ),
                headers={"Refresh": str(REFRESH_SECONDS)},
            )
            return

        # 3. Read-only connection lifecycle
        try:
            conn = connect_ro(str(db_path))
        except sqlite3.OperationalError:
            self._send(
                200,
                page(
                    "Tables",
                    "<h1>Tables</h1><p class='muted'>No database yet — create a table first.</p>",
                    refresh=REFRESH_SECONDS,
                    switcher=render_switcher(None, active_route=path, base_query=base_query),
                    meta_refresh=True,
                ),
                headers={"Refresh": str(REFRESH_SECONDS)},
            )
            return

        try:
            user_tables, job_tables = discover_tables_and_jobs(conn)
            if not user_tables and not job_tables:
                self._send(
                    200,
                    page(
                        "Tables",
                        "<h1>Tables</h1><p class='muted'>No tables yet.</p>",
                        refresh=REFRESH_SECONDS,
                        switcher=render_switcher(conn, active_route=path, base_query=base_query),
                        meta_refresh=True,
                    ),
                    headers={"Refresh": str(REFRESH_SECONDS)},
                )
                return

            if path == "/":
                status, body = render_index(conn, base_query=base_query)
            elif path in ROUTES:
                status, body = ROUTES[path](conn, q, base_query)
            else:
                switcher = render_switcher(conn, active_route=path, base_query=base_query)
                home_url = build_url("/", None, base_query)
                crumbs = f'<a href="{home_url}">← Tables</a>'
                status, body = 404, page("Not found", f'<h1>404</h1><p><a href="{home_url}">← Tables</a></p>',
                                         crumbs=crumbs, switcher=switcher)

            headers = {"Refresh": str(REFRESH_SECONDS)} if "meta http-equiv=\"refresh\"" in body else None
            self._send(status, body, headers=headers)
        except Exception as e:  # never leak a stack trace to the browser
            self._send(500, page("Error", f"<h1>Inspector error</h1><pre>{html.escape(str(e))}</pre>"))
        finally:
            conn.close()


def _cleanup_pidfile(db_path: str | None) -> None:
    """Remove the session's .inspect.json if it still points to this process."""
    if not db_path:
        return
    pidfile = Path(db_path).parent / INSPECT_PIDFILE_NAME
    try:
        record = json.loads(pidfile.read_text())
        if isinstance(record, dict) and record.get("pid") not in (None, os.getpid()):
            return
    except (OSError, ValueError):
        pass
    try:
        pidfile.unlink(missing_ok=True)
    except OSError:
        pass


def _idle_watchdog():
    """Exit if idle too long or the DB disappears — a zombie-server backstop."""
    while True:
        time.sleep(60)
        if time.time() - _STATE["last_activity"] > IDLE_TIMEOUT:
            _cleanup_pidfile(_STATE["db"])
            os._exit(0)


def resolve_port(cli_port: int | None = None) -> int:
    """Resolve the TCP port to bind on 127.0.0.1.
    Precedence: CLI --port > ANTIGRAVITY_SIDECAR_WEB_PORT env var > default 8760.
    Preserves port 0 when explicitly passed (e.g. in test suites).
    """
    if cli_port is not None:
        return cli_port
    env_port = os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT")
    if env_port is not None and env_port.strip():
        try:
            return int(env_port.strip())
        except ValueError:
            pass
    return DEFAULT_PORT


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only web inspector for a session's tables")
    parser.add_argument("--db", default=None, help="Path to the SQLite database")
    parser.add_argument("--port", type=int, default=None, help="TCP port to bind on 127.0.0.1")
    args = parser.parse_args(argv)

    port = resolve_port(args.port)
    _STATE["db"] = args.db
    _STATE["last_activity"] = time.time()

    if args.db:
        threading.Thread(target=_idle_watchdog, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        _cleanup_pidfile(args.db)


if __name__ == "__main__":
    main()
