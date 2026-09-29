-- Exact-date rankings for the dashboard's Recipients tab, the counterparts of
-- donor_leaderboard (014). The top_recipients blob and each profile's
-- top_payees_by_year hold calendar years, and only each year's leaders, so a
-- Dec 1 – Nov 30 election cycle added up from them took in the whole first
-- calendar year: the 2024 cycle counted all of 2022, putting that year's
-- governor's race at the top of it.

-- Committees ranked by cash contributions received between two dates, in-kind
-- left out as everywhere else. One row per filer ID, as process.py groups
-- them (it names every row of an ID by that ID's latest name); the name is the
-- dashboard's own for the committee, else the name on its latest gift here.
create or replace function public.recipient_leaderboard(
  p_start date default null,
  p_end date default null,
  p_limit int default 100
)
returns jsonb language plpgsql stable security invoker
set search_path = public
set plan_cache_mode = force_custom_plan
-- A statewide two-year range reads ~230k index entries; cold, that has taken
-- up to 16 seconds, above PostgREST's normal limit.
set statement_timeout = '30s'
as $$
begin
  return (
    with by_filer as materialized (
      -- An index-only scan of idx_txn_cash_donor_dates (015).
      select t.filer_id, sum(t.amount) as total, max(t.tran_date) as last_date
      from public.transactions t
      where t.tran_type = 'C' and coalesce(t.sub_type, '') not in (
          'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
          'In-Kind/Forgiven Personal Expenditures'
        )
        and (p_start is null or t.tran_date >= p_start)
        and (p_end is null or t.tran_date <= p_end)
        and t.filer_id is not null
      group by t.filer_id
    ), leaders as materialized (
      select * from by_filer
      order by total desc nulls last, filer_id
      limit greatest(coalesce(p_limit, 100), 1)
    )
    select coalesce(jsonb_agg(jsonb_build_object(
      'filer_id', l.filer_id,
      'slug', fd.slug,
      'name', coalesce(fd.name, n.name, l.filer_id),
      'total', round(l.total, 2)
    ) order by l.total desc nulls last, l.filer_id), '[]'::jsonb)
    from leaders l
    left join lateral (
      select d.name, d.slug from public.filer_detail d
      where d.filer_id = l.filer_id
      order by d.updated_at desc nulls last, d.slug
      limit 1
    ) fd on true
    left join lateral (
      -- filer_canonical is often blank in the table (process.py fills it in
      -- memory), so fall back to the name as filed.
      select btrim(coalesce(t.filer_canonical, t.filer)) as name
      from public.transactions t
      where t.tran_type = 'C' and coalesce(t.sub_type, '') not in (
          'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
          'In-Kind/Forgiven Personal Expenditures'
        )
        and t.tran_date = l.last_date and t.filer_id = l.filer_id
      limit 1
    ) n on true
  );
end;
$$;

-- What one committee (all its filer IDs) paid out between two dates, by payee:
-- every expenditure, as the profile's top_payees are built. Payee names are
-- matched case-insensitively, as the dashboard merges them.
create or replace function public.payee_leaderboard(
  p_filer_ids text[],
  p_start date default null,
  p_end date default null,
  p_limit int default 200
)
returns jsonb language plpgsql stable security invoker
set search_path = public
set plan_cache_mode = force_custom_plan
set statement_timeout = '30s'
as $$
begin
  return (
    with named as (
      select coalesce(
        nullif(public.normalize_donor_label(t.contributor_payee_canonical), ''),
        public.normalize_donor_label(t.contributor_payee)
      ) as name, t.amount
      from public.transactions t
      where t.tran_type = 'E' and t.filer_id = any(p_filer_ids)
        and (p_start is null or t.tran_date >= p_start)
        and (p_end is null or t.tran_date <= p_end)
    ), grouped as (
      select lower(name) as key, min(name) as name, sum(amount) as total
      from named where name <> ''
      group by lower(name)
      order by sum(amount) desc nulls last, lower(name)
      limit greatest(coalesce(p_limit, 200), 1)
    )
    select coalesce(jsonb_agg(jsonb_build_object('name', g.name, 'total', round(g.total, 2))
                              order by g.total desc nulls last, g.key), '[]'::jsonb)
    from grouped g
  );
end;
$$;

grant execute on function public.recipient_leaderboard(date, date, int) to anon, authenticated, service_role;
grant execute on function public.payee_leaderboard(text[], date, date, int) to anon, authenticated, service_role;
notify pgrst, 'reload schema';
