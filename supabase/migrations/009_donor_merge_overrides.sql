-- ============================================================================
-- Manual entity-level merge overrides.
--
-- donor_review_decisions keys constraints on NAME pairs (norm_name → norm_name),
-- which cannot express "these two entities share a name but sit at different
-- addresses" — exactly the Philip Morris case (8 entities, same company).
--
-- These overrides are keyed on `alias_key` (= norm_name|addr_key) instead,
-- which is derived from the underlying name+address and is therefore stable
-- across resolver runs. donor_id must NOT be used: it is a content hash of the
-- cluster and changes every time the resolver runs.
--
-- decision: 'merged'   → force these two alias clusters into one entity
--           'separate' → never merge them, whatever the scores say
-- ============================================================================

create table if not exists donor_merge_overrides (
  merge_key   text primary key,          -- sorted "alias_a|||alias_b"
  alias_a     text not null,
  alias_b     text not null,
  decision    text not null default 'merged',
  -- human-readable context so a stale/dangling override can be understood later
  label_a     text,
  label_b     text,
  note        text,
  decided_by  text,
  decided_at  timestamptz not null default now()
);

create index if not exists idx_dmo_decision on donor_merge_overrides (decision);

alter table donor_merge_overrides enable row level security;

-- Anyone signed in may read; the resolver reads via the service role.
drop policy if exists "Authenticated read merges" on donor_merge_overrides;
create policy "Authenticated read merges" on donor_merge_overrides
  for select to authenticated using (true);

-- Only admins/reviewers may record or undo a merge.
drop policy if exists "Reviewer write merges" on donor_merge_overrides;
create policy "Reviewer write merges" on donor_merge_overrides
  for all to authenticated
  using (exists (select 1 from user_roles
                 where user_id = auth.uid() and role in ('admin', 'reviewer')))
  with check (exists (select 1 from user_roles
                      where user_id = auth.uid() and role in ('admin', 'reviewer')));

-- ── Entity search for the admin merge UI ────────────────────────────────────
-- Returns entities with their distinct addresses so a reviewer can tell the
-- eight "Philip Morris" records apart. SECURITY INVOKER: still subject to RLS.
create or replace function donor_search(p_q text, p_limit int default 12)
returns table (
  donor_id text, display_name text, book_type text, city text, state text,
  total_given numeric, gift_count int, alias_count int,
  rep_alias_key text, addresses text[]
)
language sql
stable
as $$
  select d.donor_id, d.display_name, d.book_type, d.city, d.state,
         d.total_given, d.gift_count, d.alias_count,
         (select a.alias_key from donor_aliases a
           where a.donor_id = d.donor_id
           order by (a.addr_key <> '') desc, a.alias_key limit 1) as rep_alias_key,
         (select array_agg(distinct a.addr_key)
            filter (where coalesce(a.addr_key,'') <> '')
          from donor_aliases a where a.donor_id = d.donor_id) as addresses
  from donors d
  where d.display_name ilike '%' || p_q || '%'
  order by d.total_given desc nulls last
  limit greatest(1, least(p_limit, 50));
$$;

grant execute on function donor_search(text, int) to authenticated;
