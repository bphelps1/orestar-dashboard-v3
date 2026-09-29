"""
Regression tests for Phase 2 dashboard changes.

Tests cover:
  - Global cash-on-hand calculation (sum of per-filer COH)
  - Date-range start uses tran_date, not filed_date
  - Per-filer COH with ORESTAR beginning balances
  - Per-filer COH without ORESTAR data (fallback)
  - Filtered date range COH calculation
  - Filer with beginning balance but no recent contributions
  - Filer with balance adjustments
  - Discrepancy severity thresholds
  - statsFromTimeline logic
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# Add scraper to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))


def run_aggregate(df, mock_orestar=None, tmp_path=None):
    """Run aggregate() with isolated, production-shaped ORESTAR cache files."""
    from process import aggregate

    if mock_orestar is None:
        mock_orestar = {}

    # Use real tmp directories so json.dump works
    agg_dir = tmp_path / "aggregated"
    filers_dir = agg_dir / "filers"
    filers_dir.mkdir(parents=True, exist_ok=True)
    trans_dir = tmp_path / "transactions"
    trans_dir.mkdir(parents=True, exist_ok=True)

    # Production no longer calls the retired scrape_account_summaries helper
    # during aggregation. Account pages are collected separately and consumed
    # from the yearly cache, with a proven earliest-page anchor alongside it.
    yearly = {}
    earliest = {}
    for fid, row in mock_orestar.items():
        year = int(row.get("year") or 0)
        if year:
            summary = {
                "beginning_balance": row.get("beginning_balance", 0.0),
                "ending_cash_balance": row.get("ending_cash_balance", 0.0),
                "contributions": row.get("orestar_contributions", 0.0),
                "expenditures": row.get("orestar_expenditures", 0.0),
                "other_receipts": row.get("orestar_other_receipts", 0.0),
                "other_disbursements": row.get("orestar_other_disbursements", 0.0),
                "balance_adjustments": row.get("balance_adjustments", 0.0),
                "scrape_ts": row.get("ts", 0),
            }
            yearly[str(fid)] = {
                "years": {str(year): summary},
                "ts": row.get("ts", 0),
            }
            earliest[str(fid)] = {
                "earliest_year": year,
                "beginning_balance": row.get("beginning_balance", 0.0),
                "reached_earliest": True,
                "ts": row.get("ts", 0),
            }
    (tmp_path / "orestar_yearly_summaries.json").write_text(json.dumps(yearly))
    (tmp_path / "earliest_balances.json").write_text(json.dumps(earliest))

    with patch("process.AGG_DIR", agg_dir), \
         patch("process.DATA_DIR", tmp_path), \
         patch("process.TRANS_DIR", trans_dir), \
         patch("process.shutil"):
        aggregate(df)

    # Read back all written files
    result = {}
    for p in agg_dir.rglob("*.json"):
        rel = str(p.relative_to(agg_dir))
        with open(p) as f:
            result[rel] = json.load(f)
    return result


# ---------------------------------------------------------------------------
# Test: discrepancy severity thresholds
# ---------------------------------------------------------------------------

def test_discrepancy_severity():
    """Severity colors match spec: <$500 gray, $500-$2000 yellow, >$2000 red."""
    def severity(abs_disc):
        if abs_disc < 500:
            return "gray"
        if abs_disc <= 2000:
            return "yellow"
        return "red"

    assert severity(0) == "gray"
    assert severity(499.99) == "gray"
    assert severity(500) == "yellow"
    assert severity(1000) == "yellow"
    assert severity(2000) == "yellow"
    assert severity(2000.01) == "red"
    assert severity(50000) == "red"


# ---------------------------------------------------------------------------
# Test: date_range_start uses tran_date
# ---------------------------------------------------------------------------

def test_date_range_start_uses_tran_date(tmp_path):
    """date_range_start should use tran_date (with filed_date fallback), not filed_date alone."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2006-12-31", "2007-01-15"],
        "tran_date": ["2006-01-05", "2007-01-10"],
        "amount": [100.0, 200.0],
        "tran_type": ["C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Donor B"],
        "filer": ["Committee X", "Committee X"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    summary = result["summary.json"]
    # tran_date min is 2006-01-05, not filed_date min 2006-12-31
    assert summary["date_range_start"] == "2006-01-05"


# ---------------------------------------------------------------------------
# Test: per-filer COH with ORESTAR beginning balance
# ---------------------------------------------------------------------------

def test_per_filer_coh_with_orestar(tmp_path):
    """COH should use ORESTAR beginning balance as anchor."""
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3"],
        "filed_date": ["2025-03-01", "2025-06-01", "2026-02-01"],
        "tran_date": ["2025-03-01", "2025-06-01", "2026-02-01"],
        "amount": [10000.0, 5000.0, 3000.0],
        "tran_type": ["C", "E", "C"],
        "sub_type": ["Cash Contribution", "Cash Expenditure", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Vendor B", "Donor C"],
        "filer": ["Test Committee", "Test Committee", "Test Committee"],
        "filer id": ["12345", "12345", "12345"],
    })

    mock_orestar = {
        "12345": {
            "year": 2026,
            "beginning_balance": 50000.0,
            "ending_cash_balance": 53000.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    # Find filer detail
    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = anchor_beginning + net_from_anchor_onward = 50000 + 3000 = 53000
    assert filer_data["cash_on_hand"] == 53000.0
    # ORESTAR supplies the opening anchor/check; the ending balance remains a
    # transaction calculation and is never silently replaced by ORESTAR.
    assert filer_data["cash_on_hand_source"] == "calculated"
    assert filer_data["orestar_account_summary"]["scrape_ts"] == 1711500000
    assert filer_data["beginning_balances"] == {"2026": 50000.0}
    timeline_by_month = {
        row["month"]: row for row in filer_data["timeline"]
    }
    # The 2025 history remains visible, but the official 2026 opening replaces
    # it as the start of the cash trajectory.
    assert timeline_by_month["2025-03"]["contributions"] == 10000.0
    assert timeline_by_month["2025-03"]["cash_balance_net"] == 0.0
    assert timeline_by_month["2025-06"]["cash_balance_net"] == 0.0
    assert timeline_by_month["2026-02"]["cash_balance_net"] == 3000.0

    global_by_month = {
        row["month"]: row for row in result["timeline.json"]
    }
    assert global_by_month.get("2025-01", {}).get("cash_balance_net", 0) == 0.0
    assert global_by_month["2026-01"]["cash_balance_net"] == 50000.0
    assert sum(row["cash_balance_net"] for row in result["timeline.json"]) == 53000.0


def test_per_filer_coh_without_orestar(tmp_path):
    """COH should fall back to transaction-based calculation."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2025-03-01", "2025-06-01"],
        "tran_date": ["2025-03-01", "2025-06-01"],
        "amount": [10000.0, 3000.0],
        "tran_type": ["C", "E"],
        "sub_type": ["Cash Contribution", "Cash Expenditure"],
        "contributor_payee": ["Donor A", "Vendor B"],
        "filer": ["No ORESTAR Committee", "No ORESTAR Committee"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = 10000 - 3000 = 7000 (no beginning balance)
    assert filer_data["cash_on_hand"] == 7000.0
    assert filer_data["cash_on_hand_source"] == "calculated"


# ---------------------------------------------------------------------------
# Test: global COH = sum of per-filer COH values
# ---------------------------------------------------------------------------

def test_global_coh_is_sum_of_filers(tmp_path):
    """Global cash_on_hand should be sum of per-filer values."""
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3", "4"],
        "filed_date": ["2025-01-01", "2025-01-01", "2025-01-01", "2025-01-01"],
        "tran_date": ["2025-01-01", "2025-01-01", "2025-01-01", "2025-01-01"],
        "amount": [1000.0, 500.0, 2000.0, 800.0],
        "tran_type": ["C", "E", "C", "E"],
        "sub_type": ["Cash Contribution", "Cash Expenditure", "Cash Contribution", "Cash Expenditure"],
        "contributor_payee": ["Donor A", "Vendor B", "Donor C", "Vendor D"],
        "filer": ["Committee Alpha", "Committee Alpha", "Committee Beta", "Committee Beta"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    summary = result["summary.json"]

    # Alpha COH = 1000 - 500 = 500
    # Beta COH = 2000 - 800 = 1200
    # Global COH = 500 + 1200 = 1700
    assert summary["global_cash_on_hand"] == 1700.0
    assert "global_beginning_balances" in summary
    assert sum(row["cash_balance_net"] for row in result["timeline.json"]) == 1700.0


# ---------------------------------------------------------------------------
# Test: filer with beginning balance but no recent contributions
# ---------------------------------------------------------------------------

def test_filer_with_only_beginning_balance(tmp_path):
    """A filer with an ORESTAR beginning balance but no contributions should show correct COH."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2026-01-15"],
        "tran_date": ["2026-01-15"],
        "amount": [100.0],
        "tran_type": ["E"],
        "sub_type": ["Cash Expenditure"],
        "contributor_payee": ["Vendor X"],
        "filer": ["Dormant Committee"],
        "filer id": ["99999"],
    })

    mock_orestar = {
        "99999": {
            "year": 2026,
            "beginning_balance": 5000.0,
            "ending_cash_balance": 4900.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = 5000 (beginning) - 100 (expenditure) = 4900
    assert filer_data["cash_on_hand"] == 4900.0


# ---------------------------------------------------------------------------
# Test: statsFromTimeline (frontend logic replicated in Python)
# ---------------------------------------------------------------------------

def test_stats_from_timeline_with_beginning_balances():
    """Replicate the JS statsFromTimeline logic."""
    rows = [
        {"month": "2025-01", "contributions": 5000, "inkind": 0, "expenditures": 2000,
         "other_receipts": 100, "other_disbursements": 50, "count": 10},
        {"month": "2025-06", "contributions": 3000, "inkind": 0, "expenditures": 1000,
         "other_receipts": 0, "other_disbursements": 0, "count": 5},
    ]
    beginning_balances = {"2025": 10000.0, "2024": 8000.0}

    total_in = sum(r["contributions"] for r in rows)
    total_out = sum(r["expenditures"] for r in rows)
    total_or = sum(r["other_receipts"] for r in rows)
    total_od = sum(r["other_disbursements"] for r in rows)

    earliest_year = rows[0]["month"][:4]
    begin_bal = beginning_balances.get(earliest_year, 0)

    net_flow = total_in + total_or - total_out - total_od
    cash_on_hand = round((begin_bal + net_flow) * 100) / 100

    assert cash_on_hand == 15050.0


def test_stats_from_timeline_filtered_range():
    """When filtering to a date range, beginning balance should match the filtered start year."""
    rows = [
        {"month": "2024-06", "contributions": 1000, "inkind": 0, "expenditures": 500,
         "other_receipts": 0, "other_disbursements": 0, "count": 3},
    ]
    beginning_balances = {"2023": 5000.0, "2024": 8000.0}

    earliest_year = rows[0]["month"][:4]
    begin_bal = beginning_balances.get(earliest_year, 0)
    net_flow = 1000 - 500
    cash_on_hand = round((begin_bal + net_flow) * 100) / 100

    assert cash_on_hand == 8500.0


# ---------------------------------------------------------------------------
# Test: filer with balance adjustments
# ---------------------------------------------------------------------------

def test_balance_adjustments_in_account_summary(tmp_path):
    """Balance adjustments should be included in filer's account summary data."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2026-01-15"],
        "tran_date": ["2026-01-15"],
        "amount": [1000.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["Donor A"],
        "filer": ["Adj Committee"],
        "filer id": ["77777"],
    })

    mock_orestar = {
        "77777": {
            "year": 2026,
            "beginning_balance": 2000.0,
            "ending_cash_balance": 3500.0,
            "balance_adjustments": 500.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    acct = filer_data.get("orestar_account_summary", {})
    assert acct.get("balance_adjustments") == 500.0
    assert acct.get("ending_cash_balance") == 3500.0
    assert acct.get("scrape_ts") == 1711500000


# ---------------------------------------------------------------------------
# Test: global_beginning_balances accumulated correctly
# ---------------------------------------------------------------------------

def test_global_beginning_balances_aggregated(tmp_path):
    """Global beginning balances should be sum of per-filer beginning balances per year."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2025-06-01", "2025-06-01"],
        "tran_date": ["2025-06-01", "2025-06-01"],
        "amount": [1000.0, 2000.0],
        "tran_type": ["C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Donor B"],
        "filer": ["Committee One", "Committee Two"],
        "filer id": ["111", "222"],
    })

    mock_orestar = {
        "111": {"year": 2025, "beginning_balance": 5000.0, "ending_cash_balance": 6000.0, "ts": 1},
        "222": {"year": 2025, "beginning_balance": 3000.0, "ending_cash_balance": 5000.0, "ts": 1},
    }

    result = run_aggregate(df, mock_orestar, tmp_path)
    summary = result["summary.json"]
    global_bb = summary["global_beginning_balances"]
    # Both filers have 2025 beginning balances: 5000 + 3000 = 8000
    assert global_bb["2025"] == 8000.0
    # Global timeline cash includes both opening anchors, so the browser can
    # roll forward correctly without pretending every committee began in the
    # first global year.
    assert sum(row["cash_balance_net"] for row in result["timeline.json"]) == 11000.0
    assert summary["global_cash_on_hand"] == 11000.0
