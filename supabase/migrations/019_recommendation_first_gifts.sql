-- The earliest positive cash contribution to each candidate, within the
-- selected planning horizon. This is observed history, not a claim that the
-- dataset contains every contribution ever made.
create or replace function public.recommendation_first_gifts(
  p_donor_ids text[], p_filer_ids text[], p_through date
) returns table(donor_id text, filer_id text, first_date date, amount numeric)
language sql stable security invoker set search_path = public as $$
  select distinct on (t.donor_id, t.filer_id)
    t.donor_id, t.filer_id, t.tran_date, t.amount
  from transactions t
  where t.donor_id = any(p_donor_ids) and t.filer_id = any(p_filer_ids)
    and t.tran_date <= p_through and t.amount > 0 and t.tran_type = 'C'
    and coalesce(t.sub_type, '') not in (
      'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
      'In-Kind/Forgiven Personal Expenditures')
  order by t.donor_id, t.filer_id, t.tran_date, t.tran_id;
$$;
grant execute on function public.recommendation_first_gifts(text[], text[], date)
  to anon, authenticated, service_role;
