-- Saved entity decisions take effect on reads immediately. Raw transaction
-- identities remain intact until the normal resolver; no full rebuild on Save.
alter table public.donor_merge_overrides add column if not exists keep_alias_key text;

-- Preserve a stable route from historical IDs to the current entity when a
-- weekly resolution changes cluster hashes. No private review notes are exposed.
create table if not exists public.donor_identity_anchors (
  donor_id text primary key, alias_key text not null
);
alter table public.donor_identity_anchors enable row level security;
drop policy if exists "Read identity anchors" on public.donor_identity_anchors;
create policy "Read identity anchors" on public.donor_identity_anchors for select using (true);
grant select on public.donor_identity_anchors to anon, authenticated, service_role;

create or replace function public.capture_donor_identity_anchors()
returns trigger language plpgsql security definer set search_path = public,pg_temp as $$
begin
  insert into donor_identity_anchors(donor_id, alias_key)
  select a.donor_id, min(a.alias_key) from donor_aliases a
  where a.donor_id in (select b.donor_id from donor_aliases b
    where b.alias_key in (new.alias_a, new.alias_b))
  group by a.donor_id on conflict (donor_id) do nothing;
  return new;
end;
$$;
revoke all on function public.capture_donor_identity_anchors() from public;
drop trigger if exists capture_merge_identity_anchors on public.donor_merge_overrides;
create trigger capture_merge_identity_anchors after insert or update on public.donor_merge_overrides
for each row execute function public.capture_donor_identity_anchors();
insert into donor_identity_anchors(donor_id, alias_key)
select a.donor_id, min(a.alias_key) from donor_aliases a
where a.donor_id in (
  select b.donor_id from donor_aliases b join donor_merge_overrides m
    on b.alias_key in (m.alias_a, m.alias_b)
  union select donor_id from donor_lobbyist_links
  union select donor_id from donor_client_links
  union select donor_id from donor_contacts
) group by a.donor_id on conflict (donor_id) do nothing;

-- Connect current donor IDs, not just alias keys: A-B on one B address and
-- B-C on another B address must form one group. UNION terminates cycles.
create or replace view public.donor_identity_map as
with recursive pairs(a,b) as (
  select a.donor_id, b.donor_id from donor_merge_overrides m
  join donor_aliases a on a.alias_key=m.alias_a
  join donor_aliases b on b.alias_key=m.alias_b where m.decision='merged'
  union
  select h.donor_id, a.donor_id from donor_identity_anchors h
  join donor_aliases a on a.alias_key=h.alias_key
  where h.donor_id <> a.donor_id and not exists (
    select 1 from donors d where d.donor_id=h.donor_id)
), edges(a,b) as (
  select a,b from pairs union select b,a from pairs
), reach(node,root) as (
  select a,a from edges union select e.b,r.root from reach r join edges e on e.a=r.node
), components as (
  select node as donor_id,min(root) as group_key from reach group by node
), choices as (
  select c.group_key, coalesce(
    (select a.donor_id from donor_merge_overrides m
      join donor_aliases a on a.alias_key=m.keep_alias_key
      join components x on x.donor_id=a.donor_id and x.group_key=c.group_key
      where m.decision='merged' order by m.decided_at desc, m.merge_key limit 1),
    min(d.donor_id)) as canonical_id
  from components c join donors d on d.donor_id=c.donor_id group by c.group_key
)
select c.donor_id,x.canonical_id,d.display_name as canonical_name
from components c join choices x using(group_key) join donors d on d.donor_id=x.canonical_id;
grant select on public.donor_identity_map to anon, authenticated, service_role;

create or replace function public.donor_group_ids(p_donor_id text)
returns text[] language sql stable security invoker set search_path=public as $$
  with m as materialized (select * from donor_identity_map),
  chosen as (select coalesce((select canonical_id from m where donor_id=p_donor_id),p_donor_id) as id)
  select array(select donor_id from m where canonical_id=(select id from chosen)
    union select id from chosen);
$$;
grant execute on function public.donor_group_ids(text) to anon, authenticated, service_role;

create or replace function public.donor_identity(p_donor_id text)
returns jsonb language sql stable security invoker set search_path=public as $$
  with m as materialized (select * from donor_identity_map),
  chosen as (select coalesce((select canonical_id from m where donor_id=p_donor_id),p_donor_id) as id),
  members as (select donor_id from m where canonical_id=(select id from chosen)
    union select id from chosen)
  select to_jsonb(d) || jsonb_build_object(
    'member_ids', (select jsonb_agg(donor_id order by donor_id) from members),
    'total_given', (select coalesce(sum(s.total_given),0) from donors s join members using(donor_id)),
    'total_received', (select coalesce(sum(s.total_received),0) from donors s join members using(donor_id)),
    'total_inkind', (select coalesce(sum(s.total_inkind),0) from donors s join members using(donor_id)),
    'gift_count', (select coalesce(sum(s.gift_count),0) from donors s join members using(donor_id)),
    'first_date', (select min(s.first_date) from donors s join members using(donor_id)),
    'last_date', (select max(s.last_date) from donors s join members using(donor_id)),
    'alias_count', (select count(*) from donor_aliases a join members using(donor_id))
  ) from donors d where d.donor_id=(select id from chosen);
$$;
grant execute on function public.donor_identity(text) to anon, authenticated, service_role;

-- Only unambiguous labels can re-key old name-only tooltip caches. A name
-- shared with an unrelated entity is never automatically folded into a merge.
create or replace view public.donor_identity_labels as
with m as materialized (select * from donor_identity_map),
labels as (
  select lower(regexp_replace(btrim(a.raw_name),'\s+',' ','g')) as label,
         a.donor_id from donor_aliases a
  union select lower(regexp_replace(btrim(d.display_name),'\s+',' ','g')),d.donor_id from donors d
), candidates as (select distinct l.label from labels l join m using(donor_id))
select l.label,min(coalesce(m.canonical_id,l.donor_id)) as canonical_id,
       max(m.canonical_name) as canonical_name
from labels l join candidates c using(label) left join m using(donor_id)
group by l.label having count(distinct coalesce(m.canonical_id,l.donor_id))=1;
grant select on public.donor_identity_labels to anon, authenticated, service_role;

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
  select t.filer_id, t.tran_date, t.amount, coalesce(mi.canonical_id,t.donor_id) as donor_id, coalesce(
    mi.canonical_name,
    nullif(public.normalize_donor_label(d.display_name), ''),
    nullif(public.normalize_donor_label(t.contributor_payee_canonical), ''),
    public.normalize_donor_label(t.contributor_payee)
  ) as name
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
    select coalesce(mi.canonical_id,r.donor_id) as donor_id, r.yr, r.total, coalesce(
      mi.canonical_name,
      nullif(public.normalize_donor_label(d.display_name), ''),
      nullif(public.normalize_donor_label(r.contributor_payee_canonical), ''),
      public.normalize_donor_label(r.contributor_payee)
    ) as name
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

-- Repair Top Recipients names and aggregate once per recipient committee ID.
-- ============================================================================
-- donor_profile(): one-call profile aggregates for the /donors page.
--
-- Returns jsonb {by_year, top_recipients} computed server-side over the
-- donor_id index — a large PAC can have 20k+ transactions, far too many to
-- aggregate in the browser. Read-only, safe for anon (RLS-readable tables).
-- ============================================================================

create or replace function donor_profile(p_donor_id text)
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'by_year', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'year', y, 'given', coalesce(g, 0), 'received', coalesce(r, 0)
             ) order by y), '[]'::jsonb)
      from (
        select extract(year from tran_date)::int as y,
               round(sum(amount) filter (where tran_type = 'C')::numeric, 2) as g,
               round(sum(amount) filter (where tran_type = 'E')::numeric, 2) as r
        from transactions
        where donor_id = any(public.donor_group_ids(p_donor_id)) and tran_date is not null
        group by 1
      ) t
    ),
    'top_recipients', (
      -- Group on committee identity before choosing a display label. A blank
      -- canonical name and a populated one must not split the same recipient.
      with named as (
        select nullif(btrim(t.filer_id), '') as filer_id,
               nullif(btrim(t.filer_canonical), '') as canonical_name,
               nullif(btrim(t.filer), '') as raw_name, t.amount
        from transactions t
        where t.donor_id = any(public.donor_group_ids(p_donor_id)) and t.tran_type = 'C'
      ), recipients as (
        select min(filer_id) as filer_id,
               coalesce(min(canonical_name), min(raw_name)) as name,
               round(sum(amount)::numeric, 2) as total, count(*) as n
        from named
        group by coalesce('id:' || filer_id,
                          'name:' || lower(coalesce(canonical_name, raw_name)),
                          'unknown:')
        order by sum(amount) desc, min(filer_id), min(canonical_name), min(raw_name)
        limit 15
      )
      select coalesce(jsonb_agg(jsonb_build_object(
        'filer', coalesce(nullif(btrim(fd.name), ''), r.name,
                          'Committee ' || r.filer_id, 'Committee name unavailable'),
        'filer_id', r.filer_id, 'slug', fd.slug, 'total', r.total, 'n', r.n
      ) order by r.total desc, r.filer_id, r.name), '[]'::jsonb)
      from recipients r
      left join lateral (
        select f.slug, f.name from filer_detail f
        where r.filer_id is not null and (f.filer_id = r.filer_id
          or (f.detail->'filer_ids') ? r.filer_id)
        order by (f.filer_id = r.filer_id) desc nulls last, f.slug
        limit 1
      ) fd on true
    )
  )
$$;

grant execute on function donor_profile(text) to anon, authenticated;

create or replace function public.search_donors(p_q text,p_limit int default 12)
returns table(donor_id text,display_name text,book_type text,city text,state text,
  total_given numeric,gift_count int,filer_slug text,matched_alias text)
language sql stable security invoker set search_path=public as $$
  with m as materialized (select * from donor_identity_map),
  hits as (
    select d.donor_id,null::text as alias from donors d
      where length(btrim(p_q))>=2 and d.display_name ilike '%' || btrim(p_q) || '%'
    union select a.donor_id,a.raw_name from donor_aliases a
      where length(btrim(p_q))>=2 and a.raw_name ilike '%' || btrim(p_q) || '%'
  ), groups as (
    select coalesce(m.canonical_id,h.donor_id) as id,min(h.alias) as alias
    from hits h left join m using(donor_id) group by 1
  ), totals as (
    select g.id,g.alias,sum(d.total_given) as total_given,sum(d.gift_count)::int as gift_count
    from donors d left join m on m.donor_id=d.donor_id
    join groups g on g.id=coalesce(m.canonical_id,d.donor_id)
    group by g.id,g.alias
  )
  select g.id,d.display_name,d.book_type,d.city,d.state,g.total_given,g.gift_count,d.filer_slug,g.alias
  from totals g join donors d on d.donor_id=g.id
  order by g.total_given desc nulls last,g.id
  limit greatest(1,least(coalesce(p_limit,12),50));
$$;
grant execute on function public.search_donors(text,int) to anon,authenticated;

-- The admin picker uses the same identity groups as every other donor read.
-- Drop first because the optional, older migration 018 added extra columns.
drop function if exists public.donor_search(text,int);
create function public.donor_search(p_q text,p_limit int default 12)
returns table(donor_id text,display_name text,book_type text,city text,state text,
 total_given numeric,gift_count int,alias_count int,rep_alias_key text,addresses text[])
language sql stable security invoker set search_path=public as $$
 select s.donor_id,s.display_name,s.book_type,s.city,s.state,s.total_given,s.gift_count,
   (select count(*)::int from donor_aliases a where a.donor_id=any(donor_group_ids(s.donor_id))),
   (select a.alias_key from donor_aliases a where a.donor_id=s.donor_id order by a.alias_key limit 1),
   (select array_agg(distinct a.addr_key) filter(where a.addr_key<>'') from donor_aliases a
      where a.donor_id=any(donor_group_ids(s.donor_id)))
 from search_donors(p_q,p_limit) s;
$$;
grant execute on function public.donor_search(text,int) to authenticated;
notify pgrst,'reload schema';

create or replace view public.donor_merge_filers as
select distinct t.filer_id from transactions t join donor_identity_map m using(donor_id)
where t.filer_id is not null;
grant select on public.donor_merge_filers to anon,authenticated,service_role;

create or replace function public.recommendation_first_gifts(
 p_donor_ids text[],p_filer_ids text[],p_through date
) returns table(donor_id text,filer_id text,first_date date,amount numeric)
language sql stable security invoker set search_path=public as $$
 with source_ids as (select distinct unnest(donor_group_ids(id)) as id from unnest(p_donor_ids) x(id)),
 gifts as (
  select coalesce(m.canonical_id,t.donor_id) as donor_id,t.filer_id,t.tran_date,t.amount,t.tran_id
  from transactions t left join donor_identity_map m on m.donor_id=t.donor_id
  where t.donor_id in (select id from source_ids) and t.filer_id=any(p_filer_ids)
   and t.tran_date<=p_through and t.amount>0 and t.tran_type='C'
   and coalesce(t.sub_type,'') not in ('In-Kind Contribution',
    'In-Kind/Forgiven Account Payable','In-Kind/Forgiven Personal Expenditures')
 ) select distinct on (g.donor_id,g.filer_id) g.donor_id,g.filer_id,g.tran_date,g.amount
 from gifts g order by g.donor_id,g.filer_id,g.tran_date,g.tran_id;
$$;
notify pgrst,'reload schema';

-- Negative review decisions veto the combined identity too.
create or replace view donor_lobbyists with (security_invoker = true) as
with identity_map as materialized (select * from donor_identity_map),
mapped_direct as (
  select (jsonb_populate_record(null::donor_lobbyist_links,to_jsonb(l) ||
    jsonb_build_object('donor_id',coalesce(m.canonical_id,l.donor_id)))).*
  from donor_lobbyist_links l left join identity_map m on m.donor_id=l.donor_id
), client_rows as (
  select (jsonb_populate_record(null::donor_client_links,to_jsonb(l) ||
    jsonb_build_object('donor_id',coalesce(m.canonical_id,l.donor_id)))).*
  from donor_client_links l left join identity_map m on m.donor_id=l.donor_id
), mapped_clients as (
  select distinct on (donor_id,client_key) * from client_rows
  order by donor_id,client_key,(status='rejected') desc,(status='confirmed') desc,
    decided_at desc nulls last,score desc nulls last
), client_leads as (
  select c.client_key, d.lobbyist_id
  from mapped_clients c
  join mapped_direct d on d.donor_id = c.donor_id and d.status = 'confirmed'
  join lobbyist_clients lc on lc.client_key = c.client_key
                          and lc.lobbyist_id = d.lobbyist_id and lc.active
  where c.status <> 'rejected'
  group by c.client_key, d.lobbyist_id
),
paths as (
  select l.donor_id, l.lobbyist_id, l.status, l.method, l.score,
         null::text as client_name, l.is_primary
  from mapped_direct l
  where l.status <> 'rejected'
  union all
  select c.donor_id, lc.lobbyist_id, c.status, 'client:' || c.method, c.score,
         c.client_name,
         not exists (select 1 from mapped_direct pri
                     where pri.donor_id = c.donor_id and pri.is_primary
                       and pri.status <> 'rejected')
         and (lc.is_lead or (
           not exists (select 1 from lobbyist_clients x
                       where x.client_key = c.client_key and x.is_lead and x.active)
           and exists (select 1 from client_leads cl
                       where cl.client_key = c.client_key and cl.lobbyist_id = lc.lobbyist_id)))
  from mapped_clients c
  join lobbyist_clients lc on lc.client_key = c.client_key and lc.active
  where c.status <> 'rejected'
)
select p.donor_id,
       p.lobbyist_id,
       case when bool_or(p.status = 'confirmed') then 'confirmed' else 'suggested' end as status,
       array_agg(distinct p.method) as methods,
       array_remove(array_agg(distinct p.client_name), null) as client_names,
       max(p.score) as score,
       bool_or(p.is_primary) as is_primary
from paths p
where not exists (
  select 1 from mapped_direct r
  where r.donor_id = p.donor_id and r.lobbyist_id = p.lobbyist_id and r.status = 'rejected'
)
group by p.donor_id, p.lobbyist_id;


grant select on donor_lobbyists to authenticated;

create or replace function public.validate_entity_merge()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
declare target text;
begin
  if new.decision<>'merged' then return new; end if;
  select coalesce(m.canonical_id,a.donor_id) into target from donor_aliases a
    left join donor_identity_map m using(donor_id) where a.alias_key=new.alias_a;
  if exists (
    select 1 from donor_merge_overrides s
    join donor_aliases a on a.alias_key=s.alias_a
    join donor_aliases b on b.alias_key=s.alias_b
    left join donor_identity_map ma on ma.donor_id=a.donor_id
    left join donor_identity_map mb on mb.donor_id=b.donor_id
    where s.decision='separate' and coalesce(ma.canonical_id,a.donor_id)=target
      and coalesce(mb.canonical_id,b.donor_id)=target
  ) then raise exception 'This merge conflicts with an existing separate decision. Resolve that decision first.'; end if;
  return new;
end;
$$;
revoke all on function public.validate_entity_merge() from public;
drop trigger if exists validate_entity_merge on public.donor_merge_overrides;
create constraint trigger validate_entity_merge after insert or update on public.donor_merge_overrides
 deferrable initially immediate for each row execute function public.validate_entity_merge();
