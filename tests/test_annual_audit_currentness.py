"""Annual audits must not diagnose mismatched collection snapshots."""

from __future__ import annotations

import json
import sys
from pathlib import Path


SCRAPER = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER))

import audit_2006_basis  # noqa: E402
import audit_consistency  # noqa: E402
import audit_yearly_balance  # noqa: E402


def _annual_row(*, current=True, delta=50.0) -> dict:
    return {
        "comparison_current": current,
        "delta_contributions": delta,
        "delta_expenditures": 0.0,
        "delta_other_receipts": 0.0,
        "delta_other_disbursements": 0.0,
        "delta_begin": 0.0,
        "discrepancy": delta,
        "our_net": 150.0,
        "orestar_movement": 100.0,
        "delta_movement": delta,
    }


def _detail(row: dict) -> dict:
    return {
        "name": "Committee",
        "total_in": 0.0,
        "total_out": 0.0,
        "cash_on_hand": 0.0,
        "timeline": [],
        "beginning_balances": {},
        "yearly_discrepancies": {"2006": row},
        "basis_2006": {
            "tran_2006": 150.0,
            "tran_2006_filed_by_2006": 100.0,
        },
    }


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql):
        return None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor

    def close(self):
        return None


def test_consistency_audit_requires_literal_current_annual_evidence() -> None:
    stale = _annual_row(current=False)
    legacy = _annual_row()
    legacy.pop("comparison_current")
    truthy_but_invalid = _annual_row(current=1)

    for row in (stale, legacy, truthy_but_invalid):
        assert audit_consistency.audit_filer("committee", _detail(row)) == []

    findings = audit_consistency.audit_filer(
        "committee", _detail(_annual_row(current=True))
    )
    assert {finding["check"] for finding in findings} == {
        "ORESTAR contributions", "ORESTAR movement",
    }


def test_year_local_evidence_does_not_diagnose_rolled_balance_delta() -> None:
    row = _annual_row(current=True, delta=0.0)
    row["delta_begin"] = 50.0
    row["discrepancy"] = 50.0

    assert audit_consistency.audit_filer("committee", _detail(row)) == []


def test_yearly_balance_report_excludes_stale_annual_delta(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [
        ("stale", "Stale", _detail(_annual_row(current=False))),
        ("current", "Current", _detail(_annual_row(current=True))),
    ]
    monkeypatch.setattr(
        audit_yearly_balance.supabase_sync, "_connect",
        lambda: _Connection(rows),
    )
    output = tmp_path / "yearly.json"
    monkeypatch.setattr(
        sys, "argv", ["audit_yearly_balance.py", "--json", str(output)]
    )

    assert audit_yearly_balance.main() == 0
    payload = json.loads(output.read_text())
    assert [row["filer_id"] for row in payload] == ["current"]


def test_2006_basis_report_excludes_stale_annual_delta(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [
        ("stale", "Stale", _detail(_annual_row(current=False))),
        ("current", "Current", _detail(_annual_row(current=True))),
    ]
    monkeypatch.setattr(
        audit_2006_basis.supabase_sync, "_connect",
        lambda: _Connection(rows),
    )
    output = tmp_path / "basis.json"
    monkeypatch.setattr(
        sys, "argv", ["audit_2006_basis.py", "--json", str(output)]
    )

    assert audit_2006_basis.main() == 0
    payload = json.loads(output.read_text())
    assert [row["filer_id"] for row in payload] == ["current"]
