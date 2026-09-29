"""Opt-in PostgreSQL regressions, isolated from all persistent application data.

Run with ORESTAR_TEST_DB=1 and the usual SUPABASE_DB_URL configuration. Every
table, view and function lives in this connection's pg_temp schema; teardown
rolls back the entire fixture. The tests never run the refresh script's main.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))

pytestmark = pytest.mark.skipif(
    os.environ.get("ORESTAR_TEST_DB") != "1",
    reason="Set ORESTAR_TEST_DB=1 to run rollback-only PostgreSQL tests",
)

MISC = "Miscellaneous Cash Contributions $100 and under"
MISC_KEY = "label:miscellaneous cash contributions $100 and under"
INKIND_SUBTYPES = (
    "In-Kind Contribution", "In-Kind/Forgiven Account Payable",
    "In-Kind/Forgiven Personal Expenditures",
)


@pytest.fixture
def db():
    import supabase_sync

    try:
        conn = supabase_sync._connect(attempts=1)
    except Exception:
        # psycopg2's connection frame contains the DSN/password; do not let
        # pytest print its local arguments when the external DB is unavailable.
        pytest.fail("Database connection unavailable; no test objects created", pytrace=False)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("set local statement_timeout = '30s'")
            cur.execute("set local search_path = pg_temp, public")
            cur.execute("""
                create temporary table donors (
                    donor_id text primary key, display_name text
                ) on commit drop;
                create temporary table transactions (
                    filer_id text, tran_date date, amount numeric,
                    donor_id text, contributor_payee_canonical text,
                    contributor_payee text, tran_type text, sub_type text
                ) on commit drop;
                create temporary table filer_detail (
                    slug text primary key, filer_id text, detail jsonb
                ) on commit drop;
            """)
            migration = (ROOT / "supabase/migrations/014_donor_leaderboard.sql").read_text()
            # Relocate the actual implementation instead of copying its logic.
            migration = migration.replace("public.", "pg_temp.")
            migration = migration.replace("set search_path = public", "set search_path = pg_temp")
            # ACLs and the API schema notification are deployment-only steps.
            migration = re.sub(r"^grant\b[^;]*;", "", migration, flags=re.MULTILINE)
            migration = re.sub(r"^notify\b[^;]*;", "", migration, flags=re.MULTILINE)
            assert "public." not in migration
            cur.execute(migration)
            yield cur
    finally:
        conn.rollback()
        conn.close()


def add_transaction(cur, name, amount, *, date="2026-06-15", filer="one",
                    donor_id=None, canonical=None, tran_type="C", subtype="Cash Contribution"):
    cur.execute("""
        insert into pg_temp.transactions
          (filer_id, tran_date, amount, donor_id, contributor_payee_canonical,
           contributor_payee, tran_type, sub_type)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (filer, date, amount, donor_id, canonical, name, tran_type, subtype))


def leaderboard(cur, start=None, end=None, filers=None):
    cur.execute("select pg_temp.donor_leaderboard(%s::date, %s::date, %s::text[])",
                (start, end, filers))
    return cur.fetchone()[0]


def indexed(rows):
    return {row["donor_key"]: row for row in rows}


def test_exact_date_range_includes_both_boundaries_and_excludes_adjacent_days(db):
    db.execute("insert into pg_temp.donors values ('fahey', 'Friends of Julie Fahey')")
    for date, amount in [
        ("2024-11-30", 761500), ("2024-12-01", 25),
        ("2025-06-01", 105000), ("2026-06-01", 335832.50),
        ("2026-11-30", 75), ("2026-12-01", 900000), (None, 999999),
    ]:
        add_transaction(db, "Fahey raw alias", amount, date=date, donor_id="fahey")
    for subtype in INKIND_SUBTYPES:
        add_transaction(db, "In-kind only: " + subtype, 500000, subtype=subtype)
    add_transaction(db, "Expenditure only", 500000, tran_type="E")

    result = leaderboard(db, "2024-12-01", "2026-11-30")
    assert result["all_time"] == [{
        "donor_key": "fahey", "donor_id": "fahey", "name": "Friends of Julie Fahey",
        "total": 440932.50,
    }]
    assert {year: rows[0]["total"] for year, rows in result["by_year"].items()} == {
        "2024": 25, "2025": 105000, "2026": 335907.50,
    }
    one_day = leaderboard(db, "2024-12-01", "2024-12-01")
    assert one_day["all_time"][0]["total"] == 25
    assert leaderboard(db, "2026-11-30", "2024-12-01") == {"all_time": [], "by_year": {}}


def test_labels_merge_pooled_category_but_preserve_resolved_identities(db):
    db.executemany("insert into pg_temp.donors values (%s, %s)", [
        ("misc-a", MISC + " "),
        ("misc-b", " \tMISCELLANEOUS  CASH CONTRIBUTIONS $100 AND UNDER\u00a0"),
        ("person-a", "Shared Name"), ("person-b", "Shared Name"),
    ])
    add_transaction(db, "raw category a", 10, donor_id="misc-a")
    add_transaction(db, "raw category b", 20, donor_id="misc-b")
    add_transaction(db, MISC.lower(), 30)
    add_transaction(db, "raw third variant", 40, canonical=" " + MISC + "\t")
    add_transaction(db, "Person A Alias", 50, donor_id="person-a")
    add_transaction(db, "Person B Alias", 60, donor_id="person-b")
    add_transaction(db, " Unknown  Donor\u00a0", 70)
    add_transaction(db, "unknown donor", 80, canonical=" \t\u00a0")
    add_transaction(db, "Ignored raw name", 90, canonical="Canonical Donor")
    add_transaction(db, "Missing donor record", 15, donor_id="not-in-donors")

    result = leaderboard(db)
    rows = indexed(result["all_time"])
    assert len(rows) == 6
    assert rows[MISC_KEY] == {"donor_key": MISC_KEY, "donor_id": None, "name": MISC, "total": 100}
    assert rows["person-a"]["total"] == 50
    assert rows["person-b"]["total"] == 60
    assert rows["person-a"]["name"] == rows["person-b"]["name"] == "Shared Name"
    assert rows["name:unknown donor"]["total"] == 150
    assert rows["name:canonical donor"]["total"] == 90
    assert rows["not-in-donors"]["total"] == 15
    assert result["by_year"]["2026"] == result["all_time"]


def test_filtering_precedes_top_1000_and_empty_scope_is_empty(db):
    db.execute("""
        insert into pg_temp.transactions
          (filer_id, tran_date, amount, contributor_payee, tran_type)
        select 'one', '2024-11-30'::date, 10000, 'Old donor ' || n, 'C'
        from generate_series(1, 1001) n;
        insert into pg_temp.transactions
          (filer_id, tran_date, amount, contributor_payee, tran_type)
        select 'other', '2026-01-01'::date, 20000, 'Other filer donor ' || n, 'C'
        from generate_series(1, 1001) n;
    """)
    add_transaction(db, "Scoped Donor", 5)
    add_transaction(db, "Scoped Donor", 7, filer="two")
    assert "name:scoped donor" not in indexed(leaderboard(db)["all_time"])
    assert leaderboard(db, "2024-12-01", "2026-11-30", ["one"])["all_time"] == [{
        "donor_key": "name:scoped donor", "donor_id": None, "name": "Scoped Donor", "total": 5,
    }]
    assert leaderboard(db, "2024-12-01", "2026-11-30", ["one", "two"])["all_time"][0]["total"] == 12
    assert leaderboard(db, filers=[]) == {"all_time": [], "by_year": {}}
    assert leaderboard(db, filers=["missing"]) == {"all_time": [], "by_year": {}}


def test_year_rows_follow_ranked_range_donors_without_independent_year_cutoff(db):
    db.execute("""
        insert into pg_temp.transactions
          (filer_id, tran_date, amount, contributor_payee, tran_type)
        select 'one', '2025-06-01'::date, 100, 'Annual donor ' || lpad(n::text, 4, '0'), 'C'
        from generate_series(1, 1000) n;
    """)
    add_transaction(db, "Spanning Donor", 90, date="2025-06-01")
    add_transaction(db, "Spanning Donor", 90, date="2026-06-01")
    # Each spelling misses the cutoff alone; grouping must happen before rank.
    add_transaction(db, MISC, 75, date="2025-06-01")
    add_transaction(db, MISC.lower() + " ", 75, date="2025-06-01")

    result = leaderboard(db, "2024-12-01", "2026-11-30")
    leaders = indexed(result["all_time"])
    assert len(leaders) == 1000
    assert result["all_time"][0]["donor_key"] == "name:spanning donor"
    assert leaders[MISC_KEY]["total"] == 150
    assert len(result["by_year"]["2025"]) == 1000
    assert set(indexed(result["by_year"]["2025"])) == set(leaders)
    assert indexed(result["by_year"]["2025"])["name:spanning donor"]["total"] == 90
    assert list(indexed(result["by_year"]["2026"])) == ["name:spanning donor"]


def test_refresh_builders_preserve_multi_id_profiles_and_other_account_fields(db):
    import refresh_donor_aggregates as refresh

    db.executemany("insert into pg_temp.donors values (%s, %s)", [
        ("misc-a", MISC), ("misc-b", MISC.lower() + " "),
    ])
    add_transaction(db, "raw a", 10, donor_id="misc-a", filer="one", date="2025-06-01")
    add_transaction(db, "raw b", 20, donor_id="misc-b", filer="two")
    add_transaction(db, "Unresolved", 7, filer="two")
    add_transaction(db, "Outside profile", 999, filer="other")
    for subtype in INKIND_SUBTYPES:
        add_transaction(db, "In-kind: " + subtype, 999, filer="one", subtype=subtype)
    marker = {"balance": 123.45, "expenditures": [{"name": "Vendor", "total": 40}]}
    db.executemany("insert into pg_temp.filer_detail values (%s, %s, %s::jsonb)", [
        ("multi", "one", json.dumps({**marker, "filer_ids": ["one", "two", "two"]})),
        ("fallback", "two", json.dumps({**marker, "filer_ids": []})),
        ("empty", "no-transactions", json.dumps({**marker, "top_donors": [{"name": "Stale"}]})),
    ])

    refresh.stage_donor_rows(db)
    top = refresh.build_top_donors(db)
    assert indexed(top["all_time"])[MISC_KEY]["total"] == 30
    assert indexed(top["by_year"]["2025"])[MISC_KEY]["total"] == 10
    assert indexed(top["by_year"]["2026"])[MISC_KEY]["total"] == 20
    assert "name:unresolved" in indexed(top["all_time"])
    for subtype in INKIND_SUBTYPES:
        assert "name:in-kind: " + subtype.lower() not in indexed(top["all_time"])
    assert refresh.rebuild_filer_donors(db) == 3
    db.execute("select slug, detail from pg_temp.filer_detail")
    profiles = dict(db.fetchall())
    for detail in profiles.values():
        for key, value in marker.items():
            assert detail[key] == value
    multi = profiles["multi"]
    assert indexed(multi["top_donors"])[MISC_KEY]["total"] == 30
    assert set(indexed(multi["top_donors"])) == {MISC_KEY, "name:unresolved"}
    assert indexed(multi["top_donors_by_year"]["2025"])[MISC_KEY]["total"] == 10
    assert indexed(multi["top_donors_by_year"]["2026"])[MISC_KEY]["total"] == 20
    assert indexed(profiles["fallback"]["top_donors"])[MISC_KEY]["total"] == 20
    assert profiles["empty"]["top_donors"] == []
    assert profiles["empty"]["top_donors_by_year"] == {}
