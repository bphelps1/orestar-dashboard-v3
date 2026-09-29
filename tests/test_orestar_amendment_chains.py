"""Early-era amendment chains, on the chains verified on ORESTAR 2026-09-18/19.

Every chain below was read from ORESTAR's Transaction History pages. The
fixtures' HTML is ORESTAR's own markup, trimmed to the rows used.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

import orestar_amendment_chains as AC  # noqa: E402


def _history_row(tran_id, status, tran_date, filed, sub_type, payee, filed_by, amount):
    return f"""<tr valign="middle" class="evenRow">
<td valign="middle" style="text-align:right"> <a href="/orestar/transactionHistPubDetail.do?tranRsn={tran_id}&amp;transType=currentTran" title="Click to display Transaction {tran_id}"> {tran_id} <img src="/orestar/images/view.png" alt="View Transaction" border="0"> </a> </td>
<td>{status}</td> <td>{tran_date}</td> <td>{filed}</td>
<td style="text-align:center">{sub_type}</td> <td style="text-align:left">{payee} </td>
<td style="text-align:left">{filed_by}</td> <td style="text-align:right">{amount}</td> </tr>"""


HISTORY_67411 = """<html><body><table><tr><td><table style="border-collapse: separate" width="100%"><tbody>
<tr class="shadedHeader"> <th>Tran ID</th> <th>Status</th> <th>Tran Date</th> <th>Filed Date</th>
<th>Tran Subtype</th> <th>Contributor/Payee</th> <th>Filed By</th> <th>Amount</th> </tr>
""" + "\n".join([
    _history_row("21190", "Original", "10/24/2006", "02/05/2007 05:35 PM", "Cash Expenditure",
                 "Reilly, T.J. for State Senate (5322)", "Robert M Armstrong", "$40,000.00"),
    _history_row("31233", "Amended", "10/24/2006", "03/03/2007 05:05 AM", "Cash Expenditure",
                 "Reilly, T.J. for State Senate (5322)", "Robert M Armstrong", "$40,000.00"),
    _history_row("34525", "Amended", "10/24/2006", "03/13/2007 05:14 AM", "Cash Expenditure",
                 "T.J. Reilly For State Senate (5322)", "Robert M Armstrong", "$40,000.00"),
    _history_row("67411", "Deleted", "10/24/2006", "06/21/2007 09:16 AM", "Cash Expenditure",
                 "Reilly, T.J. for State Senate (5322)", "Robert M Armstrong", "$40,000.00"),
]) + "</tbody></table></td></tr></table></body></html>"


def _results_row(tran_id, tran_date, status, payee, sub_type, amount):
    return f"""<tr valign="middle" class="evenRow"> <!-- Display RSN -->
<td valign="middle" style="text-align:right"> <a href="/orestar/gotoPublicTransactionDetail.do?tranRsn={tran_id}" title="Click to display Transaction {tran_id}"> {tran_id} <img src="/orestar/images/view.png" alt="View Transaction" border="0"> </a> </td>
<td>{tran_date}</td> <td>{status}</td>
<td style="text-align:left"> <a href="/orestar/sooDetail.do?cneCommitteeId=3571"> Bradbury, Bill, Friends of </a> </td>
<td style="text-align:left"> {payee} </td> <td style="text-align:left">{sub_type}</td>
<td style="text-align:right">{amount}</td> </tr>"""


RESULTS_3571 = """<html><body><table><tr><td>Results :\t3 records found for the above search criteria\t</td></tr></table>
<table><tr class="shadedHeader"> <th> <a href="#"><img src="/up.gif"></a> Tran ID </th> <th>Tran Date</th>
<th>Status</th> <th>Filer/Committee</th> <th>Contributor/Payee</th> <th>Sub Type</th> <th>Amount</th> </tr>
""" + "\n".join([
    _results_row("51330", "04/30/2007", "Original *", "Miscellaneous Cash Expenditures $100 and under",
                 "Cash Expenditure", "$50.00"),
    _results_row("54956", "04/30/2007", "Amended *", "Miscellaneous Cash Expenditures $100 and under",
                 "Cash Expenditure", "$30.95"),
    _results_row("66827", "04/15/2007", "Deleted", "Miscellaneous Cash Expenditures $100 and under",
                 "Cash Expenditure", "$5.95"),
]) + "</table></body></html>"


def test_history_page_parses_every_version() -> None:
    versions = AC.parse_history_page(HISTORY_67411)

    assert [(v["tran_id"], v["status"], v["amount"]) for v in versions] == [
        ("21190", "Original", 40000.0),
        ("31233", "Amended", 40000.0),
        ("34525", "Amended", 40000.0),
        ("67411", "Deleted", 40000.0),
    ]
    assert versions[1]["filed_date"] == "03/03/2007 05:05 AM"
    assert versions[2]["payee"] == "T.J. Reilly For State Senate (5322)"


def test_results_page_parses_statuses_and_the_expired_mark() -> None:
    count, rows = AC.parse_results_page(RESULTS_3571)

    assert count == 3
    assert [(r["tran_id"], r["status"], r["expired"], r["amount"]) for r in rows] == [
        ("51330", "Original", True, 50.0),
        ("54956", "Amended", True, 30.95),
        ("66827", "Deleted", False, 5.95),
    ]


def test_unrendered_pages_are_not_mistaken_for_empty_ones() -> None:
    challenge = "<html><body><script>/* F5 challenge */</script></body></html>"
    assert AC.parse_results_page(challenge) is None
    assert AC.parse_history_page(challenge) is None
    assert AC.parse_results_page("<p>No records found for the above search criteria</p>") == (0, [])


def _v(tran_id, status, amount, filed, sub_type="Cash Expenditure", tran_date="04/30/2007"):
    return {"tran_id": tran_id, "status": status, "amount": amount, "filed_date": filed,
            "sub_type": sub_type, "tran_date": tran_date}


FERRIOLI_40K = [
    _v("21190", "Original", 40000.0, "02/05/2007 05:35 PM", tran_date="10/24/2006"),
    _v("31233", "Amended", 40000.0, "03/03/2007 05:05 AM", tran_date="10/24/2006"),
    _v("34525", "Amended", 40000.0, "03/13/2007 05:14 AM", tran_date="10/24/2006"),
    _v("67411", "Deleted", 40000.0, "06/21/2007 09:16 AM", tran_date="10/24/2006"),
]
FERRIOLI_15K = [
    _v("21195", "Original", 15000.0, "02/05/2007 05:35 PM", tran_date="10/30/2006"),
    *[_v(t, "Amended", 15000.0, "03/13/2007 05:14 AM", tran_date="10/30/2006")
      for t in ("31234", "31237", "31239", "34527")],
    *[_v(t, "Deleted", 15000.0, "06/21/2007 09:16 AM", tran_date="10/30/2006")
      for t in ("67412", "67413", "67414", "67416")],
]
GARDNER_KEY_BANK = [
    _v("58731", "Original", 109.50, "06/07/2007 05:00 PM", tran_date="05/08/2007"),
    _v("67292", "Amended", 109.50, "06/20/2007 02:12 PM", tran_date="05/08/2007"),
    _v("72103", "Amended", 106.50, "06/29/2007 03:44 PM", tran_date="05/08/2007"),
    _v("72440", "Deleted", 109.50, "06/30/2007 01:00 PM", tran_date="05/08/2007"),
]
GARDNER_MISC_0416 = [
    _v("46676", "Original", 2.50, "04/17/2007 11:30 AM", tran_date="04/16/2007"),
    _v("51337", "Amended", 2.50, "04/30/2007 04:34 PM", tran_date="04/16/2007"),
    _v("55374", "Amended", 46.90, "05/10/2007 04:10 PM", tran_date="04/16/2007"),
    _v("58736", "Amended", 46.90, "05/21/2007 03:08 PM", tran_date="04/16/2007"),
    _v("72436", "Deleted", 46.90, "06/30/2007 01:00 PM", tran_date="04/16/2007"),
]
BRADBURY = [
    _v("51330", "Original", 50.00, "05/03/2007 10:31 AM"),
    _v("54956", "Amended", 30.95, "05/09/2007 08:51 PM"),
    _v("59051", "Amended", 30.95, "05/23/2007 11:01 PM"),
    _v("64304", "Amended", 80.95, "06/11/2007 10:25 PM"),
]


@pytest.mark.parametrize("versions, counted_total", [
    (FERRIOLI_40K, 40000.00),        # two amendments, one deletion
    (FERRIOLI_15K, 0.00),            # four and four cancel
    (GARDNER_KEY_BANK, 106.50),      # the deletion matches the $109.50 copy
    (GARDNER_MISC_0416, 49.40),      # $2.50 + one $46.90 survive
    (BRADBURY, 142.85),              # every amendment, no deletion
])
def test_early_era_counting_on_verified_chains(versions, counted_total) -> None:
    counted = AC.counted_versions_2007(versions)
    assert round(sum(v["amount"] for v in counted), 2) == counted_total


def test_unamended_original_counts_and_a_deletion_cancels_it() -> None:
    lone = [_v("157612", "Original", 644.17, "11/24/2007 10:00 AM")]
    assert AC.counted_versions_2007(lone) == lone
    deleted = lone + [_v("158013", "Deleted", 644.17, "12/01/2007 10:00 AM")]
    assert AC.counted_versions_2007(deleted) == []


def test_a_live_original_counts_beside_its_amendment() -> None:
    # Oregon Firearms Federation PAC 2041931 / 2041946.
    chain = [_v("2041931", "Original", 100.0, "07/20/2015 01:32 PM", "Cash Contribution"),
             _v("2041946", "Amended", 100.0, "07/20/2015 01:43 PM", "Cash Contribution")]
    assert [v["tran_id"] for v in AC.counted_versions_2007(chain, original_live=True)] == [
        "2041931", "2041946"]


def test_a_deletion_cancels_an_expired_version_before_the_live_one() -> None:
    # Gardner misc cash 02/28/2007: three $1.90 amendments, one live, two deletions.
    chain = [
        _v("38054", "Original", 15.00, "03/01/2007 10:00 AM"),
        _v("38062", "Amended", 1.90, "03/02/2007 10:00 AM"),
        _v("40154", "Amended", 1.90, "03/05/2007 10:00 AM"),
        _v("40320", "Amended", 1.90, "03/06/2007 10:00 AM"),
        _v("72437", "Deleted", 1.90, "06/30/2007 01:00 PM"),
        _v("72438", "Deleted", 1.90, "06/30/2007 01:00 PM"),
    ]
    counted = AC.counted_versions_2007(chain, live_ids={"40154"})
    assert [v["tran_id"] for v in counted] == ["40154"]


def test_a_deletion_that_matches_nothing_is_not_guessed() -> None:
    chain = [_v("1", "Original", 10.0, "03/01/2007 10:00 AM"),
             _v("2", "Amended", 12.0, "03/02/2007 10:00 AM"),
             _v("3", "Deleted", 99.0, "03/03/2007 10:00 AM")]
    assert AC.counted_versions_2007(chain) is None


def _row(tran_id, status, amount, expired=False, sub_type="Cash Expenditure",
         tran_date="04/30/2007"):
    return {"tran_id": tran_id, "status": status, "expired": expired, "amount": amount,
            "sub_type": sub_type, "tran_date": tran_date, "payee": "Payee"}


def _bradbury_entry(reported=None):
    rows = [
        _row("1001", "Original", 500.00),
        _row("1002", "Original", 25.00, sub_type="Personal Expenditure for Reimbursement"),
        _row("51330", "Original", 50.00, expired=True),
        _row("54956", "Amended", 30.95, expired=True),
        _row("59051", "Amended", 30.95, expired=True),
        _row("64304", "Amended", 80.95),
        _row("48702", "Original", 5.95, expired=True, tran_date="04/15/2007"),
        _row("66827", "Deleted", 5.95, tran_date="04/15/2007"),
    ]
    return {
        "filer_id": "3571", "year": 2007, "tran_type": "E",
        "reported": len(rows) if reported is None else reported,
        "rows": rows,
        "chains": [
            {"versions": BRADBURY},
            {"versions": [_v("48702", "Original", 5.95, "04/16/2007 10:00 AM", tran_date="04/15/2007"),
                          _v("66827", "Deleted", 5.95, "06/01/2007 10:00 AM", tran_date="04/15/2007")]},
        ],
    }


# ORESTAR's line: $500 standalone + $142.85 from the chain; in-kind $10 is
# part of the total and subtracted.
BRADBURY_SUMMARY = {"expenditures": 652.85, "inkind_expenditures": 10.0}
BRADBURY_HELD = {"1001": 500.00, "64304": 80.95}


def test_bradbury_is_reconciled_by_adding_the_expired_amendments() -> None:
    decision = AC.evaluate_target(_bradbury_entry(), BRADBURY_SUMMARY, BRADBURY_HELD, "E")

    assert decision["applied"] is True
    assert decision["model_total"] == decision["orestar_line"] == 642.85
    assert decision["adjustments"] == [
        {"tran_id": "54956", "effect": "add", "amount": 30.95},
        {"tran_id": "59051", "effect": "add", "amount": 30.95},
    ]
    assert decision["moved"] == 61.90
    assert [v["tran_id"] for v in decision["chains"][0]["versions"]] == [
        "51330", "54956", "59051", "64304"]


def test_a_modern_summary_is_left_alone() -> None:
    # Julie for County Commissioner: ORESTAR counts only the newest amendment,
    # so the early-era count overshoots its line and nothing is applied.
    modern = {"expenditures": 580.95, "inkind_expenditures": 0.0}
    decision = AC.evaluate_target(_bradbury_entry(), modern, BRADBURY_HELD, "E")

    assert decision["applied"] is False
    assert decision["reason"] == "model_does_not_reproduce_summary"
    assert decision["adjustments"] == []


@pytest.mark.parametrize("mutate, reason", [
    (lambda e, h: e.update(reported=9), "listing_incomplete"),
    (lambda e, h: e["chains"].pop(), "chain_not_collected"),
    (lambda e, h: e["chains"][0]["versions"][1].update(tran_date="01/03/2008"),
     "chain_crosses_year"),
    (lambda e, h: h.update({"9999": 1.0}), "held_row_not_listed"),
    (lambda e, h: h.pop("1001"), "listed_row_not_held"),
])
def test_every_gate_refuses_rather_than_guesses(mutate, reason) -> None:
    import copy
    entry = copy.deepcopy(_bradbury_entry())
    held = dict(BRADBURY_HELD)
    mutate(entry, held)

    decision = AC.evaluate_target(entry, BRADBURY_SUMMARY, held, "E")

    assert decision["applied"] is False
    assert decision["reason"] == reason


def test_a_year_already_matching_needs_no_adjustment() -> None:
    held = {**BRADBURY_HELD, "54956": 30.95, "59051": 30.95}
    decision = AC.evaluate_target(_bradbury_entry(), BRADBURY_SUMMARY, held, "E")
    assert decision["applied"] is False
    assert decision["reason"] == "already_reconciled"


def test_merge_keeps_stored_entries_when_a_collection_is_partial() -> None:
    good = _bradbury_entry()
    stored = AC.merge_targets(None, [good], "2026-09-19T00:00:00Z")
    partial = {**_bradbury_entry(), "reported": 99}

    merged = AC.merge_targets(stored, [partial], "2026-09-20T00:00:00Z")

    entry = merged["targets"]["3571:2007:E"]
    assert entry["fetched_at"] == "2026-09-19T00:00:00Z"
    assert AC.targets_for_filer(merged, "3571") == [entry]


# ── Through the real aggregation ─────────────────────────────────────────────


def _agg_row(tran_id: str, amount: float, sub_type: str = "Cash Expenditure"):
    import pandas as pd
    from test_nonreturning_balance_guard import _cash_row

    when = pd.Timestamp("2007-04-30")
    row = _cash_row(tran_id, amount, "E", sub_type)
    row.update({"filed_date": when, "tran_date": when, "year": 2007, "month": "2007-04"})
    return row


def _aggregate(tmp_path, summary_expenditures: float, with_chains: bool = True):
    import json
    from test_nonreturning_balance_guard import _aggregate_cash_rows

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    if with_chains:
        entry = {**_bradbury_entry(), "filer_id": "1"}
        (data_dir / AC.CHAINS_FILENAME).write_text(json.dumps(
            AC.merge_targets(None, [entry], "2026-09-19T00:00:00Z")))
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {"2007": {
        "beginning_balance": 0.0, "ending_cash_balance": -summary_expenditures + 10.0,
        "contributions": 10.0, "inkind_contributions": 10.0,
        "expenditures": summary_expenditures, "inkind_expenditures": 10.0,
        "other_receipts": 0.0, "other_disbursements": 0.0, "balance_adjustments": 0.0,
    }}}}
    return _aggregate_cash_rows(
        tmp_path, [_agg_row("1001", 500.00), _agg_row("64304", 80.95)], set(), yearly,
    )


def test_aggregation_lands_on_orestars_line_and_names_the_chain(tmp_path) -> None:
    from test_nonreturning_balance_guard import _timeline_net

    detail = _aggregate(tmp_path, 652.85)

    # Held: -$580.95. ORESTAR also counts the expired $30.95 amendments.
    assert detail["cash_on_hand"] == -642.85
    assert _timeline_net(detail) == -642.85
    [applied] = detail["orestar_amendment_chains"]
    assert applied["cash_effect"] == -61.90
    assert [a["tran_id"] for a in applied["adjustments"]] == ["54956", "59051"]
    assert {v["tran_id"] for v in applied["chains"][0]["versions"]} == {
        "51330", "54956", "59051", "64304"}
    assert detail["yearly_discrepancies"]["2007"]["amendment_chain_adjustment"] == -61.90


def test_aggregation_changes_nothing_when_the_count_does_not_reproduce(tmp_path) -> None:
    detail = _aggregate(tmp_path, 590.95)       # a modern-style summary

    assert detail["cash_on_hand"] == -580.95
    assert detail["orestar_amendment_chains"] == []


def test_aggregation_without_a_chains_file_is_unchanged(tmp_path) -> None:
    detail = _aggregate(tmp_path, 652.85, with_chains=False)

    assert detail["cash_on_hand"] == -580.95
    assert detail["orestar_amendment_chains"] == []


# ── Collector: the parts that need no browser ────────────────────────────────


def test_targets_parse_into_one_collection_per_type() -> None:
    import fetch_amendment_chains as F

    assert F.parse_target("3215:2006:C,E") == [("3215", 2006, "C"), ("3215", 2006, "E")]
    assert F.parse_target("21452:2021:OR,OD") == [("21452", 2021, "OR"), ("21452", 2021, "OD")]
    for bad in ("3215:2006", "x:2006:E", "3215:06:E", "3215:2006:O", "3215:2006:OA"):
        with pytest.raises(ValueError):
            F.parse_target(bad)


def test_result_pages_follow_orestars_paging_scheme() -> None:
    from urllib.parse import parse_qs, urlsplit

    import fetch_amendment_chains as F

    first = parse_qs(urlsplit(F.results_url("TOK", "2281", 2007, "E", 1)).query)
    third = parse_qs(urlsplit(F.results_url("TOK", "2281", 2007, "E", 3)).query)
    assert first["cneSearchButtonName"] == ["search"] and first["cneSearchPageIdx"] == ["0"]
    assert third["cneSearchButtonName"] == ["next"] and third["cneSearchPageIdx"] == ["1"]
    for query in (first, third):
        assert query["viewDeletedTransactions"] == ["on"]
        assert query["viewExpiredTransactions"] == ["on"]
        assert query["cneSearchTranStartDate"] == ["01/01/2007"]
        assert query["cneSearchTranType"] == ["E"]



# ── Other receipts and other disbursements ───────────────────────────────────


def _od_entry(rows, chains):
    return {"filer_id": "1", "year": 2021, "tran_type": "OD",
            "reported": len(rows), "rows": rows, "chains": chains}


def test_an_other_disbursement_chain_reconciles_like_the_cash_lines() -> None:
    rows = [
        _row("900", "Original", 200.0, sub_type="Return or Refund of Contribution",
             tran_date="04/22/2021"),
        _row("901", "Original", 50.0, expired=True,
             sub_type="Return or Refund of Contribution", tran_date="04/22/2021"),
        _row("902", "Amended", 50.0, expired=True,
             sub_type="Return or Refund of Contribution", tran_date="04/22/2021"),
        _row("903", "Amended", 50.0, sub_type="Return or Refund of Contribution",
             tran_date="04/22/2021"),
    ]
    chain = [_v(t, st, 50.0, f"05/0{i + 1}/2021 10:00 AM",
                sub_type="Return or Refund of Contribution", tran_date="04/22/2021")
             for i, (t, st) in enumerate((("901", "Original"), ("902", "Amended"),
                                          ("903", "Amended")))]
    decision = AC.evaluate_target(
        _od_entry(rows, [{"versions": chain}]),
        {"other_disbursements": 300.0},        # 200 + both $50 amendments
        {"900": 200.0, "903": 50.0},
        "OD",
    )

    assert decision["applied"] is True
    assert decision["adjustments"] == [{"tran_id": "902", "effect": "add", "amount": 50.0}]


def test_a_summary_that_drops_a_live_row_is_not_explained_by_chains() -> None:
    # Greg Stoll 2021: ORESTAR lists the $100 lumped refund 3803605 as a live
    # original yet reports $0 of other disbursements. No chain can reproduce
    # that, so the gate refuses and nothing moves.
    rows = [_row("3803605", "Original", 100.0, sub_type="Return or Refund of Contribution",
                 tran_date="04/22/2021")]
    decision = AC.evaluate_target(_od_entry(rows, []), {"other_disbursements": 0.0},
                                  {"3803605": 100.0}, "OD")

    assert decision["applied"] is False
    assert decision["reason"] == "model_does_not_reproduce_summary"


def test_other_receipts_leave_exempt_loans_to_their_own_line() -> None:
    assert "Loan Received (Exempt)" not in AC.CASH_BUCKETS["OR"]
    assert "Loan Payment (Exempt)" not in AC.CASH_BUCKETS["OD"]
    assert AC.summary_line({"other_receipts": 422.72}, "OR") == 422.72
    assert AC.CASH_SIGN == {"C": 1.0, "OR": 1.0, "E": -1.0, "OD": -1.0}


def test_aggregation_applies_an_other_receipt_chain_and_ignores_exempt_loans(tmp_path) -> None:
    import json

    import pandas as pd
    from test_nonreturning_balance_guard import _aggregate_cash_rows, _cash_row

    def agg_row(tran_id, amount, sub_type):
        row = _cash_row(tran_id, amount, "OR", sub_type)
        when = pd.Timestamp("2014-11-05")
        row.update({"filed_date": when, "tran_date": when, "year": 2014, "month": "2014-11"})
        return row

    entry = {
        "filer_id": "1", "year": 2014, "tran_type": "OR", "reported": 4,
        "rows": [
            _row("10", "Original", 500.0, sub_type="Lost or Returned Check", tran_date="11/05/2014"),
            _row("11", "Original", 50.0, expired=True, sub_type="Lost or Returned Check",
                 tran_date="11/05/2014"),
            _row("12", "Amended", 50.0, expired=True, sub_type="Lost or Returned Check",
                 tran_date="11/05/2014"),
            _row("14", "Amended", 50.0, sub_type="Lost or Returned Check", tran_date="11/05/2014"),
        ],
        "chains": [{"versions": [
            _v("11", "Original", 50.0, "11/06/2014 10:00 AM", sub_type="Lost or Returned Check",
               tran_date="11/05/2014"),
            _v("12", "Amended", 50.0, "11/07/2014 10:00 AM", sub_type="Lost or Returned Check",
               tran_date="11/05/2014"),
            _v("14", "Amended", 50.0, "11/08/2014 10:00 AM", sub_type="Lost or Returned Check",
               tran_date="11/05/2014"),
        ]}],
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / AC.CHAINS_FILENAME).write_text(json.dumps(
        AC.merge_targets(None, [entry], "2026-09-19T00:00:00Z")))
    # ORESTAR counts both $50 amendments; exempt loans sit on their own line.
    yearly = {"1": {"ts": 1_800_000_000.0, "years": {"2014": {
        "beginning_balance": 0.0, "ending_cash_balance": 600.0,
        "contributions": 0.0, "expenditures": 0.0, "other_receipts": 600.0,
        "other_disbursements": 0.0, "balance_adjustments": 0.0,
        "loans_received_exempt": 30.0, "loan_payments_exempt": 30.0,
    }}}}
    rows = [agg_row("10", 500.0, "Lost or Returned Check"),
            agg_row("14", 50.0, "Lost or Returned Check"),
            agg_row("13", 30.0, "Loan Received (Exempt)")]

    detail = _aggregate_cash_rows(tmp_path, rows, set(), yearly)

    # Had the exempt loan leaked into the other-receipts check it would be a
    # held row ORESTAR's listing lacks, and the gate would refuse. It applies.
    [applied] = detail["orestar_amendment_chains"]
    assert applied["bucket"] == "OR" and applied["cash_effect"] == 50.0
    assert applied["adjustments"] == [{"tran_id": "12", "effect": "add", "amount": 50.0}]
