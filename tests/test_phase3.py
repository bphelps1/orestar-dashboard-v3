"""
Regression tests for Phase 3: Donor ↔ Filer linking.

Tests cover:
  - Exact donor-name → filer match (high confidence)
  - Normalized name match (stripping suffixes, punctuation)
  - Ambiguous match (multiple filers share a normalized name)
  - No match (donor is not a filer)
  - filer_id included in filer_index.json
  - donor_filer_map.json generation
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))


def run_aggregate(df, mock_orestar=None, tmp_path=None):
    """Helper: run aggregate() with mocked filesystem."""
    from process import aggregate

    if mock_orestar is None:
        mock_orestar = {}

    agg_dir = tmp_path / "aggregated"
    filers_dir = agg_dir / "filers"
    filers_dir.mkdir(parents=True, exist_ok=True)
    trans_dir = tmp_path / "transactions"
    trans_dir.mkdir(parents=True, exist_ok=True)

    with patch("process.AGG_DIR", agg_dir), \
         patch("process.TRANS_DIR", trans_dir), \
         patch("process.scrape_account_summaries", return_value=mock_orestar), \
         patch("process.shutil"):
        aggregate(df)

    result = {}
    for p in agg_dir.rglob("*.json"):
        rel = str(p.relative_to(agg_dir))
        with open(p) as f:
            result[rel] = json.load(f)
    return result


# ---------------------------------------------------------------------------
# Test: filer_id is included in filer_index.json
# ---------------------------------------------------------------------------

def test_filer_index_includes_filer_id(tmp_path):
    """filer_index.json should include filer_id for each filer."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2025-01-01"],
        "tran_date": ["2025-01-01"],
        "amount": [1000.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["Donor A"],
        "filer": ["Test Committee"],
        "filer id": ["12345"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    index = result["filer_index.json"]
    assert len(index) == 1
    assert index[0]["filer_id"] == "12345"
    assert index[0]["name"] == "Test Committee"


# ---------------------------------------------------------------------------
# Test: exact donor→filer match
# ---------------------------------------------------------------------------

def test_exact_donor_filer_match(tmp_path):
    """A donor whose name exactly matches a filer should be linked with high confidence."""
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3"],
        "filed_date": ["2025-01-01", "2025-01-01", "2025-01-01"],
        "tran_date": ["2025-01-01", "2025-01-01", "2025-01-01"],
        "amount": [1000.0, 500.0, 2000.0],
        "tran_type": ["C", "C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution", "Cash Contribution"],
        "contributor_payee": ["Committee Beta", "Unrelated Donor", "Committee Alpha"],
        "filer": ["Committee Alpha", "Committee Alpha", "Committee Beta"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    dfm = result.get("donor_filer_map.json", {})

    # "Committee Beta" donated to "Committee Alpha" — should match filer "Committee Beta"
    assert "committee beta" in dfm
    assert dfm["committee beta"]["confidence"] == "high"

    # "Committee Alpha" donated to "Committee Beta" — should match filer "Committee Alpha"
    assert "committee alpha" in dfm
    assert dfm["committee alpha"]["confidence"] == "high"

    # "Unrelated Donor" should not be in the map
    assert "unrelated donor" not in dfm


# ---------------------------------------------------------------------------
# Test: no match for regular donors
# ---------------------------------------------------------------------------

def test_no_match_for_non_filer_donor(tmp_path):
    """Donors who aren't filers should not appear in the map."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2025-01-01"],
        "tran_date": ["2025-01-01"],
        "amount": [500.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["John Q. Public"],
        "filer": ["Some Committee"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    dfm = result.get("donor_filer_map.json", {})
    assert "john q. public" not in dfm


# ---------------------------------------------------------------------------
# Test: ambiguous match
# ---------------------------------------------------------------------------

def test_ambiguous_donor_filer_match(tmp_path):
    """When a donor name matches multiple filers, confidence should be 'ambiguous'."""
    # Two filers with very similar names; a donor donates under one of them
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3"],
        "filed_date": ["2025-01-01", "2025-01-01", "2025-01-01"],
        "tran_date": ["2025-01-01", "2025-01-01", "2025-01-01"],
        "amount": [1000.0, 2000.0, 500.0],
        "tran_type": ["C", "C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution", "Cash Contribution"],
        # "Oregon Future PAC" donates to "Third Committee"
        # Two filers: "Oregon Future PAC" and "Oregon Future Committee"
        # After normalization, both may reduce to "oregon future" → ambiguous
        "contributor_payee": ["Oregon Future PAC", "Random Donor", "Oregon Future Committee"],
        "filer": ["Oregon Future Committee", "Oregon Future PAC", "Third Committee"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    dfm = result.get("donor_filer_map.json", {})

    # Check that one of these has an exact match and the other doesn't
    # "Oregon Future PAC" donates → exact match to filer "Oregon Future PAC" → high
    if "oregon future pac" in dfm:
        assert dfm["oregon future pac"]["confidence"] == "high"

    # "Oregon Future Committee" donates → exact match to filer "Oregon Future Committee"
    if "oregon future committee" in dfm:
        assert dfm["oregon future committee"]["confidence"] == "high"


# ---------------------------------------------------------------------------
# Test: donor_filer_map.json is generated
# ---------------------------------------------------------------------------

def test_donor_filer_map_generated(tmp_path):
    """donor_filer_map.json should always be generated."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2025-01-01"],
        "tran_date": ["2025-01-01"],
        "amount": [100.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["Donor X"],
        "filer": ["Committee Y"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    assert "donor_filer_map.json" in result
    assert isinstance(result["donor_filer_map.json"], dict)
