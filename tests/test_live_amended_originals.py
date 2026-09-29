"""Amended originals that ORESTAR never expired, and therefore still counts.

Amending a transaction normally expires the original: ORESTAR's default search
stops returning it and its account summary stops counting it (Eli for Portland
5554636 -> 5573033). Sometimes ORESTAR leaves the original live. Oregon
Firearms Federation PAC's $100 contribution 2041931 was amended eleven minutes
later as 2041946; ORESTAR's own history links them, both are live, both carry a
$200 donor aggregate, and ORESTAR's 2015 contributions count both. Elect Dave
Hoppe's $600 cash expenditure 1672602, re-entered as personal expenditure
1927730, is the same. The balance follows ORESTAR, so these originals are kept,
but only where exact coverage evidence shows ORESTAR still returns them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import process as P  # noqa: E402
from balance_snapshot import exact_coverage_result_shape_is_valid  # noqa: E402
from test_nonreturning_balance_guard import _aggregate_cash_rows, _cash_row  # noqa: E402


def _txn(tran_id, original_id, status, amount, filed, sub_type="Cash Contribution",
         filer_id="3865"):
    return {
        "tran_id": tran_id, "original id": original_id, "tran status": status,
        "amount": amount, "filed_date": filed, "sub_type": sub_type,
        "filer_id": filer_id,
    }


OFF_PAC = pd.DataFrame([
    _txn("2041931", "2041931", "Original", 100.0, "2015-07-20"),
    _txn("2041946", "2041931", "Amended", 100.0, "2015-07-20"),
])


def test_without_evidence_an_amended_original_is_dropped_as_before() -> None:
    kept, removed = P._drop_superseded(OFF_PAC.copy())

    assert list(kept["tran_id"]) == ["2041946"]
    assert removed == {"2041931"}


def test_an_original_orestar_still_returns_is_kept() -> None:
    kept, removed = P._drop_superseded(OFF_PAC.copy(), keep_live={"2041931"})

    assert list(kept["tran_id"]) == ["2041931", "2041946"]
    assert removed == set()


def test_keeping_one_live_original_does_not_keep_expired_ones() -> None:
    frame = pd.concat([OFF_PAC, pd.DataFrame([
        _txn("5554636", "5554636", "Original", 350.0, "2026-02-27", filer_id="23295"),
        _txn("5573033", "5554636", "Amended", 350.0, "2026-03-02", filer_id="23295"),
    ])], ignore_index=True)

    kept, removed = P._drop_superseded(frame, keep_live={"2041931"})

    assert set(kept["tran_id"]) == {"2041931", "2041946", "5573033"}
    assert removed == {"5554636"}


def test_chains_still_keep_only_the_newest_amendment() -> None:
    # Julie for County Commissioner: one $167.41 contribution, three versions.
    frame = pd.DataFrame([
        _txn("1", "1", "Original", 167.41, "2020-01-01"),
        _txn("2", "1", "Amended", 167.41, "2020-01-02"),
        _txn("3", "1", "Amended", 167.41, "2020-01-03"),
    ])

    kept, removed = P._drop_superseded(frame, keep_live=set())

    assert list(kept["tran_id"]) == ["3"]
    assert removed == {"1", "2"}


def _coverage(filer_id="3865", **overrides) -> dict:
    row = {
        "filer_id": filer_id, "name": "Oregon Firearms Federation PAC",
        "complete": True, "orestar": 3906, "held": 3905,
        "missing": [], "surplus": [], "superseded": ["2041931"],
        "live_originals": ["2041931"],
        "evidence_version": 2, "filer_digest_version": 2,
        "filer_transaction_digest": "sha256:state",
    }
    row.update(overrides)
    return row


def test_evidence_comes_from_superseded_and_live_originals() -> None:
    rows = [
        _coverage(),
        # After the original is restored it is held, so it is no longer
        # "superseded" — live_originals is what keeps the evidence alive.
        _coverage(filer_id="15343", held=93, orestar=93, superseded=[],
                  live_originals=["1672602"]),
    ]

    assert P._live_original_ids(rows) == {"2041931": "3865", "1672602": "15343"}


def test_legacy_failed_and_malformed_results_supply_no_evidence() -> None:
    legacy = _coverage()
    del legacy["evidence_version"]
    failed = _coverage(complete=None)
    malformed = _coverage(orestar=5000)              # counts no longer reconcile

    assert P._live_original_ids([legacy, failed, malformed]) == {}


def test_live_original_evidence_is_validated() -> None:
    assert exact_coverage_result_shape_is_valid(_coverage())
    no_field = _coverage()
    del no_field["live_originals"]
    assert exact_coverage_result_shape_is_valid(no_field)
    # Every superseded ID is live by definition.
    assert not exact_coverage_result_shape_is_valid(_coverage(live_originals=[]))
    assert not exact_coverage_result_shape_is_valid(
        _coverage(live_originals=["2041931", "2041931"]))
    assert not exact_coverage_result_shape_is_valid(
        _coverage(live_originals=["2041931", " 7"]))
    # A live original is on ORESTAR, so it cannot be a surplus row.
    assert not exact_coverage_result_shape_is_valid(_coverage(
        complete=False, held=3906, surplus=["2041931"], superseded=[],
        live_originals=["2041931"]))


def test_listing_names_each_original_and_every_amendment() -> None:
    rows = pd.DataFrame([
        {**_txn("1672602", "1672602", "Original", 600.0, "2014-10-22",
                sub_type="Cash Expenditure", filer_id="15343"),
         "tran_date": "03/13/2014", "filer id": "15343"},
        {**_txn("1927730", "1672602", "Amended", 600.0, "2014-11-21",
                sub_type="Personal Expenditure for Reimbursement", filer_id="15343"),
         "tran_date": "03/13/2014", "filer id": "15343"},
        # In the evidence but amended by nothing here: an ordinary row.
        {**_txn("999", "999", "Original", 5.0, "2014-01-01", filer_id="15343"),
         "tran_date": "01/01/2014", "filer id": "15343"},
    ])

    listed = P._live_originals_for(rows, {"1672602": "15343", "999": "15343"})

    assert listed == [{
        "original_id": "1672602",
        "filer_id": "15343",
        "tran_date": "2014-03-13",
        "year": 2014,
        "sub_type": "Cash Expenditure",
        "amount": 600.0,
        "amended": [{
            "tran_id": "1927730",
            "sub_type": "Personal Expenditure for Reimbursement",
            "amount": 600.0,
        }],
    }]


def test_listing_tolerates_frames_without_amendment_columns() -> None:
    assert P._live_originals_for(pd.DataFrame([{"tran_id": "1"}]), {"1": "x"}) == []
    assert P._live_originals_for(pd.DataFrame(), {"1": "x"}) == []
    assert P._live_originals_for(OFF_PAC, {}) == []


def _row(tran_id: str, original_id: str, status: str) -> dict:
    when = pd.Timestamp("2015-07-20")
    row = _cash_row(tran_id, 100.0, "C", "Cash Contribution")
    row.update({
        "filed_date": when, "tran_date": "07/20/2015", "year": 2015,
        "month": "2015-07", "original id": original_id, "tran status": status,
    })
    return row


def test_aggregation_counts_and_names_the_live_original(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "coverage_diff.json").write_text(json.dumps([_coverage(
        filer_id="1", held=2, orestar=2, superseded=[],
        live_originals=["2041931"],
    )]))
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {"2015": {
        "beginning_balance": 0.0, "ending_cash_balance": 200.0,
        "contributions": 200.0, "expenditures": 0.0, "other_receipts": 0.0,
        "other_disbursements": 0.0, "balance_adjustments": 0.0,
    }}}}

    detail = _aggregate_cash_rows(
        tmp_path,
        [_row("2041931", "2041931", "Original"), _row("2041946", "2041931", "Amended")],
        set(),
        yearly,
    )

    # Both versions count, as they do in ORESTAR's 2015 contributions.
    assert detail["cash_on_hand"] == 200.0
    assert detail["orestar_live_originals"] == [{
        "original_id": "2041931",
        "filer_id": "1",
        "tran_date": "2015-07-20",
        "year": 2015,
        "sub_type": "Cash Contribution",
        "amount": 100.0,
        "amended": [{"tran_id": "2041946", "sub_type": "Cash Contribution",
                     "amount": 100.0}],
    }]
    assert detail["yearly_discrepancies"]["2015"]["live_originals"] == ["2041931"]


def test_aggregation_without_evidence_lists_nothing(tmp_path) -> None:
    detail = _aggregate_cash_rows(
        tmp_path,
        [_row("5", "5", "Original")],
        set(),
    )

    assert detail["orestar_live_originals"] == []


def test_selector_restores_a_live_original_we_dropped(tmp_path) -> None:
    from test_identity_remediation import _run_selector

    ids, mode, _end, _resume, status, output = _run_selector(
        tmp_path,
        [{"filer_id": "7", "complete": True, "name": "live original",
          "superseded": ["orig-7"], "live_originals": ["orig-7"]}],
        transaction_rows=[
            {"tran_id": "held-7", "original id": "orig-7", "tran_date": "09/01/2026",
             "filer id": "7", "amount": "1.00"},
        ],
        with_status=True,
    )

    assert ids == "7"
    assert mode == "identity"
    assert status == "selected"
    assert "1 live originals to restore" in output
