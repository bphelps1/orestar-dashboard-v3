"""Boundary whitespace must not split one canonical committee or its capture."""

from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import balance_snapshot as BS
import generate_activity_snapshot
import process as P


NAME = "Friends of Sandeep Bali"
MEMBERS = ["21888", "23092"]


def row(fid, tid, name, amount, filed="2026-01-02"):
    return {
        "filer id": fid, "tran_id": tid, "original id": tid, "filer": name,
        "contributor_payee": "Donor", "filed_date": filed,
        "tran_date": "2026-01-01", "tran_type": "C", "sub_type": "Cash Contribution",
        "amount": amount, "book_type": "Individual", "state": "OR",
    }


@pytest.fixture
def aggregate_env(monkeypatch, tmp_path):
    data = tmp_path / "data"
    aggregated = data / "aggregated"
    transactions = data / "transactions"
    transactions.mkdir(parents=True)
    aggregated.mkdir()
    monkeypatch.setattr(P, "DATA_DIR", data)
    monkeypatch.setattr(P, "AGG_DIR", aggregated)
    monkeypatch.setattr(P, "TRANS_DIR", transactions)
    monkeypatch.setattr(P, "COMMITTEES", data / "committees.csv")
    monkeypatch.setattr(P.supabase_sync, "upsert_dashboard_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(P.supabase_sync, "bulk_upsert_filer_detail", lambda *_a, **_k: None)
    monkeypatch.setattr(P.supabase_sync, "get_dashboard_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(generate_activity_snapshot, "generate", lambda *_a, **_k: {
        "meta": {"total_candidates": 0}, "legislative_map": {},
    })

    def run(frame):
        original = frame.copy(deep=True)
        with gzip.open(transactions / "txn_2026.csv.gz", "wt", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=frame.columns)
            writer.writeheader()
            writer.writerows(frame.to_dict(orient="records"))
        P.aggregate(frame)
        pd.testing.assert_frame_equal(frame, original)
        source = json.loads((aggregated / BS.SOURCE_FILENAME).read_text())
        report = json.loads((aggregated / "balance_discrepancies.json").read_text())
        return source, report

    return data, aggregated, run


@pytest.mark.parametrize("variant", [NAME + " ", "  " + NAME, "\t" + NAME + "\n", NAME + "\u00a0"])
def test_latest_filer_name_trims_boundaries_without_merging_distinct_names(aggregate_env, variant):
    _data, aggregated, run = aggregate_env
    frame = pd.DataFrame([
        row("21888", "1", "Former Committee Name", 10, "2026-01-01"),
        row("21888", "2", variant, 20),
        row("23092", "3", NAME, 30),
        row("999", "4", "Friends for Sandeep Bali", 5),
    ])
    source, _report = run(frame)
    assert set(source["scopes"]) == {"21888|23092", "999"}
    combined = source["scopes"]["21888|23092"]
    assert combined["name"] == NAME
    assert combined["filer_ids"] == MEMBERS
    assert combined["tran_count"] == 3
    assert combined["cash_on_hand"] == 60
    assert source["scopes"]["999"]["name"] == "Friends for Sandeep Bali"
    index = json.loads((aggregated / "filer_index.json").read_text())
    assert {item["name"] for item in index} == {NAME, "Friends for Sandeep Bali"}


@pytest.mark.parametrize("missing_member", [False, True])
def test_whitespace_reaggregation_preserves_real_capture_scope_and_digest(
    aggregate_env, missing_member,
):
    data, aggregated, run = aggregate_env
    frame = pd.DataFrame([row("21888", "1", NAME, 40), row("23092", "2", NAME, 60)])
    before, _report = run(frame)
    captured_at = datetime.now(timezone.utc).timestamp()
    yearly = {}
    for fid, cash in [("21888", 40), ("23092", 60)]:
        summary = {"beginning_balance": 0, "ending_cash_balance": cash,
                   "contributions": cash, "expenditures": 0,
                   "other_receipts": 0, "other_disbursements": 0, "balance_adjustments": 0}
        capture = BS.make_summary_capture(fid, 2026, summary, captured_at,
                                          before, before["transaction_snapshot_id"])
        capture["scope_capture_id"] = "21888|23092@original-genuine-capture"
        summary.update(scrape_ts=captured_at, scope_capture_id=capture["scope_capture_id"],
                       calculation_version=BS.CALCULATION_VERSION,
                       app_year_transaction_digest=capture["app_year_transaction_digest"])
        yearly[fid] = {"ts": captured_at, "years": {"2026": summary},
                       "comparison_capture": capture}
    if missing_member:
        yearly["23092"].pop("comparison_capture")
    cache = data / "orestar_yearly_summaries.json"
    cache.write_text(json.dumps(yearly))
    original_cache = cache.read_bytes()
    frame.loc[frame["filer id"].eq("21888"), "filer"] = NAME + " "
    after, report = run(frame)
    assert after["transaction_snapshot_id"] != before["transaction_snapshot_id"]
    assert set(after["scopes"]) == {"21888|23092"}
    assert after["scopes"]["21888|23092"] == before["scopes"]["21888|23092"]
    assert cache.read_bytes() == original_cache  # Reaggregation cannot rebase a capture.
    detail = json.loads((aggregated / "filers" / "friends_of_sandeep_bali.json").read_text())
    comparison = detail["orestar_comparison"]
    assert comparison["status"] == ("legacy_unpaired" if missing_member else "paired")
    if not missing_member:
        assert comparison["app_transaction_snapshot_id"] == before["transaction_snapshot_id"]
        assert comparison["scope_digest_matches_capture"] is True
        assert comparison["filer_ids"] == MEMBERS
        assert comparison["delta_at_capture"] == 0
        assert report["unpaired"] == 0
    else:
        assert report["unpaired"] == 1
