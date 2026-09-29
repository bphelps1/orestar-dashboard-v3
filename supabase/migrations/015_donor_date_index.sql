-- The public donor query has a bounded request timeout. A date/type bitmap
-- scan otherwise reads hundreds of MB of wide transaction rows for one cycle.
-- Keep the cash-only date scan and its aggregation fields in a covering index.
-- For an initial install on a busy database, build this index CONCURRENTLY
-- outside a transaction. Subsequent migration runs are idempotent.
create index if not exists idx_txn_cash_donor_dates
  on public.transactions (tran_date, filer_id)
  include (donor_id, contributor_payee_canonical, contributor_payee, amount)
  where tran_type = 'C' and coalesce(sub_type, '') not in (
    'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
    'In-Kind/Forgiven Personal Expenditures'
  );
