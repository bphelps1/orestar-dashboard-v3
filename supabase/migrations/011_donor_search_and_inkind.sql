-- ============================================================================
-- 1. Donor search that also matches aliases
-- 2. donors.total_inkind — in-kind kept separate from contributions
-- ============================================================================

-- ── 1. Alias-aware donor search ────────────────────────────────────────────
--
-- /donors searched `donors.display_name` only, so looking up a raw spelling
-- ("Phillip H Knight") found nothing even though that alias resolves to Philip
-- Knight. This searches display_name AND donor_aliases.raw_name.
--
-- Same shape as search_transactions (migration 010) and for the same reason:
-- Postgres cannot estimate `ilike '%x%'` selectivity, so under the anon role it
-- abandons the trigram index and walks a btree instead. VOLATILE +
-- SECURITY DEFINER lets us pin the plan with SET LOCAL; a STABLE function
-- rejects SET LOCAL outright.

create index if not exists idx_donor_aliases_raw_trgm
  on donor_aliases using gin (raw_name gin_trgm_ops);

create or replace function search_donors(p_q text, p_limit int default 12)
returns table (
  donor_id      text,
  display_name  text,
  book_type     text,
  city          text,
  state         text,
  total_given   numeric,
  gift_count    int,
  filer_slug    text,
  matched_alias text      -- non-null when the hit came from an alias, so the
)                         -- UI can show *why* an unfamiliar name matched
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  -- Whitespace between words becomes '%', so a typed "Phillip H Knight"
  -- still matches the stored alias "Phillip  H Knight " (ORESTAR names carry
  -- doubled and trailing spaces). Leading '%' keeps the trigram path.
  pattern text := '%' || regexp_replace(btrim(coalesce(p_q, '')), '\s+', '%', 'g') || '%';
begin
  if length(coalesce(p_q, '')) < 2 then
    return;
  end if;
  set local enable_indexscan = off;   -- bitmap/trigram path stays available
  set local statement_timeout = '10s';

  return query
  with hits as (
    select d.donor_id, null::text as alias
    from donors d
    where d.display_name ilike pattern
    union
    select a.donor_id, a.raw_name
    from donor_aliases a
    where a.raw_name ilike pattern
  ),
  ranked as (
    select h.donor_id,
           -- prefer showing a display_name match (alias NULL) over an alias one
           min(h.alias) filter (where h.alias is not null) as alias,
           bool_or(h.alias is null)                        as by_name
    from hits h group by h.donor_id
  )
  select d.donor_id, d.display_name, d.book_type, d.city, d.state,
         d.total_given, d.gift_count, d.filer_slug,
         case when r.by_name then null else r.alias end
  from ranked r
  join donors d on d.donor_id = r.donor_id
  order by d.total_given desc nulls last
  limit greatest(1, least(coalesce(p_limit, 12), 50));
end;
$$;

grant execute on function search_donors(text, int) to anon, authenticated;

-- ── 2. In-kind as its own column ───────────────────────────────────────────
-- Contributions are cash-only app-wide; in-kind is a distinct metric rather
-- than something folded into a contribution total.
alter table donors add column if not exists total_inkind numeric default 0;
