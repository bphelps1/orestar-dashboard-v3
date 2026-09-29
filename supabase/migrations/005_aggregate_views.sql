-- ============================================================================
-- Dashboard aggregate storage.
--
-- The dashboard's curated views (summary, timeline, top donors/recipients,
-- per-filer detail, etc.) are NOT pure transaction aggregates — they blend the
-- transactions table with ORESTAR yearly summaries (orestar_yearly_summaries),
-- cash balances (orestar_cash_balances), and filer metadata (filer_metadata),
-- and encode ORESTAR-specific matching rules (e.g. cash+in-kind contribution
-- totals, in-kind expenditure mirroring) that already live — correct and
-- tested — in scraper/process.py.
--
-- Rather than reimplement that logic as SQL materialized views (high effort,
-- easy to diverge), the scraper keeps computing these aggregates in Python and
-- upserts the results here as jsonb. The frontend reads them from Postgres via
-- the Supabase API instead of static files. The live-query surface for
-- researchers is the `transactions` table itself (migration 004).
--
-- Both tables are public record → readable by anon; writes only via service role.
-- ============================================================================

-- ── Key/value cache for whole-dashboard aggregate blobs ────────────────────
-- keys mirror the former files: 'summary', 'timeline', 'top_donors',
-- 'top_recipients', 'by_contributor_type', 'by_party_type', 'filer_index',
-- 'donor_filer_map', 'recent_transactions', 'activity_snapshot'.
create table if not exists dashboard_cache (
  key         text primary key,
  data        jsonb not null,
  updated_at  timestamptz not null default now()
);

-- ── Per-filer detail blobs (one row per filer slug, ~7,200 rows) ───────────
create table if not exists filer_detail (
  slug        text primary key,
  name        text,
  filer_id    text,
  detail      jsonb not null,
  updated_at  timestamptz not null default now()
);
create index if not exists idx_filer_detail_filer_id on filer_detail (filer_id) where filer_id is not null;

-- ── Row-Level Security: public read, no anon write ─────────────────────────
alter table dashboard_cache enable row level security;
alter table filer_detail    enable row level security;

drop policy if exists "Public read dashboard_cache" on dashboard_cache;
create policy "Public read dashboard_cache" on dashboard_cache
  for select to anon, authenticated using (true);

drop policy if exists "Public read filer_detail" on filer_detail;
create policy "Public read filer_detail" on filer_detail
  for select to anon, authenticated using (true);
