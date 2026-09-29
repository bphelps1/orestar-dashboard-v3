"""ORESTAR Certificates of Limited Contributions and Expenditures.

A committee that expects to raise and spend little may file a certificate for a
calendar year. While it is in force the committee does not itemize its
transactions. ORESTAR builds each year's account summary from itemized
transactions, so a certificate year shows no activity and closes at the balance
it opened with — yet the committee's cash really moves, and ORESTAR states the
result in a LATER year's opening balance. The prior year's ending and the next
year's "Beginning Balance (Previous Year)" then disagree, on ORESTAR's own
pages, read in a single sitting.

That is the whole mechanism behind the year-boundary breaks in ORESTAR's
balance chain. Measured 2026-09-18 across all 4,541 certificates ORESTAR holds
(2007-2026) against every consecutive pair of annual summaries:

    certificate on an adjacent year   2,140 boundaries   868 breaks   40.6%
    committee never certified        26,886 boundaries     9 breaks    0.03%

868 of 878 breaks (98.9%) touch a certificate year. Friends of Daniel Bunn
(15764) is the worked example: certificates for 2013, 2014 and 2015; breaks of
+$2,500, +$2,500 and +$3,250 into 2014, 2015 and 2016; and 2012 and 2016,
which he itemized, chain cleanly. The $8,250 is money he really handled — it is
spent through ordinary itemized expenditures in 2016 and 2017.

This module holds the pure logic: reading the certificate search, and turning
ORESTAR's own restatements into ghost rows. Fetching lives in
fetch_certificates.py and applying the rows lives in process.py, so each piece
can be tested without a browser or a full aggregation.
"""

from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from typing import Any, Iterable

CERTIFICATES_FILENAME = "orestar_certificates.json"
FORMAT_VERSION = 1

# A ghost row is a DERIVED record, never an ORESTAR transaction. The id prefix
# and sub_type make that impossible to miss in any downstream listing, and keep
# the rows out of every exact-identity comparison: they are generated during
# aggregation and are never written into the transaction mirror, whose rows
# must stay an exact copy of what ORESTAR returns.
GHOST_ID_PREFIX = "ghost-certificate-"
GHOST_SUB_TYPE = "Certificate Period Restatement (derived)"

# The results table always carries this header, even for a year with no
# certificates. Its presence is the success signal: F5's challenge scripts
# are embedded in real pages too, so their markers cannot distinguish a
# block from a result.
_HEADER_CELLS = ("year", "date filed", "committee", "expiration date", "submitted by")
_FILER_ID_RE = re.compile(r"\((\d+)\)\s*$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


class _TableRows(HTMLParser):
    """Collect every leaf <tr>'s cell texts, ignoring layout tables.

    ORESTAR nests its results table inside page-layout tables. A row counts
    only if no other table opens inside it, which is what separates the five
    certificate columns from the outer tables that contain the whole page.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._stack: list[dict] = []   # open <tr>s: {"cells", "cell", "nested"}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            for row in self._stack:
                row["nested"] = True
        elif tag == "tr":
            self._stack.append({"cells": [], "cell": None, "nested": False})
        elif tag in ("td", "th") and self._stack:
            self._stack[-1]["cell"] = []
        elif tag == "br" and self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._stack:
            row = self._stack[-1]
            if row["cell"] is not None:
                row["cells"].append("".join(row["cell"]))
                row["cell"] = None
        elif tag == "tr" and self._stack:
            row = self._stack.pop()
            if not row["nested"]:
                self.rows.append(row["cells"])

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(data)


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def parse_certificate_page(html: str, year: int) -> list[dict] | None:
    """Certificates listed on one year's search result, or None if unrendered.

    None means the results table never appeared — a bot challenge, an error
    page, a timeout. An empty list means the table rendered and the year truly
    has no certificates. Callers must not confuse the two: treating a failed
    load as "no certificates" would silently remove ghost rows and move
    balances.
    """
    parser = _TableRows()
    parser.feed(html or "")
    parser.close()
    header_seen = False
    out: list[dict] = []
    for cells in parser.rows:
        texts = [_clean(c) for c in cells]
        if len(texts) == 5 and tuple(t.lower() for t in texts) == _HEADER_CELLS:
            header_seen = True
            continue
        if len(texts) != 5 or texts[0] != str(year):
            continue
        # The committee cell is "Candidate, Name<br>Committee Name (12345)" or
        # just "Committee Name (12345)". The trailing parenthesised number is
        # the filer id, and it is the only part relied on.
        committee_lines = [_clean(line) for line in cells[2].split("\n") if _clean(line)]
        committee = committee_lines[-1] if committee_lines else ""
        match = _FILER_ID_RE.search(committee)
        if not match:
            continue
        filed = texts[1] if _DATE_RE.match(texts[1]) else None
        expires = texts[3] if _DATE_RE.match(texts[3]) else None
        out.append({
            "filer_id": match.group(1),
            "committee": _FILER_ID_RE.sub("", committee).strip(),
            "filed": filed,
            "expires": expires,
            "submitted_by": texts[4] or None,
        })
    return out if header_seen else None


def merge_certificates(
    previous: dict | None,
    fetched: dict[int, list[dict] | None],
    fetched_at: str,
) -> tuple[dict, list[int]]:
    """Fold one run's per-year results into the stored file.

    Returns (payload, years_kept_from_previous). A year that failed to render
    keeps its previous certificates rather than being dropped, and a year that
    rendered EMPTY where it previously held certificates is treated as a failed
    read too — ORESTAR does not revoke a whole year's filings, so an empty page
    there is far likelier a bad load than a real change. Either way the stored
    certificates would otherwise vanish and every affected balance would jump
    back on the next aggregation.
    """
    prior_years = ((previous or {}).get("years") or {})
    years: dict[str, dict] = {str(k): v for k, v in prior_years.items()}
    kept: list[int] = []
    for year, rows in sorted(fetched.items()):
        key = str(year)
        prior_rows = (prior_years.get(key) or {}).get("certificates") or []
        if rows is None or (not rows and prior_rows):
            if key in prior_years:
                kept.append(year)
            continue
        years[key] = {"fetched_at": fetched_at, "certificates": rows}
    return {"version": FORMAT_VERSION, "years": years}, kept


def certificates_by_filer(payload: Any) -> dict[str, dict[int, dict]]:
    """filer_id -> {year: certificate} from the stored file. Tolerates absence."""
    out: dict[str, dict[int, dict]] = {}
    if not isinstance(payload, dict):
        return out
    for key, entry in (payload.get("years") or {}).items():
        try:
            year = int(key)
        except (TypeError, ValueError):
            continue
        for cert in (entry or {}).get("certificates") or []:
            fid = str((cert or {}).get("filer_id") or "").strip()
            if fid.isdigit():
                out.setdefault(fid, {})[year] = {
                    "filed": cert.get("filed"),
                    "expires": cert.get("expires"),
                }
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def certificate_restatements(
    orestar_years: dict,
    certificate_years: Iterable[int],
    our_nets: dict[int, float] | None = None,
) -> list[dict]:
    """Ghost rows for the balance ORESTAR restated around certificate years.

    A row is generated only where ORESTAR itself restated — its opening for a
    year differs from its close for the year before — and at least one of the
    two years was a certificate year. Where ORESTAR's chain holds, its totals
    are consistent with its openings, so any difference from our rows is a
    genuine data gap and must stay visible, not be relabelled.

    ``our_nets`` is our own net cash movement per year for this filer, BEFORE
    any ghost rows. Leaving a certificate year the amount is measured from
    our side, as "ORESTAR's next opening, less its opening for the certificate
    year, less what our own rows moved in it":

        amount = opening(q) - opening(p) - our_net(p)

    That trusts ORESTAR's opening AFTER the certificate year over ORESTAR's
    total FOR it, and the second is not reliable. Friends for Safer Libraries
    is the case that forced this: one $25.95 expenditure dated 2006-12-30 and
    filed 2007-02-28 appears in ORESTAR's 2006 total AND its 2007
    (certificate-year) total, and ORESTAR's 2008 opening undoes the double
    count with a +$25.87 step. Taking that step as unitemized money — the
    earlier version of this rule — added $25.87 we never needed and pushed a
    committee that matched to within $0.08 to $25.95 off. Measured from our
    side the row is -$0.08, and the balance matches.

    Anchoring on ORESTAR's opening for the certificate year, rather than on our
    own balance entering it, means no gap from BEFORE the certificate period
    is ever absorbed: a pre-existing difference passes through unchanged. What
    CAN be absorbed is a difference inside the certificate year itself — e.g.
    an itemized row we lack from after a certificate expired mid-year. That
    part is returned separately as ``certificate_year_difference`` so it is
    disclosed rather than hidden inside the restatement.

    Entering a certificate year from an itemized one, the amount is ORESTAR's
    pure step (opening minus prior close): the itemized year's own total is
    trustworthy, and a gap there must stay visible.

    Dating keeps every row inside a certificate year:

      * leaving a certificate year -> 31 December of that year;
      * entering one               -> 1 January of the certificate year.

    A break with no certificate on either side is left alone: those nine cases
    in 26,886 year boundaries are unexplained, and a ghost row would hide them.
    """
    certs = {int(y) for y in certificate_years}
    if not certs or not isinstance(orestar_years, dict):
        return []
    ours = {int(k): float(v) for k, v in (our_nets or {}).items()}
    years = sorted(int(y) for y in orestar_years if str(y).isdigit())
    rows: list[dict] = []
    for prior, year in zip(years, years[1:]):
        if year - prior != 1 or (prior not in certs and year not in certs):
            continue
        prior_summary = orestar_years.get(str(prior)) or {}
        prior_end = _number(prior_summary.get("ending_cash_balance"))
        opening = _number((orestar_years.get(str(year)) or {}).get("beginning_balance"))
        if prior_end is None or opening is None:
            continue
        step = round(opening - prior_end, 2)
        if abs(step) <= 0.005:
            continue                     # ORESTAR did not restate anything
        leaving = prior in certs
        if leaving:
            prior_open = _number(prior_summary.get("beginning_balance"))
            if prior_open is None:
                continue
            amount = round(opening - prior_open - ours.get(prior, 0.0), 2)
        else:
            amount = step
        if abs(amount) <= 0.005:
            continue                     # our rows already carry it
        when = date(prior, 12, 31) if leaving else date(year, 1, 1)
        rows.append({
            "date": when.isoformat(),
            "year": when.year,
            "amount": amount,
            "boundary": [prior, year],
            "orestar_prior_ending": round(prior_end, 2),
            "orestar_opening": round(opening, 2),
            # ORESTAR's own restatement, and the part of `amount` that instead
            # reconciles ORESTAR's certificate-year total with our rows. They
            # sum to `amount`; the second is zero unless the two disagree.
            "orestar_restatement": step,
            "certificate_year_difference": round(amount - step, 2),
            "placement": "certificate_year_end" if leaving else "certificate_year_start",
        })
    return rows
