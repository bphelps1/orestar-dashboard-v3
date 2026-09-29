-- Search both recorded and standardized names. Existing callers and return
-- columns stay compatible. Apply 030 first so both OR branches are indexed.
-- Resolve a selected donor's saved merge group once, before scanning rows.
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
  v_donor_ids text[];
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

  v_donor_ids := case when coalesce(p_donor_id, '') = '' then array[]::text[]
    else public.donor_group_ids(p_donor_id) end;
  -- A selected entity ignores the payee text and keeps its donor-ID index path.
  if coalesce(p_filer, '') <> '' or (coalesce(p_payee, '') <> '' and coalesce(p_donor_id, '') = '') then
    set local enable_indexscan = off;
  end if;

  return query execute format($q$
    select t.tran_date, t.tran_type, t.amount,
           coalesce(nullif(btrim(t.filer_canonical), ''), nullif(btrim(t.filer), ''),
             'Committee ' || nullif(btrim(t.filer_id), ''), 'Not reported') as filer_canonical,
           coalesce(nullif(btrim(t.contributor_payee_canonical), ''),
             nullif(btrim(t.contributor_payee), ''), 'Not reported') as contributor_payee_canonical, t.book_type, t.city, t.state,
           t.employer, t.occupation, t.purpose, t.tran_id
    from transactions t
    where ($1 = '' or t.filer_canonical ilike '%%' || $1 || '%%'
                       or t.filer ilike '%%' || $1 || '%%')
      -- A resolved donor wins over the free-text box: donor_id is an indexed
      -- equality that covers every name variant.
      and (cardinality($3) > 0 or $2 = '' or t.contributor_payee_canonical ilike '%%' || $2 || '%%'
                                              or t.contributor_payee ilike '%%' || $2 || '%%')
      and (cardinality($3) = 0 or t.donor_id = any($3))
      and ($4  = '' or t.tran_type = $4)
      and ($5  = '' or t.book_type ilike '%%' || $5 || '%%')
      and ($6 is null or t.tran_date >= $6)
      and ($7 is null or t.tran_date <= $7)
      and ($8 is null or t.amount >= $8)
      and ($9 is null or t.amount <= $9)
    order by %I %s nulls last, t.tran_id desc
    limit $10 offset $11
  $q$, v_sort, v_dir)
  using coalesce(p_filer, ''), coalesce(p_payee, ''), v_donor_ids,
        coalesce(p_tran_type, ''), coalesce(p_book_type, ''),
        p_date_from, p_date_to, p_amt_min, p_amt_max,
        greatest(1, least(p_limit, 1000)), greatest(0, p_offset);
end;
$$;

grant execute on function search_transactions(
  text, text, text, text, text, date, date, numeric, numeric, text, boolean, int, int
) to anon, authenticated;

notify pgrst, 'reload schema';
