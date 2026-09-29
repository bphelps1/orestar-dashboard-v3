-- Resolve identities on writes, not while a user loads recommendations.
alter table public.donors add column if not exists canonical_entity_id text;
update public.donors set canonical_entity_id=donor_id where canonical_entity_id is null;
alter table public.donors alter column canonical_entity_id set not null;
create index if not exists idx_donors_canonical_entity on public.donors(canonical_entity_id);
create index if not exists idx_donors_redirected on public.donors(donor_id) where canonical_entity_id<>donor_id;

create or replace function public.default_canonical_donor()
returns trigger language plpgsql set search_path=public,pg_temp as $$
begin
  new.canonical_entity_id := coalesce(new.canonical_entity_id,new.donor_id);
  return new;
end;
$$;
drop trigger if exists default_canonical_donor on public.donors;
create trigger default_canonical_donor before insert on public.donors
for each row execute function public.default_canonical_donor();

-- Includes historical IDs after a resolver changes a cluster hash.
create table if not exists public.donor_identity_redirects (
 donor_id text primary key, canonical_id text not null
);
create index if not exists idx_identity_redirect_canonical on public.donor_identity_redirects(canonical_id);
create table if not exists public.donor_merge_filer_cache (filer_id text primary key);
alter table public.donor_identity_redirects enable row level security;
alter table public.donor_merge_filer_cache enable row level security;
drop policy if exists "Read stored identities" on public.donor_identity_redirects;
create policy "Read stored identities" on public.donor_identity_redirects for select using(true);
drop policy if exists "Read merge filer cache" on public.donor_merge_filer_cache;
create policy "Read merge filer cache" on public.donor_merge_filer_cache for select using(true);
revoke all on public.donor_identity_redirects,public.donor_merge_filer_cache from public,anon,authenticated;
grant select on public.donor_identity_redirects,public.donor_merge_filer_cache to anon,authenticated,service_role;

create or replace view public.donor_identity_graph as
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

-- The recursive calculation is private and used only for save/import maintenance.
revoke all on public.donor_identity_graph from public,anon,authenticated;

create or replace view public.donor_identity_map as
select r.donor_id,coalesce(d.canonical_entity_id,r.canonical_id) as canonical_id,
       canonical.display_name as canonical_name
from public.donor_identity_redirects r
left join public.donors d on d.donor_id=r.donor_id
join public.donors canonical on canonical.donor_id=coalesce(d.canonical_entity_id,r.canonical_id);
grant select on public.donor_identity_map to anon,authenticated,service_role;

create or replace view public.donor_merge_filers as
select filer_id from public.donor_merge_filer_cache;
grant select on public.donor_merge_filers to anon,authenticated,service_role;

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
end;
$$;
revoke all on function public.refresh_stored_donor_identities() from public,anon,authenticated;
grant execute on function public.refresh_stored_donor_identities() to service_role;

-- A singleton queue serializes identity-changing writers and coalesces a bulk
-- save/COPY/import into one refresh immediately before commit. Failure rolls
-- back the merge/import too; readers never see a half-applied identity change.
create table if not exists public.donor_identity_refresh_queue(id boolean primary key check(id));
alter table public.donor_identity_refresh_queue enable row level security;
revoke all on public.donor_identity_refresh_queue from public,anon,authenticated;
create or replace function public.queue_donor_identity_refresh()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
  perform pg_advisory_xact_lock(726027);
  insert into public.donor_identity_refresh_queue values(true) on conflict do nothing;
  return null;
end;
$$;
create or replace function public.finish_donor_identity_refresh()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
  delete from public.donor_identity_refresh_queue where id=true;
  perform public.refresh_stored_donor_identities();
  return null;
end;
$$;
revoke all on function public.queue_donor_identity_refresh(),public.finish_donor_identity_refresh() from public,anon,authenticated;
drop trigger if exists finish_donor_identity_refresh on public.donor_identity_refresh_queue;
create constraint trigger finish_donor_identity_refresh after insert on public.donor_identity_refresh_queue
 deferrable initially deferred for each row execute function public.finish_donor_identity_refresh();

drop trigger if exists queue_merge_identity_refresh on public.donor_merge_overrides;
create trigger queue_merge_identity_refresh after insert or update or delete or truncate on public.donor_merge_overrides
for each statement execute function public.queue_donor_identity_refresh();
drop trigger if exists queue_alias_identity_refresh on public.donor_aliases;
create trigger queue_alias_identity_refresh after insert or update or delete or truncate on public.donor_aliases
for each statement execute function public.queue_donor_identity_refresh();
drop trigger if exists queue_anchor_identity_refresh on public.donor_identity_anchors;
create trigger queue_anchor_identity_refresh after insert or update or delete or truncate on public.donor_identity_anchors
for each statement execute function public.queue_donor_identity_refresh();
drop trigger if exists queue_removed_donor_refresh on public.donors;
create trigger queue_removed_donor_refresh after insert or delete or truncate on public.donors
for each statement execute function public.queue_donor_identity_refresh();

-- New imports and resolver reassignments can affect a previously untouched
-- committee. Keep the small cache current from the statement's new rows.
-- Deletions may leave a harmless extra cache entry until the next refresh:
-- this requests fresh totals rather than incorrectly serving a stale profile.
create or replace function public.track_merged_transaction_filers()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
  perform pg_advisory_xact_lock(726027);
  insert into public.donor_merge_filer_cache(filer_id)
    select distinct n.filer_id from new_rows n
    join public.donor_identity_redirects m on m.donor_id=n.donor_id
    where n.filer_id is not null on conflict do nothing;
  return null;
end;
$$;
revoke all on function public.track_merged_transaction_filers() from public,anon,authenticated;
drop trigger if exists track_inserted_merge_filers on public.transactions;
create trigger track_inserted_merge_filers after insert on public.transactions
 referencing new table as new_rows for each statement execute function public.track_merged_transaction_filers();
drop trigger if exists track_updated_merge_filers on public.transactions;
create trigger track_updated_merge_filers after update on public.transactions
 referencing new table as new_rows for each statement execute function public.track_merged_transaction_filers();

-- Validate against the live graph, since a deferred refresh has not run yet.
create or replace function public.validate_entity_merge()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
declare target text;
begin
  if new.decision<>'merged' then return new; end if;
  select coalesce(m.canonical_id,a.donor_id) into target from donor_aliases a
    left join donor_identity_graph m using(donor_id) where a.alias_key=new.alias_a;
  if exists (
    select 1 from donor_merge_overrides s
    join donor_aliases a on a.alias_key=s.alias_a
    join donor_aliases b on b.alias_key=s.alias_b
    left join donor_identity_graph ma on ma.donor_id=a.donor_id
    left join donor_identity_graph mb on mb.donor_id=b.donor_id
    where s.decision='separate' and coalesce(ma.canonical_id,a.donor_id)=target
      and coalesce(mb.canonical_id,b.donor_id)=target
  ) then raise exception 'This merge conflicts with an existing separate decision. Resolve that decision first.'; end if;
  return new;
end;
$$;

select public.refresh_stored_donor_identities();
notify pgrst,'reload schema';
