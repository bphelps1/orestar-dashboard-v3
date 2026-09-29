-- Shared donor grouping for rebuilt caches and exact-date Donors queries.
-- Raw transaction labels stay intact. Resolved people retain their identities;
-- ORESTAR's pooled small-contribution category is one reporting bucket.
create or replace function public.normalize_donor_label(value text)
returns text language sql immutable parallel safe
set search_path = public
as $$
  select case when lower(label) = 'miscellaneous cash contributions $100 and under'
    then 'Miscellaneous Cash Contributions $100 and under' else label end
  from (select btrim(regexp_replace(replace(coalesce(value, ''), chr(160), ' '),
                                   '\s+', ' ', 'g')) as label) n
$$;

create or replace view public.donor_contribution_rows
with (security_invoker = true) as
with raw_totals as materialized (
  select filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee,
         sum(amount) as amount
  from public.transactions
  where tran_type = 'C' and coalesce(sub_type, '') not in (
    'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
    'In-Kind/Forgiven Personal Expenditures'
  )
  group by filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee
), named as materialized (
  select t.filer_id, t.tran_date, t.amount, t.donor_id, coalesce(
    nullif(public.normalize_donor_label(d.display_name), ''),
    nullif(public.normalize_donor_label(t.contributor_payee_canonical), ''),
    public.normalize_donor_label(t.contributor_payee)
  ) as name
  from raw_totals t left join public.donors d on d.donor_id = t.donor_id
)
select n.filer_id, n.tran_date, n.amount,
       case when lower(n.name) = 'miscellaneous cash contributions $100 and under'
            then 'label:miscellaneous cash contributions $100 and under'
            else coalesce(n.donor_id, 'name:' || lower(n.name)) end as donor_key,
       case when lower(n.name) = 'miscellaneous cash contributions $100 and under'
            then null else n.donor_id end as donor_id,
       n.name
from named n;

grant select on public.donor_contribution_rows to anon, authenticated, service_role;

-- Rank AFTER applying dates and filer scope. Year columns contain the same
-- ranked donors, including a donor below a particular year's top-1000 cutoff.
create or replace function public.donor_leaderboard(
  p_start date default null,
  p_end date default null,
  p_filer_ids text[] default null
)
returns jsonb language plpgsql stable security invoker
set search_path = public
set plan_cache_mode = force_custom_plan
-- PostgREST applies this limit to this RPC's transaction. Cold index reads
-- after a full refresh can exceed the normal API limit even with a custom plan.
set statement_timeout = '30s'
as $$
begin
  -- Bind this call's dates and filer scope when planning the query. A generic
  -- plan cannot simplify the optional filters into bounded index conditions.
  return (
  -- Collapse repeated source labels before normalization and donor joins.
  -- A large pooled category can have hundreds of thousands of transactions;
  -- normalizing every transaction repeatedly made statewide ranges too slow.
  with raw_totals as materialized (
    select t.donor_id, t.contributor_payee_canonical, t.contributor_payee,
           extract(year from t.tran_date)::int as yr, sum(t.amount) as total
    from public.transactions t
    where t.tran_type = 'C' and coalesce(t.sub_type, '') not in (
        'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
        'In-Kind/Forgiven Personal Expenditures'
      )
      and (p_start is null or t.tran_date >= p_start)
      and (p_end is null or t.tran_date <= p_end)
      and (p_filer_ids is null or t.filer_id = any(p_filer_ids))
    group by t.donor_id, t.contributor_payee_canonical, t.contributor_payee,
             extract(year from t.tran_date)::int
  ), named as materialized (
    select r.donor_id, r.yr, r.total, coalesce(
      nullif(public.normalize_donor_label(d.display_name), ''),
      nullif(public.normalize_donor_label(r.contributor_payee_canonical), ''),
      public.normalize_donor_label(r.contributor_payee)
    ) as name
    from raw_totals r left join public.donors d on d.donor_id = r.donor_id
  ), normalized as (
    select case when lower(name) = 'miscellaneous cash contributions $100 and under'
           then 'label:miscellaneous cash contributions $100 and under'
           else coalesce(donor_id, 'name:' || lower(name)) end as donor_key,
           case when lower(name) = 'miscellaneous cash contributions $100 and under'
           then null else donor_id end as donor_id,
           name, yr, total
    from named
  ), yearly as materialized (
    select donor_key, min(donor_id) as donor_id, min(name) as name, yr, sum(total) as total
    from normalized group by donor_key, yr
  ), leaders as materialized (
    select donor_key, min(donor_id) as donor_id, min(name) as name,
           round(sum(total), 2) as total
    from yearly group by donor_key
    order by sum(total) desc nulls last, donor_key limit 1000
  ), years as (
    select y.yr, jsonb_agg(jsonb_build_object(
      'donor_key', l.donor_key, 'donor_id', l.donor_id, 'name', l.name,
      'total', round(y.total, 2)
    ) order by y.total desc nulls last, l.donor_key) as rows
    from yearly y join leaders l using (donor_key)
    where y.yr is not null group by y.yr
  )
  select jsonb_build_object(
    'all_time', coalesce((select jsonb_agg(to_jsonb(l) order by l.total desc nulls last,
                                         l.donor_key) from leaders l), '[]'::jsonb),
    'by_year', coalesce((select jsonb_object_agg(yr::text, rows) from years), '{}'::jsonb)
  )
  );
end;
$$;

grant execute on function public.normalize_donor_label(text) to anon, authenticated, service_role;
grant execute on function public.donor_leaderboard(date, date, text[]) to anon, authenticated, service_role;
notify pgrst, 'reload schema';
