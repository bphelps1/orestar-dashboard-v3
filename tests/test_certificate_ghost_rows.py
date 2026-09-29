"""Ghost rows through the real aggregation, on Friends of Daniel Bunn's figures.

The pure rule is covered in test_orestar_certificates.py. These tests drive
process.aggregate_filers itself, because the property that matters is
end-to-end: the ghost rows must reach the balance, the per-year comparison and
the monthly timeline together. aggregate_filers raises if any year's timeline
disagrees with its yearly net, so a ghost row that reached one and not the
other would fail loudly here rather than on the next production refresh.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from test_nonreturning_balance_guard import _aggregate_cash_rows, _cash_row, _timeline_net


def _row(tran_id: str, amount: float, tran_type: str, sub_type: str, day: str) -> dict:
    when = pd.Timestamp(day)
    row = _cash_row(tran_id, amount, tran_type, sub_type)
    row.update({
        "filed_date": when,
        "tran_date": when,
        "year": int(when.year),
        "month": when.to_period("M").strftime("%Y-%m"),
    })
    return row


# Bunn's itemized history: $1,709.94 left after 2012, nothing itemized in the
# certificate years 2013-2015, then $9,959.94 spent in 2016-2017 — money the
# itemized record never shows arriving.
ROWS = [
    _row("c1", 6750.00, "C", "Cash Contribution", "2012-05-11"),
    _row("e1", 5040.06, "E", "Cash Expenditure", "2012-09-17"),
    _row("e2", 5000.00, "E", "Cash Expenditure", "2016-08-15"),
    _row("e3", 4959.94, "E", "Cash Expenditure", "2017-01-25"),
]


def _summary(begin: float, end: float, contributions: float = 0.0,
             expenditures: float = 0.0) -> dict:
    return {
        "beginning_balance": begin, "ending_cash_balance": end,
        "contributions": contributions, "expenditures": expenditures,
        "other_receipts": 0.0, "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
    }


def _yearly() -> dict:
    return {"1": {"ts": 1_800_000_000.0, "years": {
        "2012": _summary(0.00, 1709.94, 6750.00, 5040.06),
        "2013": _summary(1709.94, 1709.94),
        "2014": _summary(4209.94, 4209.94),
        "2015": _summary(6709.94, 6709.94),
        "2016": _summary(9959.94, 4959.94, 0.0, 5000.00),
        "2017": _summary(4959.94, 0.00, 0.0, 4959.94),
    }}}


def _write_certificates(tmp_path: Path, years: list[int]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "orestar_certificates.json").write_text(json.dumps({
        "version": 1,
        "years": {
            str(y): {"certificates": [{
                "filer_id": "1", "committee": "Committee",
                "filed": f"01/09/{y}", "expires": None, "submitted_by": None,
            }]}
            for y in years
        },
    }))


def test_without_certificates_the_gap_is_exactly_what_orestar_restated(tmp_path) -> None:
    """The control: no certificate data, no ghost rows, the known -$8,250."""
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    assert detail["cash_on_hand"] == -8250.00
    assert detail["certificate_restatements"] == []


def test_ghost_rows_bring_the_balance_to_orestars_figure(tmp_path) -> None:
    _write_certificates(tmp_path, [2013, 2014, 2015])
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    # ORESTAR's 2017 ending balance, to the cent.
    assert detail["cash_on_hand"] == 0.00
    assert [(r["date"], r["amount"]) for r in detail["certificate_restatements"]] == [
        ("2013-12-31", 2500.00), ("2014-12-31", 2500.00), ("2015-12-31", 3250.00),
    ]
    assert detail["certificate_restatement_total"] == 8250.00


def test_the_timeline_carries_the_ghost_rows_too(tmp_path) -> None:
    """The browser rebuilds cash from the timeline; it must agree with the balance."""
    _write_certificates(tmp_path, [2013, 2014, 2015])
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    assert round(_timeline_net(detail), 2) == detail["cash_on_hand"]


def test_the_year_after_the_certificates_opens_at_orestars_balance(tmp_path) -> None:
    _write_certificates(tmp_path, [2013, 2014, 2015])
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    y2016 = detail["yearly_discrepancies"]["2016"]
    assert y2016["our_begin"] == 9959.94
    assert y2016["orestar_begin"] == 9959.94
    assert y2016["certificate_year"] is False


def test_certificate_years_are_labelled_with_their_restatement(tmp_path) -> None:
    _write_certificates(tmp_path, [2013, 2014, 2015])
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    for year, amount in (("2013", 2500.00), ("2014", 2500.00), ("2015", 3250.00)):
        row = detail["yearly_discrepancies"][year]
        assert row["certificate_year"] is True
        assert row["certificate_restatement"] == amount
    assert sorted(detail["orestar_certificates"]) == ["2013", "2014", "2015"]


def test_ghost_rows_never_count_as_filed_transactions(tmp_path) -> None:
    """Derived rows move cash only: no transaction count, no display totals."""
    _write_certificates(tmp_path, [2013, 2014, 2015])
    detail = _aggregate_cash_rows(tmp_path, ROWS, set(), _yearly())
    assert detail["tran_count"] == len(ROWS)
    assert all(
        not row.get("balance_adjustments")
        for row in detail["timeline"]
    )


def test_a_gap_from_before_the_certificates_passes_through_untouched(tmp_path) -> None:
    """Restatements are anchored on ORESTAR's own opening for each certificate
    year, so a difference that already existed going in is never absorbed.

    Drop a $100 2012 contribution from our rows: before the certificate years
    we are $100 short of ORESTAR, and after them we must still be exactly
    $100 short — not silently reconciled by the ghost rows.
    """
    _write_certificates(tmp_path, [2013, 2014, 2015])
    rows = [dict(r) for r in ROWS]
    rows[0]["amount"] = 6650.00          # was 6,750.00
    detail = _aggregate_cash_rows(tmp_path, rows, set(), _yearly())
    assert detail["cash_on_hand"] == -100.00
    assert detail["certificate_restatement_total"] == 8250.00
