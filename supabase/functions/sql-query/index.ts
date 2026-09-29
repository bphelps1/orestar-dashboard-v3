// sql-query — public read-only SQL endpoint for the Explore page's SQL box.
//
// Security is layered:
//   1. This function connects as the `public_query` Postgres role (secret
//      QUERY_DB_URL), which can SELECT only from the `query.transactions` view
//      and is forced read-only (migration 006). A 25s statement timeout is set
//      per-transaction below, since the pooler doesn't reliably propagate the
//      role-level GUC.
//   2. Belt-and-suspenders in code: reject anything that isn't a single SELECT
//      /WITH statement, and wrap it in an outer LIMIT so a bare `select *`
//      can't stream millions of rows to the browser.
//
// Request:  POST { "sql": "select ... " }
// Response: { columns: string[], rows: object[], truncated: boolean }  | { error }

import { Client } from "https://deno.land/x/postgres@v0.19.3/mod.ts";

const MAX_ROWS = 5000;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-supabase-api-version",
};

function json(body: unknown, status = 200): Response {
  // Postgres int8/numeric come back as BigInt, which JSON.stringify throws on —
  // count(*) and sum() would otherwise fail. Serialize them as strings.
  const text = JSON.stringify(body, (_k, v) =>
    typeof v === "bigint" ? v.toString() : v
  );
  return new Response(text, {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

/** Allow a single read-only SELECT/WITH statement only. */
function validate(raw: string): string | null {
  let sql = raw.trim().replace(/;\s*$/, ""); // drop one trailing semicolon
  if (!sql) return null;
  if (sql.includes(";")) return null; // no statement chaining
  // Strip leading line/block comments before checking the first keyword.
  const stripped = sql.replace(/^\s*(--[^\n]*\n|\/\*[\s\S]*?\*\/)\s*/g, "").trim();
  if (!/^(select|with)\b/i.test(stripped)) return null;
  // Block obviously-writing / admin keywords as an extra guard (the role can't
  // do these anyway, but fail fast with a clear message).
  if (/\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|vacuum|analyze)\b/i.test(stripped)) {
    return null;
  }
  return sql;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  let sql: string;
  try {
    ({ sql } = await req.json());
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }
  const clean = validate(sql ?? "");
  if (!clean) {
    return json({ error: "Only a single read-only SELECT statement is allowed." }, 400);
  }

  const dsn = Deno.env.get("QUERY_DB_URL");
  if (!dsn) return json({ error: "Query backend not configured" }, 500);

  const client = new Client(dsn);
  try {
    await client.connect();
    // Read-only transaction with an explicit statement timeout. We set it here
    // rather than rely on the public_query role's ALTER ROLE SET, because
    // Supabase's connection pooler doesn't reliably propagate role-level GUCs
    // to pooled connections. SET LOCAL scopes it to this transaction.
    await client.queryArray(
      "BEGIN; SET TRANSACTION READ ONLY; SET LOCAL statement_timeout = 25000",
    );
    const wrapped = `SELECT * FROM (${clean}) AS q LIMIT ${MAX_ROWS + 1}`;
    const result = await client.queryObject(wrapped);
    await client.queryArray("COMMIT");

    const truncated = result.rows.length > MAX_ROWS;
    const rows = truncated ? result.rows.slice(0, MAX_ROWS) : result.rows;
    const columns = result.columns ?? (rows[0] ? Object.keys(rows[0]) : []);
    return json({ columns, rows, truncated });
  } catch (e) {
    return json({ error: String(e?.message ?? e) }, 400);
  } finally {
    try { await client.end(); } catch { /* ignore */ }
  }
});
