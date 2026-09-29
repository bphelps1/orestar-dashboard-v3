-- Repair Top Recipients names and aggregate once per recipient committee ID.
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
      -- Group on committee identity before choosing a display label. A blank
      -- canonical name and a populated one must not split the same recipient.
      with named as (
        select nullif(btrim(t.filer_id), '') as filer_id,
               nullif(btrim(t.filer_canonical), '') as canonical_name,
               nullif(btrim(t.filer), '') as raw_name, t.amount
        from transactions t
        where t.donor_id = p_donor_id and t.tran_type = 'C'
      ), recipients as (
        select min(filer_id) as filer_id,
               coalesce(min(canonical_name), min(raw_name)) as name,
               round(sum(amount)::numeric, 2) as total, count(*) as n
        from named
        group by coalesce('id:' || filer_id,
                          'name:' || lower(coalesce(canonical_name, raw_name)),
                          'unknown:')
        order by sum(amount) desc, min(filer_id), min(canonical_name), min(raw_name)
        limit 15
      )
      select coalesce(jsonb_agg(jsonb_build_object(
        'filer', coalesce(nullif(btrim(fd.name), ''), r.name,
                          'Committee ' || r.filer_id, 'Committee name unavailable'),
        'filer_id', r.filer_id, 'slug', fd.slug, 'total', r.total, 'n', r.n
      ) order by r.total desc, r.filer_id, r.name), '[]'::jsonb)
      from recipients r
      left join lateral (
        select f.slug, f.name from filer_detail f
        where r.filer_id is not null and (f.filer_id = r.filer_id
          or (f.detail->'filer_ids') ? r.filer_id)
        order by (f.filer_id = r.filer_id) desc nulls last, f.slug
        limit 1
      ) fd on true
    )
  )
$$;

grant execute on function donor_profile(text) to anon, authenticated;
