-- Resolve recipient names in batches. The old per-recipient OR lookup scanned
-- and decompressed every filer detail up to fifteen times for one donor.
create or replace function public.donor_profile(p_donor_id text)
returns jsonb language sql stable security invoker set search_path=public as $$
with member_ids as materialized (
 select public.donor_group_ids(p_donor_id) as ids
), donor_rows as materialized (
 select tran_date,tran_type,amount,filer_id,filer_canonical,filer
 from transactions where donor_id=any((select ids from member_ids)::text[])
), annual as (
 select extract(year from tran_date)::int as year,
   round(sum(amount) filter(where tran_type='C'),2) as given,
   round(sum(amount) filter(where tran_type='E'),2) as received
 from donor_rows where tran_date is not null group by 1
), named as (
 select nullif(btrim(filer_id),'') as filer_id,
   nullif(btrim(filer_canonical),'') as canonical_name,
   nullif(btrim(filer),'') as raw_name,amount
 from donor_rows where tran_type='C'
), recipients as materialized (
 select min(filer_id) as filer_id,coalesce(min(canonical_name),min(raw_name)) as name,
   round(sum(amount),2) as total,count(*) as n
 from named
 group by coalesce('id:'||filer_id,'name:'||lower(coalesce(canonical_name,raw_name)),'unknown:')
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
grant execute on function public.donor_profile(text) to anon,authenticated,service_role;
notify pgrst,'reload schema';
