"""Amount-aware coverage diffs.

ORESTAR edits lumped "Miscellaneous ... $100 and under" rows in place: the Tran
ID, status and filed date stay the same while the amount changes. Eli for
Portland 5675002 went from $340 to $830 after we copied it, and seven such rows
were its whole -$800 gap, yet the identity diff reported the committee complete
(1,000 held, 1,000 on ORESTAR). These tests pin the amount comparison that
closes that blind spot. None of them touch ORESTAR or a database.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import diff_coverage as DC  # noqa: E402
import process as P  # noqa: E402
from balance_snapshot import (  # noqa: E402
    exact_coverage_result_shape_is_valid,
    parse_transaction_amount,
    transaction_filer_snapshots,
)
from test_identity_remediation import _run_selector  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ("340.0", 340.0),
    ("340", 340.0),
    ("$1,234.50", 1234.5),
    ("($68.50)", -68.5),
    ("-68.5", -68.5),
    (159.99, 159.99),
    (0.1 + 0.2, 0.3),
    ("", None),
    ("nan", None),
    (float("nan"), None),
    (None, None),
    (True, None),
    ("abc", None),
])
def test_amounts_parse_from_every_format_orestar_and_shards_use(raw, expected) -> None:
    assert parse_transaction_amount(raw) == expected


def test_export_rows_carry_amounts_and_skip_unreadable_ones() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tran Id", "Tran Date", "Amount"])
    sheet.append([5675002, "05/28/2026", 830])
    sheet.append([5524881, "01/30/2026", "195.00"])
    sheet.append([5686507, "06/04/2026", None])
    payload = io.BytesIO()
    workbook.save(payload)

    assert DC._parse_export_rows(payload.getvalue()) == {
        "5675002": {"amount": 830.0},
        "5524881": {"amount": 195.0},
        "5686507": {},
    }


def test_export_amount_header_may_be_spelled_tran_amount() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Transaction ID", "Tran Amount"])
    sheet.append([5637543, 200])
    payload = io.BytesIO()
    workbook.save(payload)

    assert DC._parse_export_rows(payload.getvalue()) == {"5637543": {"amount": 200.0}}


def test_drift_lists_same_id_rows_whose_amount_moved() -> None:
    theirs = {
        "5675002": {"amount": 830.0},   # grew in place
        "5686514": {"amount": 60.0},    # grew in place
        "5410718": {"amount": 50.0},    # shrank in place (CM Hall)
        "5505204": {"amount": 350.0},   # unchanged
        "9999999": {"amount": 25.0},    # missing: not ours to compare
        "5686507": {},                  # ORESTAR amount unreadable
    }
    held = {
        "5675002": 340.0,
        "5686514": 35.0,
        "5410718": 100.0,
        "5505204": 350.0,
        "5686507": 65.0,
        "1111111": 10.0,                # surplus: not on ORESTAR
    }

    changed, checked = DC._amount_drift(theirs, held)

    assert changed == [
        {"tran_id": "5410718", "held": 100.0, "orestar": 50.0},
        {"tran_id": "5675002", "held": 340.0, "orestar": 830.0},
        {"tran_id": "5686514", "held": 35.0, "orestar": 60.0},
    ]
    # Only rows with a readable amount on both sides were compared.
    assert checked == 4
    assert sum(i["orestar"] - i["held"] for i in changed) == pytest.approx(465.0)


def test_paging_rows_compare_the_same_way_as_export_rows() -> None:
    text = "5675002\t05/28/2026\tOriginal\tEli for Portland\tMisc\tCash Contribution\t$830.00"
    theirs = DC._parse_rows(text)

    changed, checked = DC._amount_drift(theirs, {"5675002": 340.0})

    assert checked == 1
    assert changed == [{"tran_id": "5675002", "held": 340.0, "orestar": 830.0}]


def test_cent_rounding_noise_is_not_drift() -> None:
    changed, checked = DC._amount_drift(
        {"1": {"amount": 0.30000000000000004}}, {"1": 0.3},
    )
    assert (changed, checked) == ([], 1)


def test_local_snapshot_reports_each_held_rows_amount(tmp_path) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    with gzip.open(transactions / "txn_2026.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "tran_id", "original id", "tran_date", "filer id", "amount",
        ])
        writer.writeheader()
        writer.writerows([
            {"tran_id": "5675002", "original id": "5675002",
             "tran_date": "05/28/2026", "filer id": "23295", "amount": "340.0"},
            {"tran_id": "5686507", "original id": "5686507",
             "tran_date": "06/04/2026", "filer id": "23295", "amount": ""},
        ])

    snapshot = transaction_filer_snapshots(
        transactions, ["23295"], date(2006, 1, 1), date(2026, 9, 18),
    )["23295"]

    assert snapshot["held_amounts"] == {"5675002": 340.0, "5686507": None}


def _result(**overrides) -> dict:
    row = {
        "filer_id": "23295",
        "complete": True,
        "orestar": 3,
        "held": 3,
        "missing": [],
        "surplus": [],
        "superseded": [],
        "filer_transaction_digest": "sha256:abc",
        "filer_digest_version": 2,
        "amount_changed": [{"tran_id": "5675002", "held": 340.0, "orestar": 830.0}],
        "amount_checked": 3,
    }
    row.update(overrides)
    return row


def test_result_shape_accepts_coherent_amount_evidence() -> None:
    assert exact_coverage_result_shape_is_valid(_result())
    assert exact_coverage_result_shape_is_valid(_result(amount_changed=[]))


def test_results_from_before_amounts_were_recorded_stay_valid() -> None:
    legacy = _result()
    del legacy["amount_changed"], legacy["amount_checked"]
    assert exact_coverage_result_shape_is_valid(legacy)


@pytest.mark.parametrize("overrides", [
    {"amount_checked": 0},                                   # fewer checked than changed
    {"amount_checked": 4},                                   # more checked than held
    {"amount_checked": "3"},
    {"amount_changed": "5675002"},
    {"amount_changed": [{"tran_id": "5675002", "held": 340.0, "orestar": 340.0}]},
    {"amount_changed": [{"tran_id": "5675002", "held": True, "orestar": 830.0}]},
    {"amount_changed": [{"tran_id": " 5675002", "held": 340.0, "orestar": 830.0}]},
    {"amount_changed": [
        {"tran_id": "5675002", "held": 340.0, "orestar": 830.0},
        {"tran_id": "5675002", "held": 340.0, "orestar": 830.0},
    ]},
    # A changed row is a held row; it cannot also be surplus.
    {"complete": False, "surplus": ["5675002"], "orestar": 2},
])
def test_result_shape_refuses_incoherent_amount_evidence(overrides) -> None:
    assert not exact_coverage_result_shape_is_valid(_result(**overrides))


def test_result_shape_refuses_half_of_the_amount_evidence() -> None:
    only_changed = _result()
    del only_changed["amount_checked"]
    only_checked = _result()
    del only_checked["amount_changed"]
    assert not exact_coverage_result_shape_is_valid(only_changed)
    assert not exact_coverage_result_shape_is_valid(only_checked)


def test_identity_complete_committee_with_drift_is_not_rows_complete(
    monkeypatch, tmp_path,
) -> None:
    drifted = {**_result(), "evidence_version": 2}
    clean = {**_result(filer_id="1", amount_changed=[]), "evidence_version": 2}
    (tmp_path / "coverage_diff.json").write_text(json.dumps([drifted, clean]))
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)

    complete, _rows = P._row_diff()

    # The identity verdict in the file is unchanged; only the balance-facing
    # verdict refuses to call drifted rows complete.
    assert drifted["complete"] is True
    assert complete["23295"][0] is False
    assert complete["1"][0] is True


def test_usable_history_keeps_the_amount_evidence() -> None:
    record = DC._usable_history_record(_result())
    assert record["amount_changed"] == _result()["amount_changed"]
    assert record["amount_checked"] == 3


def test_selector_repairs_a_committee_whose_only_problem_is_drift(tmp_path) -> None:
    ids, mode, end, _resume, status, output = _run_selector(
        tmp_path,
        [{
            "filer_id": "7",
            "complete": True,
            "name": "drifted",
            "amount_changed": [{"tran_id": "held-7", "held": 1.0, "orestar": 5.0}],
            "amount_checked": 1,
        }],
        with_status=True,
    )

    assert ids == "7"
    assert mode == "identity"
    assert status == "selected"
    assert end == "2026-09-02"
    assert "0 exact IDs missing, 1 amounts changed, 0 live originals to restore" in output


def test_selector_leaves_a_clean_priced_committee_alone(tmp_path) -> None:
    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{
            "filer_id": "7",
            "complete": True,
            "name": "clean",
            "amount_changed": [],
            "amount_checked": 1,
        }],
        with_status=True,
    )

    assert ids is None
    assert status == "idle"
