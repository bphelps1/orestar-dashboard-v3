"""Opt-in PostgreSQL regressions for daily_contributions (039).

Run with ORESTAR_TEST_DB=1 and the usual SUPABASE_DB_URL configuration. As in
test_recipient_leaderboard_sql.py, the table and function live in this
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
                create temporary table transactions (
                    filer_id text, tran_date date, amount numeric, tran_type text, sub_type text
                ) on commit drop;
            """)
            cur.execute(relocated("039_daily_contributions.sql"))
            yield cur
    finally:
        conn.rollback()
        conn.close()


def add(cur, filer_id, amount, date, *, tran_type="C", subtype="Cash Contribution"):
    cur.execute(
        "insert into pg_temp.transactions values (%s, %s, %s, %s, %s)",
        (filer_id, date, amount, tran_type, subtype),
    )


def daily(cur, start, end, filers=None):
    cur.execute("select pg_temp.daily_contributions(%s::date, %s::date, %s::text[])", (start, end, filers))
    return [(r["d"], r["total"]) for r in cur.fetchone()[0]]


def test_cash_by_day_inside_the_window(db):
    for date, amount in [("2024-11-30", 1), ("2024-12-01", 10), ("2024-12-01", 15), ("2026-10-01", 7),
                         ("2026-11-30", 3), ("2026-12-01", 99)]:
        add(db, "a", amount, date)
    for subtype in INKIND_SUBTYPES:
        add(db, "a", 1_000, "2025-06-01", subtype=subtype)
    add(db, "a", 1_000, "2025-06-01", tran_type="E")

    assert daily(db, "2024-12-01", "2026-11-30") == [
        ("2024-12-01", 25), ("2026-10-01", 7), ("2026-11-30", 3)]


def test_filter_to_one_committees_filer_ids(db):
    add(db, "a", 10, "2025-01-05")
    add(db, "b", 20, "2025-01-05")
    add(db, "c", 40, "2025-01-05")

    assert daily(db, "2024-12-01", "2026-11-30", ["a", "b"]) == [("2025-01-05", 30)]
    assert daily(db, "2024-12-01", "2026-11-30") == [("2025-01-05", 70)]
    assert daily(db, "2024-12-01", "2026-11-30", ["none"]) == []
