-- db_admin.py builds this index concurrently, without blocking imports.
-- Explore opens newest first, ordered by tran_date desc nulls last, tran_id desc.
-- idx_txn_tran_date read backwards gives nulls first, so it cannot serve that
-- order, and the unfiltered first page was a 12.5 s scan and sort of every row
-- (past the anon role's 10 s timeout, so Explore opened on an error).
create index if not exists idx_txn_date_desc
  on public.transactions (tran_date desc nulls last, tran_id desc);
