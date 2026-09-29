-- db_admin.py builds this index concurrently, without blocking imports.
-- Serves the Explore sub-type filter under its default newest-first order.
-- Without it a bare sub-type filter is a 12 s parallel sequential scan, and
-- the anon role times out at 10 s.
create index if not exists idx_txn_sub_type_date
  on public.transactions (sub_type, tran_date desc nulls last, tran_id desc);
