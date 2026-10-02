"""The Fundraising Pulse's Biggest Donors, rebuilt from netted donor rows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import refresh_donor_aggregates as r


def test_month_bounds_cover_whole_months_across_a_year_end():
    assert r._month_bounds(["2026-09", "2026-10"]) == ("2026-09-01", "2026-11-01")
    assert r._month_bounds(["2025-01", "2026-12"]) == ("2025-01-01", "2027-01-01")


class FakeCursor:
    """Answers pulse_donors' query with per-(donor, committee) kept amounts."""
    def __init__(self, rows):
        self.rows, self.params = rows, []

    def execute(self, sql, params):
        assert "having sum(r.amount) > 0" in sql and "not like 'miscellaneous %%'" in sql
        self.params.append(params)

    def fetchall(self):
        return self.rows


def test_each_period_ranks_donors_by_what_committees_kept():
    rows = [("c1", "c1", "Big PAC", "a", "Cmte A", 300.0), ("c1", "c1", "Big PAC", "b", "Cmte B", 500.0),
            ("d2", "d2", "Person", "a", "Cmte A", 600.0)]
    cur = FakeCursor(rows)
    snap = {"periods": {"30d": {"months": ["2026-10", "2026-09"], "top_donors": ["stale"]},
                        "empty": {"months": [], "top_donors": ["kept"]}}}
    out = r.rebuild_pulse_donors(cur, snap)["periods"]
    assert cur.params == [("2026-09-01", "2026-11-01")]
    assert out["empty"]["top_donors"] == ["kept"]
    assert [(d["name"], d["total"], d["committees"]) for d in out["30d"]["top_donors"]] == [
        ("Big PAC", 800.0, 2), ("Person", 600.0, 1)]
    assert [x["slug"] for x in out["30d"]["top_donors"][0]["details"]] == ["b", "a"]
    assert out["30d"]["top_donors"][0]["donor_id"] == "c1"
