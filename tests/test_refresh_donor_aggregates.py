"""The donor re-key works one year, or one batch of committees, at a time.

Staging the whole donor_contribution_rows view into a 1.8M-row temp table —
100 s of index and heap reads and 330 MB of temp files in one statement —
hung the Micro instance on 2026-09-23, 2026-10-19 and 2026-10-22, and did so
twice more when fed from a cache instead of the view, while 27 per-year
statements writing the same rows went through. Now every writer of
`transactions` marks the years it touched, the sums are rebuilt for those
years only (migration 042), identities are applied per year and written only
where they changed, the blobs are built per year and per batch, and a
statement the server neither answers nor cancels ends the run.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import refresh_donor_aggregates as r
import supabase_sync as s


class FakeCursor:
    """Answers each statement from `answers`, matched by a substring, and
    records every (sql, params) pair it was given."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.rowcount = -1
        self._rows = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self._rows = []
        for needle, rows in self.answers.items():
            if needle in normalized:
                self._rows = list(rows)
                break
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def sql(self, needle):
        return [sql for sql, _ in self.calls if needle in sql]

    def position(self, prefix):
        """Index of the first statement starting with `prefix`."""
        return next(i for i, (sql, _) in enumerate(self.calls) if sql.startswith(prefix))


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


YEARS = "with recursive years as"


# ── Statement runner ────────────────────────────────────────────────────────

def test_a_statement_the_server_never_answers_is_reported_not_waited_for():
    class Stuck:
        connection = None

        def execute(self, sql, params=None):
            time.sleep(1)

    with pytest.raises(r.DatabaseUnresponsive, match="treating the database as unresponsive"):
        r.run(Stuck(), "select 1", label="probe", budget=0.05)


def test_a_statement_error_reaches_the_caller_on_its_own_thread():
    class Broken:
        def execute(self, sql, params=None):
            raise ValueError("bad sql")

    with pytest.raises(ValueError, match="bad sql"):
        r.run(Broken(), "select 1")


def test_a_hung_statement_is_cancelled_through_the_connection():
    cancelled = threading.Event()

    class Conn:
        def cancel(self):
            cancelled.set()

    class Stuck:
        connection = Conn()

        def execute(self, sql, params=None):
            time.sleep(1)

    with pytest.raises(r.DatabaseUnresponsive):
        r.run(Stuck(), "select 1", budget=0.05)
    assert cancelled.wait(1)


# ── 1. Which years of sums to rebuild ───────────────────────────────────────

def test_only_marked_years_are_rebuilt_when_the_cache_is_populated():
    cur = FakeCursor({"from donor_contribution_dirty": [(2026,), (2019,)],
                      "select exists (select 1 from donor_contribution_sums)": [(True,)]})
    assert r.years_to_rebuild(cur) == [2019, 2026]
    assert not cur.sql("delete from donor_contribution_dirty")


@pytest.mark.parametrize("marked, populated, full", [
    ([(s.DIRTY_ALL,), (2026,)], True, False),   # a writer marked every year
    ([], False, False),                          # first run: the cache is empty
    ([], True, True),                            # --full
])
def test_every_year_is_rebuilt_and_listed_explicitly(marked, populated, full):
    cur = FakeCursor({"from donor_contribution_dirty": marked,
                      "select exists (select 1 from donor_contribution_sums)": [(populated,)],
                      "extract(year from min(tran_date))": [(2024, 2026, True)],
                      "to_regclass": [(True,)]})
    assert r.years_to_rebuild(cur, full=full) == [0, 2024, 2025, 2026]
    # The sentinel is replaced by the explicit years before any year is
    # rebuilt, so an interrupted rebuild resumes with the rest still marked.
    assert cur.sql("delete from donor_contribution_dirty")
    (_, params), = [c for c in cur.calls if "unnest" in c[0]]
    assert params == ([0, 2024, 2025, 2026],)


def test_a_year_of_sums_is_unmarked_cleared_and_refilled_in_that_order():
    cur = FakeCursor()
    r.rebuild_sums_year(cur, 2025)
    assert [sql[:40] for sql, _ in cur.calls] == [
        "delete from donor_contribution_dirty whe",
        "delete from donor_contribution_sums wher",
        "insert into donor_contribution_sums (yr,",
    ]
    insert, params = cur.calls[-1]
    assert params["start"] == "2025-01-01" and params["end"] == "2026-01-01"
    assert "tran_date >= %(start)s and tran_date < %(end)s" in insert
    # Gifts and refunds, with the same predicates as donor_contribution_rows.
    assert "tran_type = 'C' and coalesce(sub_type, '') not in" in insert
    assert "sub_type = 'Return or Refund of Contribution'" in insert and "-sum(amount)" in insert


def test_undated_rows_are_year_zero():
    cur = FakeCursor()
    r.rebuild_sums_year(cur, 0)
    insert, params = cur.calls[-1]
    assert params["yr"] == 0 and "tran_date is null" in insert and "%(start)s" not in insert


def test_each_rebuilt_year_of_sums_commits_on_its_own():
    cur = FakeCursor({"to_regclass": [(True,)],
                      "from donor_contribution_dirty": [(2025,), (2026,)],
                      "select exists (select 1 from donor_contribution_sums)": [(True,)]})
    conn = FakeConnection()
    assert r.refresh_contribution_sums(conn, cur) == [2025, 2026]
    # the year list, each year, and the analyze
    assert conn.commits == 4
    assert cur.sql("analyze donor_contribution_sums")


def test_a_dry_run_never_commits():
    cur = FakeCursor({"to_regclass": [(True,)],
                      "from donor_contribution_dirty": [(2025,)],
                      "select exists (select 1 from donor_contribution_sums)": [(True,)]})
    conn = FakeConnection()
    r.refresh_contribution_sums(conn, cur, commit=False)
    assert conn.commits == 0


# ── 2. Identities applied one year at a time ────────────────────────────────

def test_an_unchanged_year_of_totals_is_left_alone():
    cur = FakeCursor({"except table": [(False,)]})
    assert r.rebuild_totals_year(cur, 2024) is None
    (rows,) = cur.sql("create temporary table y_rows")
    assert "from donor_contribution_rows_cached where yr = %(yr)s" in rows
    assert not cur.sql("delete from donor_year_totals")
    assert not cur.sql("insert into")


def test_a_changed_year_of_totals_is_replaced_with_its_canonical_map():
    cur = FakeCursor({"except table": [(True,)],
                      "insert into donor_year_totals": [("row",)] * 3})
    assert r.rebuild_totals_year(cur, 2024) == 3
    assert cur.position("delete from donor_year_totals") < cur.position("insert into donor_year_totals")
    assert cur.position("delete from donor_canonical_totals") < cur.position("insert into donor_canonical_totals")
    (canon,) = cur.sql("insert into donor_canonical_totals")
    assert "group by contributor_payee_canonical, donor_id" in canon


def test_a_year_whose_sums_were_rebuilt_is_replaced_without_comparing():
    cur = FakeCursor({"except table": [(False,)]})
    assert r.rebuild_totals_year(cur, 2026, force=True) == 0
    assert not cur.sql("except table") and cur.sql("insert into donor_year_totals")


def test_every_year_of_sums_gets_its_identities_and_only_changed_years_commit_writes():
    cur = FakeCursor({YEARS: [(0,), (2025,), (2026,)],
                      "except table": [(False,)]})
    conn = FakeConnection()
    assert r.rebuild_year_totals(conn, cur, dirty=[2026]) == [2026]
    assert [p["yr"] for sql, p in cur.calls if sql.startswith("create temporary table y_rows")] == [0, 2025, 2026]
    # one commit per year, plus the analyze after a replacement
    assert conn.commits == 4
    assert not [sql for sql, _ in cur.calls if "from transactions" in sql]


# ── 3. The blobs read the per-year totals ───────────────────────────────────

def test_statewide_lists_are_ranked_all_time_and_per_year_from_the_totals():
    cur = FakeCursor({"from donor_year_totals group by donor_key": [("c1", "c1", "Big PAC", 900)],
                      "from donor_year_totals where yr = %(yr)s": [("d2", "d2", "Person", 50)],
                      YEARS: [(2025,), (2026,)]})
    top = r.build_top_donors(cur)
    assert top["all_time"] == [{"name": "Big PAC", "total": 900.0, "donor_id": "c1", "donor_key": "c1"}]
    assert list(top["by_year"]) == ["2025", "2026"]
    assert top["by_year"]["2026"][0]["donor_key"] == "d2"
    years = [p["yr"] for sql, p in cur.calls if "where yr = %(yr)s" in sql]
    assert years == [2025, 2026]
    (distinct,) = cur.sql(YEARS)
    assert "and yr > 0" in distinct  # undated gifts count all-time only
    assert not [sql for sql, _ in cur.calls if "grouping sets" in sql]


def test_committee_tables_are_rebuilt_in_batches_and_written_only_where_changed():
    cur = FakeCursor({"select slug from filer_detail": [(f"c{i}",) for i in range(2500)]})
    assert r.rebuild_filer_donors(cur) == 0
    updates = [(sql, p) for sql, p in cur.calls if "update filer_detail" in sql]
    assert [len(p["slugs"]) for _, p in updates] == [1000, 1000, 500]
    update = updates[0][0]
    assert "where fd.slug = any(%(slugs)s)" in update and "from donor_year_totals" in update
    assert "fd.detail->'top_donors' is distinct from p.all_time" in update
    assert "fd.detail->'top_donors_by_year' is distinct from p.by_year" in update


def test_the_pulse_reads_the_cached_rows_by_date():
    cur = FakeCursor()
    r.pulse_donors(cur, "2026-09-01", "2026-11-01")
    (sql, params), = cur.calls
    assert "from donor_contribution_rows_cached r" in sql and params == ("2026-09-01", "2026-11-01")
    assert "having sum(r.amount) > 0" in sql


def test_the_publication_keeps_the_keyed_lists_a_re_key_wrote():
    # Without this every committee would differ from its keyed version after
    # process.py's raw-label rebuild, and the re-key would rewrite all 7,400.
    sql = " ".join(s._DETAIL_SQL.split())
    assert "WHEN filer_detail.detail->'top_donors'->0 ? 'donor_key'" in sql
    assert "'top_donors', filer_detail.detail->'top_donors'" in sql
    assert "ELSE EXCLUDED.detail END" in sql


# ── Canonical-name map for by_contributor_type ─────────────────────────────

def test_blob_names_come_from_every_section():
    blob = {"all_time": [{"type": "PAC", "top_donors": [{"name": "A"}, {"name": ""}]}],
            "by_year": {"2026": [{"type": "PAC", "top_donors": [{"name": "B"}]}]},
            "by_month": {"2026-09": [{"type": "Person", "top_donors": [{"name": "C"}]}]}}
    assert r.blob_donor_names(blob) == {"A", "B", "C"}


def test_canonical_map_reads_the_cache_then_only_the_names_it_lacks():
    cur = FakeCursor({"from donor_canonical_totals": [
                          ("Big PAC", "c1", "Big PAC"), ("Big PAC", "c9", "Other")],
                      "from transactions t join donors d": [("In-kind Only", "d2", "In-kind Only")]})
    mapping = r.canonical_to_entity(cur, {"Big PAC", "In-kind Only", "Nobody"})
    cache, fallback = [p for sql, p in cur.calls]
    assert cache == (["Big PAC", "In-kind Only", "Nobody"],)
    assert fallback == (["In-kind Only", "Nobody"],)
    # The entity that gave the most under a name wins (first row per name).
    assert mapping == {"Big PAC": ("Big PAC", "c1"), "In-kind Only": ("In-kind Only", "d2")}


def test_no_names_means_no_queries():
    cur = FakeCursor()
    assert r.canonical_to_entity(cur, set()) == {} and cur.calls == []


# ── Writers mark the years they touch ───────────────────────────────────────

def test_marking_years_is_deduplicated_and_sorted():
    cur = FakeCursor({"to_regclass": [(True,)]})
    s.mark_donor_sum_years(cur, [2026, 2019, 2026, 0])
    (_, params), = [c for c in cur.calls if "unnest" in c[0]]
    assert params == ([0, 2019, 2026],)
    assert "on conflict do nothing" in cur.calls[-1][0]


def test_marking_every_year_writes_the_sentinel():
    cur = FakeCursor({"to_regclass": [(True,)]})
    s.mark_donor_sum_years(cur, all_years=True)
    assert cur.calls[-1][1] == (s.DIRTY_ALL,)


def test_nothing_is_written_for_no_years():
    cur = FakeCursor({"to_regclass": [(True,)]})
    s.mark_donor_sum_years(cur, [])
    assert not [sql for sql, _ in cur.calls if "insert" in sql]


def test_the_cache_tables_are_created_where_no_migration_ran():
    cur = FakeCursor({"to_regclass": [(False,)]})
    assert s.ensure_donor_contribution_cache(cur) is True
    created = cur.calls[-1][0]
    for table in ("donor_contribution_sums", "donor_contribution_dirty",
                  "donor_year_totals", "donor_canonical_totals"):
        assert f"create table if not exists public.{table}" in created
    assert "create or replace view public.donor_contribution_rows_cached" in created
    cur = FakeCursor({"to_regclass": [(True,)]})
    assert s.ensure_donor_contribution_cache(cur) is False
    assert len(cur.calls) == 1


class _Conn:
    """What upsert_transactions / delete_transactions need from a connection."""

    def __init__(self, cur):
        self.cur, self.committed = cur, False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StageCursor(FakeCursor):
    def __init__(self, answers=None):
        super().__init__(answers)
        self.copied = []

    def copy_expert(self, sql, buf):
        self.copied.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def synced(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://u:p@h:5432/db")
    cur = _StageCursor({"to_regclass": [(True,)],
                        "from _txn_stage": [(2026,), (2019,)],
                        "from transactions where tran_id = any": [(2007,)]})
    conn = _Conn(cur)
    monkeypatch.setattr(s, "_connect", lambda *a, **k: conn)
    return conn, cur


def test_the_daily_upsert_marks_the_years_of_new_and_replaced_rows(synced):
    import pandas as pd
    conn, cur = synced
    s.upsert_transactions(pd.DataFrame({"tran_id": ["1", "2"], "tran_date": ["2026-10-01", ""],
                                        "amount": ["5", "6"], "filer id": ["a", "b"]}))
    (years,) = cur.sql("from _txn_stage union")
    # new years from the staged rows, old years from the rows they replace
    assert "join _txn_stage s on s.tran_id = t.tran_id" in years
    (_, params), = [c for c in cur.calls if "unnest" in c[0]]
    assert params == ([2019, 2026],)
    assert cur.position("insert into donor_contribution_dirty") < cur.position("INSERT INTO transactions")
    assert conn.committed


def test_deletions_mark_the_years_of_the_rows_removed(synced):
    conn, cur = synced
    s.delete_transactions(["10", "x", "11"])
    (lookup, params), = [c for c in cur.calls if "where tran_id = any" in c[0] and "select" in c[0]]
    assert params == ([10, 11],)
    (_, marked), = [c for c in cur.calls if "unnest" in c[0]]
    assert marked == ([2007],)
    assert cur.position("insert into donor_contribution_dirty") < cur.position("DELETE FROM transactions")


# ── The whole run ───────────────────────────────────────────────────────────

def _answers():
    return {"to_regclass": [(True,)],
            "select count(*) from donors": [(5,)],
            "from donor_contribution_dirty": [(2026,)],
            "select exists (select 1 from donor_contribution_sums)": [(True,)],
            YEARS: [(2025,), (2026,)],
            "except table": [(False,)],
            "select slug from filer_detail": [("a",), ("b",)],
            "key='activity_snapshot'": [({"periods": {"30d": {"months": ["2026-10"]}}},)],
            "key='by_contributor_type'": [({"all_time": [{"type": "PAC", "top_donors": [
                {"name": "Big PAC", "total": 5.0}]}]},)],
            "from donor_canonical_totals": [("Big PAC", "c1", "Big PAC")]}


def test_a_run_rebuilds_sums_then_totals_then_the_blobs_and_commits_at_the_end():
    cur, conn = FakeCursor(_answers()), FakeConnection()
    assert r.refresh(conn, cur) == 0
    sums, totals, lists = (cur.position("insert into donor_contribution_sums"),
                           cur.position("create temporary table y_rows"),
                           cur.position("select donor_key, min(donor_id), min(name)"))
    assert sums < totals < lists
    writes = [p[0] for sql, p in cur.calls if sql.startswith("insert into dashboard_cache")]
    assert writes == ["top_donors", "activity_snapshot", "by_contributor_type"]
    assert conn.rollbacks == 0 and conn.commits > 0
    # the pulse and the committee lists never touch transactions
    assert not [sql for sql, _ in cur.calls if "join scope s" in sql and "transactions" in sql]


def test_a_dry_run_rolls_everything_back():
    cur, conn = FakeCursor(_answers()), FakeConnection()
    assert r.refresh(conn, cur, dry_run=True) == 0
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_an_empty_donors_table_stops_the_run():
    cur = FakeCursor({**_answers(), "select count(*) from donors": [(0,)]})
    assert r.refresh(FakeConnection(), cur) == 1
