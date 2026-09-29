"""Certificates of Limited Contributions and Expenditures, and their ghost rows."""

from __future__ import annotations

import sys
from pathlib import Path

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import orestar_certificates as OC  # noqa: E402


# Mirrors the live page's structure: results nested inside layout tables, a
# sortable header, a committee cell carrying the candidate name before a <br>,
# and one certificate that expired mid-year.
def _page(rows_html: str, *, header: bool = True) -> str:
    head = (
        '<tr class="shadeHeader"><th class="shadeHeader"> Year </th>'
        '<th class="shadeHeader">Date Filed&nbsp; <a href="javaScript:setSortFlds(\'date\', \'a\')">'
        '<img src="/orestar/images/white-up.gif"></a> </th>'
        '<th class="shadeHeader"> Committee <a href="#"><img src="x.gif"></a></th>'
        '<th class="shadeHeader"> Expiration Date </th>'
        '<th class="shadeHeader"> Submitted By </th></tr>'
    ) if header else ""
    return (
        "<html><body><table><tr><td>Public Search</td><td>"
        "<table><tr><td><script src='/TSPD/abc'></script>"
        f"<table>{head}{rows_html}</table>"
        "</td></tr></table></td></tr></table></body></html>"
    )


BUNN_2015 = (
    '<tr class="evenRow"><td> 2015 </td><td> 02/11/2015 </td>'
    '<td> Bunn, Daniel <br> Friends of Daniel Bunn (15764) </td>'
    '<td> </td><td> Daniel L Bunn </td></tr>'
)
BAKER_2015 = (
    '<tr class="oddRow"><td> 2015 </td><td> 01/28/2015 </td>'
    '<td> Baker County Republican Central Committee (290) </td>'
    '<td> 04/30/2015 </td><td> Kyle L Knight </td></tr>'
)


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parses_rows_and_takes_the_filer_id_from_the_committee_line() -> None:
    rows = OC.parse_certificate_page(_page(BUNN_2015 + BAKER_2015), 2015)
    assert [r["filer_id"] for r in rows] == ["15764", "290"]
    assert rows[0]["committee"] == "Friends of Daniel Bunn"
    assert rows[0]["filed"] == "02/11/2015"
    assert rows[0]["expires"] is None


def test_keeps_the_expiration_date_of_a_certificate_that_ended_mid_year() -> None:
    rows = OC.parse_certificate_page(_page(BAKER_2015), 2015)
    assert rows[0]["expires"] == "04/30/2015"


def test_layout_rows_are_never_mistaken_for_certificates() -> None:
    """The page is tables inside tables; only leaf rows are candidates."""
    rows = OC.parse_certificate_page(_page(BUNN_2015), 2015)
    assert len(rows) == 1


def test_an_unrendered_page_is_none_not_an_empty_year() -> None:
    """A challenge page must never read as 'no certificates this year'.

    Treating it as empty would delete every stored certificate for the year
    and move every affected balance on the next aggregation.
    """
    challenge = "<html><script src='/TSPD/08abc'></script>Please wait</html>"
    assert OC.parse_certificate_page(challenge, 2015) is None
    assert OC.parse_certificate_page(_page(BUNN_2015, header=False), 2015) is None


def test_a_rendered_year_with_no_certificates_is_an_empty_list() -> None:
    assert OC.parse_certificate_page(_page(""), 2031) == []


def test_rows_for_another_year_are_ignored() -> None:
    assert OC.parse_certificate_page(_page(BUNN_2015), 2014) == []


# ── Merging runs ─────────────────────────────────────────────────────────────

def _cert(fid: str) -> dict:
    return {"filer_id": fid, "committee": "C", "filed": "01/02/2014",
            "expires": None, "submitted_by": None}


def test_a_failed_year_keeps_its_previous_certificates() -> None:
    previous = {"version": 1, "years": {"2014": {"certificates": [_cert("1")]}}}
    payload, kept = OC.merge_certificates(previous, {2014: None}, "t")
    assert payload["years"]["2014"]["certificates"] == [_cert("1")]
    assert kept == [2014]


def test_an_empty_read_cannot_erase_a_year_that_had_certificates() -> None:
    """ORESTAR does not revoke a year's filings; an empty page is a bad load."""
    previous = {"version": 1, "years": {"2014": {"certificates": [_cert("1")]}}}
    payload, kept = OC.merge_certificates(previous, {2014: []}, "t")
    assert payload["years"]["2014"]["certificates"] == [_cert("1")]
    assert kept == [2014]


def test_a_successful_read_replaces_the_year() -> None:
    previous = {"version": 1, "years": {"2014": {"certificates": [_cert("1")]}}}
    payload, kept = OC.merge_certificates(previous, {2014: [_cert("1"), _cert("2")]}, "t")
    assert [c["filer_id"] for c in payload["years"]["2014"]["certificates"]] == ["1", "2"]
    assert kept == []


def test_certificates_by_filer_indexes_years() -> None:
    payload = {"years": {"2013": {"certificates": [_cert("15764")]},
                         "2014": {"certificates": [_cert("15764"), _cert("9")]}}}
    by = OC.certificates_by_filer(payload)
    assert sorted(by["15764"]) == [2013, 2014]
    assert sorted(by["9"]) == [2014]
    assert OC.certificates_by_filer(None) == {}


# ── Ghost rows ───────────────────────────────────────────────────────────────

def _summary(begin: float, end: float) -> dict:
    return {"beginning_balance": begin, "ending_cash_balance": end}


# Friends of Daniel Bunn, exactly as ORESTAR states it.
BUNN_YEARS = {
    "2012": _summary(0.0, 1709.94),
    "2013": _summary(1709.94, 1709.94),
    "2014": _summary(4209.94, 4209.94),
    "2015": _summary(6709.94, 6709.94),
    "2016": _summary(9959.94, 4959.94),
    "2017": _summary(4959.94, 0.0),
}


def test_bunn_gets_one_ghost_row_per_certificate_year() -> None:
    rows = OC.certificate_restatements(BUNN_YEARS, {2013, 2014, 2015})
    assert [(r["date"], r["amount"]) for r in rows] == [
        ("2013-12-31", 2500.0),
        ("2014-12-31", 2500.0),
        ("2015-12-31", 3250.0),
    ]
    # The three rows are the whole $8,250 — ORESTAR's post-certificate opening
    # less its last pre-certificate close.
    assert round(sum(r["amount"] for r in rows), 2) == round(9959.94 - 1709.94, 2)


def test_every_ghost_row_sits_inside_a_certificate_year() -> None:
    certs = {2013, 2014, 2015}
    for row in OC.certificate_restatements(BUNN_YEARS, certs):
        assert row["year"] in certs


def test_entering_a_certificate_year_is_dated_its_first_day() -> None:
    """Entering from an itemized year: ORESTAR's pure step, whatever we hold."""
    years = {"2019": _summary(0.0, 500.0), "2020": _summary(800.0, 800.0)}
    rows = OC.certificate_restatements(years, {2020}, {2019: 480.0})
    assert rows == [{
        "date": "2020-01-01", "year": 2020, "amount": 300.0, "boundary": [2019, 2020],
        "orestar_prior_ending": 500.0, "orestar_opening": 800.0,
        "orestar_restatement": 300.0, "certificate_year_difference": 0.0,
        "placement": "certificate_year_start",
    }]


def test_a_certificate_period_where_orestar_chains_cleanly_yields_nothing() -> None:
    """No restatement, no ghost row — a gap on our side stays visible."""
    years = {"2019": _summary(0.0, 500.0), "2020": _summary(500.0, 500.0),
             "2021": _summary(500.0, 200.0)}
    assert OC.certificate_restatements(years, {2020}) == []


def test_a_break_with_no_adjacent_certificate_is_left_alone() -> None:
    """The unexplained residue must stay unexplained, not be relabelled."""
    years = {"2019": _summary(0.0, 500.0), "2020": _summary(900.0, 900.0)}
    assert OC.certificate_restatements(years, {2015}) == []


def test_non_consecutive_summary_years_are_not_bridged() -> None:
    """A gap in ORESTAR's cached years is not evidence of a restatement."""
    years = {"2013": _summary(0.0, 100.0), "2016": _summary(400.0, 400.0)}
    assert OC.certificate_restatements(years, {2014, 2015}) == []


def test_negative_restatements_are_kept_with_their_sign() -> None:
    years = {"2014": _summary(3494.83, 3494.83), "2015": _summary(0.0, 0.0)}
    rows = OC.certificate_restatements(years, {2015})
    assert rows[0]["amount"] == -3494.83
    assert rows[0]["date"] == "2015-01-01"


def test_a_double_count_in_orestars_certificate_year_is_not_imported() -> None:
    """Friends for Safer Libraries, exactly as ORESTAR states it.

    One $25.95 expenditure dated 2006-12-30, filed 2007-02-28, is counted in
    ORESTAR's 2006 total AND its 2007 certificate-year total; the 2008 opening
    undoes it with a +$25.87 step. We never double counted, so taking that step
    as unitemized money would add $25.87 we do not need. Measured from our
    side the row is -$0.08 and the balance matches ORESTAR's 2008 opening.
    """
    years = {
        "2006": {**_summary(66.48, 40.53), "expenditures": 25.95},
        "2007": {**_summary(40.53, 14.58), "expenditures": 25.95},
        "2008": _summary(40.45, 40.45),
    }
    rows = OC.certificate_restatements(years, {2007}, {2006: -25.95, 2007: 0.0})
    assert [(r["date"], r["amount"]) for r in rows] == [("2007-12-31", -0.08)]
    assert rows[0]["orestar_restatement"] == 25.87
    assert rows[0]["certificate_year_difference"] == -25.95
    # Our 2008 opening: 2006 close 40.53, nothing in 2007, plus the ghost row.
    assert round(40.53 + 0.0 + rows[0]["amount"], 2) == 40.45


def test_itemized_rows_in_a_certificate_year_are_not_double_counted() -> None:
    """A certificate that expired mid-year: both sides itemize the remainder."""
    years = {"2014": _summary(100.0, 100.0),
             "2015": {**_summary(100.0, 160.0), "contributions": 60.0},
             "2016": _summary(260.0, 260.0)}
    rows = OC.certificate_restatements(years, {2015}, {2015: 60.0})
    # ORESTAR restated +100 on top of the 60 both sides hold; only 100 is new.
    assert rows[0]["amount"] == 100.0
    assert rows[0]["certificate_year_difference"] == 0.0


def test_a_shortfall_inside_a_certificate_year_is_disclosed_not_hidden() -> None:
    """Missing itemized rows in the year are absorbed — and reported as such."""
    years = {"2015": {**_summary(100.0, 160.0), "contributions": 60.0},
             "2016": _summary(260.0, 260.0)}
    rows = OC.certificate_restatements(years, {2015}, {2015: 45.0})
    assert rows[0]["amount"] == 115.0
    assert rows[0]["orestar_restatement"] == 100.0
    assert rows[0]["certificate_year_difference"] == 15.0


def test_no_restatement_means_no_row_even_if_our_rows_differ() -> None:
    """Where ORESTAR's chain holds, a difference is a real gap and stays visible."""
    years = {"2015": {**_summary(100.0, 160.0), "contributions": 60.0},
             "2016": _summary(160.0, 160.0)}
    assert OC.certificate_restatements(years, {2015}, {2015: 45.0}) == []


def test_when_our_rows_already_carry_the_restatement_there_is_no_row() -> None:
    years = {"2015": _summary(100.0, 100.0), "2016": _summary(160.0, 160.0)}
    assert OC.certificate_restatements(years, {2015}, {2015: 60.0}) == []


def test_missing_or_non_numeric_summary_fields_are_skipped() -> None:
    years = {"2013": {"beginning_balance": 0.0},
             "2014": _summary(100.0, 100.0),
             "2015": {"beginning_balance": "n/a", "ending_cash_balance": 0.0}}
    assert OC.certificate_restatements(years, {2013, 2014, 2015}) == []
