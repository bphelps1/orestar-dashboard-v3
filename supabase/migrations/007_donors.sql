-- ============================================================================
-- Donor entity resolution: master donors table + alias mapping.
--
-- Every contributor/payee row in `transactions` resolves to one donor entity:
--   • Committee entities (donor_id 'c<committee_id>'): backed by ORESTAR's
--     contributor/payee committee id. When that id is a filer we track,
--     filer_slug links the donor to its committee page.
--   • Person/org clusters (donor_id 'd<sha1[:12]>'): built by the resolver
--     (scraper/resolve_donors.py) from name+address+employer signals under
--     the Moderate match policy. The id hashes the cluster's smallest
--     alias_key, so it is stable across weekly re-runs unless clusters change.
--
-- donor_aliases maps every observed (normalized name, address) variant to its
-- entity — the daily incremental assigner joins new transactions against it.
-- alias_scope='committee' marks aliases learned from committee-id-tagged rows:
-- they must NOT capture id-less rows unless those rows independently look like
-- committee activity (book_type / name pattern guard) — this is what keeps
-- "GELSER, SARA" the person separate from "Sara Gelser for State Senate".
--
-- Public data → anon read. Writes only via service role (resolver on CI).
-- ============================================================================

create table if not exists donors (
  donor_id           text primary key,
  display_name       text not null,
  book_type          text,
  committee_id       text,
  filer_slug         text,          -- set when this donor IS a tracked committee
  related_filer_slug text,          -- person ↔ their candidate committee (link, never a merge)
  city               text,
  state              text,
  zip                text,
  employer           text,
  occupation         text,
  total_given        numeric not null default 0,
  total_received     numeric not null default 0,
  gift_count         integer not null default 0,
  first_date         date,
  last_date          date,
  alias_count        integer not null default 1,
  updated_at         timestamptz not null default now()
);

create table if not exists donor_aliases (
  alias_key   text primary key,     -- norm_name || '|' || addr_key
  donor_id    text not null references donors(donor_id) on delete cascade,
  raw_name    text not null,
  norm_name   text not null,
  addr_key    text not null default '',
  source      text not null,        -- 'committee_id' | 'filer_match' | 'review' | 'cluster' | 'provisional'
  alias_scope text not null default 'any'   -- 'any' | 'committee' (see header)
);

alter table transactions add column if not exists donor_id text;

-- ── Indexes ─────────────────────────────────────────────────────────────────
create index if not exists idx_donors_name_trgm on donors using gin (display_name gin_trgm_ops);
create index if not exists idx_donors_total_given on donors (total_given desc);
create index if not exists idx_donors_filer_slug on donors (filer_slug) where filer_slug is not null;
create index if not exists idx_aliases_donor on donor_aliases (donor_id);
create index if not exists idx_aliases_norm_name on donor_aliases (norm_name);
create index if not exists idx_txn_donor_id on transactions (donor_id);

-- ── RLS: public read, no anon write ─────────────────────────────────────────
alter table donors enable row level security;
alter table donor_aliases enable row level security;

drop policy if exists "Public read donors" on donors;
create policy "Public read donors" on donors
  for select to anon, authenticated using (true);

drop policy if exists "Public read donor_aliases" on donor_aliases;
create policy "Public read donor_aliases" on donor_aliases
  for select to anon, authenticated using (true);

-- ── SQL-box surface: expose donors to the public_query role ─────────────────
create or replace view query.donors as
  select donor_id, display_name, book_type, committee_id, filer_slug,
         related_filer_slug, city, state, zip, employer, occupation,
         total_given, total_received, gift_count, first_date, last_date,
         alias_count
  from public.donors;

grant select on query.donors to public_query;
