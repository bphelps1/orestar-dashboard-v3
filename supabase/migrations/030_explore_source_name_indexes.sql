-- db_admin.py builds each index concurrently, without blocking imports.
-- Source names are present even when derived canonical fields are not populated.
create index if not exists idx_txn_source_filer_trgm
  on public.transactions using gin (filer gin_trgm_ops);
create index if not exists idx_txn_source_payee_trgm
  on public.transactions using gin (contributor_payee gin_trgm_ops);
