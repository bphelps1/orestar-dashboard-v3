-- ============================================================================
-- donor_profile(): one-call profile aggregates for the /donors page.
--
-- Returns jsonb {by_year, top_recipients} computed server-side over the
-- donor_id index — a large PAC can have 20k+ transactions, far too many to
-- aggregate in the browser. Read-only, safe for anon (RLS-readable tables).
-- ============================================================================

create or replace function donor_profile(p_donor_id text)
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'by_year', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'year', y, 'given', coalesce(g, 0), 'received', coalesce(r, 0)
             ) order by y), '[]'::jsonb)
      from (
        select extract(year from tran_date)::int as y,
               round(sum(amount) filter (where tran_type = 'C')::numeric, 2) as g,
               round(sum(amount) filter (where tran_type = 'E')::numeric, 2) as r
        from transactions
        where donor_id = p_donor_id and tran_date is not null
        group by 1
      ) t
    ),
    'top_recipients', (
      select coalesce(jsonb_agg(rec order by (rec->>'total')::numeric desc), '[]'::jsonb)
      from (
        select jsonb_build_object(
                 'filer', t.filer_canonical,
                 'filer_id', t.filer_id,
                 'slug', (select fd.slug from filer_detail fd
                          where fd.filer_id = t.filer_id limit 1),
                 'total', round(sum(t.amount)::numeric, 2),
                 'n', count(*)
               ) as rec
        from transactions t
        where t.donor_id = p_donor_id and t.tran_type = 'C'
        group by t.filer_canonical, t.filer_id
        order by sum(t.amount) desc
        limit 15
      ) x
    )
  )
$$;

grant execute on function donor_profile(text) to anon, authenticated;
