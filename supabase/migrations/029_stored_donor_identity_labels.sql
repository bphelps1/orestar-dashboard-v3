-- Name-only chart caches need an unambiguous label-to-entity lookup too.
-- Store it at the same atomic save/import boundary as canonical IDs.
create table if not exists public.donor_identity_label_cache (
 label text primary key,canonical_id text not null
);
alter table public.donor_identity_label_cache enable row level security;
drop policy if exists "Read stored identity labels" on public.donor_identity_label_cache;
create policy "Read stored identity labels" on public.donor_identity_label_cache for select using(true);
revoke all on public.donor_identity_label_cache from public,anon,authenticated;
grant select on public.donor_identity_label_cache to anon,authenticated,service_role;

create or replace function public.refresh_stored_donor_labels()
returns void language plpgsql security definer set search_path=public,pg_temp as $$
begin
  perform pg_advisory_xact_lock(726027);
  delete from public.donor_identity_label_cache;
  insert into public.donor_identity_label_cache(label,canonical_id)
  with m as materialized (select donor_id,canonical_id from public.donor_identity_map),
  candidates as materialized (
    select lower(regexp_replace(btrim(a.raw_name),'\s+',' ','g')) as label
    from public.donor_aliases a join m using(donor_id)
    union
    select lower(regexp_replace(btrim(d.display_name),'\s+',' ','g'))
    from public.donors d join m using(donor_id)
  )
  select l.label,min(coalesce(m.canonical_id,s.donor_id))
  from candidates l
  cross join lateral (
    -- Check ALL owners of each candidate label, not just merged entities.
    -- Both branches use migration 028's expression indexes.
    select a.donor_id from public.donor_aliases a
      where lower(regexp_replace(btrim(a.raw_name),'\s+',' ','g'))=l.label
    union
    select d.donor_id from public.donors d
      where lower(regexp_replace(btrim(d.display_name),'\s+',' ','g'))=l.label
  ) s
  left join m on m.donor_id=s.donor_id
  where l.label is not null
  group by l.label having count(distinct coalesce(m.canonical_id,s.donor_id))=1;
end;
$$;
revoke all on function public.refresh_stored_donor_labels() from public,anon,authenticated;
grant execute on function public.refresh_stored_donor_labels() to service_role;

create or replace view public.donor_identity_labels as
select l.label,l.canonical_id,d.display_name as canonical_name
from public.donor_identity_label_cache l join public.donors d on d.donor_id=l.canonical_id;
grant select on public.donor_identity_labels to anon,authenticated,service_role;

create or replace function public.refresh_stored_donor_identities()
returns void language plpgsql security definer set search_path=public,pg_temp as $$
declare changed_ids text[];
begin
  -- Serialize refreshes and the new-transaction cache maintenance.
  perform pg_advisory_xact_lock(726027);
  with next_map as materialized (select donor_id,canonical_id from public.donor_identity_graph),
  removed as (delete from public.donor_identity_redirects r
    where not exists (select 1 from next_map n where n.donor_id=r.donor_id)),
  changed as (
    insert into public.donor_identity_redirects as r(donor_id,canonical_id)
      select donor_id,canonical_id from next_map
    on conflict(donor_id) do update set canonical_id=excluded.canonical_id
      where r.canonical_id is distinct from excluded.canonical_id
    returning donor_id
  ) select array_agg(donor_id) into changed_ids from changed;
  update public.donors d set canonical_entity_id=d.donor_id
    where d.canonical_entity_id<>d.donor_id and not exists
      (select 1 from public.donor_identity_redirects r where r.donor_id=d.donor_id);
  update public.donors d set canonical_entity_id=r.canonical_id
    from public.donor_identity_redirects r
    where d.donor_id=r.donor_id and d.canonical_entity_id is distinct from r.canonical_id;
  -- Only new/changed members need a history lookup on Save. A removed member
  -- may retain a harmless cache flag: fresh totals are safer than a stale cache.
  insert into public.donor_merge_filer_cache(filer_id)
    select distinct t.filer_id from public.transactions t
    where t.donor_id=any(changed_ids) and t.filer_id is not null
    on conflict do nothing;
  if not exists (select 1 from public.donor_identity_redirects) then
    delete from public.donor_merge_filer_cache;
  end if;
  perform public.refresh_stored_donor_labels();
end;
$$;
revoke all on function public.refresh_stored_donor_identities() from public,anon,authenticated;
grant execute on function public.refresh_stored_donor_identities() to service_role;

-- Name edits can introduce/remove a collision with a different donor even
-- when canonical IDs have not changed. Normal amount/stat updates do not queue.
drop trigger if exists queue_donor_label_refresh on public.donors;
create trigger queue_donor_label_refresh after update of display_name on public.donors
for each statement execute function public.queue_donor_identity_refresh();

select public.refresh_stored_donor_labels();
notify pgrst,'reload schema';
