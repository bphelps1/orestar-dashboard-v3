-- db_admin.py builds these separately and concurrently on production.
create index if not exists idx_aliases_identity_label on public.donor_aliases
  (lower(regexp_replace(btrim(raw_name),'\s+',' ','g')));
create index if not exists idx_donors_identity_label on public.donors
  (lower(regexp_replace(btrim(display_name),'\s+',' ','g')));
