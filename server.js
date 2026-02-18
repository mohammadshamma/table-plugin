/**
 * table MCP Server
 * Exposes SQLite operations as MCP tools.
 * Delegates to table_tool.py for actual database operations.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));
const TABLE_TOOL = resolve(__dirname, "table_tool.py");

async function checkPython() {
  try {
    const { stdout } = await exec("python3", ["--version"]);
    const major = parseInt(stdout.trim().split(" ")[1], 10);
    if (major < 3) throw new Error("Python 2");
  } catch {
    console.error("table requires python3 on PATH");
    process.exit(1);
  }
}
await checkPython();

/**
 * Run table_tool.py with the given arguments and return parsed JSON.
 */
async function runTableTool(...args) {
  try {
    const { stdout } = await exec("python3", [TABLE_TOOL, ...args], {
      timeout: 30000,
      maxBuffer: 10 * 1024 * 1024, // 10MB
    });
    return JSON.parse(stdout);
  } catch (err) {
    // If table_tool returned JSON error on stdout, parse it
    if (err.stdout) {
      try {
        return JSON.parse(err.stdout);
      } catch {}
    }
    throw new Error(`table_tool failed: ${err.stderr || err.message}`);
  }
}

const server = new McpServer({
  name: "table",
  version: "0.0.3",
});

// ─── table_create ───────────────────────────────────────────────────────────

server.registerTool(
  "table_create",
  {
    description:
      "Create a new SQLite table. The database file is created automatically if it doesn't exist.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      table: z.string().describe("Name of the table to create"),
      columns: z
        .record(z.string())
        .describe(
          'Object mapping column names to SQL types, e.g. {"name": "TEXT NOT NULL", "age": "INTEGER"}'
        ),
      primary_key: z
        .string()
        .optional()
        .describe(
          "Column name for an auto-incrementing INTEGER PRIMARY KEY (optional)"
        ),
      unique: z
        .array(z.string())
        .optional()
        .describe("List of columns that should have UNIQUE constraints"),
      if_not_exists: z
        .boolean()
        .optional()
        .describe("If true, don't error if the table already exists"),
    }).shape,
  },
  async ({ db, table, columns, primary_key, unique, if_not_exists }) => {
    const spec = { columns };
    if (primary_key) spec.primary_key = primary_key;
    if (unique) spec.unique = unique;
    if (if_not_exists) spec.if_not_exists = if_not_exists;

    const result = await runTableTool("create-table", db, table, JSON.stringify(spec));
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_insert ───────────────────────────────────────────────────────────

server.registerTool(
  "table_insert",
  {
    description: "Insert one or more rows into an existing SQLite table.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      table: z.string().describe("Table to insert into"),
      rows: z
        .array(z.record(z.any()))
        .describe(
          'Array of row objects, e.g. [{"name": "Alice", "age": 30}]'
        ),
    }).shape,
  },
  async ({ db, table, rows }) => {
    const result = await runTableTool("insert", db, table, JSON.stringify({ rows }));
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_join ─────────────────────────────────────────────────────────────

server.registerTool(
  "table_join",
  {
    description:
      "Join two SQLite tables and store the result in a new table.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      output_table: z.string().describe("Name for the new joined table"),
      left: z.string().describe("Left table name"),
      right: z.string().describe("Right table name"),
      on: z
        .string()
        .optional()
        .describe("Join key if the column name is the same in both tables"),
      on_left: z
        .string()
        .optional()
        .describe("Left table join column (use with on_right)"),
      on_right: z
        .string()
        .optional()
        .describe("Right table join column (use with on_left)"),
      type: z
        .enum(["inner", "left", "cross"])
        .optional()
        .describe("Join type (default: inner)"),
      select: z
        .array(z.string())
        .optional()
        .describe('Columns to select, e.g. ["users.name", "orders.total"]'),
      if_not_exists: z.boolean().optional(),
    }).shape,
  },
  async ({ db, output_table, ...spec }) => {
    const result = await runTableTool(
      "join",
      db,
      output_table,
      JSON.stringify(spec)
    );
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_group_by ─────────────────────────────────────────────────────────

server.registerTool(
  "table_group_by",
  {
    description:
      "Group rows by one or more columns with aggregation functions. Optionally save results to a new table.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      table: z.string().describe("Table to group"),
      by: z.array(z.string()).describe("Columns to group by"),
      aggs: z
        .record(z.string())
        .describe(
          'Aggregations: {"alias": "SQL_EXPR"}, e.g. {"count": "COUNT(*)", "total": "SUM(amount)"}'
        ),
      having: z.string().optional().describe("HAVING clause, e.g. COUNT(*) > 5"),
      order_by: z
        .string()
        .optional()
        .describe("ORDER BY clause, e.g. count DESC"),
      limit: z.number().optional().describe("Max rows to return"),
      into: z
        .string()
        .optional()
        .describe("If set, save results into this new table"),
    }).shape,
  },
  async ({ db, table, ...spec }) => {
    const result = await runTableTool("group", db, table, JSON.stringify(spec));
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_run_sql ──────────────────────────────────────────────────────────

server.registerTool(
  "table_run_sql",
  {
    description:
      "Run an arbitrary SQL query and return results as JSON. Use for SELECT, UPDATE, DELETE, or any SQL not covered by other tools.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      sql: z.string().describe("SQL statement to execute"),
    }).shape,
  },
  async ({ db, sql }) => {
    const result = await runTableTool("query", db, sql);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_schema ───────────────────────────────────────────────────────────

server.registerTool(
  "table_schema",
  {
    description:
      "Get the schema of a specific table or all tables in the database.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      table: z
        .string()
        .optional()
        .describe("Table name (omit to get all tables)"),
    }).shape,
  },
  async ({ db, table }) => {
    const args = ["schema", db];
    if (table) args.push(table);
    const result = await runTableTool(...args);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_list ─────────────────────────────────────────────────────────────

server.registerTool(
  "table_list",
  {
    description: "List all tables in a SQLite database.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
    }).shape,
  },
  async ({ db }) => {
    const result = await runTableTool("tables", db);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── table_drop ─────────────────────────────────────────────────────────────

server.registerTool(
  "table_drop",
  {
    description: "Drop (delete) a table from the database.",
    inputSchema: z.object({
      db: z.string().describe("Path to the SQLite database file"),
      table: z.string().describe("Table to drop"),
    }).shape,
  },
  async ({ db, table }) => {
    const result = await runTableTool("drop", db, table);
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

// ─── Start ───────────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
