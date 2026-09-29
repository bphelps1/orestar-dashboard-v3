-- Merge saves refresh derived identity caches inside the same transaction, and
-- Supabase preloads supautils, whose safeupdate guard rejects DELETE without a
-- WHERE clause — including inside SECURITY DEFINER trigger functions. Both
-- rebuilds below intentionally clear their cache before repopulating it, so the
-- intent is made explicit rather than the guard disabled for every statement.
--
-- Without this, saving a merge at /admin/donors fails with
--   "DELETE requires a WHERE clause"
-- and the whole save rolls back. That happened on 2026-09-23: the same patch
-- had been applied to the live database by hand, and re-running
-- `db_admin.py apply` — which re-executes every migration, including the
-- `create or replace function` in 027 and 029 — silently reverted it. Living
-- in the migration list is what makes it survive the next apply.
--
-- Definitions are patched in place because the deployed functions may be newer
-- than this checkout. Where the functions do not exist, this is a no-op.
do $$
declare
  patch record;
  definition text;
begin
  for patch in select * from (values
    ('public.refresh_stored_donor_labels()',
     'delete from public.donor_identity_label_cache;',
     'delete from public.donor_identity_label_cache where true;'),
    ('public.refresh_stored_donor_identities()',
     'delete from public.donor_merge_filer_cache;',
     'delete from public.donor_merge_filer_cache where true;')
  ) as patches(signature, old_sql, new_sql)
  loop
    if to_regprocedure(patch.signature) is null then
      continue;
    end if;
    select pg_get_functiondef(to_regprocedure(patch.signature)) into definition;
    if strpos(definition, patch.old_sql) > 0 then
      execute replace(definition, patch.old_sql, patch.new_sql);
    elsif strpos(definition, patch.new_sql) = 0 then
      raise exception 'Unexpected definition for %, inspect before patching', patch.signature;
    end if;
  end loop;
end;
$$;
