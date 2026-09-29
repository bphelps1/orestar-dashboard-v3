-- ============================================================================
-- search_transactions() — the Explore page's filtered browse.
--
-- Why an RPC instead of PostgREST filters:
--
-- Postgres cannot estimate `ilike '%substring%'` selectivity. For "nike" it
-- predicted 24,008 matching rows when there are 1,808, so it chose to walk
-- idx_txn_tran_date backwards and filter row by row — discarding 276,000 rows
-- to find 100. That ran ~2s (over 3s under load) and returned HTTP 500
-- "canceling statement due to statement timeout". Raising the statistics
-- target to 1000 helped but did not fix it: substring selectivity simply isn't
-- captured by column statistics.
--
-- Denying the plain index scan makes the planner use the trigram bitmap index
-- and a top-N sort instead — measured 0.16s for the same queries. Bitmap index
-- scans are governed by enable_bitmapscan, so `enable_indexscan = off` leaves
-- the fast path available while removing the trap. It is applied with SET LOCAL
-- and ONLY when a text filter is present, so date/amount-only queries keep
-- their normal index scans.
--
-- SECURITY DEFINER so the planner isn't additionally constrained by RLS on a
-- non-leakproof operator. The function is read-only, parameterised, and its
-- sort key is whitelisted, so it exposes nothing the table's public read
-- policy doesn't already allow.
-- ============================================================================

create or replace function search_transactions(
  p_filer      text    default '',
  p_payee      text    default '',
  p_donor_id   text    default '',
  p_tran_type  text    default '',
  p_book_type  text    default '',
  p_date_from  date    default null,
  p_date_to    date    default null,
  p_amt_min    numeric default null,
  p_amt_max    numeric default null,
  p_sort       text    default 'tran_date',
  p_asc        boolean default false,
  p_limit      int     default 100,
  p_offset     int     default 0
)
returns table (
  tran_date                   date,
  tran_type                   text,
  amount                      numeric,
  filer_canonical             text,
  contributor_payee_canonical text,
  book_type                   text,
  city                        text,
  state                       text,
  employer                    text,
  occupation                  text,
  purpose                     text,
  tran_id                     bigint
)
language plpgsql
-- VOLATILE, not STABLE: `SET LOCAL` is rejected in a non-volatile function.
-- The body only reads, and PostgREST already calls this over POST.
volatile
security definer
set search_path = public
as $$
declare
  v_sort text;
  v_dir  text := case when p_asc then 'asc' else 'desc' end;
begin
  -- Whitelist the sort column: it is interpolated into the statement.
  v_sort := case p_sort
    when 'tran_date'                   then 'tran_date'
    when 'amount'                      then 'amount'
    when 'filer_canonical'             then 'filer_canonical'
    when 'contributor_payee_canonical' then 'contributor_payee_canonical'
    when 'book_type'                   then 'book_type'
    when 'city'                        then 'city'
    when 'state'                       then 'state'
    when 'employer'                    then 'employer'
    when 'occupation'                  then 'occupation'
    when 'purpose'                     then 'purpose'
    when 'tran_id'                     then 'tran_id'
    else 'tran_date'
  end;

  -- Only for substring search — see header. Date/amount filters still want
  -- their ordinary index scans.
  if coalesce(p_filer, '') <> '' or coalesce(p_payee, '') <> '' then
    set local enable_indexscan = off;
  end if;

  return query execute format($q$
    select t.tran_date, t.tran_type, t.amount, t.filer_canonical,
           t.contributor_payee_canonical, t.book_type, t.city, t.state,
           t.employer, t.occupation, t.purpose, t.tran_id
    from transactions t
    where ($1  = '' or t.filer_canonical ilike '%%' || $1 || '%%')
      -- A resolved donor wins over the free-text box: donor_id is an indexed
      -- equality that covers every name variant.
      and ($3 <> '' or $2 = '' or t.contributor_payee_canonical ilike '%%' || $2 || '%%')
      and ($3  = '' or t.donor_id = $3)
      and ($4  = '' or t.tran_type = $4)
      and ($5  = '' or t.book_type ilike '%%' || $5 || '%%')
      and ($6 is null or t.tran_date >= $6)
      and ($7 is null or t.tran_date <= $7)
      and ($8 is null or t.amount >= $8)
      and ($9 is null or t.amount <= $9)
    order by %I %s nulls last, t.tran_id desc
    limit $10 offset $11
  $q$, v_sort, v_dir)
  using coalesce(p_filer, ''), coalesce(p_payee, ''), coalesce(p_donor_id, ''),
        coalesce(p_tran_type, ''), coalesce(p_book_type, ''),
        p_date_from, p_date_to, p_amt_min, p_amt_max,
        greatest(1, least(p_limit, 1000)), greatest(0, p_offset);
end;
$$;

grant execute on function search_transactions(
  text, text, text, text, text, date, date, numeric, numeric, text, boolean, int, int
) to anon, authenticated;

-- Give the public API headroom: the fast plan is ~0.2s, but a deliberately
-- broad term ("oregon" matches 123,937 rows) is inherently expensive to sort
-- however it is planned. 3s left no margin under load.
alter role anon set statement_timeout = '10s';
