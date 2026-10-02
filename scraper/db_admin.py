"""
db_admin.py — apply Supabase migrations and verify schema.

Reads credentials from .env via supabase_sync (never prints them).

Usage:
    python db_admin.py apply            # run migrations 004-006 in order
    python db_admin.py verify           # report tables / indexes / policies / roles
    python db_admin.py seed-aggregates  # upsert dashboard_cache + filer_detail from data/aggregated
"""
import json
import sys
from pathlib import Path

import supabase_sync as s

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
MIGRATIONS = [
    "004_transactions.sql",
    "005_aggregate_views.sql",
    "006_public_query_role.sql",
    "007_donors.sql",
    "008_donor_profile.sql",
    "009_donor_merge_overrides.sql",
    "010_search_transactions.sql",
    "011_donor_search_and_inkind.sql",
    "012_election_results.sql",
    "013_candidate_committee_links.sql",
    "014_donor_leaderboard.sql",
    "015_donor_date_index.sql",
    "019_recommendation_first_gifts.sql",
    "020_donor_profile_recipients.sql",
    "021_immediate_entity_merges.sql",
    "022_donor_display_aliases.sql",
    "024_donor_profile_lookup_performance.sql",
    "025_recommendation_first_gift_performance.sql",
    "026_donor_filer_index.sql",
    "027_stored_donor_identities.sql",
    "028_donor_identity_label_indexes.sql",
    "029_stored_donor_identity_labels.sql",
    "030_explore_source_name_indexes.sql",
    "031_explore_complete_name_search.sql",
    "033_explore_sub_type_index.sql",
    "034_explore_sub_types.sql",
    "035_explore_default_order_index.sql",
    "036_exact_date_recipients.sql",
    "037_transactions_autovacuum.sql",
    "039_daily_contributions.sql",
    # After 021 and 024, whose view and functions it replaces.
    "041_net_refunds_against_donors.sql",
    # Last on purpose: it patches the functions 027 and 029 define, so it has
    # to run after them on every apply.
    "032_merge_refresh_safe_deletes.sql",
    # After 012 and 025, whose anon grants it revokes on every apply.
    "038_recommend_signed_in_only.sql",
]

# Index-only migrations built CONCURRENTLY, one statement at a time, and the
# indexes each creates (a cancelled concurrent build leaves an invalid index
# that IF NOT EXISTS would skip, so those are dropped and rebuilt).
CONCURRENT_INDEXES = {
    "028_donor_identity_label_indexes.sql": ("idx_aliases_identity_label", "idx_donors_identity_label"),
    "030_explore_source_name_indexes.sql": ("idx_txn_source_filer_trgm", "idx_txn_source_payee_trgm"),
    "033_explore_sub_type_index.sql": ("idx_txn_sub_type_date",),
    "035_explore_default_order_index.sql": ("idx_txn_date_desc",),
}


def apply(only: str | None = None):
    """Apply every migration, or just the one named (they are all re-runnable)."""
    names = MIGRATIONS
    if only:
        if only not in MIGRATIONS:
            raise SystemExit(f"unknown migration {only!r}; known: {', '.join(MIGRATIONS)}")
        names = [only]
    conn = s._connect()
    conn.autocommit = True
    cur = conn.cursor()
    for name in names:
        path = MIGRATIONS_DIR / name
        sql = path.read_text()
        print(f"→ applying {name} …", flush=True)
        if name == "015_donor_date_index.sql":
            # Build without blocking transaction imports. A cancelled concurrent
            # build can leave an invalid index which IF NOT EXISTS would skip.
            # Replace the earlier predicate that included forgiven in-kind rows.
            cur.execute("""select i.indisvalid, pg_get_expr(i.indpred, i.indrelid)
                           from pg_index i
                           where i.indexrelid = to_regclass('public.idx_txn_cash_donor_dates')""")
            row = cur.fetchone()
            if row and (not row[0] or any(subtype not in (row[1] or "") for subtype in (
                "In-Kind/Forgiven Account Payable",
                "In-Kind/Forgiven Personal Expenditures",
            ))):
                cur.execute("drop index concurrently public.idx_txn_cash_donor_dates")
            sql = sql.replace("create index if not exists", "create index concurrently if not exists")
        if name == "026_donor_filer_index.sql":
            cur.execute("select indisvalid from pg_index where indexrelid=to_regclass('public.idx_txn_donor_filer')")
            row = cur.fetchone()
            if row and not row[0]:
                cur.execute("drop index concurrently public.idx_txn_donor_filer")
            sql = sql.replace("create index if not exists", "create index concurrently if not exists")
        if name in CONCURRENT_INDEXES:
            # Each CONCURRENTLY statement must be its own transaction.
            for index in CONCURRENT_INDEXES[name]:
                cur.execute("select indisvalid from pg_index where indexrelid=to_regclass(%s)", ("public." + index,))
                row = cur.fetchone()
                if row and not row[0]:
                    cur.execute("drop index concurrently public." + index)
            # These migrations contain only CREATE INDEX statements, with no
            # procedural SQL or semicolons inside literals/comments.
            for statement in sql.split(";"):
                if statement.strip():
                    cur.execute(statement.replace("create index if not exists", "create index concurrently if not exists"))
        else:
            cur.execute(sql)
        print(f"  ✓ {name}")
    conn.close()
    print("All migrations applied.")


def verify():
    conn = s._connect()
    cur = conn.cursor()

    def q(sql, args=None):
        cur.execute(sql, args or ())
        return cur.fetchall()

    print("== Tables ==")
    for (t,) in q("""
        select table_name from information_schema.tables
        where table_schema='public' and table_name in
          ('transactions','dashboard_cache','filer_detail')
        order by table_name"""):
        print("  ", t)

    print("== transactions indexes ==")
    for (i,) in q("select indexname from pg_indexes where tablename='transactions' order by indexname"):
        print("  ", i)

    print("== RLS policies (public read) ==")
    for tbl, pol in q("""
        select tablename, policyname from pg_policies
        where schemaname='public' and tablename in
          ('transactions','dashboard_cache','filer_detail')
        order by tablename"""):
        print(f"   {tbl}: {pol}")

    print("== query schema view ==")
    for (v,) in q("select table_name from information_schema.views where table_schema='query'"):
        print("  query.", v, sep="")

    print("== public_query role ==")
    rows = q("select rolname, rolcanlogin from pg_roles where rolname='public_query'")
    print("  ", rows[0] if rows else "MISSING")

    print("== row counts ==")
    for tbl in ("transactions", "dashboard_cache", "filer_detail"):
        (n,) = q(f"select count(*) from {tbl}")[0]
        print(f"   {tbl}: {n:,}")

    conn.close()


def seed_aggregates():
    """Upsert the dashboard aggregate blobs and per-filer detail from the
    committed data/aggregated JSON files (no scraping/regeneration needed)."""
    # dashboard_cache: every top-level *.json → keyed by filename stem
    for path in sorted(AGG_DIR.glob("*.json")):
        key = path.stem
        s.upsert_dashboard_cache(key, json.loads(path.read_text()))

    # filer_detail: one row per data/aggregated/filers/*.json
    slug_to_fid = {}
    idx_path = AGG_DIR / "filer_index.json"
    if idx_path.exists():
        for row in json.loads(idx_path.read_text()):
            slug_to_fid[row.get("slug")] = row.get("filer_id", "")

    rows = []
    for path in sorted((AGG_DIR / "filers").glob("*.json")):
        detail = json.loads(path.read_text())
        slug = detail.get("slug") or path.stem
        rows.append({
            "slug": slug,
            "name": detail.get("name"),
            "filer_id": slug_to_fid.get(slug, ""),
            "detail": detail,
        })
    print(f"→ seeding {len(rows)} filer_detail rows …", flush=True)
    # Upsert in batches so one call isn't enormous.
    for i in range(0, len(rows), 500):
        s.bulk_upsert_filer_detail(rows[i:i + 500])
    print("Aggregate seed complete.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "apply":
        apply(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        {"verify": verify, "seed-aggregates": seed_aggregates}.get(cmd, verify)()
