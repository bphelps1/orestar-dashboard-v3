-- Build merge membership once for the entire donor batch, not once per donor.
create or replace function public.recommendation_first_gifts(
 p_donor_ids text[],p_filer_ids text[],p_through date
) returns table(donor_id text,filer_id text,first_date date,amount numeric)
language sql stable security invoker set search_path=public as $$
 with identity_map as materialized (select * from donor_identity_map),
 requested as materialized (
   select distinct coalesce(m.canonical_id,r.id) as id
   from unnest(p_donor_ids) r(id) left join identity_map m on m.donor_id=r.id
 ), source_ids as materialized (
   select id from requested
   union select m.donor_id from identity_map m join requested r on r.id=m.canonical_id
 ), gifts as (
  select coalesce(m.canonical_id,t.donor_id) as donor_id,t.filer_id,t.tran_date,t.amount,t.tran_id
  from transactions t left join identity_map m on m.donor_id=t.donor_id
  where t.donor_id in (select id from source_ids) and t.filer_id=any(p_filer_ids)
   and t.tran_date<=p_through and t.amount>0 and t.tran_type='C'
   and coalesce(t.sub_type,'') not in ('In-Kind Contribution',
    'In-Kind/Forgiven Account Payable','In-Kind/Forgiven Personal Expenditures')
 ) select distinct on (g.donor_id,g.filer_id) g.donor_id,g.filer_id,g.tran_date,g.amount
 from gifts g order by g.donor_id,g.filer_id,g.tran_date,g.tran_id;
$$;
grant execute on function public.recommendation_first_gifts(text[],text[],date) to anon,authenticated,service_role;
notify pgrst,'reload schema';
