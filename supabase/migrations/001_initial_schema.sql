-- ============================================================================
-- Phase 4 Schema: Donor recommendations, dedup, leadership roles
-- ============================================================================

-- ── Canonical donor identities ──────────────────────────────────────────────
create table if not exists donor_canonical (
  id            bigint generated always as identity primary key,
  canonical_name text not null,
  filer_id      text,                -- ORESTAR filer ID if this donor is also a filer
  employer      text,
  occupation    text,
  city          text,
  state         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index idx_donor_canonical_name on donor_canonical (lower(canonical_name));
create index idx_donor_canonical_filer on donor_canonical (filer_id) where filer_id is not null;

-- ── Donor aliases (multiple raw names → one canonical donor) ────────────────
create table if not exists donor_alias (
  id              bigint generated always as identity primary key,
  canonical_id    bigint not null references donor_canonical(id) on delete cascade,
  alias_name      text not null,
  alias_name_lower text generated always as (lower(alias_name)) stored,
  source          text not null default 'auto',  -- 'auto' | 'admin'
  created_at      timestamptz not null default now()
);
create unique index idx_donor_alias_lower on donor_alias (alias_name_lower);

-- ── Donor merge log (audit trail, reversible) ───────────────────────────────
create table if not exists donor_merge_log (
  id              bigint generated always as identity primary key,
  kept_id         bigint not null references donor_canonical(id),
  merged_id       bigint not null,  -- no FK: row may be deleted
  merged_name     text not null,    -- snapshot of merged donor's name
  merged_aliases  jsonb,            -- snapshot of aliases that were moved
  merged_by       uuid references auth.users(id),
  action          text not null default 'merge',  -- 'merge' | 'undo'
  created_at      timestamptz not null default now()
);

-- ── Donor merge pending (clusters awaiting admin review) ────────────────────
create table if not exists donor_merge_pending (
  id              bigint generated always as identity primary key,
  cluster_key     text not null,    -- grouping key for this cluster
  donor_ids       bigint[] not null, -- array of donor_canonical IDs
  evidence        jsonb,            -- why these were grouped
  status          text not null default 'pending',  -- 'pending' | 'merged' | 'rejected'
  reviewed_by     uuid references auth.users(id),
  reviewed_at     timestamptz,
  created_at      timestamptz not null default now()
);
create index idx_merge_pending_status on donor_merge_pending (status);

-- ── Legislative leadership roles ────────────────────────────────────────────
create table if not exists leadership_roles (
  id              bigint generated always as identity primary key,
  filer_id        text,             -- ORESTAR filer ID
  filer_name      text not null,
  role_title      text not null,    -- e.g. 'Speaker', 'Senate President', 'Committee Chair'
  chamber         text,             -- 'House' | 'Senate' | null
  party           text,
  district        text,
  effective_date  date,
  end_date        date,
  source          text not null default 'manual',  -- 'scraped' | 'manual'
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index idx_leadership_filer on leadership_roles (filer_id);
create index idx_leadership_active on leadership_roles (end_date) where end_date is null;

-- ── Leadership refresh log ──────────────────────────────────────────────────
create table if not exists leadership_refresh_log (
  id              bigint generated always as identity primary key,
  refreshed_at    timestamptz not null default now(),
  source          text not null,    -- 'github_action' | 'manual' | 'admin'
  roles_updated   int not null default 0,
  notes           text
);

-- ── Admin tags (manual filer/donor tags for special cases) ──────────────────
create table if not exists admin_tags (
  id              bigint generated always as identity primary key,
  entity_type     text not null,    -- 'filer' | 'donor'
  entity_id       text not null,    -- filer slug or donor canonical ID
  tag             text not null,    -- e.g. 'leadership', 'prolific', 'exclude'
  value           text,             -- optional value
  created_by      uuid references auth.users(id),
  created_at      timestamptz not null default now()
);
create index idx_admin_tags_entity on admin_tags (entity_type, entity_id);

-- ── Recommendation cache ────────────────────────────────────────────────────
create table if not exists recommendation_cache (
  id              bigint generated always as identity primary key,
  filer_slug      text not null,
  cycle           int not null,     -- e.g. 2026
  params_hash     text not null,    -- hash of scoring params for cache invalidation
  results         jsonb not null,   -- full recommendation output
  computed_at     timestamptz not null default now()
);
create unique index idx_rec_cache_lookup on recommendation_cache (filer_slug, cycle, params_hash);

-- ── Row-Level Security ──────────────────────────────────────────────────────
-- All tables require authenticated user for writes; reads are open for
-- non-sensitive tables (leadership, tags) and restricted for admin tables.

alter table donor_canonical enable row level security;
alter table donor_alias enable row level security;
alter table donor_merge_log enable row level security;
alter table donor_merge_pending enable row level security;
alter table leadership_roles enable row level security;
alter table leadership_refresh_log enable row level security;
alter table admin_tags enable row level security;
alter table recommendation_cache enable row level security;

-- Read policies: authenticated users can read everything
create policy "Authenticated read" on donor_canonical for select to authenticated using (true);
create policy "Authenticated read" on donor_alias for select to authenticated using (true);
create policy "Authenticated read" on donor_merge_log for select to authenticated using (true);
create policy "Authenticated read" on donor_merge_pending for select to authenticated using (true);
create policy "Authenticated read" on leadership_roles for select to authenticated using (true);
create policy "Authenticated read" on leadership_refresh_log for select to authenticated using (true);
create policy "Authenticated read" on admin_tags for select to authenticated using (true);
create policy "Authenticated read" on recommendation_cache for select to authenticated using (true);

-- Write policies: authenticated users can insert/update/delete
create policy "Authenticated write" on donor_canonical for all to authenticated using (true) with check (true);
create policy "Authenticated write" on donor_alias for all to authenticated using (true) with check (true);
create policy "Authenticated write" on donor_merge_log for all to authenticated using (true) with check (true);
create policy "Authenticated write" on donor_merge_pending for all to authenticated using (true) with check (true);
create policy "Authenticated write" on leadership_roles for all to authenticated using (true) with check (true);
create policy "Authenticated write" on leadership_refresh_log for all to authenticated using (true) with check (true);
create policy "Authenticated write" on admin_tags for all to authenticated using (true) with check (true);
create policy "Authenticated write" on recommendation_cache for all to authenticated using (true) with check (true);
