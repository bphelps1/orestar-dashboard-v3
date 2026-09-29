"""Discontinued committees and Independent Expenditure Filers.

Both look alike in stored summaries (a balance that drops to $0.00 with no
transaction behind it) and mean different things on ORESTAR, verified
2026-09-19:

* A committee that filed a Discontinuation gets blank statements afterwards,
  opening at $0.00. The balance follows ORESTAR (Total Recall PAC, 21075).
* An Independent Expenditure Filer (ORESTAR filer type "IF") has no published
  balance at all; the page script hides it. None is shown or compared.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent))

import fetch_earliest_balances as FEB  # noqa: E402
import fetch_filer_metadata as FFM  # noqa: E402
import orestar_closures as OC  # noqa: E402
import orestar_parse  # noqa: E402
from test_nonreturning_balance_guard import _aggregate_cash_rows, _cash_row  # noqa: E402


# ── ORESTAR's filer type ─────────────────────────────────────────────────────

HIDE_FIELDS = """<script type="text/javascript">
 function hideFields() { var filerType = "%s";
 if ( filerType != null && filerType === "IF" ) {
   document.getElementById( "independentFilerSummary" ).style.display = "block";
   document.getElementById( "committeeSummary" ).style.display = "none"; } }
</script>"""


@pytest.mark.parametrize("code, expected", [("IF", "IF"), ("PAC", "PAC"), ("CC", "CC"), ("", None)])
def test_filer_type_is_read_from_orestars_page_script(code, expected) -> None:
    assert orestar_parse.parse_filer_type(HIDE_FIELDS % code) == expected
    assert orestar_parse.parse_filer_type("<html>no script</html>") is None


def test_each_parsed_statement_records_the_filer_type() -> None:
    rows = "".join(
        f"<tr><td>{label}</td><td>&nbsp;</td><td>$0.00</td></tr>"
        for label in ("Beginning Balance (Previous Year)", "Total Contributions",
                      "Total Expenditures", "Other Receipts", "Other Disbursements",
                      "Balance Adjustments", "Ending Cash Balance")
    )
    html = f"<html>{HIDE_FIELDS % 'IF'}<table>{rows}</table></html>"

    summary = FEB._parse_yearly_summary(html)

    assert summary is not None and summary["filer_type"] == "IF"


# ── The Discontinuation filing ───────────────────────────────────────────────

TOTAL_RECALL_PAGE = (
    "Statement of Organization for Political Action Committee\n"
    "Committee Information\n"
    "Name:\tTotal Recall PAC\tID:\t21075\n"
    "Acronym:\t\tPAC Type:\tMiscellaneous\n"
    "Filing Effective From:\t02/07/2026 to 02/07/2026\tFiling Type:\tDiscontinuation\n"
    "Address:\t3403 NE Stanton St\n"
)


def test_metadata_records_the_filing_type_and_its_dates() -> None:
    parsed = FFM._parse_detail_text(TOTAL_RECALL_PAGE, "21075")

    assert parsed["filing_type"] == "Discontinuation"
    assert parsed["filing_effective_from"] == "02/07/2026"
    assert parsed["filing_effective_to"] == "02/07/2026"
    assert OC.discontinued_on(parsed) == "02/07/2026"


def test_an_active_committee_is_not_discontinued() -> None:
    active = TOTAL_RECALL_PAGE.replace(
        "02/07/2026 to 02/07/2026\tFiling Type:\tDiscontinuation",
        "04/18/2019 to present\tFiling Type:\tAmendment")
    parsed = FFM._parse_detail_text(active, "15038")

    assert parsed["filing_effective_to"] == "present"
    assert OC.discontinued_on(parsed) is None
    assert OC.discontinued_on({"filing_type": "Discontinuation"}) is None


# ── The closure rule ─────────────────────────────────────────────────────────

def _statement(begin, end, **activity):
    row = {"beginning_balance": begin, "ending_cash_balance": end}
    for key in ("contributions", "expenditures", "other_receipts",
                "other_disbursements", "balance_adjustments"):
        row[key] = activity.get(key, 0.0)
    return row


TOTAL_RECALL = {
    "2021": _statement(1658.61, 8428.85, contributions=140458.80, expenditures=140385.46,
                       other_receipts=8596.90, other_disbursements=1900.00),
    "2022": _statement(8428.85, 8459.85, contributions=1430.00, expenditures=1399.00),
    "2023": _statement(8459.85, 8459.85),
    "2024": _statement(8459.85, 8459.85),
    "2025": _statement(0.0, 0.0),
    "2026": _statement(0.0, 0.0),
}


def test_total_recall_pac_follows_orestar_to_zero() -> None:
    spec = OC.closure_reset(TOTAL_RECALL, (), {2021: 6770.24, 2022: 31.00}, "02/07/2026")

    assert spec == {
        "date": "2025-01-01", "year": 2025, "amount": -8459.85,
        "boundary": [2024, 2025], "orestar_prior_ending": 8459.85,
        "tail_offset": 0.0, "discontinued_on": "02/07/2026",
    }


def test_our_own_rows_in_the_blank_years_are_not_counted_twice() -> None:
    # Promote Oregon Leadership PAC: its final $968.02 payment (01/31/2022) is
    # a row we hold, though ORESTAR's blank 2022 statement ignores it.
    years = {"2021": _statement(4397.94, 968.02, expenditures=12723.18,
                                balance_adjustments=4193.90),
             "2022": _statement(0.0, 0.0)}

    assert OC.closure_reset(years, (), {2022: -968.02}, "12/20/2021") is None


def test_a_gap_from_before_the_reset_stays_visible() -> None:
    # Our rows carry $100 ORESTAR never saw; the reset removes only ORESTAR's
    # own close, so the committee still ends $100 off.
    spec = OC.closure_reset(TOTAL_RECALL, (), {}, "02/07/2026")
    our_close = 8459.85 + 100.0
    assert round(our_close + spec["amount"], 2) == 100.0


@pytest.mark.parametrize("years, certs, discontinued", [
    (TOTAL_RECALL, (), None),                                  # no Discontinuation filing
    (TOTAL_RECALL, (2024,), "02/07/2026"),                     # certificate owns the step
    (TOTAL_RECALL, (2025,), "02/07/2026"),
    (TOTAL_RECALL, (), "03/01/2023"),                          # discontinued before its last real year
    ({**TOTAL_RECALL, "2026": _statement(0.0, 25.0, contributions=25.0)}, (), "02/07/2026"),
    ({k: v for k, v in TOTAL_RECALL.items() if k != "2025"}, (), "02/07/2026"),  # years not consecutive
    ({"2025": _statement(0.0, 0.0), "2026": _statement(0.0, 0.0)}, (), "02/07/2026"),  # never active
    ({**TOTAL_RECALL, "2024": _statement(8459.85, 0.0, expenditures=8459.85)}, (), "02/07/2026"),
])
def test_the_rule_refuses_everything_else(years, certs, discontinued) -> None:
    assert OC.closure_reset(years, certs, {}, discontinued) is None


# ── Through the real aggregation ─────────────────────────────────────────────

def _row(tran_id, amount, tran_type, sub_type, day):
    when = pd.Timestamp(day)
    row = _cash_row(tran_id, amount, tran_type, sub_type)
    row.update({"filed_date": when, "tran_date": when, "year": when.year,
                "month": when.to_period("M").strftime("%Y-%m")})
    return row


def _write_metadata(tmp_path, entry):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "filer_metadata.json").write_text(json.dumps({"1": entry}))


def test_aggregation_ends_a_discontinued_committee_at_zero(tmp_path) -> None:
    _write_metadata(tmp_path, {"committee_type": "Political Action Committee",
                               "filing_type": "Discontinuation",
                               "filing_effective_from": "02/07/2026",
                               "filing_effective_to": "02/07/2026"})
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {
        "2022": _statement(0.0, 8459.85, contributions=10000.0, expenditures=1540.15),
        "2023": _statement(8459.85, 8459.85),
        "2024": _statement(8459.85, 8459.85),
        "2025": _statement(0.0, 0.0),
    }}}
    rows = [_row("c1", 10000.0, "C", "Cash Contribution", "2022-03-01"),
            _row("e1", 1540.15, "E", "Cash Expenditure", "2022-06-01")]

    detail = _aggregate_cash_rows(tmp_path, rows, set(), yearly)

    assert detail["cash_on_hand"] == 0.0
    [reset] = detail["orestar_closure_resets"]
    assert reset["amount"] == -8459.85 and reset["discontinued_on"] == "02/07/2026"
    assert detail["yearly_discrepancies"]["2025"]["closure_reset"] == -8459.85
    assert detail["balance_published"] is True


def test_aggregation_leaves_an_undiscontinued_committee_alone(tmp_path) -> None:
    _write_metadata(tmp_path, {"committee_type": "Political Action Committee",
                               "filing_type": "Amendment",
                               "filing_effective_from": "04/18/2019",
                               "filing_effective_to": "present"})
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {
        "2022": _statement(0.0, 8459.85, contributions=10000.0, expenditures=1540.15),
        "2023": _statement(0.0, 0.0),
    }}}
    rows = [_row("c1", 10000.0, "C", "Cash Contribution", "2022-03-01"),
            _row("e1", 1540.15, "E", "Cash Expenditure", "2022-06-01")]

    detail = _aggregate_cash_rows(tmp_path, rows, set(), yearly)

    assert detail["cash_on_hand"] == 8459.85
    assert detail["orestar_closure_resets"] == []


def test_an_independent_expenditure_filer_has_no_balance_to_compare(tmp_path) -> None:
    _write_metadata(tmp_path, {"committee_type": "", "not_found": True})
    statement = {**_statement(0.0, -9640.0, expenditures=9640.0), "filer_type": "IF"}
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {
        "2019": statement,
        "2022": {**_statement(0.0, 0.0), "filer_type": "IF"},
    }}}
    rows = [_row("e1", 9040.0, "E", "Cash Expenditure", "2019-08-06"),
            _row("e2", 600.0, "E", "Cash Expenditure", "2019-03-29")]

    detail = _aggregate_cash_rows(tmp_path, rows, set(), yearly, paired_summary=True)

    assert detail["balance_published"] is False
    assert detail["filer_kind"] == "independent_expenditure"
    assert detail["orestar_closure_resets"] == []        # not a closure
    disc = json.loads((tmp_path / "data" / "aggregated" /
                       "balance_discrepancies.json").read_text())
    assert disc["no_published_balance"] == 1
    assert not any(r.get("filer_ids") == ["1"]
                   for key in ("rows", "refresh_rows", "nonactionable_rows")
                   for r in disc.get(key) or [])


# ── Statewide cash leaves independent expenditure filers out ────────────────

def test_statewide_cash_excludes_independent_expenditure_filers(tmp_path) -> None:
    from unittest.mock import patch

    import generate_activity_snapshot
    import process as P

    data_dir = tmp_path / "data"
    agg_dir = data_dir / "aggregated"
    transactions = data_dir / "transactions"
    agg_dir.mkdir(parents=True)
    transactions.mkdir()
    (data_dir / "orestar_yearly_summaries.json").write_text(json.dumps({
        "1": {"ts": 1_800_000_000.0, "years": {
            "2019": {**_statement(0.0, -9640.0, expenditures=9640.0), "filer_type": "IF"}}},
        "2": {"ts": 1_800_000_000.0, "years": {
            "2019": {**_statement(0.0, 500.0, contributions=500.0), "filer_type": "PAC"}}},
    }))
    independent = _row("e1", 9640.0, "E", "Cash Expenditure", "2019-08-06")
    independent.update({"filer": "Tyler Miller", "filer id": "1"})
    committee = _row("c1", 500.0, "C", "Cash Contribution", "2019-05-01")
    committee.update({"filer": "A Committee", "filer id": "2"})
    df = pd.DataFrame([independent, committee])

    with patch.object(P, "DATA_DIR", data_dir), \
         patch.object(P, "AGG_DIR", agg_dir), \
         patch.object(P, "TRANS_DIR", transactions), \
         patch.object(P, "transaction_snapshot_id", return_value="sha256:now"), \
         patch.object(P, "_row_completeness", return_value={}), \
         patch.object(P, "_row_diff", return_value=({}, [])), \
         patch.object(P, "_certified_orestar_absent", return_value=({}, set(), None)), \
         patch.object(P.supabase_sync, "bulk_upsert_filer_detail"), \
         patch.object(P.supabase_sync, "upsert_dashboard_cache"), \
         patch.object(P.supabase_sync, "get_dashboard_cache", return_value={}), \
         patch.object(generate_activity_snapshot, "generate",
                      return_value={"meta": {"total_candidates": 0}, "legislative_map": {}}):
        result = P.aggregate_filers(
            df, df[df["tran_type"] == "C"], df.iloc[0:0], df[df["tran_type"] == "E"],
            df.iloc[0:0], df.iloc[0:0], df.iloc[0:0], "filer", "contributor_payee",
        )

    # Only the committee's cash is statewide cash.
    assert result["global_cash_on_hand"] == 500.0
    assert round(sum(result["global_cash_timeline"].values()), 2) == 500.0
    assert result["global_cash_excludes_independent_filers"] == {
        "filers": 1, "cash_excluded": -9640.0}
    index = json.loads((agg_dir / "filer_index.json").read_text())
    assert {r["name"]: r["balance_published"] for r in index} == {
        "Tyler Miller": False, "A Committee": True}
