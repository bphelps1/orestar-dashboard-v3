"""Opt-in PostgreSQL regressions for the exact-date Recipients rankings (036).

Run with ORESTAR_TEST_DB=1 and the usual SUPABASE_DB_URL configuration. As in
test_donor_leaderboard_sql.py, every table and function lives in this
connection's pg_temp schema and teardown rolls the whole fixture back.
"""

from __future__ import annotations

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

INKIND_SUBTYPES = (
    "In-Kind Contribution", "In-Kind/Forgiven Account Payable",
    "In-Kind/Forgiven Personal Expenditures",
)


def relocated(name: str) -> str:
    """A migration's actual SQL, moved into pg_temp without its deployment-only steps."""
    sql = (ROOT / "supabase/migrations" / name).read_text()
    sql = sql.replace("public.", "pg_temp.").replace("set search_path = public", "set search_path = pg_temp")
    sql = re.sub(r"^grant\b[^;]*;", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"^notify\b[^;]*;", "", sql, flags=re.MULTILINE)
    assert "public." not in sql
    return sql


@pytest.fixture
def db():
    import supabase_sync

    try:
        conn = supabase_sync._connect(attempts=1)
    except Exception:
        pytest.fail("Database connection unavailable; no test objects created", pytrace=False)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("set local statement_timeout = '30s'")
            cur.execute("set local search_path = pg_temp, public")
            cur.execute("""
                create temporary table donors (donor_id text primary key, display_name text) on commit drop;
                create temporary table transactions (
                    filer_id text, filer text, filer_canonical text, tran_date date, amount numeric,
                    donor_id text, contributor_payee_canonical text, contributor_payee text,
                    tran_type text, sub_type text
                ) on commit drop;
                create temporary table filer_detail (
                    slug text primary key, name text, filer_id text, detail jsonb, updated_at timestamptz
                ) on commit drop;
            """)
            cur.execute(relocated("014_donor_leaderboard.sql"))      # normalize_donor_label
            cur.execute(relocated("036_exact_date_recipients.sql"))
            yield cur
    finally:
        conn.rollback()
        conn.close()


def add(cur, filer_id, amount, *, date="2026-06-15", tran_type="C", subtype="Cash Contribution",
        filer=None, canonical=None, payee="Someone", payee_canonical=None):
    cur.execute("""
        insert into pg_temp.transactions
          (filer_id, filer, filer_canonical, tran_date, amount, contributor_payee,
           contributor_payee_canonical, tran_type, sub_type)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (filer_id, filer or f"Filer {filer_id}", canonical, date, amount, payee, payee_canonical,
          tran_type, subtype))


def recipients(cur, start=None, end=None, limit=100):
    cur.execute("select pg_temp.recipient_leaderboard(%s::date, %s::date, %s)", (start, end, limit))
    return cur.fetchone()[0]


def payees(cur, filers, start=None, end=None):
    cur.execute("select pg_temp.payee_leaderboard(%s::text[], %s::date, %s::date)", (filers, start, end))
    return cur.fetchone()[0]


def test_a_cycle_counts_only_its_own_days(db):
    # The 2024 cycle is Dec 1, 2022 – Nov 30, 2024: the governor's race of
    # 2022 falls before it, a gift on each boundary day falls inside.
    db.execute("insert into pg_temp.filer_detail values ('gov', 'Governor 2022', 'g', '{}', now())")
    for date, amount in [("2022-11-30", 20_000_000), ("2022-12-01", 25), ("2024-11-30", 75), ("2024-12-01", 900)]:
        add(db, "g", amount, date=date)
    add(db, "m", 16_000_000, date="2024-06-01", filer="Measure Campaign")
    for subtype in INKIND_SUBTYPES:
        add(db, "k", 50_000_000, date="2024-06-01", subtype=subtype)
    add(db, "x", 50_000_000, date="2024-06-01", tran_type="E")

    rows = recipients(db, "2022-12-01", "2024-11-30")
    assert [(r["name"], r["total"]) for r in rows] == [("Measure Campaign", 16_000_000), ("Governor 2022", 100)]
    assert rows[1]["slug"] == "gov" and rows[0]["slug"] is None
    assert recipients(db, "2024-11-30", "2022-12-01") == []


def test_a_committee_is_one_row_named_as_the_dashboard_names_it(db):
    # Renamed mid-cycle, with canonical names mostly blank in the table: one
    # row for the filer ID, under the dashboard's name for it.
    db.execute("insert into pg_temp.filer_detail values ('new', 'New Name PAC', 'r', '{}', now())")
    add(db, "r", 100, date="2025-01-10", filer="Old Name PAC")
    add(db, "r", 200, date="2026-02-10", filer="New Name PAC ", canonical=None)
    # No filer_detail row: the name on its latest gift in range, trimmed.
    add(db, "u", 50, date="2025-03-01", filer="Earlier Name")
    add(db, "u", 60, date="2026-03-01", filer="  Latest Name ", canonical=None)
    rows = recipients(db, "2024-12-01", "2026-11-30")
    assert [(r["filer_id"], r["name"], r["total"]) for r in rows] == [
        ("r", "New Name PAC", 300), ("u", "Latest Name", 110)]
    assert len(recipients(db, limit=1)) == 1


def test_payees_cover_every_filer_id_of_the_committee_and_merge_case(db):
    add(db, "a", 1000, tran_type="E", payee="canal partners media", date="2025-05-01")
    add(db, "b", 500, tran_type="E", payee="Canal Partners Media", date="2026-05-01")
    add(db, "a", 300, tran_type="E", payee="raw", payee_canonical="ADP", date="2026-01-01")
    add(db, "a", 999, tran_type="E", payee="Before the cycle", date="2024-11-30")
    add(db, "other", 5000, tran_type="E", payee="Another committee's vendor", date="2025-05-01")
    add(db, "a", 7000, payee="A donor, not a payee", date="2025-05-01")
    rows = payees(db, ["a", "b"], "2024-12-01", "2026-11-30")
    assert [(r["name"].lower(), r["total"]) for r in rows] == [("canal partners media", 1500), ("adp", 300)]
