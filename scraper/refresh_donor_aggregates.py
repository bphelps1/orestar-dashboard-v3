"""
refresh_donor_aggregates.py — rebuild normalized donor tables from Postgres.

Global and per-committee donor tables use donor_contribution_rows, the same
cash-only, normalized grouping as the exact-date donor_leaderboard RPC. Ranking
happens after grouping so aliases below the old cutoff can still enter the
leaderboard together. Unresolved contributions are retained, and the pooled
miscellaneous cash category is combined across donor IDs.

Existing contributor-type lists are re-keyed and re-summed; their state/month
classification remains owned by process.py. All cache writes commit together.
Requires migration 014_donor_leaderboard.sql.

Usage:  python scraper/refresh_donor_aggregates.py
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s

TOP_N = 1000

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def build_top_donors(cur) -> dict:
    """{all_time: [{name,total,donor_id}], by_year: {year: [...]}} by entity."""
    log.info("Rebuilding top_donors with normalized donor identities…")
    cur.execute("""
        with totals as (
          select donor_key, min(donor_id) as donor_id, min(name) as name,
                 extract(year from tran_date)::int as yr,
                 grouping(extract(year from tran_date)::int) as all_years,
                 round(sum(amount), 2) as total
          from donor_contribution_rows
          group by grouping sets ((donor_key),
            (donor_key, extract(year from tran_date)::int))
        ), ranked as (
          select *, row_number() over (partition by all_years, yr
            order by total desc nulls last, donor_key) as rn from totals
        )
        select all_years, yr, donor_id, name, total, donor_key
        from ranked where rn <= %s and (all_years = 1 or yr is not null)
        order by all_years desc, yr, total desc nulls last, donor_key
    """, (TOP_N,))
    all_time = []
    by_year: dict[str, list] = defaultdict(list)
    for all_years, yr, did, name, total, key in cur.fetchall():
        row = {"name": name, "total": float(total or 0),
               "donor_id": did, "donor_key": key}
        if all_years:
            all_time.append(row)
        else:
            by_year[str(yr)].append(row)

    log.info("  %d entities all-time, %d years", len(all_time), len(by_year))
    return {"all_time": all_time, "by_year": dict(by_year)}


def canonical_to_entity(cur) -> dict:
    """contributor_payee_canonical -> (display_name, donor_id).

    The blobs are keyed by the canonical name, not the raw one, so the mapping
    is taken from `transactions` rather than `donor_aliases`.
    """
    cur.execute("""
        select t.contributor_payee_canonical, t.donor_id, max(d.display_name)
        from transactions t
        join donors d on d.donor_id = t.donor_id
        where coalesce(t.contributor_payee_canonical,'') <> ''
        group by 1, 2
    """)
    out = {}
    for canon, did, disp in cur.fetchall():
        # A canonical name maps to one entity in practice; if it somehow spans
        # two, keep the first — the merge still collapses the common case.
        out.setdefault(canon, (disp, did))
    return out


def remap_donor_list(rows: list, mapping: dict) -> list:
    """Merge a [{name,total}] list onto entities, re-sum, re-sort."""
    from donor_labels import normalize_donor_label, donor_label_key
    merged: dict[str, dict] = {}
    for r in rows or []:
        name = r.get("name", "")
        disp, did = mapping.get(name, (name, None))
        disp = normalize_donor_label(disp)
        label_key = donor_label_key(disp)
        if label_key == "miscellaneous cash contributions $100 and under":
            did = None
        key = did or label_key
        cur = merged.setdefault(key, {"name": disp, "total": 0.0, "donor_id": did})
        cur["total"] = round(cur["total"] + (r.get("total") or 0), 2)
    return sorted(merged.values(), key=lambda x: -x["total"])


def remap_by_contributor_type(blob: dict, mapping: dict) -> dict:
    """Re-key every nested top_donors list onto entities, preserving structure."""
    def fix_types(type_rows):
        for tr in type_rows or []:
            if "top_donors" in tr:
                keep = len(tr["top_donors"])
                tr["top_donors"] = remap_donor_list(tr["top_donors"], mapping)[:keep]
        return type_rows

    out = dict(blob)
    if "all_time" in out:
        out["all_time"] = fix_types(out["all_time"])
    for section in ("by_year", "by_month"):
        if isinstance(out.get(section), dict):
            out[section] = {k: fix_types(v) for k, v in out[section].items()}
    return out


def _upsert(conn, cur, key: str, data) -> None:
    """Write through the connection we already hold.

    supabase_sync.upsert_dashboard_cache opens its own connection, which the
    pooler may have dropped while the long aggregation queries were running.
    """
    cur.execute(
        "insert into dashboard_cache (key, data, updated_at) "
        "values (%s, %s::jsonb, now()) "
        "on conflict (key) do update set data = excluded.data, updated_at = now()",
        (key, json.dumps(data, default=str)),
    )
    log.info("  wrote dashboard_cache['%s']", key)


def rebuild_filer_donors(cur) -> int:
    """Rebuild each committee's donor tables without touching its account data.

    Map every stored filer ID to its profile, including profiles with several
    IDs. Group before ranking; never try to repair already-truncated lists.
    """
    log.info("Rebuilding per-committee donor tables…")
    cur.execute("""
      with scope as materialized (
        select distinct fd.slug, ids.filer_id
        from filer_detail fd
        cross join lateral jsonb_array_elements_text(
          case when jsonb_typeof(fd.detail->'filer_ids') = 'array'
                     and jsonb_array_length(fd.detail->'filer_ids') > 0
               then fd.detail->'filer_ids' else jsonb_build_array(fd.filer_id) end
        ) ids(filer_id)
      ), totals as (
        select s.slug, r.donor_key, min(r.donor_id) as donor_id, min(r.name) as name,
               extract(year from r.tran_date)::int as yr,
               grouping(extract(year from r.tran_date)::int) as all_years,
               round(sum(r.amount), 2) as total
        from donor_contribution_rows r join scope s on s.filer_id = r.filer_id
        group by grouping sets ((s.slug, r.donor_key),
          (s.slug, r.donor_key, extract(year from r.tran_date)::int))
      ), ranked as (
        select *, row_number() over (partition by slug, all_years, yr
          order by total desc nulls last, donor_key) as rn from totals
      ), lists as (
        select slug, all_years, yr,
               jsonb_agg(jsonb_build_object('name', name, 'donor_id', donor_id,
                 'donor_key', donor_key, 'total', coalesce(total, 0))
                 order by total desc nulls last, donor_key) as donors
        from ranked where rn <= 1000 and (all_years = 1 or yr is not null)
        group by slug, all_years, yr
      ), profiles as (
        select slug,
          (jsonb_agg(donors) filter (where all_years = 1))->0 as all_time,
          jsonb_object_agg(yr::text, donors) filter (where all_years = 0) as by_year
        from lists group by slug
      )
      update filer_detail fd set detail = jsonb_set(jsonb_set(fd.detail,
        '{top_donors}', coalesce(p.all_time, '[]'::jsonb)),
        '{top_donors_by_year}', coalesce(p.by_year, '{}'::jsonb))
      from (select f.slug, p.all_time, p.by_year
            from filer_detail f left join profiles p using (slug)) p
      where fd.slug = p.slug
    """)
    log.info("  rebuilt %d committee donor tables", cur.rowcount)
    return cur.rowcount


def stage_donor_rows(cur) -> None:
    """Read and normalize the source once for a consistent, bounded rebuild."""
    log.info("Staging normalized donor contributions…")
    cur.execute("""create temporary table donor_rebuild_rows on commit drop as
                   select * from donor_contribution_rows""")
    log.info("  staged %d donor/date/committee groups", cur.rowcount)
    cur.execute("analyze pg_temp.donor_rebuild_rows")
    # Builders use this session's snapshot; public queries still use the live
    # view. No permanent tables, permissions, or transaction rows change here.
    cur.execute("""create or replace temporary view donor_contribution_rows as
                   select * from pg_temp.donor_rebuild_rows""")


def main() -> int:
    conn = s._connect()
    cur = conn.cursor()
    cur.execute("set statement_timeout = '5min'")

    cur.execute("select count(*) from donors")
    if not cur.fetchone()[0]:
        log.error("donors table is empty — run resolve_donors.py first")
        return 1

    stage_donor_rows(cur)
    top = build_top_donors(cur)
    _upsert(conn, cur, "top_donors", top)
    rebuild_filer_donors(cur)

    log.info("Re-keying by_contributor_type onto entities…")
    mapping = canonical_to_entity(cur)
    log.info("  %d canonical names -> %d entities",
             len(mapping), len({v[1] for v in mapping.values()}))
    cur.execute("select data from dashboard_cache where key='by_contributor_type'")
    row = cur.fetchone()
    if row:
        _upsert(conn, cur, "by_contributor_type",
                remap_by_contributor_type(row[0], mapping))
    else:
        log.warning("by_contributor_type not in dashboard_cache — skipped")

    conn.commit()
    conn.close()
    log.info("Donor aggregates refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
