-- Net refunds of contributions against the donor who got the money back.
--
-- Donor totals counted every cash gift and ignored refunds, so a returned
-- check stayed on the donor's books. Future PAC refunded Friends of Julie
-- Fahey $80,000 in Oct 2024 ("check error, re-sending") and the resent check
-- was counted as well, so Fahey ranked $80,000 above what Future PAC kept.
-- SDLF's $50,000 refund to Friends of Rob Wagner did the same.
--
-- A refund ("Return or Refund of Contribution", filed as tran_type OD) carries
-- the same donor_id as the gift, so it is subtracted by its own date, from the
-- committee that refunded it:
--   * donor_contribution_rows — the source for the statewide and per-committee
--     donor tables refresh_donor_aggregates.py builds
--   * donor_leaderboard()     — exact-date rankings (Donors tab, caucus tile,
--     Recommend)
--   * donor_profile()         — the donor page's by-year giving and recipients
-- Rankings leave out a donor whose gifts net to zero or less in the scope
-- asked for: a donor refunded in full gave nothing that was kept. A refund in
-- a later year than its gift lowers that later year, so a year can net to
-- zero or less and is then left out of that donor's by-year list.
--
-- Committee-level totals (timeline contributions, ORESTAR summaries) stay
-- gross, as ORESTAR reports them. The refund rows (about 7,600) are read
-- through idx_txn_sub_type_date (033), a quarter-second statewide.

-- ORESTAR pools refunds of $100 and under under their own label; they belong
-- to the same pooled bucket as the small gifts they returned.
create or replace function public.donor_bucket_label(value text)
returns text language sql immutable parallel safe
set search_path = public
as $$
  select case when lower(value) = 'miscellaneous return/refund of contribution $100 and under'
    then 'Miscellaneous Cash Contributions $100 and under' else value end
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
  union all
  select filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee,
         -sum(amount) as amount
  from public.transactions
  where sub_type = 'Return or Refund of Contribution'
  group by filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee
), named as materialized (
  select t.filer_id, t.tran_date, t.amount, coalesce(mi.canonical_id,t.donor_id) as donor_id,
    public.donor_bucket_label(coalesce(
      mi.canonical_name,
      nullif(public.normalize_donor_label(d.display_name), ''),
      nullif(public.normalize_donor_label(t.contributor_payee_canonical), ''),
      public.normalize_donor_label(t.contributor_payee)
    )) as name
  from raw_totals t left join public.donors d on d.donor_id = t.donor_id
  left join public.donor_identity_map mi on mi.donor_id=t.donor_id
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
  -- Gifts read idx_txn_cash_donor_dates (015), refunds idx_txn_sub_type_date
  -- (033).
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
    union all
    select t.donor_id, t.contributor_payee_canonical, t.contributor_payee,
           extract(year from t.tran_date)::int as yr, -sum(t.amount) as total
    from public.transactions t
    where t.sub_type = 'Return or Refund of Contribution'
      and (p_start is null or t.tran_date >= p_start)
      and (p_end is null or t.tran_date <= p_end)
      and (p_filer_ids is null or t.filer_id = any(p_filer_ids))
    group by t.donor_id, t.contributor_payee_canonical, t.contributor_payee,
             extract(year from t.tran_date)::int
  ), named as materialized (
    select coalesce(mi.canonical_id,r.donor_id) as donor_id, r.yr, r.total,
      public.donor_bucket_label(coalesce(
        mi.canonical_name,
        nullif(public.normalize_donor_label(d.display_name), ''),
        nullif(public.normalize_donor_label(r.contributor_payee_canonical), ''),
        public.normalize_donor_label(r.contributor_payee)
      )) as name
    from raw_totals r left join public.donors d on d.donor_id = r.donor_id
    left join public.donor_identity_map mi on mi.donor_id=r.donor_id
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
    having sum(total) > 0
    order by sum(total) desc nulls last, donor_key limit 1000
  ), years as (
    select y.yr, jsonb_agg(jsonb_build_object(
      'donor_key', l.donor_key, 'donor_id', l.donor_id, 'name', l.name,
      'total', round(y.total, 2)
    ) order by y.total desc nulls last, l.donor_key) as rows
    from yearly y join leaders l using (donor_key)
    where y.yr is not null and y.total > 0 group by y.yr
  )
  select jsonb_build_object(
    'all_time', coalesce((select jsonb_agg(to_jsonb(l) order by l.total desc nulls last,
                                         l.donor_key) from leaders l), '[]'::jsonb),
    'by_year', coalesce((select jsonb_object_agg(yr::text, rows) from years), '{}'::jsonb)
  )
  );
end;
$$;

-- donor_profile as in 024, with refunds netted: "given" per year is gifts less
-- refunds that year (never below zero), and each recipient's total is what it
-- kept. n still counts gifts.
create or replace function public.donor_profile(p_donor_id text)
returns jsonb language sql stable security invoker set search_path=public as $$
with member_ids as materialized (
 select public.donor_group_ids(p_donor_id) as ids
), donor_rows as materialized (
 select tran_date,tran_type,sub_type,amount,filer_id,filer_canonical,filer
 from transactions where donor_id=any((select ids from member_ids)::text[])
), annual as (
 select extract(year from tran_date)::int as year,
   round(greatest(coalesce(sum(amount) filter(where tran_type='C'),0)
     - coalesce(sum(amount) filter(where sub_type='Return or Refund of Contribution'),0),0),2) as given,
   round(sum(amount) filter(where tran_type='E'),2) as received
 from donor_rows where tran_date is not null group by 1
), named as (
 select nullif(btrim(filer_id),'') as filer_id,
   nullif(btrim(filer_canonical),'') as canonical_name,
   nullif(btrim(filer),'') as raw_name,
   case when tran_type='C' then amount else -amount end as amount,
   tran_type='C' as is_gift
 from donor_rows where tran_type='C' or sub_type='Return or Refund of Contribution'
), recipients as materialized (
 select min(filer_id) as filer_id,coalesce(min(canonical_name),min(raw_name)) as name,
   round(sum(amount),2) as total,count(*) filter(where is_gift) as n
 from named
 group by coalesce('id:'||filer_id,'name:'||lower(coalesce(canonical_name,raw_name)),'unknown:')
 having sum(amount) > 0
 order by sum(amount) desc,min(filer_id),min(canonical_name),min(raw_name) limit 15
), exact_names as materialized (
 select r.filer_id,f.slug,f.name
 from recipients r join filer_detail f on f.filer_id=r.filer_id
), missing_ids as materialized (
 select coalesce(array_agg(distinct r.filer_id),'{}'::text[]) as ids
 from recipients r where r.filer_id is not null
 and not exists(select 1 from exact_names e where e.filer_id=r.filer_id)
), fallback_names as materialized (
 select f.slug,f.name,f.detail->'filer_ids' as ids
 from filer_detail f
 where cardinality((select ids from missing_ids))>0
 and (f.detail->'filer_ids') ?| (select ids from missing_ids)
)
select jsonb_build_object(
 'by_year',coalesce((select jsonb_agg(jsonb_build_object(
   'year',year,'given',coalesce(given,0),'received',coalesce(received,0)) order by year) from annual),'[]'::jsonb),
 'top_recipients',coalesce((select jsonb_agg(jsonb_build_object(
   'filer',coalesce(nullif(btrim(fd.name),''),r.name,'Committee '||r.filer_id,'Committee name unavailable'),
   'filer_id',r.filer_id,'slug',fd.slug,'total',r.total,'n',r.n
 ) order by r.total desc,r.filer_id,r.name)
 from recipients r left join lateral (
   select e.slug,e.name from exact_names e where e.filer_id=r.filer_id
   union all
   select f.slug,f.name from fallback_names f where f.ids ? r.filer_id
     and not exists(select 1 from exact_names e where e.filer_id=r.filer_id)
   order by slug limit 1
 ) fd on true),'[]'::jsonb)
);
$$;

grant execute on function public.donor_bucket_label(text) to anon, authenticated, service_role;
grant execute on function public.donor_leaderboard(date, date, text[]) to anon, authenticated, service_role;
grant execute on function public.donor_profile(text) to anon, authenticated, service_role;
notify pgrst, 'reload schema';
