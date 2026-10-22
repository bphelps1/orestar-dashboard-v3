-- Per-year caches behind the donor re-key, so no run reads 3M transactions or
-- builds a multi-hundred-MB result in one statement.
--
-- refresh_donor_aggregates.py ("Re-key donor aggregates", the step every
-- aggregation is followed by) used to stage the whole donor_contribution_rows
-- view into a 1.8M-row temp table on every run: an index-only scan of the
-- 1.9M cash rows with ~400k heap fetches, two hash joins at the Micro
-- instance's 3.5 MB work_mem spilling 330 MB of temp files, a 220 MB temp
-- table, then a second read of the whole 1.7 GB heap for the canonical-name
-- map. That one staging statement is what hung the database on 2026-09-23,
-- 2026-10-19 and 2026-10-22 — and again, twice, on 2026-10-22 evening when
-- the same statement was run from a per-year cache instead of the view, while
-- 27 per-year statements writing the same 1.8M rows to a table went through
-- unremarked. On this instance the size of a statement's result is what
-- matters, not where the rows come from.
--
-- So every table here is written one year at a time, and read one year or
-- one batch of committees at a time:
--   donor_contribution_sums   the view's raw_totals CTE (gifts and refunds by
--                             committee, date, donor_id and raw labels) with
--                             the calendar year; depends on transactions only.
--                             yr = 0 holds the few undated rows.
--   donor_contribution_dirty  years a writer of transactions touched
--                             (supabase_sync.mark_donor_sum_years); the re-key
--                             rebuilds just those from idx_txn_cash_donor_dates,
--                             3-7 s a year. yr = -1 means every year (a full
--                             reload, the weekly resolve's restamp); an empty
--                             cache rebuilds in full.
--   donor_year_totals         the sums joined to the live donors and
--                             donor_identity_map, by committee, year and
--                             donor_key. Recomputed for every year on every
--                             run (a merge saved at /admin/donors can touch any
--                             year) but replaced only where it differs.
--   donor_canonical_totals    canonical label -> entity, by year, for re-keying
--                             by_contributor_type without the heap scan.
-- donor_contribution_rows_cached is donor_contribution_rows (041) over the
-- sums cache: same rows, plus the year and the raw canonical label. The
-- predicates below mirror 041's view; change them together.

create table if not exists public.donor_contribution_sums (
  yr                          int not null,
  filer_id                    text,
  tran_date                   date,
  donor_id                    text,
  contributor_payee_canonical text,
  contributor_payee           text,
  amount                      numeric not null
);
create index if not exists idx_donor_sums_yr on public.donor_contribution_sums (yr);
-- The Fundraising Pulse reads a few months of rows by date.
create index if not exists idx_donor_sums_date on public.donor_contribution_sums (tran_date);
-- A daily rebuild deletes and reinserts one or two years (~250k rows); keep
-- the dead rows from piling up, as 037 does for transactions.
alter table public.donor_contribution_sums set (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_scale_factor = 0.02
);

create table if not exists public.donor_contribution_dirty (
  yr        int primary key,
  marked_at timestamptz not null default now()
);

create table if not exists public.donor_year_totals (
  yr        int not null,
  filer_id  text,
  donor_key text not null,
  donor_id  text,
  name      text,
  total     numeric not null
);
create index if not exists idx_donor_year_totals_yr on public.donor_year_totals (yr);
-- The per-committee rebuild reads one batch of committees at a time.
create index if not exists idx_donor_year_totals_filer on public.donor_year_totals (filer_id);
alter table public.donor_year_totals set (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_scale_factor = 0.02
);

create table if not exists public.donor_canonical_totals (
  yr                          int not null,
  contributor_payee_canonical text not null,
  donor_id                    text not null,
  name                        text,
  total                       numeric not null
);
create index if not exists idx_donor_canonical_totals_yr on public.donor_canonical_totals (yr);

-- Internal to the pipeline, not part of the API: Supabase's default
-- privileges would otherwise hand anon full access to a new public table.
alter table public.donor_contribution_sums enable row level security;
alter table public.donor_contribution_dirty enable row level security;
alter table public.donor_year_totals enable row level security;
alter table public.donor_canonical_totals enable row level security;
revoke all on public.donor_contribution_sums, public.donor_contribution_dirty,
  public.donor_year_totals, public.donor_canonical_totals
  from public, anon, authenticated;

create or replace view public.donor_contribution_rows_cached
with (security_invoker = true) as
with named as (
  select s.yr, s.filer_id, s.tran_date, s.amount, s.contributor_payee_canonical,
         coalesce(mi.canonical_id, s.donor_id) as donor_id,
         public.donor_bucket_label(coalesce(
           mi.canonical_name,
           nullif(public.normalize_donor_label(d.display_name), ''),
           nullif(public.normalize_donor_label(s.contributor_payee_canonical), ''),
           public.normalize_donor_label(s.contributor_payee)
         )) as name
  from public.donor_contribution_sums s
  left join public.donors d on d.donor_id = s.donor_id
  left join public.donor_identity_map mi on mi.donor_id = s.donor_id
)
select n.yr, n.filer_id, n.tran_date, n.amount,
       case when lower(n.name) = 'miscellaneous cash contributions $100 and under'
            then 'label:miscellaneous cash contributions $100 and under'
            else coalesce(n.donor_id, 'name:' || lower(n.name)) end as donor_key,
       case when lower(n.name) = 'miscellaneous cash contributions $100 and under'
            then null else n.donor_id end as donor_id,
       n.name, n.contributor_payee_canonical
from named n;
revoke all on public.donor_contribution_rows_cached from public, anon, authenticated;
