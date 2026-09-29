"""Exact identity compatibility and the narrow generated-label exclusion."""

import csv
import gzip
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
import balance_snapshot as BS


ROW = {
    "tran_id": "100.0", "filer id": "10.0", "original id": "90.0",
    "tran_date": "09/01/2026", "filed_date": "09/02/2026", "amount": "25.00",
    "tran_type": "C", "sub_type": "Cash Contribution",
    "contributor_payee": "Raw person", "contributor_payee_canonical": "Derived person",
    "_source_file": "export.xlsx", "extra_source_field": "retained",
}


def snapshot(tmp_path, row, version):
    with gzip.open(tmp_path / "txn_2026.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    return BS.transaction_filer_snapshots(
        tmp_path, ["10"], date(2006, 1, 1), date(2026, 9, 15),
        digest_version=version,
    )["10"]


def test_v1_matches_golden_digest_from_merged_317c86e(tmp_path):
    result = snapshot(tmp_path, ROW, 1)
    # Recorded with the untouched production function at merged PR10.
    assert result["filer_transaction_digest"] == (
        "sha256:b8d1e8bb5bb914dd88c3573b9fcf1d2b03d95e1c729d1fb3a8007b2afe5e9db7"
    )
    assert result["filer_digest_version"] == 1
    assert result["held_ids"] == {"100"}
    assert result["superseded_ids"] == {"90"}


def test_generated_label_is_excluded_only_from_v2(tmp_path):
    before = {v: snapshot(tmp_path, ROW, v) for v in (1, 2)}
    renamed = {**ROW, "contributor_payee_canonical": "New generated label"}
    after = {v: snapshot(tmp_path, renamed, v) for v in (1, 2)}
    assert before[1]["filer_transaction_digest"] != after[1]["filer_transaction_digest"]
    assert before[2] == after[2]
    assert before[1]["filer_transaction_digest"] != before[2]["filer_transaction_digest"]


@pytest.mark.parametrize("field,value", [
    ("tran_id", "101"), ("filer id", "11"), ("original id", "80"),
    ("tran_date", "09/03/2026"), ("filed_date", "09/04/2026"),
    ("amount", "26.00"), ("tran_type", "E"), ("sub_type", "Cash Expenditure"),
    ("contributor_payee", "New raw payee"), ("extra_source_field", "changed"),
])
def test_every_source_field_remains_in_v2_identity(tmp_path, field, value):
    before = snapshot(tmp_path, ROW, 2)
    after = snapshot(tmp_path, {**ROW, field: value}, 2)
    assert before["filer_transaction_digest"] != after["filer_transaction_digest"]


@pytest.mark.parametrize("version", [None, True, False, 0, 3, 1.0, "1", "2", [], {}])
def test_explicit_unknown_version_is_rejected(tmp_path, version):
    assert BS.exact_filer_digest_version({"filer_digest_version": version}) is None
    with pytest.raises(ValueError, match="unsupported filer digest version"):
        snapshot(tmp_path, ROW, version)


def test_missing_version_is_legacy_without_mutating_observation():
    row = {"filer_transaction_digest": "sha256:legacy"}
    assert BS.exact_filer_digest_version(row) == 1
    assert row == {"filer_transaction_digest": "sha256:legacy"}


def test_algorithm_constraint_cannot_relabel_an_observation():
    row = {"checked_at": "2026-09-15T12:00:01Z", "filer_transaction_digest": "sha256:same"}
    assert BS.evidence_is_current(row, 0, filer_digest_version=1)
    assert not BS.evidence_is_current(row, 0, filer_digest_version=2)
    assert not BS.evidence_is_current({**row, "filer_digest_version": 2}, 0,
                                      filer_digest_version=1)
