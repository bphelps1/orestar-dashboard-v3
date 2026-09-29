"""Regression tests for matched ORESTAR/app balance captures."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


SCRAPER = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER))

from balance_snapshot import (  # noqa: E402
    CALCULATION_VERSION,
    CAPTURE_KEY,
    COVERAGE_EVIDENCE_VERSION,
    EMPTY_CASH_SCOPE_DIGEST,
    build_source,
    cash_scope_digest,
    cash_scope_digests,
    evidence_is_current,
    make_summary_capture,
    paired_comparison,
    source_year_transaction_digest,
    transaction_snapshot_id,
)


def _source(
    snapshot_id="sha256:one",
    filer_ids=None,
    cash=125.0,
    count=3,
    scope_digest="sha256:scope-one",
    year_digests=None,
):
    if year_digests is None:
        year_digests = {"2026": "sha256:year-2026"}
    return build_source(
        snapshot_id,
        [{
            "filer_ids": filer_ids or ["10"],
            "name": "Committee",
            "slug": "committee",
            "cash_on_hand": cash,
            "tran_count": count,
            "app_scope_transaction_digest": scope_digest,
            "app_year_transaction_digests": year_digests,
        }],
        created_at="2026-09-04T12:00:00",
    )


def test_transaction_snapshot_fingerprint_changes_with_shard_bytes(tmp_path):
    assert transaction_snapshot_id(tmp_path) is None
    shard = tmp_path / "txn_2026.csv.gz"
    shard.write_bytes(b"first")
    first = transaction_snapshot_id(tmp_path)
    assert first == transaction_snapshot_id(tmp_path)
    shard.write_bytes(b"second")
    assert transaction_snapshot_id(tmp_path) != first


def test_cash_scope_digest_is_stable_across_row_order_and_representation():
    first = {
        "tran_id": "200.0",
        "original_id": None,
        "filer_id": "10.0",
        "tran_date": "09/04/2026",
        "filed_date": "2026-09-05T00:00:00",
        "tran_type": " c ",
        "sub_type": "Cash Contribution",
        "amount": "$1,000.00",
        "_undated": False,
    }
    second = {
        "tran_id": "100",
        "original id": "90.0",
        "filer id": "10",
        "tran_date": "2026-09-01",
        "filed_date": "09/02/2026",
        "tran_type": "E",
        "sub_type": "Expenditure",
        "amount": 25,
        "_undated": "false",
    }
    equivalent_first = {
        **first,
        "tran_id": 200,
        "filer_id": 10,
        "tran_date": "2026-09-04",
        "filed_date": "2026-09-05",
        "tran_type": "C",
        "sub_type": "Cash Contribution",
        "amount": 1000,
        "_undated": 0,
    }
    equivalent_second = {
        **second,
        "original id": 90,
        "filer id": "10.0",
        "tran_date": "09/01/2026",
        "filed_date": "2026-09-02T12:30:00+00:00",
        "amount": "25.0",
    }

    assert cash_scope_digest([first, second]) == cash_scope_digest(
        [equivalent_second, equivalent_first]
    )


def test_cash_scope_digest_changes_with_cash_relevant_fields():
    base = {
        "tran_id": "100",
        "original_id": "90",
        "filer_id": "10",
        "tran_date": "2026-09-01",
        "filed_date": "2026-09-02",
        "tran_type": "C",
        "sub_type": "Cash Contribution",
        "amount": 25,
        "_undated": False,
    }
    expected = cash_scope_digest([base])
    for field, changed in (
        ("tran_id", "101"),
        ("tran_date", "2026-09-03"),
        ("tran_type", "E"),
        ("sub_type", " Cash Contribution "),
        ("amount", 26),
    ):
        assert cash_scope_digest([{**base, field: changed}]) != expected


def test_cash_scope_digests_partition_by_effective_year_in_one_pass():
    historical = {
        "tran_id": "old",
        "filer_id": "10",
        "tran_date": "2006-05-01",
        "filed_date": "2006-05-02",
        "tran_type": "C",
        "sub_type": "Loan Received (Non-Exempt)",
        "amount": 100,
        "_undated": False,
    }
    current = {
        "tran_id": "new",
        "filer_id": "10",
        "tran_date": None,
        "filed_date": "2026-09-02",
        "year": 2026.0,
        "tran_type": "C",
        "sub_type": "Cash Contribution",
        "amount": 25,
        "_undated": True,
    }

    full, by_year = cash_scope_digests([historical, current])
    assert full == cash_scope_digest([current, historical])
    assert set(by_year) == {"2006", "2026"}
    assert by_year["2006"] == cash_scope_digest([historical])
    assert by_year["2026"] == cash_scope_digest([current])

    later_current = {**current, "tran_id": "newer", "amount": 50}
    later_full, later_by_year = cash_scope_digests(
        [historical, current, later_current]
    )
    assert later_full != full
    assert later_by_year["2006"] == by_year["2006"]
    assert later_by_year["2026"] != by_year["2026"]


def test_cash_scope_year_digest_includes_certified_absence_state():
    historical = {
        "tran_id": "old",
        "filer_id": "10",
        "tran_date": "2006-05-01",
        "filed_date": "2006-05-02",
        "tran_type": "C",
        "sub_type": "Cash Contribution",
        "amount": 100,
        "_undated": False,
    }
    current = {
        "tran_id": "new",
        "filer_id": "10",
        "tran_date": "2026-05-01",
        "filed_date": "2026-05-02",
        "tran_type": "C",
        "sub_type": "Cash Contribution",
        "amount": 25,
        "_undated": False,
    }

    raw_full, raw_years = cash_scope_digests([historical, current])
    excluded_full, excluded_years = cash_scope_digests(
        [historical, current],
        cash_excluded_transaction_ids={"old"},
    )

    # The raw scope digest remains usable to certify the exact-diff result.
    assert excluded_full == raw_full
    # Only the year whose cash treatment changed loses annual provenance.
    assert excluded_years["2006"] != raw_years["2006"]
    assert excluded_years["2026"] == raw_years["2026"]


def test_source_year_digest_uses_canonical_empty_fallback():
    source = _source(year_digests={"2006": "sha256:year-2006"})
    record = source["scopes"]["10"]
    assert source_year_transaction_digest(record, 2006) == "sha256:year-2006"
    assert source_year_transaction_digest(record, "2026") == EMPTY_CASH_SCOPE_DIGEST
    assert source_year_transaction_digest(record, "not-a-year") is None


def test_capture_refuses_stale_app_source():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        _source(), "sha256:two",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_source_stale"


def test_capture_refuses_old_app_calculation_source():
    source = _source()
    source["calculation_version"] = "cash-balance-v0"
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_calculation_version"


def test_capture_refuses_source_scope_without_transaction_digest():
    source = _source()
    del source["scopes"]["10"]["app_scope_transaction_digest"]
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_scope_digest_missing"


def test_capture_refuses_source_scope_without_valid_year_digest_map():
    source = _source()
    source["scopes"]["10"]["app_year_transaction_digests"] = None
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_year_digest_map_missing"


def test_capture_persists_only_its_annual_digest_not_the_source_map():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        _source(year_digests={
            "2025": "sha256:year-2025",
            "2026": "sha256:year-2026",
        }),
        "sha256:one",
    )
    assert capture["status"] == "paired"
    assert capture["app_year_transaction_digest"] == "sha256:year-2026"
    assert "app_year_transaction_digests" not in capture


def test_duplicate_app_scope_is_explicitly_unpairable():
    source = build_source(
        "sha256:one",
        [
            {"filer_ids": ["10"], "name": "First", "slug": "first",
             "cash_on_hand": 100, "tran_count": 1,
             "app_scope_transaction_digest": "sha256:first",
             "app_year_transaction_digests": {"2026": "sha256:first-year"}},
            {"filer_ids": ["10"], "name": "Second", "slug": "second",
             "cash_on_hand": 900, "tran_count": 9,
             "app_scope_transaction_digest": "sha256:second",
             "app_year_transaction_digests": {"2026": "sha256:second-year"}},
        ],
        created_at="2026-09-04T12:00:00+00:00",
    )
    assert source["scopes"]["10"]["status"] == "ambiguous"
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_scope_missing_or_ambiguous"


def test_paired_delta_is_immutable_when_app_data_later_changes():
    # The ORESTAR page and the app's $125 balance are frozen together.  A later
    # transaction snapshot does not get reconstructed from filed_date and does
    # not alter the $25 discrepancy.
    source = _source()
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    comparison = paired_comparison(
        ["10"], {"10": {CAPTURE_KEY: capture}},
        current_transaction_id="sha256:later",
    )
    assert comparison["status"] == "paired"
    assert comparison["app_cash_on_hand"] == 125.0
    assert comparison["orestar_cash_on_hand"] == 100.0
    assert comparison["delta_at_capture"] == 25.0
    assert comparison["app_data_changed_since_capture"] is True


def test_scope_digest_is_primary_currentness_signal():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        _source(), "sha256:one",
    )
    yearly = {"10": {CAPTURE_KEY: capture}}

    # An unrelated committee can replace the global shard fingerprint without
    # making this committee's paired account summary stale.
    current = paired_comparison(
        ["10"], yearly,
        current_transaction_id="sha256:global-later",
        current_scope_digest="sha256:scope-one",
    )
    assert current["scope_digest_matches_capture"] is True
    assert current["app_data_changed_since_capture"] is False
    assert current["app_scope_transaction_digest"] == "sha256:scope-one"
    assert current["current_scope_transaction_digest"] == "sha256:scope-one"

    changed = paired_comparison(
        ["10"], yearly,
        current_transaction_id="sha256:one",
        current_scope_digest="sha256:scope-later",
    )
    assert changed["scope_digest_matches_capture"] is False
    assert changed["app_data_changed_since_capture"] is True


def test_legacy_summary_is_unknown_not_a_live_discrepancy():
    comparison = paired_comparison(
        ["10"], {"10": {"ts": 1000, "years": {"2026": {}}}},
        current_transaction_id="sha256:now",
    )
    assert comparison == {"status": "legacy_unpaired", "reason": "capture_missing"}


def test_multi_filer_scope_requires_every_component_on_same_app_snapshot():
    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    first = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    second = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    first["scope_capture_id"] = "10|20@test"
    second["scope_capture_id"] = "10|20@test"
    yearly = {"10": {CAPTURE_KEY: first}, "20": {CAPTURE_KEY: second}}
    comparison = paired_comparison(
        ["20", "10"], yearly, current_transaction_id="sha256:one"
    )
    assert comparison["status"] == "paired"
    assert comparison["capture_started_at"] == 1000
    assert comparison["captured_at"] == 1010
    assert comparison["app_cash_on_hand"] == 500
    assert comparison["orestar_cash_on_hand"] == 485
    assert comparison["delta_at_capture"] == 15

    second["app_transaction_snapshot_id"] = "sha256:different"
    assert paired_comparison(["10", "20"], yearly)["reason"] == "component_snapshot_mismatch"


def test_multi_filer_scope_requires_one_component_scope_digest():
    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    first = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    second = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    first["scope_capture_id"] = "10|20@test"
    second["scope_capture_id"] = "10|20@test"
    second["app_scope_transaction_digest"] = "sha256:other-scope"

    result = paired_comparison(
        ["10", "20"],
        {"10": {CAPTURE_KEY: first}, "20": {CAPTURE_KEY: second}},
    )
    assert result == {"status": "unpaired", "reason": "component_snapshot_mismatch"}


def test_old_calculation_version_is_not_actionable():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    capture["calculation_version"] = "cash-balance-v0"
    result = paired_comparison(["10"], {"10": {CAPTURE_KEY: capture}})
    assert result == {"status": "unpaired", "reason": "component_snapshot_mismatch"}


def test_old_capture_format_is_not_actionable():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    capture["version"] = 0
    result = paired_comparison(["10"], {"10": {CAPTURE_KEY: capture}})
    assert result == {"status": "unpaired", "reason": "capture_version_mismatch"}


def test_newer_unpaired_summary_makes_old_pair_refresh_only():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    attempt = {
        "status": "unpaired",
        "reason": "app_snapshot_source_stale",
        "captured_at": 2000,
        "orestar_ending_cash_balance": 125,
    }
    result = paired_comparison(
        ["10"],
        {"10": {CAPTURE_KEY: capture, "comparison_capture_attempt": attempt}},
        current_transaction_id="sha256:one",
    )
    assert result["status"] == "paired"
    assert result["delta_at_capture"] == 25
    assert result["orestar_data_changed_since_capture"] is True
    assert result["latest_unpaired_capture_at"] == 2000


def test_current_page_timestamp_is_taken_before_historical_paging():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"

    class NoPrev:
        @property
        def first(self):
            return self

        def count(self):
            return 0

    class Page:
        def locator(self, _selector):
            return NoPrev()

        def content(self):
            return html

    summary = {"ending_cash_balance": 42.5, "beginning_balance": 1.0}
    with patch.object(feb, "_load_summary_page", return_value=html), \
         patch.object(feb, "_parse_yearly_summary", return_value=summary), \
         patch.object(feb, "_parse_beginning_balance", return_value=1.0), \
         patch.object(feb.time, "time", return_value=1234.5):
        earliest, yearly, capture = feb._scrape_filer_earliest(Page(), "10")

    assert capture["captured_at"] == 1234.5
    assert capture["summary"] == summary
    assert yearly["2026"] == summary
    assert earliest["reached_earliest"] is True


def test_heading_only_partial_page_creates_no_capture():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"
    with patch.object(feb, "_load_summary_page", return_value=html):
        earliest, yearly, capture = feb._scrape_filer_earliest(
            object(), "10", current_only=True
        )

    assert earliest is None
    assert yearly == {}
    assert capture is None


def test_required_amount_cannot_be_borrowed_from_the_next_html_row():
    import fetch_earliest_balances as feb
    import orestar_parse

    def row(label, value):
        return f"<tr><td>{label}</td><td>{value}</td></tr>"

    html = "".join([
        row("Beginning Balance (Previous Year)", "$10.00"),
        row("Total Contributions", ""),
        row("Total Expenditures", "$500.00"),
        row("Other Receipts", "$2.00"),
        row("Other Disbursements", "$3.00"),
        row("Balance Adjustments", "$0.00"),
        row("Ending Cash Balance", "$9.00"),
    ])

    assert orestar_parse.parse_dollar(
        html, "Total Contributions", None
    ) is None
    assert feb._parse_yearly_summary(html) is None


def test_missing_optional_loan_value_stays_unknown_not_synthetic_zero():
    import fetch_earliest_balances as feb

    def row(label, value):
        return f"<tr><td>{label}</td><td>{value}</td></tr>"

    html = "".join([
        row("Beginning Balance (Previous Year)", "$10.00"),
        row("Total Contributions", "$2.00"),
        row("Total Expenditures", "$1.00"),
        row("Other Receipts", "$0.00"),
        row("Other Disbursements", "$0.00"),
        row("Balance Adjustments", "$0.00"),
        row("Ending Cash Balance", "$11.00"),
        row("Loans Received (Non-Exempt)", ""),
        row("Loans Received (Exempt)", "$50.00"),
    ])

    summary = feb._parse_yearly_summary(html)
    assert summary is not None
    assert summary["summary_field_version"] == feb.SUMMARY_FIELD_VERSION
    assert summary["loans_received"] is None
    assert summary["loans_received_exempt"] == 50.0


def test_noop_prev_click_does_not_bank_current_year_as_earliest():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"

    class Prev:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self):
            return None

    class Page:
        def locator(self, _selector):
            return Prev()

        def content(self):
            return html

    summary = {"beginning_balance": 777.0, "ending_cash_balance": 777.0}
    with patch.object(feb, "_load_summary_page", return_value=html), \
         patch.object(feb, "_parse_yearly_summary", return_value=summary), \
         patch.object(feb, "CHALLENGE_WAIT", 0), \
         patch.object(feb.time, "sleep"):
        earliest, _, _ = feb._scrape_filer_earliest(Page(), "10")

    assert earliest["earliest_year"] == 2026
    assert earliest["reached_earliest"] is False


def test_stale_member_expands_to_complete_multi_filer_scope():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    assert feb._group_ids_by_source_scope(["20"], source) == [["10", "20"]]


def test_source_scope_without_digest_is_not_sweep_eligible():
    import fetch_earliest_balances as feb

    source = _source()
    source["scopes"]["10"]["app_scope_transaction_digest"] = None
    assert feb._eligible_source_scopes(source) == {}
    assert feb._snapshot_source_ready(source, "sha256:one") is False


def test_source_scope_without_valid_year_digest_map_is_not_sweep_eligible():
    import fetch_earliest_balances as feb

    source = _source()
    source["scopes"]["10"]["app_year_transaction_digests"] = {
        "2026": " bad-digest "
    }
    assert feb._eligible_source_scopes(source) == {}
    assert feb._snapshot_source_ready(source, "sha256:one") is False


def test_overlapping_source_scopes_are_wholly_ineligible():
    import fetch_earliest_balances as feb

    source = {
        "version": feb.FORMAT_VERSION,
        "calculation_version": feb.CALCULATION_VERSION,
        "transaction_snapshot_id": "sha256:one",
        "scopes": {
            "10|20": {
                "filer_ids": ["10", "20"],
                "app_scope_transaction_digest": "sha256:first",
                "app_year_transaction_digests": {
                    "2026": "sha256:first-year",
                },
            },
            "20|30": {
                "filer_ids": ["20", "30"],
                "app_scope_transaction_digest": "sha256:second",
                "app_year_transaction_digests": {
                    "2026": "sha256:second-year",
                },
            },
        },
    }
    assert feb._eligible_source_scopes(source) == {}
    assert feb._snapshot_source_ready(source, "sha256:one") is False


def test_partial_multi_scope_refresh_preserves_last_common_pair():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    old_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    old_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    old_a["scope_capture_id"] = "10|20@old"
    old_b["scope_capture_id"] = "10|20@old"
    yearly = {
        "10": {"years": {}, CAPTURE_KEY: old_a},
        "20": {"years": {}, CAPTURE_KEY: old_b},
    }

    # First attempt reads A but F5 refuses B; a later attempt does the reverse.
    # Neither can be spliced together into a new authoritative pair.
    new_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 2000,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(["10", "20"], {"10": new_a}, yearly) is False
    assert yearly["10"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    assert yearly["20"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    comparison = paired_comparison(["10", "20"], yearly)
    assert comparison["status"] == "paired"
    assert comparison["orestar_data_changed_since_capture"] is True

    new_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 3000,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(["10", "20"], {"20": new_b}, yearly) is False
    assert yearly["10"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    assert yearly["20"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"

    # Only one complete group attempt promotes, with a common attempt identity.
    final_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 4000,
        source, "sha256:one",
    )
    final_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 4010,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(
        ["10", "20"], {"10": final_a, "20": final_b}, yearly
    ) is True
    assert (yearly["10"][CAPTURE_KEY]["scope_capture_id"]
            == yearly["20"][CAPTURE_KEY]["scope_capture_id"])
    assert "comparison_capture_attempt" not in yearly["10"]
    assert "comparison_capture_attempt" not in yearly["20"]


def test_scope_commit_stamps_every_fresh_year_with_shared_provenance():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    first = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 4000,
        source, "sha256:one",
    )
    second = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 4010,
        source, "sha256:one",
    )
    old_provenance = {
        "app_year_transaction_digest": "sha256:old-year",
        "calculation_version": "cash-balance-v1",
        "scope_capture_id": "10|20@old",
    }
    yearly = {
        "10": {"years": {
            "2026": {"ending_cash_balance": 220, **old_provenance},
            "2025": {"ending_cash_balance": 200},
            "2024": {"ending_cash_balance": 180, **old_provenance},
        }},
        "20": {"years": {
            "2026": {"ending_cash_balance": 280, **old_provenance},
        }},
    }

    assert feb._commit_scope_captures(
        ["10", "20"],
        {"10": first, "20": second},
        yearly,
        {
            "10": {
                "2025": "sha256:year-2025",
                "2026": "sha256:year-2026",
            },
            "20": {"2026": "sha256:year-2026"},
        },
    ) is True

    capture_id = yearly["10"][CAPTURE_KEY]["scope_capture_id"]
    assert capture_id == yearly["20"][CAPTURE_KEY]["scope_capture_id"]
    expected = {
        "app_year_transaction_digest": "sha256:year-2026",
        "calculation_version": feb.CALCULATION_VERSION,
        "scope_capture_id": capture_id,
    }
    assert {key: yearly["10"]["years"]["2026"][key] for key in expected} == expected
    assert {key: yearly["20"]["years"]["2026"][key] for key in expected} == expected
    expected_2025 = {
        **expected,
        "app_year_transaction_digest": "sha256:year-2025",
    }
    assert {
        key: yearly["10"]["years"]["2025"][key] for key in expected_2025
    } == expected_2025
    assert {
        key: yearly["10"]["years"]["2024"][key] for key in old_provenance
    } == old_provenance


def test_scope_commit_stamps_canonical_empty_digest_for_an_empty_app_year():
    import fetch_earliest_balances as feb

    source = _source(year_digests={})
    capture = make_summary_capture(
        "10", 2006, {"ending_cash_balance": 0}, 4000,
        source, "sha256:one",
    )
    yearly = {"10": {"years": {"2006": {"ending_cash_balance": 0}}}}

    assert capture["app_year_transaction_digest"] == EMPTY_CASH_SCOPE_DIGEST
    assert feb._commit_scope_captures(
        ["10"], {"10": capture}, yearly,
        {"10": {"2006": source_year_transaction_digest(
            source["scopes"]["10"], 2006,
        )}},
    ) is True
    assert (yearly["10"]["years"]["2006"]["app_year_transaction_digest"]
            == EMPTY_CASH_SCOPE_DIGEST)


def test_scope_commit_rejects_invalid_fresh_year_provenance():
    import fetch_earliest_balances as feb

    source = _source()
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 4000,
        source, "sha256:one",
    )
    yearly = {
        "10": {"years": {"2026": {
            "ending_cash_balance": 100,
            "app_year_transaction_digest": "sha256:stale",
            "calculation_version": "cash-balance-v1",
            "scope_capture_id": "10@old",
        }}}
    }

    assert feb._commit_scope_captures(
        ["10"], {"10": capture}, yearly,
        {"10": {"2026": None}},
    ) is False
    annual = yearly["10"]["years"]["2026"]
    assert "app_year_transaction_digest" not in annual
    assert "calculation_version" not in annual
    assert "scope_capture_id" not in annual


def test_partial_scope_keeps_fresh_year_data_but_clears_its_provenance():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    old_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    old_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    old_a["scope_capture_id"] = "10|20@old"
    old_b["scope_capture_id"] = "10|20@old"
    stale_provenance = {
        "app_year_transaction_digest": "sha256:stale",
        "calculation_version": "cash-balance-v1",
        "scope_capture_id": "10|20@stale",
    }
    yearly = {
        "10": {
            CAPTURE_KEY: old_a,
            "years": {
                "2026": {
                    "ending_cash_balance": 220,
                    "scrape_ts": 2000,
                    **stale_provenance,
                },
                "2025": {"ending_cash_balance": 200, **stale_provenance},
            },
        },
        "20": {CAPTURE_KEY: old_b, "years": {}},
    }
    new_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 2000,
        source, "sha256:one",
    )

    assert feb._commit_scope_captures(
        ["10", "20"], {"10": new_a}, yearly,
        {"10": {"2026": "sha256:year-2026"}},
    ) is False

    fresh = yearly["10"]["years"]["2026"]
    assert fresh["ending_cash_balance"] == 220
    assert fresh["scrape_ts"] == 2000
    for key in stale_provenance:
        assert key not in fresh
    assert {
        key: yearly["10"]["years"]["2025"][key]
        for key in stale_provenance
    } == stale_provenance


def test_scope_capture_commit_rejects_component_digest_mismatch():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    first = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 4000,
        source, "sha256:one",
    )
    second = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 4010,
        source, "sha256:one",
    )
    second["app_scope_transaction_digest"] = "sha256:different"
    yearly = {}

    assert feb._commit_scope_captures(
        ["10", "20"], {"10": first, "20": second}, yearly
    ) is False
    assert yearly["10"][CAPTURE_KEY]["reason"] == "scope_capture_mismatch"
    assert yearly["20"][CAPTURE_KEY]["reason"] == "scope_capture_mismatch"


def test_recent_old_version_capture_is_still_requeued():
    import fetch_earliest_balances as feb

    entry = {
        CAPTURE_KEY: {
            "version": 0,
            "calculation_version": "cash-balance-v0",
            "status": "paired",
            "captured_at": 9999,
        }
    }
    assert feb._current_capture_needs_refresh(entry, cutoff=1000) is True


def test_current_capture_is_requeued_when_current_source_scope_changes():
    import fetch_earliest_balances as feb

    source = _source()
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 2000,
        source, "sha256:one",
    )
    entry = {CAPTURE_KEY: capture}
    source_record = source["scopes"]["10"]

    assert feb._current_capture_needs_refresh(
        entry, cutoff=1000, source_record=source_record
    ) is False
    # Callers without a source record retain the timestamp-based legacy check.
    assert feb._current_capture_needs_refresh(entry, cutoff=1000) is False

    for field, changed in (
        ("app_scope_transaction_digest", "sha256:scope-later"),
        ("cash_on_hand", 125.01),
        ("tran_count", 4),
    ):
        assert feb._current_capture_needs_refresh(
            entry,
            cutoff=1000,
            source_record={**source_record, field: changed},
        ) is True


def test_legacy_annual_rows_requeue_only_the_historical_sweep():
    import fetch_earliest_balances as feb

    source = _source(year_digests={
        "2025": "sha256:year-2025",
        "2026": "sha256:year-2026",
    })
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 2000,
        source, "sha256:one",
    )
    entry = {
        CAPTURE_KEY: capture,
        "ts": 2000,
        "years": {
            "2026": {
                "ending_cash_balance": 100,
                "app_year_transaction_digest": "sha256:year-2026",
                "calculation_version": CALCULATION_VERSION,
                "scope_capture_id": "10@test",
            },
            "2025": {"ending_cash_balance": 90},
        },
    }

    # A fresh current pair remains satisfied: the current-only sweep cannot
    # repair the historical row and therefore must not retry it forever.
    assert feb._current_capture_needs_refresh(
        entry, cutoff=1000, source_record=source["scopes"]["10"]
    ) is False
    # The full historical sweep ignores the entry-level age and backfills 2025.
    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source["scopes"]["10"]
    ) is True
    entry["years"]["2025"].update({
        "app_year_transaction_digest": "sha256:year-2025",
        "calculation_version": CALCULATION_VERSION,
        "scope_capture_id": "10@test",
    })
    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source["scopes"]["10"]
    ) is False


def test_historical_provenance_migration_waits_for_a_current_eligible_source():
    import fetch_earliest_balances as feb

    legacy_entry = {
        "ts": 2000,
        "years": {"2025": {"ending_cash_balance": 90}},
    }

    # With no app source, provenance cannot be repaired. Returning False lets
    # a fresh historical entry fall out of the next chained batch instead of
    # selecting and re-scraping it forever. Age, missing-year and incomplete-
    # opening predicates in main remain separate and can still select it.
    assert feb._historical_year_provenance_needs_refresh(
        legacy_entry, source_record=None
    ) is False

    stale_source = _source(snapshot_id="sha256:old")
    assert feb._snapshot_source_ready(stale_source, "sha256:current") is False
    stale_record = (
        stale_source["scopes"]["10"]
        if feb._snapshot_source_ready(stale_source, "sha256:current")
        else None
    )
    assert feb._historical_year_provenance_needs_refresh(
        legacy_entry, source_record=stale_record
    ) is False

    ineligible_record = dict(_source()["scopes"]["10"])
    ineligible_record["app_year_transaction_digests"] = None
    assert feb._historical_year_provenance_needs_refresh(
        legacy_entry, source_record=ineligible_record
    ) is False

    # Once a usable source exists, the same legacy row is selected exactly for
    # the migration it can now complete.
    current_record = _source(year_digests={
        "2025": "sha256:year-2025",
    })["scopes"]["10"]
    assert feb._historical_year_provenance_needs_refresh(
        legacy_entry, source_record=current_record
    ) is True


def test_changed_historical_year_digest_requeues_full_sweep():
    import fetch_earliest_balances as feb

    entry = {
        "years": {
            "2006": {
                "ending_cash_balance": 90,
                "app_year_transaction_digest": "sha256:year-2006-old",
                "calculation_version": CALCULATION_VERSION,
                "scope_capture_id": "10@old-snapshot",
            },
            "2007": {
                "ending_cash_balance": 100,
                "app_year_transaction_digest": "sha256:year-2007",
                "calculation_version": CALCULATION_VERSION,
                "scope_capture_id": "10@old-snapshot",
            },
        },
    }
    source_record = _source(year_digests={
        "2006": "sha256:year-2006-new",
        "2007": "sha256:year-2007",
    })["scopes"]["10"]

    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source_record
    ) is True

    entry["years"]["2006"]["app_year_transaction_digest"] = (
        "sha256:year-2006-new"
    )
    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source_record
    ) is False


def test_historical_year_digest_compares_against_empty_source_year():
    import fetch_earliest_balances as feb

    entry = {
        "years": {
            "2006": {
                "ending_cash_balance": 0,
                "app_year_transaction_digest": "sha256:previously-nonempty",
                "calculation_version": CALCULATION_VERSION,
                "scope_capture_id": "10@old-snapshot",
            },
        },
    }
    source_record = _source(year_digests={})["scopes"]["10"]

    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source_record
    ) is True
    entry["years"]["2006"][
        "app_year_transaction_digest"
    ] = EMPTY_CASH_SCOPE_DIGEST
    assert feb._historical_year_provenance_needs_refresh(
        entry, source_record=source_record
    ) is False


def test_partial_scope_capture_and_newer_attempt_stay_requeued():
    import fetch_earliest_balances as feb

    partial = {
        CAPTURE_KEY: {
            "version": feb.FORMAT_VERSION,
            "calculation_version": feb.CALCULATION_VERSION,
            "status": "unpaired",
            "reason": "scope_capture_incomplete",
            "captured_at": 2000,
        }
    }
    assert feb._current_capture_needs_refresh(partial, cutoff=1000) is True

    paired = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 2000,
        _source(), "sha256:one",
    )
    with_attempt = {
        CAPTURE_KEY: paired,
        "comparison_capture_attempt": {
            "status": "unpaired",
            "reason": "scope_capture_incomplete",
            "captured_at": 3000,
        },
    }
    assert feb._current_capture_needs_refresh(with_attempt, cutoff=1000) is True


def test_failed_historical_recrawl_preserves_proven_opening_anchor_for_retry():
    import fetch_earliest_balances as feb

    previous = {
        "earliest_year": 2006,
        "beginning_balance": 123.45,
        "reached_earliest": True,
        "ts": 1000,
    }
    failed = {
        "earliest_year": 2021,
        "beginning_balance": 9000.0,
        "reached_earliest": False,
        "ts": 2000,
    }

    merged = feb._merge_earliest_result(previous, failed)
    assert merged["earliest_year"] == 2006
    assert merged["beginning_balance"] == 123.45
    assert merged["reached_earliest"] is True
    # The old timestamp stays old, so the fixed sweep cutoff selects it again.
    assert merged["ts"] < 1500
    assert merged["incomplete_refresh_attempt"] == failed


def test_newer_reported_floor_cannot_erase_proven_older_anchor():
    import fetch_earliest_balances as feb

    previous = {
        "earliest_year": 2006,
        "beginning_balance": 123.45,
        "reached_earliest": True,
        "ts": 1000,
    }
    suspicious = {
        "earliest_year": 2026,
        "beginning_balance": 9999.0,
        "reached_earliest": True,
        "ts": 2000,
    }

    merged = feb._merge_earliest_result(previous, suspicious)
    assert merged["earliest_year"] == 2006
    assert merged["beginning_balance"] == 123.45
    assert merged["ts"] == 1000
    assert merged["inconsistent_refresh_attempt"] == suspicious


def test_empty_or_all_ambiguous_source_cannot_start_current_sweep():
    import fetch_earliest_balances as feb

    empty = build_source(
        "sha256:one", [], created_at="2026-09-04T12:00:00+00:00"
    )
    ambiguous = build_source(
        "sha256:one",
        [
            {"filer_ids": ["10"], "name": "A", "cash_on_hand": 1,
             "app_scope_transaction_digest": "sha256:first",
             "app_year_transaction_digests": {"2026": "sha256:first-year"}},
            {"filer_ids": ["10"], "name": "B", "cash_on_hand": 2,
             "app_scope_transaction_digest": "sha256:second",
             "app_year_transaction_digests": {"2026": "sha256:second-year"}},
        ],
        created_at="2026-09-04T12:00:00+00:00",
    )
    assert feb._snapshot_source_ready(empty, "sha256:one") is False
    assert feb._snapshot_source_ready(ambiguous, "sha256:one") is False


def test_supporting_count_must_postdate_summary_capture():
    captured = 1_788_220_800  # 2026-09-01T00:00:00Z
    assert evidence_is_current({"checked": "2026-08-31"}, captured) is False
    # Legacy date-only evidence on the same day is midnight and cannot prove it
    # followed a later page read.
    assert evidence_is_current({"checked": "2026-09-01"}, captured + 3600) is False
    assert evidence_is_current(
        {"checked_at": "2026-09-01T02:00:00+00:00"}, captured + 3600
    ) is True


def test_automation_rejects_legacy_or_ambiguous_coverage_evidence():
    captured = 1_788_220_800  # 2026-09-01T00:00:00Z
    base = {
        "evidence_version": COVERAGE_EVIDENCE_VERSION, "filer_digest_version": 2,
        "collection_started_at": "2026-09-01T01:59:59.123456Z",
        "checked_at": "2026-09-01T02:00:00.123456Z",
        "transaction_snapshot_id": "sha256:one",
        "filer_transaction_digest": "sha256:filer-one",
        "range_start": "2006-01-01",
        "range_end": "2026-09-01",
    }
    constraints = {
        "require_precise": True,
        "require_collection_started": True,
        "strictly_after": True,
        "transaction_snapshot_id": "sha256:one",
        "filer_transaction_digest": "sha256:filer-one",
        "range_start": "2006-01-01",
        "range_end": "2026-09-01",
    }

    assert evidence_is_current(base, captured, **constraints) is True
    assert evidence_is_current({**base, "checked_at": "2026-09-01"}, captured,
                               **constraints) is False
    assert evidence_is_current({**base, "checked_at": "2026-09-01T02:00:00"},
                               captured, **constraints) is False
    assert evidence_is_current({**base, "checked_at": "2026-09-01T03:00:00+01:00"},
                               captured, **constraints) is False
    assert evidence_is_current({**base, "evidence_version": 1}, captured,
                               **constraints) is False
    without_filer_digest = dict(base)
    without_filer_digest.pop("filer_transaction_digest")
    assert evidence_is_current(without_filer_digest, captured,
                               **constraints) is False


def test_automation_requires_exact_snapshot_and_intended_range():
    captured = 1_788_220_800
    evidence = {
        "evidence_version": COVERAGE_EVIDENCE_VERSION, "filer_digest_version": 2,
        "collection_started_at": "2026-09-01T23:59:59.999999Z",
        "checked_at": "2026-09-02T00:00:00.000001Z",
        "transaction_snapshot_id": "sha256:one",
        "range_start": "2006-01-01",
        "range_end": "2026-09-02",
    }

    assert evidence_is_current(
        evidence, captured, require_precise=True,
        require_collection_started=True, strictly_after=True,
        transaction_snapshot_id="sha256:one", range_start="2006-01-01",
        minimum_range_end="2026-09-01",
    ) is True
    assert evidence_is_current(
        evidence, captured, require_precise=True,
        require_collection_started=True, strictly_after=True,
        transaction_snapshot_id="sha256:two", range_start="2006-01-01",
        minimum_range_end="2026-09-01",
    ) is False
    assert evidence_is_current(
        evidence, captured, require_precise=True,
        require_collection_started=True, strictly_after=True,
        transaction_snapshot_id="sha256:one", range_start="2007-01-01",
        minimum_range_end="2026-09-01",
    ) is False
    assert evidence_is_current(
        evidence, captured, require_precise=True,
        require_collection_started=True, strictly_after=True,
        transaction_snapshot_id="sha256:one", range_start="2006-01-01",
        range_end="2026-09-01",
    ) is False


def test_automation_requires_query_to_start_after_capture_and_before_completion():
    captured = 1_788_220_800  # 2026-09-01T00:00:00Z
    evidence = {
        "evidence_version": COVERAGE_EVIDENCE_VERSION, "filer_digest_version": 2,
        "collection_started_at": "2026-09-01T00:00:00.000001Z",
        "checked_at": "2026-09-01T00:00:01Z",
    }
    kwargs = {
        "require_precise": True,
        "require_collection_started": True,
        "strictly_after": True,
    }

    assert evidence_is_current(evidence, captured, **kwargs) is True
    for invalid_start in (
        "2026-08-31T23:59:59.999999Z",  # straddles the capture
        "2026-09-01T00:00:00Z",         # equality is not ordering proof
        "2026-09-01T00:00:02Z",         # starts after completion
        "2026-09-01T00:00:00.000001",   # timezone is ambiguous
        "2026-09-01T01:00:00.000001+01:00",  # non-UTC representation
    ):
        assert evidence_is_current(
            {**evidence, "collection_started_at": invalid_start},
            captured,
            **kwargs,
        ) is False
