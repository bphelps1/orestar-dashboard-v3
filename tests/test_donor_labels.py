"""Regression coverage for donor grouping, including pre-ranking merges."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

import process as P  # noqa: E402
from donor_labels import (  # noqa: E402
    MISC_CASH_LABEL,
    build_donor_label_map,
    donor_label_key,
    normalize_donor_label,
)


def test_label_normalization_is_conservative_and_stable():
    variants = ["  FRIENDS  OF JULIE FAHEY ", "friends of julie fahey", "Friends of Julie Fahey"]
    mapping = build_donor_label_map(variants)

    assert set(map(donor_label_key, variants)) == {"friends of julie fahey"}
    assert mapping == {"friends of julie fahey": "Friends of Julie Fahey"}
    assert build_donor_label_map(reversed(variants)) == mapping
    assert donor_label_key("Friends of Julie Fahey") != donor_label_key("Friends for Julie Fahey")
    assert donor_label_key("ACME, Inc.") != donor_label_key("ACME Inc")
    assert normalize_donor_label(" \tMiscellaneous  Cash Contributions $100 AND UNDER\u00a0") == MISC_CASH_LABEL
    assert normalize_donor_label(None) == ""


def test_missing_historical_canonical_names_fall_back_to_raw_without_mutation():
    frame = pd.DataFrame({
        "contributor_payee_canonical": [None, " ", float("nan"), "Friends of Julie Fahey"],
        "contributor_payee": ["Miscellaneous Cash Contributions $100 and under ",
                              "miscellaneous cash contributions $100 and under", "Raw Donor", "Original Name"],
    })
    original = frame.copy(deep=True)

    assert P._donor_grouping_labels(frame).tolist() == [MISC_CASH_LABEL, MISC_CASH_LABEL,
                                                       "Raw Donor", "Friends of Julie Fahey"]
    pd.testing.assert_frame_equal(frame, original)


def test_every_donor_output_groups_variants_before_ranking(tmp_path, monkeypatch):
    aggregate_dir = tmp_path / "aggregated"
    aggregate_dir.mkdir()
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)
    monkeypatch.setattr(P, "AGG_DIR", aggregate_dir)
    monkeypatch.setattr(P, "COMMITTEES", tmp_path / "committees.csv")
    monkeypatch.setattr(P.supabase_sync, "upsert_dashboard_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(P.supabase_sync, "bulk_upsert_filer_detail", lambda *args, **kwargs: None)
    monkeypatch.setattr(P.supabase_sync, "get_dashboard_cache", lambda *args, **kwargs: None)

    def row(name, amount, canonical=None, state="OR"):
        return {
            "contributor_payee": name, "contributor_payee_canonical": canonical,
            "amount": amount, "tran_type": "C", "sub_type": "Cash Contribution",
            "filed_date": "2026-01-15", "tran_date": "2026-01-14", "filer": "Recipient",
            "book_type": "Individual", "state": state,
        }

    # Each spelling is below the old top-1000 threshold by itself, while the
    # combined donor must lead both the leaderboard and the top-five lists.
    rows = [row(f"Donor {number:04}", 100) for number in range(1000)]
    rows += [row("Miscellaneous Cash Contributions $100 and under ", 75),
             row("miscellaneous cash contributions $100 and under", 75),
             row("Ignored raw label", 75, canonical=" Miscellaneous  Cash Contributions $100 and under ")]
    rows += [row(" Out  of State Donor ", 20, state="WA"),
             row("out of state donor", 25, state="WA")]
    # None of ORESTAR's three non-cash subtypes may enter a donor leaderboard
    # or its nested committee/type/month lists after normalization and ranking.
    rows += [{**row("In-kind Only Donor", 1000000), "sub_type": subtype}
             for subtype in ["In-Kind Contribution", "In-Kind/Forgiven Account Payable",
                             "In-Kind/Forgiven Personal Expenditures"]]
    frame = pd.DataFrame(rows)
    original = frame.copy(deep=True)

    P.aggregate(frame)

    def read(filename):
        return json.loads((aggregate_dir / filename).read_text())

    def check_donors(donors):
        assert donors[0] == {"name": MISC_CASH_LABEL, "total": 225.0}
        assert all(donor["name"] != "In-kind Only Donor" for donor in donors)
        keys = [donor_label_key(donor["name"]) for donor in donors]
        assert len(keys) == len(set(keys))

    def check_types(types):
        by_type = {entry["type"]: entry for entry in types}
        check_donors(by_type["Individual"]["top_donors"])
        assert by_type["Individual (out of state)"]["top_donors"] == [
            {"name": "Out of State Donor", "total": 45.0}
        ]

    leaderboard = read("top_donors.json")
    check_donors(leaderboard["all_time"])
    check_donors(leaderboard["by_year"]["2026"])
    global_types = read("by_contributor_type.json")
    check_types(global_types["all_time"])
    check_types(global_types["by_year"]["2026"])
    check_types(global_types["by_month"]["2026-01"])
    filer = read("filers/recipient.json")
    check_donors(filer["top_donors"])
    check_donors(filer["top_donors_by_year"]["2026"])
    check_types(filer["by_contributor_type"])
    check_types(filer["by_contributor_type_by_year"]["2026"])
    check_types(filer["by_contributor_type_by_month"]["2026-01"])
    pd.testing.assert_frame_equal(frame, original)


def test_legacy_filer_aggregation_normalizes_separate_contributions_without_mutation(tmp_path, monkeypatch):
    aggregate_dir = tmp_path / "aggregated"
    aggregate_dir.mkdir()
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)
    monkeypatch.setattr(P, "AGG_DIR", aggregate_dir)
    monkeypatch.setattr(P, "COMMITTEES", tmp_path / "committees.csv")
    monkeypatch.setattr(P.supabase_sync, "upsert_dashboard_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(P.supabase_sync, "bulk_upsert_filer_detail", lambda *args, **kwargs: None)
    monkeypatch.setattr(P.supabase_sync, "get_dashboard_cache", lambda *args, **kwargs: None)

    frame = pd.DataFrame({
        "contributor_payee": ["Miscellaneous Cash Contributions $100 and under ",
                              " miscellaneous  cash contributions $100 and UNDER",
                              "In-kind Donor", "Vendor"],
        "amount": [75.0, 75.0, 1000.0, 20.0],
        "tran_type": ["C", "C", "C", "E"],
        "sub_type": ["Cash Contribution", "Cash Contribution", "In-Kind Contribution", "Cash Expenditure"],
        "filed_date": pd.to_datetime(["2026-01-15"] * 4),
        "filer": ["Recipient"] * 4,
        "year": [2026] * 4, "month": ["2026-01"] * 4,
        "book_type": ["Individual"] * 4,
        "is_out_of_state": [False] * 4, "_undated": [False] * 4,
    })
    # These independent frames do not acquire a column when df is relabeled.
    contributions = frame[frame["tran_type"] == "C"].copy()
    inkind = contributions[contributions["sub_type"] == "In-Kind Contribution"].copy()
    expenditures = frame[frame["tran_type"] == "E"].copy()
    empty = frame.iloc[0:0].copy()
    originals = [part.copy(deep=True) for part in [frame, contributions, inkind, expenditures, empty]]

    P.aggregate_filers(frame, contributions, inkind, expenditures, empty, empty, empty,
                       "filer", "contributor_payee")

    detail = json.loads((aggregate_dir / "filers/recipient.json").read_text())
    expected = [{"name": MISC_CASH_LABEL, "total": 150.0}]
    assert detail["top_donors"] == expected
    assert detail["top_donors_by_year"]["2026"] == expected
    for groups in [detail["by_contributor_type"], detail["by_contributor_type_by_year"]["2026"],
                   detail["by_contributor_type_by_month"]["2026-01"]]:
        assert groups == [{"type": "Individual", "total": 150.0, "top_donors": expected}]
    assert detail["total_in"] == 150.0
    assert detail["total_inkind"] == 1000.0
    assert detail["cash_on_hand"] == 130.0
    assert detail["top_payees"] == [{"name": "Vendor", "total": 20.0}]
    for part, original in zip([frame, contributions, inkind, expenditures, empty], originals):
        pd.testing.assert_frame_equal(part, original)
