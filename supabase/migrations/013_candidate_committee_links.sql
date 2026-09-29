-- ============================================================================
-- Manual candidate → committee links for the legislative race map.
--
-- The matcher pairs a filed candidate with a committee using the district plus
-- a tiered name comparison. What it cannot do is decide the genuinely
-- ambiguous cases, and it should not try:
--
--   * "David Nelson" filed for HD 17; a committee named "David Nelson" exists
--     but reads "State Senator, 28th District" — plausibly a different person.
--   * "Clay Bearnson" filed for SD 3; his only committee is for Mayor of
--     Medford — the same person, or a stale committee, or neither.
--
-- Guessing either way is the silent-wrongness failure this schema exists to
-- avoid: a wrong link shows one candidate's money under another's name, and a
-- missed link shows a funded candidate at $0. Both look equally confident on
-- the map. So a human decides, and the decision is recorded here.
--
-- Mirrors scraper/candidate_overrides.json, which stays as the checked-in
-- mechanism. The table is for decisions made in the browser, which must not
-- require a repository commit to take effect.
--
-- decision: 'link' → candidate_key belongs to filer_id
--           'none' → this candidate genuinely has no committee; stop
--                    listing them for review
-- ============================================================================

create table if not exists candidate_committee_links (
  -- "<ballot name>|<office_district>", lowercased — the same key
  -- scraper/generate_activity_snapshot.py::_override_key builds.
  candidate_key   text primary key,
  ballot_name     text not null,
  office_district text not null,
  filer_id        text,                   -- null when decision = 'none'
  decision        text not null default 'link',
  -- Human-readable context, so a link that later dangles (a committee closes,
  -- a candidate withdraws) can still be understood rather than just deleted.
  committee_name  text,
  note            text,
  decided_by      text,
  decided_at      timestamptz not null default now(),
  constraint candidate_committee_links_decision_check
    check (decision in ('link', 'none')),
  -- A 'link' without a filer_id would silently behave as no decision at all.
  constraint candidate_committee_links_filer_required
    check (decision <> 'link' or filer_id is not null)
);

create index if not exists idx_ccl_decision on candidate_committee_links (decision);

alter table candidate_committee_links enable row level security;

-- Anyone signed in may read; the map builder reads via the service role.
drop policy if exists "Authenticated read candidate links" on candidate_committee_links;
create policy "Authenticated read candidate links" on candidate_committee_links
  for select to authenticated using (true);

-- Only admins/reviewers may record or undo a link.
drop policy if exists "Reviewer write candidate links" on candidate_committee_links;
create policy "Reviewer write candidate links" on candidate_committee_links
  for all to authenticated
  using (exists (select 1 from user_roles
                 where user_id = auth.uid() and role in ('admin', 'reviewer')))
  with check (exists (select 1 from user_roles
                      where user_id = auth.uid() and role in ('admin', 'reviewer')));
