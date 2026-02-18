/**
 * Integration tests for table_tool.py
 * Uses Node.js built-in test runner (node --test).
 */

import { describe, it, after } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { unlinkSync } from "node:fs";

const exec = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));
const TABLE_TOOL = resolve(__dirname, "table_tool.py");
const DB = resolve(__dirname, "test_temp.db");

async function run(...args) {
  const { stdout } = await exec("python3", [TABLE_TOOL, ...args], {
    timeout: 10000,
  });
  return JSON.parse(stdout);
}

describe("table_tool", () => {
  after(() => {
    try {
      unlinkSync(DB);
    } catch {}
  });

  it("create-table", async () => {
    const result = await run(
      "create-table",
      DB,
      "users",
      JSON.stringify({
        columns: { name: "TEXT NOT NULL", age: "INTEGER" },
        primary_key: "id",
      })
    );
    assert.equal(result.ok, true);
    assert.equal(result.table, "users");
  });

  it("insert", async () => {
    const result = await run(
      "insert",
      DB,
      "users",
      JSON.stringify({
        rows: [
          { name: "Alice", age: 30 },
          { name: "Bob", age: 25 },
        ],
      })
    );
    assert.equal(result.ok, true);
    assert.equal(result.inserted, 2);
  });

  it("query", async () => {
    const result = await run("query", DB, "SELECT * FROM users ORDER BY name");
    assert.equal(result.ok, true);
    assert.equal(result.count, 2);
    assert.equal(result.rows[0].name, "Alice");
    assert.equal(result.rows[1].name, "Bob");
  });

  it("schema", async () => {
    const result = await run("schema", DB, "users");
    assert.equal(result.table, "users");
    assert.ok(result.columns.name);
    assert.equal(result.columns.name.type, "TEXT");
    assert.equal(result.columns.name.notnull, true);
    assert.ok(result.columns.age);
    assert.equal(result.columns.age.type, "INTEGER");
  });

  it("tables", async () => {
    const result = await run("tables", DB);
    assert.ok(result.tables.includes("users"));
  });

  it("group", async () => {
    const result = await run(
      "group",
      DB,
      "users",
      JSON.stringify({
        by: ["age"],
        aggs: { count: "COUNT(*)" },
        order_by: "age",
      })
    );
    assert.equal(result.ok, true);
    assert.equal(result.count, 2);
    assert.equal(result.rows[0].age, 25);
    assert.equal(result.rows[0].count, 1);
  });

  it("join", async () => {
    // Create a second table for joining
    await run(
      "create-table",
      DB,
      "orders",
      JSON.stringify({
        columns: { user_name: "TEXT", amount: "REAL" },
      })
    );
    await run(
      "insert",
      DB,
      "orders",
      JSON.stringify({
        rows: [
          { user_name: "Alice", amount: 99.5 },
          { user_name: "Bob", amount: 45.0 },
        ],
      })
    );

    const result = await run(
      "join",
      DB,
      "user_orders",
      JSON.stringify({
        left: "users",
        right: "orders",
        on_left: "name",
        on_right: "user_name",
        type: "inner",
        select: ["users.name", "orders.amount"],
      })
    );
    assert.equal(result.ok, true);
    assert.equal(result.rows, 2);
    assert.equal(result.table, "user_orders");
  });

  it("drop", async () => {
    const result = await run("drop", DB, "user_orders");
    assert.equal(result.ok, true);
    assert.equal(result.dropped, "user_orders");

    // Verify it's gone
    const tables = await run("tables", DB);
    assert.ok(!tables.tables.includes("user_orders"));
  });
});
