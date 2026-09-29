-- ============================================================================
-- Donor review decisions (admin accepts/rejects fuzzy-matched pairs)
-- ============================================================================

create table if not exists donor_review_decisions (
  pair_key    text primary key,           -- sorted lowercase "name_a|||name_b"
  decision    text not null,              -- 'merged' | 'rejected'
  kept_name   text,                       -- which name was kept (for merges)
  merged_name text,                       -- which name was merged away
  score       double precision,           -- fuzzy match score (80-95)
  decided_at  timestamptz not null default now()
);

alter table donor_review_decisions enable row level security;

create policy "Authenticated read" on donor_review_decisions
  for select to authenticated using (true);

create policy "Authenticated write" on donor_review_decisions
  for all to authenticated using (true) with check (true);
