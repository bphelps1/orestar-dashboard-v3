-- Use db_admin.py apply for an online concurrent build on production.
-- This covers merge-save affected-filer collection without reading wide rows.
create index if not exists idx_txn_donor_filer on public.transactions (donor_id, filer_id);
