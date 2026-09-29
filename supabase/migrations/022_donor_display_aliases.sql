-- Publish only adopted spellings, never private review metadata. Including
-- the kept spelling itself preserves an admin's chosen capitalization.
create or replace view public.donor_display_aliases as
with labels as (
  select merged_name as label,kept_name,decided_at,pair_key from public.donor_review_decisions
  where decision='merged' and nullif(btrim(merged_name),'') is not null
    and nullif(btrim(kept_name),'') is not null
  union all
  select kept_name,kept_name,decided_at,pair_key from public.donor_review_decisions
  where decision='merged' and nullif(btrim(kept_name),'') is not null
)
select distinct on (lower(regexp_replace(btrim(label),'\s+',' ','g')))
  lower(regexp_replace(btrim(label),'\s+',' ','g')) as alias,
  kept_name as display_name
from labels
order by lower(regexp_replace(btrim(label),'\s+',' ','g')),decided_at desc,pair_key;
grant select on public.donor_display_aliases to anon,authenticated,service_role;
notify pgrst,'reload schema';
