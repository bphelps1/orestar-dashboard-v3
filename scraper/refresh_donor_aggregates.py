"""
refresh_donor_aggregates.py — rebuild normalized donor tables from Postgres.

Global and per-committee donor tables use the same cash-only, normalized
grouping as the exact-date donor_leaderboard RPC, with refunds of
contributions netted against the donor (migration 041). A donor or donor-year
that nets to zero or less is left out of the lists. Ranking happens after
grouping so aliases below the old cutoff can still enter the leaderboard
together. Unresolved contributions are retained, and the pooled miscellaneous
cash category is combined across donor IDs.

Nothing here reads all 3M transactions or builds a large result in one
statement. Staging the whole donor_contribution_rows view into a 1.8M-row temp
table — one statement, 100 s of index and heap reads, 330 MB of temp files —
is what hung the Micro instance on 2026-09-23, 2026-10-19 and 2026-10-22, and
it hung it twice more that evening when the same statement was fed from a
cache instead of the view, while 27 per-year statements writing the same rows
went through unremarked. So the work is cut by year (migration 042):

  1. donor_contribution_sums holds the view's raw sums per calendar year.
     Every writer of `transactions` records the years it touched in
     donor_contribution_dirty (supabase_sync.mark_donor_sum_years); those
     years are rebuilt from idx_txn_cash_donor_dates, a few seconds each.
  2. donor_year_totals joins each year of the sums to the live donors and
     donor_identity_map tables, so a merge saved at /admin/donors is applied
     on the next run as before. Every year is recomputed (a merge can touch
     any year) and replaced only where it differs; donor_canonical_totals,
     for by_contributor_type, goes with it.
  3. The statewide lists come from those totals one year at a time, the
     committee lists one batch of committees at a time, and only a committee
     whose lists changed is written (process.py's publication keeps the keyed
     lists from the last run, see supabase_sync._DETAIL_SQL).

The Fundraising Pulse's "Biggest Donors" lists in activity_snapshot are
rebuilt from the cached rows over each period's months, so they are netted
and merged like every other donor ranking. Existing contributor-type lists
are re-keyed and re-summed; their state/month classification remains owned
by process.py. The blob writes commit together; the caches commit year by
year, so an interrupted rebuild resumes where it stopped.

Every statement runs under statement_timeout. A server that neither answers
nor cancels within the grace period after that is the dead backend behind the
pooler (see supabase_sync._connect), and the process exits at once instead of
waiting for the workflow step's timeout.

Usage:  python scraper/refresh_donor_aggregates.py [--full] [--dry-run]
  --full     rebuild every year of the sums cache (after a bulk edit of
             transactions; the weekly resolve marks every year itself)
  --dry-run  run everything, then roll back
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s

TOP_N = 1000
# Committees per statement when rebuilding their donor tables: a batch reads
# its own rows through idx_donor_year_totals_filer and rewrites at most this
# many blobs before the next one starts.
FILER_BATCH = 1000

# Per-statement budget, enforced by the server. A statement still unanswered
# STATEMENT_GRACE seconds after it should have been cancelled is not slow: the
# backend is gone, or the instance is wedged, and nothing this process does
# can bring it back.
STATEMENT_TIMEOUT = 240
STATEMENT_GRACE = 60
# work_mem is 3.5 MB on the Micro instance, so the donors hash join spilled
# in every statement. 32 MB (a hash node may use twice that) keeps one year's
# join and the all-time grouping in memory without crowding the 1 GB box.
WORK_MEM = "32MB"

SESSION_SQL = f"""
    set statement_timeout = '{STATEMENT_TIMEOUT}s';
    set lock_timeout = '30s';
    set idle_in_transaction_session_timeout = '600s';
    set work_mem = '{WORK_MEM}';
"""

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


class DatabaseUnresponsive(RuntimeError):
    """A statement outlived its server-side timeout without any answer."""


def run(cur, sql, params=None, *, label="statement",
        budget=STATEMENT_TIMEOUT + STATEMENT_GRACE) -> float:
    """Execute one statement; raise DatabaseUnresponsive if nothing comes back.

    The statement runs on a helper thread so this thread keeps its own clock:
    psycopg2 releases the GIL while it waits on the socket, and a connection
    may be cancel()led from another thread. Returns the seconds it took.
    """
    done = threading.Event()
    outcome: list = []

    def work():
        try:
            cur.execute(sql, params)
        except BaseException as exc:  # handed back to the caller's thread
            outcome.append(exc)
        finally:
            done.set()

    started = time.monotonic()
    threading.Thread(target=work, name=label, daemon=True).start()
    if not done.wait(budget):
        connection = getattr(cur, "connection", None)
        if connection is not None:  # best effort; this can hang too
            threading.Thread(target=connection.cancel, daemon=True).start()
        raise DatabaseUnresponsive(
            f"{label}: no answer after {budget:.0f}s, and the server did not cancel "
            f"it at {STATEMENT_TIMEOUT}s either — treating the database as unresponsive")
    if outcome:
        raise outcome[0]
    return time.monotonic() - started


def _label(yr: int) -> str:
    return str(yr) if yr else "undated"


# Loose index scan: the distinct years of a table through its (yr) index.
DISTINCT_YEARS_SQL = """
    with recursive years as (
      select min(yr) as yr from {table}
      union all
      select (select min(yr) from {table} where yr > y.yr) from years y where y.yr is not null
    )
    select yr from years where yr is not null{extra} order by yr
"""


def distinct_years(cur, table: str, *, dated_only: bool = False) -> list[int]:
    sql = DISTINCT_YEARS_SQL.format(table=table, extra=" and yr > 0" if dated_only else "")
    run(cur, sql, label=f"years of {table}")
    return [row[0] for row in cur.fetchall()]


# ── 1. donor_contribution_sums: raw per-year sums from transactions ─────────

def years_to_rebuild(cur, *, full: bool = False) -> list[int]:
    """Years of donor_contribution_sums to rebuild now, oldest first.

    Marked years come from donor_contribution_dirty. Everything is rebuilt
    when asked (--full), when a writer marked every year (a full reload, the
    weekly resolve's restamp) or when the cache is empty (first run). The
    all-years sentinel is replaced by the explicit list before any year is
    touched, so an interrupted rebuild resumes with the years still marked.
    """
    run(cur, "select yr from donor_contribution_dirty", label="dirty years")
    marked = {row[0] for row in cur.fetchall()}
    run(cur, "select exists (select 1 from donor_contribution_sums)", label="cache check")
    populated = cur.fetchone()[0]
    if not (full or s.DIRTY_ALL in marked or not populated):
        return sorted(marked)
    run(cur, """select extract(year from min(tran_date))::int,
                       extract(year from max(tran_date))::int,
                       (select exists (select 1 from transactions where tran_date is null))
                from transactions""", label="year range")
    first, last, undated = cur.fetchone()
    years = set(range(first, last + 1)) if first is not None else set()
    if undated:
        years.add(0)
    years |= marked - {s.DIRTY_ALL}
    run(cur, "delete from donor_contribution_dirty", label="reset dirty years")
    s.mark_donor_sum_years(cur, years)
    return sorted(years)


# Mirrors raw_totals in donor_contribution_rows (041): keep them identical.
CASH_PREDICATE = """tran_type = 'C' and coalesce(sub_type, '') not in (
        'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
        'In-Kind/Forgiven Personal Expenditures')"""

YEAR_SUMS_SQL = """
    insert into donor_contribution_sums
      (yr, filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee, amount)
    select %(yr)s, filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee,
           sum(amount)
    from transactions
    where {cash} and {dates}
    group by filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee
    union all
    select %(yr)s, filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee,
           -sum(amount)
    from transactions
    where sub_type = 'Return or Refund of Contribution' and {dates}
    group by filer_id, tran_date, donor_id, contributor_payee_canonical, contributor_payee
"""


def rebuild_sums_year(cur, yr: int) -> int:
    """Replace one year of the sums from transactions; returns the rows written.

    Year 0 holds the undated rows. The dirty mark goes first, in the same
    transaction: a writer marking this year meanwhile blocks on the row until
    the commit and then re-marks it, and a failure puts the mark back.
    """
    dates = ("tran_date is null" if yr == 0
             else "tran_date >= %(start)s and tran_date < %(end)s")
    params = {"yr": yr, "start": f"{yr:04d}-01-01", "end": f"{yr + 1:04d}-01-01"}
    label = f"sums {_label(yr)}"
    run(cur, "delete from donor_contribution_dirty where yr = %(yr)s", params, label=label)
    run(cur, "delete from donor_contribution_sums where yr = %(yr)s", params, label=label)
    run(cur, YEAR_SUMS_SQL.format(cash=CASH_PREDICATE, dates=dates), params, label=label)
    return cur.rowcount


def refresh_contribution_sums(conn, cur, *, full: bool = False, commit: bool = True) -> list[int]:
    """Bring donor_contribution_sums up to date, one year per transaction."""
    s.ensure_donor_contribution_cache(cur)
    years = years_to_rebuild(cur, full=full)
    if commit:
        conn.commit()  # the explicit year list outlives an interruption
    if not years:
        log.info("Contribution sums are current: no years marked")
        return []
    log.info("Rebuilding %d year(s) of contribution sums: %s", len(years),
             ", ".join(_label(y) for y in years))
    started = time.monotonic()
    total = 0
    for yr in years:
        t0 = time.monotonic()
        rows = rebuild_sums_year(cur, yr)
        if commit:
            conn.commit()
        total += rows
        log.info("  %s: %s rows in %.1fs", _label(yr), f"{rows:,}", time.monotonic() - t0)
    run(cur, "analyze donor_contribution_sums", label="analyze sums")
    if commit:
        conn.commit()
    log.info("  sums: %s rows for %d year(s) rebuilt in %.1fs", f"{total:,}", len(years),
             time.monotonic() - started)
    return years


# ── 2. donor_year_totals / donor_canonical_totals: identities applied ───────

YEAR_ROWS_SQL = """
    create temporary table y_rows on commit drop as
    select filer_id, donor_key, donor_id, name, contributor_payee_canonical, amount
    from donor_contribution_rows_cached where yr = %(yr)s
"""
YEAR_TOTALS_SQL = """
    select %(yr)s as yr, filer_id, donor_key, min(donor_id) as donor_id, min(name) as name,
           sum(amount) as total
    from y_rows group by filer_id, donor_key
"""
TOTALS_CHANGED_SQL = f"""
    with new_rows as ({YEAR_TOTALS_SQL}),
         old_rows as (select yr, filer_id, donor_key, donor_id, name, total
                      from donor_year_totals where yr = %(yr)s)
    select exists (select 1 from (table new_rows except table old_rows) d)
        or exists (select 1 from (table old_rows except table new_rows) d)
"""
REPLACE_TOTALS_SQL = (
    "delete from donor_year_totals where yr = %(yr)s",
    "insert into donor_year_totals (yr, filer_id, donor_key, donor_id, name, total)"
    + YEAR_TOTALS_SQL,
    "delete from donor_canonical_totals where yr = %(yr)s",
    """insert into donor_canonical_totals (yr, contributor_payee_canonical, donor_id, name, total)
       select %(yr)s, contributor_payee_canonical, donor_id, min(name), sum(amount)
       from y_rows
       where donor_id is not null and coalesce(contributor_payee_canonical, '') <> ''
       group by contributor_payee_canonical, donor_id""",
)


def rebuild_totals_year(cur, yr: int, *, force: bool = False):
    """Recompute one year's identity-resolved totals from the cached view and
    replace the stored year when it differs (or when its sums were rebuilt).
    Returns the rows written, or None when the year was left alone."""
    params = {"yr": yr}
    label = f"totals {_label(yr)}"
    run(cur, "drop table if exists pg_temp.y_rows", label=label)
    run(cur, YEAR_ROWS_SQL, params, label=label)
    run(cur, "analyze y_rows", label=label)
    if not force:
        run(cur, TOTALS_CHANGED_SQL, params, label=label)
        if not cur.fetchone()[0]:
            return None
    written = 0
    for sql in REPLACE_TOTALS_SQL:
        run(cur, sql, params, label=label)
        if sql.startswith("insert into donor_year_totals"):
            written = cur.rowcount
    return written


def rebuild_year_totals(conn, cur, *, dirty=(), commit: bool = True) -> list[int]:
    """Every year's identity-resolved totals, replaced only where they changed."""
    years = distinct_years(cur, "donor_contribution_sums")
    log.info("Applying donor identities to %d year(s) of sums…", len(years))
    started = time.monotonic()
    replaced = []
    for yr in years:
        t0 = time.monotonic()
        written = rebuild_totals_year(cur, yr, force=yr in set(dirty))
        if commit:
            conn.commit()
        if written is None:
            log.info("  %s: unchanged (%.1fs)", _label(yr), time.monotonic() - t0)
        else:
            replaced.append(yr)
            log.info("  %s: %s rows replaced in %.1fs", _label(yr), f"{written:,}",
                     time.monotonic() - t0)
    if replaced:
        run(cur, "analyze donor_year_totals", label="analyze totals")
        run(cur, "analyze donor_canonical_totals", label="analyze canonical totals")
        if commit:
            conn.commit()
    log.info("  totals: %d of %d year(s) replaced in %.1fs", len(replaced), len(years),
             time.monotonic() - started)
    return replaced


def refresh_caches(conn, cur, *, full: bool = False, commit: bool = True):
    """Bring both caches up to date; returns (sums years rebuilt, totals years replaced)."""
    dirty = refresh_contribution_sums(conn, cur, full=full, commit=commit)
    replaced = rebuild_year_totals(conn, cur, dirty=dirty, commit=commit)
    return dirty, replaced


# ── 3. The dashboard blobs ──────────────────────────────────────────────────

RANKED_YEAR_SQL = """
    select donor_key, min(donor_id), min(name), round(sum(total), 2) as total
    from donor_year_totals{scope}
    group by donor_key
    -- Refunds are negative rows: a donor refunded in full kept nothing.
    having sum(total) > 0
    order by round(sum(total), 2) desc nulls last, donor_key
    limit %(top)s
"""


def _ranked(cur, *, yr=None) -> list[dict]:
    scope = "" if yr is None else " where yr = %(yr)s"
    run(cur, RANKED_YEAR_SQL.format(scope=scope), {"top": TOP_N, "yr": yr},
        label=f"top_donors {_label(yr) if yr else 'all-time'}")
    return [{"name": name, "total": float(total or 0), "donor_id": did, "donor_key": key}
            for key, did, name, total in cur.fetchall()]


def build_top_donors(cur) -> dict:
    """{all_time: [{name,total,donor_id}], by_year: {year: [...]}} by entity.

    Each year is ranked on its own, so a donor below the all-time cutoff can
    still lead a year; undated gifts count all-time only.
    """
    log.info("Rebuilding top_donors with normalized donor identities…")
    started = time.monotonic()
    all_time = _ranked(cur)
    by_year = {str(yr): _ranked(cur, yr=yr)
               for yr in distinct_years(cur, "donor_year_totals", dated_only=True)}
    log.info("  %d entities all-time, %d years in %.1fs", len(all_time), len(by_year),
             time.monotonic() - started)
    return {"all_time": all_time, "by_year": by_year}


def blob_donor_names(blob: dict) -> set[str]:
    """Every donor name in a by_contributor_type blob, across its sections."""
    names: set[str] = set()

    def take(type_rows):
        for tr in type_rows or []:
            for d in tr.get("top_donors") or []:
                names.add(d.get("name") or "")

    take(blob.get("all_time"))
    for section in ("by_year", "by_month"):
        if isinstance(blob.get(section), dict):
            for rows in blob[section].values():
                take(rows)
    names.discard("")
    return names


def canonical_to_entity(cur, names) -> dict:
    """contributor_payee_canonical -> (display_name, donor_id) for these names.

    The blobs are keyed by the canonical name, not the raw one, so the mapping
    comes from the transactions themselves rather than donor_aliases: first
    donor_canonical_totals (the entity that gave the most under a name), then,
    for a name with no cash gift there (an in-kind only donor), the
    transactions it appears in, by idx_txn_payee_canon.
    """
    out: dict = {}
    wanted = sorted(n for n in names if n)
    if not wanted:
        return out
    run(cur, """
        select contributor_payee_canonical, donor_id, max(name)
        from donor_canonical_totals
        where contributor_payee_canonical = any(%s)
        group by 1, 2 order by 1, sum(total) desc
    """, (wanted,), label="canonical names (cache)")
    for canon, did, disp in cur.fetchall():
        out.setdefault(canon, (disp, did))
    missing = [n for n in wanted if n not in out]
    for start in range(0, len(missing), 1000):
        chunk = missing[start:start + 1000]
        run(cur, """
            select t.contributor_payee_canonical, t.donor_id, max(d.display_name)
            from transactions t join donors d on d.donor_id = t.donor_id
            where t.contributor_payee_canonical = any(%s)
            group by 1, 2 order by 1, count(*) desc
        """, (chunk,), label="canonical names (transactions)")
        for canon, did, disp in cur.fetchall():
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
    run(cur,
        "insert into dashboard_cache (key, data, updated_at) "
        "values (%s, %s::jsonb, now()) "
        "on conflict (key) do update set data = excluded.data, updated_at = now()",
        (key, json.dumps(data, default=str)), label=f"dashboard_cache[{key}]")
    log.info("  wrote dashboard_cache['%s']", key)


FILER_SQL = """
  with scope as materialized (
    select distinct fd.slug, ids.filer_id
    from filer_detail fd
    cross join lateral jsonb_array_elements_text(
      case when jsonb_typeof(fd.detail->'filer_ids') = 'array'
                 and jsonb_array_length(fd.detail->'filer_ids') > 0
           then fd.detail->'filer_ids' else jsonb_build_array(fd.filer_id) end
    ) ids(filer_id)
    where fd.slug = any(%(slugs)s)
  ), totals as (
    select s.slug, t.donor_key, min(t.donor_id) as donor_id, min(t.name) as name,
           t.yr, grouping(t.yr) as all_years, round(sum(t.total), 2) as total
    from (select filer_id, donor_key, donor_id, name, nullif(yr, 0) as yr, total
          from donor_year_totals) t
    join scope s on s.filer_id = t.filer_id
    group by grouping sets ((s.slug, t.donor_key), (s.slug, t.donor_key, t.yr))
    having sum(t.total) > 0
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
    '{top_donors}', p.all_time), '{top_donors_by_year}', p.by_year)
  from (select f.slug, coalesce(p.all_time, '[]'::jsonb) as all_time,
               coalesce(p.by_year, '{}'::jsonb) as by_year
        from filer_detail f left join profiles p using (slug)
        where f.slug = any(%(slugs)s)) p
  where fd.slug = p.slug
    and (fd.detail->'top_donors' is distinct from p.all_time
         or fd.detail->'top_donors_by_year' is distinct from p.by_year)
"""


def rebuild_filer_donors(cur, batch_size: int = FILER_BATCH) -> int:
    """Rebuild each committee's donor tables without touching its account data.

    Map every stored filer ID to its profile, including profiles with several
    IDs. Group before ranking; never try to repair already-truncated lists.
    One batch of committees per statement; only a blob whose lists differ
    from the new ones is written. Returns the number written.
    """
    log.info("Rebuilding per-committee donor tables…")
    run(cur, "select slug from filer_detail order by slug", label="committee slugs")
    slugs = [row[0] for row in cur.fetchall()]
    started = time.monotonic()
    changed = 0
    for start in range(0, len(slugs), batch_size):
        batch = slugs[start:start + batch_size]
        label = f"committees {start + 1}-{start + len(batch)}"
        took = run(cur, FILER_SQL, {"slugs": batch}, label=label)
        changed += cur.rowcount
        log.info("  %s of %d: %d rewritten (%.1fs)", label, len(slugs), cur.rowcount, took)
    log.info("  rebuilt %d committee donor tables (%d unchanged) in %.1fs",
             changed, len(slugs) - changed, time.monotonic() - started)
    return changed


PULSE_TOP = 10
PULSE_DETAILS = 10


def _month_bounds(months: list[str]) -> tuple[str, str]:
    """First and last day covered by a sorted list of "YYYY-MM" months."""
    y, m = map(int, months[-1].split("-"))
    after = f"{y + (m == 12):04d}-{m % 12 + 1:02d}-01"
    return f"{months[0]}-01", after


def pulse_donors(cur, start: str, before: str) -> list[dict]:
    """Biggest donors with gifts dated in [start, before), across committees.

    Each committee's figure is what it kept from the donor in the window: its
    gifts less its refunds to that donor there. A refund issued now for a gift
    made before the window has nothing to cancel inside it, so that committee
    simply drops out instead of pulling the donor's other gifts down. ORESTAR's
    pooled "Miscellaneous … $100 and under" lines are not donors and are left
    out, as before; they come under more than one wording. The window's rows
    come through idx_donor_sums_date.
    """
    run(cur, """
      with scope as materialized (
        -- One profile per filer ID, so no gift is counted twice.
        select distinct on (ids.filer_id) ids.filer_id, fd.slug, fd.name
        from filer_detail fd
        cross join lateral jsonb_array_elements_text(
          case when jsonb_typeof(fd.detail->'filer_ids') = 'array'
                     and jsonb_array_length(fd.detail->'filer_ids') > 0
               then fd.detail->'filer_ids' else jsonb_build_array(fd.filer_id) end
        ) ids(filer_id)
        order by ids.filer_id, (fd.filer_id = ids.filer_id) desc, fd.slug
      )
      select r.donor_key, min(r.donor_id), min(r.name), s.slug, min(s.name),
             round(sum(r.amount), 2)
      from donor_contribution_rows_cached r join scope s on s.filer_id = r.filer_id
      where r.tran_date >= %s and r.tran_date < %s and lower(r.name) not like 'miscellaneous %%'
      group by r.donor_key, s.slug
      having sum(r.amount) > 0
    """, (start, before), label=f"pulse {start}")
    donors: dict[str, dict] = {}
    for key, did, name, slug, filer, amount in cur.fetchall():
        d = donors.setdefault(key, {"name": name, "donor_id": did, "donor_key": key,
                                    "total": 0.0, "details": []})
        d["total"] += float(amount)
        d["details"].append({"filer": filer, "slug": slug, "amount": float(amount)})
    ranked = sorted(donors.values(), key=lambda d: (-d["total"], d["donor_key"]))[:PULSE_TOP]
    for d in ranked:
        d["total"] = round(d["total"], 2)
        d["committees"] = len(d["details"])
        d["details"] = sorted(d["details"], key=lambda x: -x["amount"])[:PULSE_DETAILS]
    return ranked


def rebuild_pulse_donors(cur, snapshot: dict) -> dict:
    """Replace each Fundraising Pulse period's top_donors, over its own months."""
    for key, period in (snapshot.get("periods") or {}).items():
        months = sorted(period.get("months") or [])
        if not months:
            continue
        start, before = _month_bounds(months)
        period["top_donors"] = pulse_donors(cur, start, before)
        log.info("  pulse %s (%s to %s): %d donors", key, start, before,
                 len(period["top_donors"]))
    return snapshot


# ── Entry point ──────────────────────────────────────────────────────────────

def refresh(conn, cur, *, full: bool = False, dry_run: bool = False) -> int:
    """Everything after the connection is open; returns the exit status."""
    started = time.monotonic()
    cur.execute(SESSION_SQL)
    run(cur, "select count(*) from donors", label="donors check")
    if not cur.fetchone()[0]:
        log.error("donors table is empty — run resolve_donors.py first")
        return 1

    dirty, replaced = refresh_caches(conn, cur, full=full, commit=not dry_run)
    top = build_top_donors(cur)
    _upsert(conn, cur, "top_donors", top)
    changed = rebuild_filer_donors(cur)

    log.info("Rebuilding the Fundraising Pulse's biggest donors…")
    run(cur, "select data from dashboard_cache where key='activity_snapshot'",
        label="activity_snapshot")
    row = cur.fetchone()
    if row:
        _upsert(conn, cur, "activity_snapshot", rebuild_pulse_donors(cur, row[0]))
    else:
        log.warning("activity_snapshot not in dashboard_cache — skipped")

    log.info("Re-keying by_contributor_type onto entities…")
    run(cur, "select data from dashboard_cache where key='by_contributor_type'",
        label="by_contributor_type")
    row = cur.fetchone()
    if row:
        mapping = canonical_to_entity(cur, blob_donor_names(row[0]))
        log.info("  %d canonical names -> %d entities",
                 len(mapping), len({v[1] for v in mapping.values()}))
        _upsert(conn, cur, "by_contributor_type",
                remap_by_contributor_type(row[0], mapping))
    else:
        log.warning("by_contributor_type not in dashboard_cache — skipped")

    summary = (f"{len(dirty)} year(s) of sums rebuilt, {len(replaced)} year(s) of totals "
               f"replaced, {changed} committees rewritten")
    elapsed = time.monotonic() - started
    if dry_run:
        conn.rollback()
        log.info("--dry-run: rolled back after %.1fs (%s)", elapsed, summary)
    else:
        conn.commit()
        log.info("Donor aggregates refreshed in %.1fs (%s).", elapsed, summary)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the donor tables onto resolved entities.")
    ap.add_argument("--full", action="store_true",
                    help="rebuild every year of the contribution sums")
    ap.add_argument("--dry-run", action="store_true", help="run everything, then roll back")
    args = ap.parse_args(argv)
    conn = s._connect()
    try:
        return refresh(conn, conn.cursor(), full=args.full, dry_run=args.dry_run)
    except DatabaseUnresponsive as exc:
        log.error("%s", exc)
        logging.shutdown()
        # Closing the connection could wait on the same dead pooler socket.
        os._exit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
