-- ============================================================================
-- Transactions: the queryable source-of-truth table.
--
-- Holds every ORESTAR cash contribution / expenditure / other-receipt row
-- (2006–present, ~2.95M rows). Columns mirror data/transactions/txn_*.csv.gz
-- in source order, snake_cased, so the scraper's COPY loads column-for-column.
--
-- Public record → readable by the anon role. Writes happen only via the
-- service role from the scraper (no anon write policy).
-- ============================================================================

create extension if not exists pg_trgm;

create table if not exists transactions (
  tran_id                        bigint primary key,
  original_id                    bigint,
  tran_date                      date,
  tran_status                    text,
  filer                          text,
  contributor_payee              text,
  sub_type                       text,
  payer_of_personal_expenditure  text,
  amount                         numeric,
  aggregate_amount               numeric,
  contributor_payee_committee_id text,
  filer_id                       text,
  attest_by_name                 text,
  attest_date                    date,
  review_by_name                 text,
  review_date                    date,
  due_date                       date,
  occptn_ltr_date                date,
  pymt_sched_txt                 text,
  purpose                        text,
  intrst_rate                    text,
  check_nbr                      text,
  tran_stsfd_ind                 text,
  filed_by_name                  text,
  filed_date                     date,
  addr_book_agent_name           text,
  book_type                      text,
  title_txt                      text,
  occupation                     text,
  employer                       text,
  emp_city                       text,
  emp_state                      text,
  employ_ind                     text,
  self_employ_ind                text,
  addr_line1                     text,
  addr_line2                     text,
  city                           text,
  state                          text,
  zip                            text,
  zip_plus_four                  text,
  county                         text,
  country                        text,
  foreign_postal_code            text,
  purpose_codes                  text,
  exp_date                       date,
  source_file                    text,
  tran_type                      text,
  contributor_type               text,
  office                         text,
  party                          text,
  contributor_payee_canonical    text,
  filer_canonical                text,
  contributor_type_label         text
);

-- ── Indexes for the filter / sort / search surface ─────────────────────────
create index if not exists idx_txn_filer_canon    on transactions (filer_canonical);
create index if not exists idx_txn_payee_canon     on transactions (contributor_payee_canonical);
create index if not exists idx_txn_tran_date       on transactions (tran_date);
create index if not exists idx_txn_amount          on transactions (amount);
create index if not exists idx_txn_type_label      on transactions (contributor_type_label);
create index if not exists idx_txn_party           on transactions (party);
create index if not exists idx_txn_tran_type       on transactions (tran_type);
create index if not exists idx_txn_filer_id        on transactions (filer_id);

-- Trigram indexes power case-insensitive substring/fuzzy name search
-- (ILIKE '%name%') in the Explore filter UI. Large but essential for search.
create index if not exists idx_txn_payee_trgm on transactions using gin (contributor_payee_canonical gin_trgm_ops);
create index if not exists idx_txn_filer_trgm on transactions using gin (filer_canonical gin_trgm_ops);

-- ── Row-Level Security: public read, no anon write ─────────────────────────
alter table transactions enable row level security;

drop policy if exists "Public read transactions" on transactions;
create policy "Public read transactions" on transactions
  for select to anon, authenticated using (true);

-- NOTE: after applying, cap PostgREST result size so a bare anon SELECT can't
-- stream all ~3M rows. In the Supabase dashboard set
--   Settings → API → Max rows = 10000
-- (or `db-pool`/`max-rows` in the PostgREST config). The full-dataset download
-- is served from Storage instead (see scraper upload_full_csv).
