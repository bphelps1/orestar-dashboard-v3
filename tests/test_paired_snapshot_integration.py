"""Focused integration regressions for collection-time balance comparisons."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).parent.parent
SCRAPER = ROOT / "scraper"
sys.path.insert(0, str(SCRAPER))

from balance_snapshot import (  # noqa: E402
    CALCULATION_VERSION,
    cash_scope_digest,
)


def test_current_only_reads_no_prev_control() -> None:
    import fetch_earliest_balances as balances

    html = "Account Summary Information for the year 2026"

    class Page:
        def locator(self, _selector):
            raise AssertionError("current-only mode must never inspect Prev")

    summary = {"beginning_balance": 10.0, "ending_cash_balance": 25.0}
    with patch.object(balances, "_load_summary_page", return_value=html), \
         patch.object(balances, "_parse_yearly_summary", return_value=summary), \
         patch.object(balances.time, "time", return_value=2222.5):
        earliest, yearly, capture = balances._scrape_filer_earliest(
            Page(), "10", current_only=True
        )

    assert earliest is None
    assert yearly == {"2026": {**summary, "scrape_ts": 2222.5}}
    assert capture == {
        "captured_at": 2222.5,
        "orestar_year": 2026,
        "summary": {**summary, "scrape_ts": 2222.5},
    }


def test_weekly_and_monthly_modes_survive_self_retrigger() -> None:
    workflow = (ROOT / ".github/workflows/earliest-balances.yml").read_text()

    assert '- cron: "0 18 * * 0"' in workflow
    assert '- cron: "0 7 1 * *"' in workflow

    # The first batch translates the schedule once, then persists mode and one
    # fixed sweep cutoff through GITHUB_ENV. A self-dispatched child has no
    # event.schedule, so carrying those explicit inputs preserves its mode and
    # prevents late batches from falling on the wrong side of a moving TTL.
    assert workflow.count('if [ "${{ github.event.schedule }}" = "0 18 * * 0" ]; then') == 1
    assert 'echo "current_only=$CURRENT_ONLY" >> "$GITHUB_ENV"' in workflow
    assert 'echo "refresh_before_ts=$REFRESH_BEFORE" >> "$GITHUB_ENV"' in workflow
    assert 'CURRENT_ONLY="${{ env.current_only }}"' in workflow
    assert 'REFRESH_BEFORE="${{ env.refresh_before_ts }}"' in workflow
    assert '-f max_age_days="$MAX_AGE"' in workflow
    assert '-f current_only="$CURRENT_ONLY"' in workflow
    assert '-f refresh_before_ts="$REFRESH_BEFORE"' in workflow

    scrape_start = workflow.index("      - name: Scrape account summaries")
    scrape_end = workflow.index("\n      - name:", scrape_start + 1)
    scrape = workflow[scrape_start:scrape_end]
    assert 'ARGS="$ARGS --current-only"' in scrape


def _summary(ending: float) -> dict:
    return {
        "summary_field_version": 2,
        "beginning_balance": 0.0,
        "ending_cash_balance": ending,
        "contributions": ending,
        "expenditures": 0.0,
        "other_receipts": 0.0,
        "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
        "loans_received": 0.0,
        "loans_received_exempt": 0.0,
        "loan_payments": 0.0,
        "loan_payments_exempt": 0.0,
        "inkind_contributions": 0.0,
        "inkind_expenditures": 0.0,
        "accounts_receivable": 0.0,
        "accounts_payable": 0.0,
        "total_outstanding_loans": 0.0,
        "outstanding_personal_expenditures": 0.0,
        "balance_deficit": 0.0,
    }


def _paired_capture(
    fid: str,
    *,
    app_cash: float,
    orestar_cash: float,
    scope_ids: list[str] | None = None,
    count: int = 1,
    scope_digest: str = "sha256:test-scope",
) -> dict:
    scope_ids = scope_ids or [fid]
    return {
        "version": 2,
        "status": "paired",
        "captured_at": 1000.0,
        "orestar_year": 2026,
        "orestar_ending_cash_balance": orestar_cash,
        "app_scope_key": "|".join(sorted(scope_ids)),
        "app_scope_filer_ids": sorted(scope_ids),
        "app_cash_on_hand": app_cash,
        "app_tran_count": count,
        "app_transaction_snapshot_id": "sha256:at-capture",
        "app_scope_transaction_digest": scope_digest,
        "app_snapshot_created_at": "2026-09-01T00:00:00",
        "calculation_version": CALCULATION_VERSION,
        **({"scope_capture_id": f"{'|'.join(sorted(scope_ids))}@test"}
           if len(scope_ids) > 1 else {}),
    }


def test_process_flags_only_paired_capture_and_keeps_frozen_delta(
    tmp_path: Path,
) -> None:
    """A legacy live mismatch is unknown; later app rows don't rewrite a pair."""
    import generate_activity_snapshot
    import process

    data_dir = tmp_path / "data"
    agg_dir = data_dir / "aggregated"
    trans_dir = data_dir / "transactions"
    agg_dir.mkdir(parents=True)
    trans_dir.mkdir()

    yearly = {
        # Legacy summary: live app $100 vs ORESTAR $90, but no paired app state.
        "10": {"years": {"2026": _summary(90.0)}, "ts": 1000.0},
        # Pair froze app $125 vs ORESTAR $100 ($25 delta). The app now has
        # $150; this movement is context, not a rewritten $50 discrepancy.
        "20": {
            "years": {"2026": _summary(100.0)},
            "ts": 1000.0,
            "comparison_capture": _paired_capture(
                "20", app_cash=125.0, orestar_cash=100.0
            ),
        },
        # An unchanged pair remains actionable.
        "30": {
            "years": {"2026": _summary(100.0)},
            "ts": 1000.0,
            "comparison_capture": _paired_capture(
                "30", app_cash=125.0, orestar_cash=100.0
            ),
        },
        # The latest current page is a trailing blank after a real 2025
        # statement. Even though the frozen pair differs by $125, this is
        # closure evidence, not a missing-transaction backfill instruction.
        "40": {
            "years": {"2025": _summary(100.0), "2026": _summary(0.0)},
            "ts": 1000.0,
            "comparison_capture": _paired_capture(
                "40", app_cash=125.0, orestar_cash=0.0
            ),
        },
        # One physical filer has a trailing blank; the other simply has no
        # newer statement. The lossy year-wise union looks blank in 2026, but
        # the canonical profile is not conclusively closed.
        "50": {
            "years": {"2025": _summary(50.0), "2026": _summary(0.0)},
            "ts": 1000.0,
            "comparison_capture": _paired_capture(
                "50", app_cash=200.0, orestar_cash=0.0,
                scope_ids=["50", "60"], count=2,
            ),
        },
        "60": {
            "years": {"2025": _summary(100.0)},
            "ts": 1000.0,
            "comparison_capture": _paired_capture(
                "60", app_cash=200.0, orestar_cash=100.0,
                scope_ids=["50", "60"], count=2,
            ),
        },
        # Only half of this canonical scope has ever produced a summary.
        # It belongs in the unusable denominator rather than disappearing.
        "80": {
            "years": {"2026": _summary(50.0)},
            "ts": 1000.0,
        },
    }
    df = pd.DataFrame({
        "tran_id": [
            "legacy-row", "paired-old", "paired-new", "stable-pair", "closed-pair",
            "mixed-closed", "mixed-open",
            "partial-one", "partial-two",
        ],
        "filed_date": pd.to_datetime(
            [
                "2026-01-01", "2026-01-01", "2026-09-01", "2026-01-01",
                "2025-01-01",
                "2025-01-02", "2025-01-03",
                "2026-02-01", "2026-02-02",
            ]
        ),
        "amount": [100.0, 125.0, 25.0, 125.0, 125.0, 50.0, 150.0, 50.0, 50.0],
        "tran_type": ["C"] * 9,
        "sub_type": ["Cash Contribution"] * 9,
        "contributor_payee": ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        "filer": [
            "Legacy Committee", "Changed Pair", "Changed Pair", "Stable Pair",
            "Closed Committee",
            "Mixed Closure Scope", "Mixed Closure Scope",
            "Partial Summary Scope", "Partial Summary Scope",
        ],
        "filer id": ["10", "20", "20", "30", "40", "50", "60", "80", "90"],
        "year": [2026, 2026, 2026, 2026, 2025, 2025, 2025, 2026, 2026],
        "month": [
            "2026-01", "2026-01", "2026-09", "2026-01", "2025-01",
            "2025-01", "2025-01",
            "2026-02", "2026-02",
        ],
        "book_type": ["Individual"] * 9,
        "is_out_of_state": [False] * 9,
        "_undated": [False] * 9,
    })
    # Scope 20 was captured before its second row arrived. Every other capture
    # describes the exact canonical row set used by this aggregation.
    captured_rows = {
        "20": df[
            (df["filer"] == "Changed Pair")
            & (df["tran_id"] == "paired-old")
        ],
        "30": df[df["filer"] == "Stable Pair"],
        "40": df[df["filer"] == "Closed Committee"],
        "50": df[df["filer"] == "Mixed Closure Scope"],
        "60": df[df["filer"] == "Mixed Closure Scope"],
    }
    for fid, rows_at_capture in captured_rows.items():
        yearly[fid]["comparison_capture"][
            "app_scope_transaction_digest"
        ] = cash_scope_digest(rows_at_capture.to_dict(orient="records"))
    (data_dir / "orestar_yearly_summaries.json").write_text(json.dumps(yearly))

    contributions = df.copy()
    empty = df.iloc[0:0].copy()

    empty_snapshot = {"meta": {"total_candidates": 0}, "legislative_map": {}}
    with patch.object(process, "DATA_DIR", data_dir), \
         patch.object(process, "AGG_DIR", agg_dir), \
         patch.object(process, "TRANS_DIR", trans_dir), \
         patch.object(process, "transaction_snapshot_id", return_value="sha256:now"), \
         patch.object(process, "_row_completeness", return_value={}), \
         patch.object(process, "_row_diff", return_value=({}, {})), \
         patch.object(process.supabase_sync, "bulk_upsert_filer_detail"), \
         patch.object(process.supabase_sync, "upsert_dashboard_cache"), \
         patch.object(process.supabase_sync, "get_dashboard_cache", return_value={}), \
         patch.object(generate_activity_snapshot, "generate", return_value=empty_snapshot):
        process.aggregate_filers(
            df,
            contributions,
            empty,
            empty,
            empty,
            empty,
            empty,
            "filer",
            "contributor_payee",
        )

    payload = json.loads((agg_dir / "balance_discrepancies.json").read_text())
    assert payload["schema_version"] == 2
    assert payload["basis"] == "paired_capture_window_v1"
    assert payload["population"] == 6
    assert payload["checked"] == 5
    assert payload["unchecked"] == 1
    assert payload["paired"] == 4
    assert payload["comparable"] == 2
    assert payload["unpaired"] == 2
    assert payload["refresh_needed"] == 1
    assert payload["nonactionable"] == 1
    assert payload["flagged"] == 2

    unpaired_by_name = {row["name"]: row for row in payload["unpaired_rows"]}
    assert unpaired_by_name["Legacy Committee"]["status"] == "legacy_unpaired"
    partial = unpaired_by_name["Partial Summary Scope"]
    assert partial["status"] == "unchecked"
    assert partial["reason"] == "summary_scope_incomplete"
    assert partial["available_filer_ids"] == ["80"]
    assert partial["missing_filer_ids"] == ["90"]

    [refresh] = payload["refresh_rows"]
    assert refresh["filer_id"] == "20"
    assert refresh["delta"] == 25.0
    assert refresh["current_calculated"] == 150.0
    assert refresh["app_balance_change_since_capture"] == 25.0

    flagged_by_id = {row["filer_id"]: row for row in payload["rows"]}
    flagged = flagged_by_id["30"]
    assert flagged["filer_id"] == "30"
    assert flagged["calculated"] == 125.0
    assert flagged["orestar"] == 100.0
    assert flagged["delta"] == 25.0
    assert flagged["current_calculated"] == 125.0
    assert flagged["app_balance_change_since_capture"] == 0.0
    assert flagged["newer_app_data"] is False

    [closed] = payload["nonactionable_rows"]
    assert closed["filer_id"] == "40"
    assert closed["delta"] == 125.0
    assert closed["reason"] == "closed_trailing_summary"
    assert closed["comparison_status"] == "paired"

    closed_detail = next(
        json.loads(path.read_text())
        for path in (agg_dir / "filers").glob("*.json")
        if json.loads(path.read_text()).get("name") == "Closed Committee"
    )
    assert closed_detail["closed"] is True
    assert closed_detail["orestar_comparison"]["actionable"] is False
    assert closed_detail["orestar_discrepancy"] is None

    mixed = flagged_by_id["60"]
    assert mixed["filer_ids"] == ["50", "60"]
    assert mixed["delta"] == 100.0
    mixed_detail = next(
        json.loads(path.read_text())
        for path in (agg_dir / "filers").glob("*.json")
        if json.loads(path.read_text()).get("name") == "Mixed Closure Scope"
    )
    assert mixed_detail["closed"] is False
    assert mixed_detail["orestar_comparison"]["actionable"] is True
    assert mixed_detail["orestar_discrepancy"] == 100.0


def test_threshold_verifier_does_not_guess_inside_multi_id_scope(
    tmp_path: Path,
) -> None:
    import verify_filer

    agg = tmp_path / "aggregated"
    filers = agg / "filers"
    filers.mkdir(parents=True)
    (agg / "filer_index.json").write_text(json.dumps([
        {"slug": "multi", "name": "Multi", "filer_id": "20"},
        {"slug": "single", "name": "Single", "filer_id": "30"},
    ]))
    (filers / "multi.json").write_text(json.dumps({
        "filer_ids": ["10", "20"], "orestar_discrepancy": 500,
        "closed": False,
        "orestar_comparison": {
            "status": "paired", "actionable": True,
            "delta_at_capture": 500,
        },
    }))
    (filers / "single.json").write_text(json.dumps({
        "filer_ids": ["30"], "orestar_discrepancy": 250,
        "closed": False,
        "orestar_comparison": {
            "status": "paired", "actionable": True,
            "delta_at_capture": 250,
        },
    }))

    with patch.object(verify_filer, "DATA_DIR", tmp_path):
        assert verify_filer.get_filers_with_discrepancies(100) == [
            ("30", "Single", 250.0)
        ]


def test_threshold_verifier_rejects_legacy_or_nonactionable_scalars(
    tmp_path: Path,
) -> None:
    import verify_filer

    agg = tmp_path / "aggregated"
    filers = agg / "filers"
    filers.mkdir(parents=True)
    entries = [
        {"slug": "legacy", "name": "Legacy"},
        {"slug": "unpaired", "name": "Unpaired"},
        {"slug": "stale", "name": "Stale"},
        {"slug": "closed", "name": "Closed"},
    ]
    (agg / "filer_index.json").write_text(json.dumps(entries))
    common = {"filer_ids": ["10"], "orestar_discrepancy": 999}
    (filers / "legacy.json").write_text(json.dumps(common))
    (filers / "unpaired.json").write_text(json.dumps({
        **common,
        "orestar_comparison": {
            "status": "legacy_unpaired", "actionable": True,
            "delta_at_capture": 999,
        },
    }))
    (filers / "stale.json").write_text(json.dumps({
        **common,
        "orestar_comparison": {
            "status": "paired", "actionable": False,
            "delta_at_capture": 999,
        },
    }))
    (filers / "closed.json").write_text(json.dumps({
        **common,
        "closed": True,
        "orestar_comparison": {
            "status": "paired", "actionable": True,
            "delta_at_capture": 999,
        },
    }))

    with patch.object(verify_filer, "DATA_DIR", tmp_path):
        assert verify_filer.get_filers_with_discrepancies(100) == []


def test_fresh_current_page_does_not_promote_old_historical_loan_fields(
    tmp_path: Path,
) -> None:
    """A current-only refresh must not turn an old parser-default zero real."""
    import generate_activity_snapshot
    import process

    data_dir = tmp_path / "data"
    agg_dir = data_dir / "aggregated"
    trans_dir = data_dir / "transactions"
    agg_dir.mkdir(parents=True)
    trans_dir.mkdir()

    old = _summary(0.0)
    old["scrape_ts"] = 1_700_000_000.0
    current = _summary(0.0)
    current["scrape_ts"] = 1_800_000_000.0
    (data_dir / "orestar_yearly_summaries.json").write_text(json.dumps({
        "70": {
            "years": {"2006": old, "2026": current},
            # The filer-level timestamp advanced in a current-only scrape.
            "ts": 1_800_000_000.0,
        }
    }))

    df = pd.DataFrame({
        "tran_id": ["old-loan"],
        "filed_date": pd.to_datetime(["2006-01-01"]),
        "amount": [100.0],
        "tran_type": ["C"],
        "sub_type": ["Loan Received (Non-Exempt)"],
        "contributor_payee": ["Candidate"],
        "filer": ["Old Loan Committee"],
        "filer id": ["70"],
        "year": [2006],
        "month": ["2006-01"],
        "book_type": ["Individual"],
        "is_out_of_state": [False],
        "_undated": [False],
    })
    empty = df.iloc[0:0].copy()
    empty_snapshot = {"meta": {"total_candidates": 0}, "legislative_map": {}}
    with patch.object(process, "DATA_DIR", data_dir), \
         patch.object(process, "AGG_DIR", agg_dir), \
         patch.object(process, "TRANS_DIR", trans_dir), \
         patch.object(process, "transaction_snapshot_id", return_value="sha256:now"), \
         patch.object(process, "_row_completeness", return_value={}), \
         patch.object(process, "_row_diff", return_value=({}, {})), \
         patch.object(process.supabase_sync, "bulk_upsert_filer_detail"), \
         patch.object(process.supabase_sync, "upsert_dashboard_cache"), \
         patch.object(process.supabase_sync, "get_dashboard_cache", return_value={}), \
         patch.object(generate_activity_snapshot, "generate", return_value=empty_snapshot):
        process.aggregate_filers(
            df, df, empty, empty, empty, empty, empty,
            "filer", "contributor_payee",
        )

    [detail_path] = (agg_dir / "filers").glob("*.json")
    detail = json.loads(detail_path.read_text())
    assert detail["cash_on_hand"] == 100.0
    assert detail["timeline"][0]["loans_received"] == 100.0

    # A multi-ID year is trustworthy only when its oldest contributing page is.
    assert process._loan_fields_trustworthy({
        "scrape_ts": 1_800_000_000.0,
        "scrape_ts_min": 1_700_000_000.0,
    }) is False


def test_zero_cash_year_with_outstanding_obligations_is_not_blank() -> None:
    import process

    active = _summary(100.0)
    obligations = _summary(0.0)
    obligations.update({
        "accounts_payable": 7_703.0,
        "total_outstanding_loans": 565_000.0,
        "outstanding_personal_expenditures": 39_689.0,
    })
    assert process._trailing_blank_closure({
        "2025": active,
        "2026": obligations,
    }) == (False, None, None)


def test_missing_or_legacy_status_fields_cannot_prove_closure() -> None:
    import process

    active = _summary(100.0)
    missing = _summary(0.0)
    missing["accounts_payable"] = None
    assert process._trailing_blank_closure({
        "2025": active,
        "2026": missing,
    }) == (False, None, None)

    legacy = _summary(0.0)
    legacy.pop("summary_field_version")
    assert process._trailing_blank_closure({
        "2025": active,
        "2026": legacy,
    }) == (False, None, None)


def test_legacy_year_timestamps_migrate_per_component_without_overwrite() -> None:
    import process

    yearly = {
        "10": {"ts": 1_800_000_000.0, "years": {"2006": _summary(0.0)}},
        "20": {"ts": 1_810_000_000.0, "years": {
            "2006": {**_summary(0.0), "scrape_ts": 1_700_000_000.0},
        }},
    }
    normalized = process._normalize_year_scrape_timestamps(yearly)

    assert normalized["10"]["years"]["2006"]["scrape_ts"] == 1_800_000_000.0
    # An explicit per-year timestamp is older evidence and must win over the
    # newer filer-level timestamp.
    assert normalized["20"]["years"]["2006"]["scrape_ts"] == 1_700_000_000.0
    assert min(
        normalized[fid]["years"]["2006"]["scrape_ts"]
        for fid in ("10", "20")
    ) == 1_700_000_000.0


def test_mixed_parser_versions_cannot_bless_multi_id_loan_fields() -> None:
    import process

    assert process._loan_fields_trustworthy({
        "scrape_ts": 1_810_000_000.0,
        "scrape_ts_min": 1_800_000_000.0,
        "summary_field_version": 2,
        "summary_field_version_min": 0,
    }) is False


def test_multi_id_summary_sum_propagates_unknown_optional_fields() -> None:
    import process

    first = _summary(10.0)
    second = _summary(20.0)
    first["loans_received"] = None
    first["accounts_payable"] = None

    assert process._sum_summary_field([first, second], "ending_cash_balance") == 30.0
    assert process._sum_summary_field([first, second], "loans_received") is None
    assert process._sum_summary_field([first, second], "accounts_payable") is None


def test_multi_id_yearly_cash_provenance_must_be_one_atomic_capture() -> None:
    import process

    first = {
        **_summary(10.0),
        "scrape_ts": 1_800_000_000.0,
        "app_year_transaction_digest": "sha256:year",
        "calculation_version": CALCULATION_VERSION,
        "scope_capture_id": "10|20@capture",
    }
    second = {
        **_summary(20.0),
        "scrape_ts": 1_800_000_001.0,
        "app_year_transaction_digest": "sha256:year",
        "calculation_version": CALCULATION_VERSION,
        "scope_capture_id": "10|20@capture",
    }
    [paired] = process._combine_scope_yearly_summaries([
        {"2026": first}, {"2026": second},
    ]).values()
    assert paired["app_year_transaction_digest"] == "sha256:year"
    assert paired["scope_capture_id"] == "10|20@capture"

    second["scope_capture_id"] = "10|20@different-crawl"
    [mixed] = process._combine_scope_yearly_summaries([
        {"2026": first}, {"2026": second},
    ]).values()
    assert "app_year_transaction_digest" not in mixed
    assert "calculation_version" not in mixed
    assert "scope_capture_id" not in mixed


def test_multi_id_year_missing_from_one_component_stays_unknown() -> None:
    import process

    first_2006 = {**_summary(0.0), "scrape_ts": 1_800_000_000.0}
    first_2020 = {**_summary(10.0), "scrape_ts": 1_800_000_001.0}
    partial_2020 = {**_summary(20.0), "scrape_ts": 1_800_000_002.0}
    combined = process._combine_scope_yearly_summaries([
        {"2006": first_2006, "2020": first_2020},
        {"2020": partial_2020},
    ])

    assert "2006" not in combined
    assert combined["2020"]["ending_cash_balance"] == 30.0


def test_capped_sweep_resumes_original_cutoff_instead_of_restarting(
    tmp_path: Path,
) -> None:
    import fetch_earliest_balances as balances

    state_path = tmp_path / "sweep-state.json"
    with patch.object(balances, "SWEEP_STATE_PATH", state_path):
        first, _ = balances._begin_or_resume_sweep(
            "historical", 1000.0, 1100.0
        )
        # A later scheduled invocation proposes a new cutoff. The unfinished
        # sweep keeps 1000 so already-completed prefix IDs stay excluded and
        # the tail can eventually be reached.
        resumed, state = balances._begin_or_resume_sweep(
            "historical", 9000.0, 9100.0
        )

    assert first == 1000.0
    assert resumed == 1000.0
    assert state["historical"]["refresh_before_ts"] == 1000.0


def test_coverage_survey_expands_actionable_multi_id_scope() -> None:
    import survey_coverage

    rows = survey_coverage._expand_actionable_rows([
        {
            "filer_id": "20",
            "filer_ids": ["10", "20"],
            "name": "Merged",
            "delta": 500,
            "comparison_status": "paired",
            "newer_app_data": False,
            "closed": False,
        },
        {
            "filer_id": "30",
            "filer_ids": ["30"],
            "delta": 900,
            "comparison_status": "paired",
            "newer_app_data": True,
        },
    ])

    assert {row["filer_id"] for row in rows} == {"10", "20"}
