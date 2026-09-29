"""Amendment chains ORESTAR still counts in its account summaries.

An amended transaction is a chain of versions: the original, its amendments
and any deletions, linked on ORESTAR's Transaction History page
(ceTransactionHistory.do). Today ORESTAR counts only the newest version, and
process.py's _drop_superseded does the same. Julie for County Commissioner's
2026 expense 5538035 has four amendments and ORESTAR counts one of them.

In ORESTAR's early years it did not. For chains filed in 2007 its summary
counts EVERY amendment, including ones its search now marks expired, less
one amendment per deletion, and the original only if it was never amended.
Verified on ORESTAR 2026-09-18/19, to the cent, on three committees whose
live rows could not explain their summaries:

  Friends of Ted Ferrioli, 2006     +$40,000 / +$300  two chains, each with
                                     two amendments and one deletion
  Committee to Elect Dan Gardner    +$157.80           three such chains
  Friends of Bill Bradbury, 2007    +$61.90            a chain with three
                                     amendments and no deletion

A version marked expired or deleted never appears in ORESTAR's default
search, so our transaction mirror cannot see any of this. The collector
(fetch_amendment_chains.py) reads the chains with ORESTAR's "Include Deleted"
and "Show Expired" options ticked. This module decides what the result means.

Which rule a year follows is not known in advance (the switch date is
unknown), so nothing is assumed. evaluate_target() applies the 2007 counting
only when it reproduces ORESTAR's own summary line to the cent from ORESTAR's
own rows, and only to rows ORESTAR lists. Otherwise it changes nothing.
"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Iterable

CHAINS_FILENAME = "orestar_amendment_chains.json"
FORMAT_VERSION = 1
CHAIN_ID_PREFIX = "chain-version-"
CHAIN_SUB_TYPE = "Amendment Chain Version (derived)"

# Cash lines of ORESTAR's account summary, by the sub types that feed them.
# Matches process.py's _COH_C_TYPES / _COH_E_TYPES; in-kind and personal
# expenditures are not cash and never enter either bucket. The Other Receipts
# and Other Disbursements lines leave exempt loans out: ORESTAR prints those
# on their own "Loans Received (exempt)" / "Loan Payments (exempt)" lines.
CASH_BUCKETS = {
    "C": frozenset({"Cash Contribution", "Loan Received (Non-Exempt)"}),
    "E": frozenset({"Cash Expenditure", "Loan Payment (Non-Exempt)"}),
    "OR": frozenset({
        "Miscellaneous Other Receipt", "Refunds and Rebates",
        "Lost or Returned Check", "Interest/Investment Income",
        "Items Sold at Fair Market Value",
    }),
    "OD": frozenset({
        "Miscellaneous Other Disbursement", "Return or Refund of Contribution",
        "Nonpartisan Activity",
    }),
}
# The summary field each bucket is checked against, less its in-kind part.
SUMMARY_LINES = {
    "C": ("contributions", "inkind_contributions"),
    "E": ("expenditures", "inkind_expenditures"),
    "OR": ("other_receipts", None),
    "OD": ("other_disbursements", None),
}
# Which way each bucket moves cash.
CASH_SIGN = {"C": 1.0, "OR": 1.0, "E": -1.0, "OD": -1.0}
SUPPORTED_TYPES = tuple(CASH_BUCKETS)

_RESULTS_HEADER = ("tran id", "tran date", "status", "filer/committee",
                   "contributor/payee", "sub type", "amount")
_HISTORY_HEADER = ("tran id", "status", "tran date", "filed date",
                   "tran subtype", "contributor/payee", "filed by", "amount")
_COUNT_RE = re.compile(r"(\d[\d,]*)\s+records?\s+found", re.IGNORECASE)
_STATUSES = {"Original", "Amended", "Deleted"}


class _LeafRows(HTMLParser):
    """Cell texts of every <tr> that contains no nested table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._stack: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            for row in self._stack:
                row["nested"] = True
        elif tag == "tr":
            self._stack.append({"cells": [], "cell": None, "nested": False})
        elif tag in ("td", "th") and self._stack:
            self._stack[-1]["cell"] = []

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
    return " ".join(str(text or "").replace("\xa0", " ").split())


def _leaf_rows(html: str) -> list[list[str]]:
    parser = _LeafRows()
    parser.feed(html or "")
    parser.close()
    return [[_clean(cell) for cell in row] for row in parser.rows]


def parse_amount(text: Any) -> float | None:
    """ORESTAR's "$1,234.56" or "($68.50)", to cents; None if unreadable."""
    raw = _clean(text).replace("$", "").replace(",", "")
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1]
    try:
        value = round(float(raw), 2)
    except ValueError:
        return None
    return -value if negative else value


def _status(text: str) -> tuple[str, bool] | None:
    """("Amended", True) for "Amended *": the base status and its expired mark."""
    cleaned = _clean(text)
    expired = cleaned.endswith("*")
    base = cleaned.rstrip("*").strip()
    return (base, expired) if base in _STATUSES else None


def parse_results_page(html: str) -> tuple[int, list[dict]] | None:
    """(reported count, rows) from one results page, or None if unrendered.

    None means the results table never appeared (a challenge, an error page).
    A rendered search with no matches has a count of 0 and no rows.
    """
    text = _clean(re.sub(r"<[^>]+>", " ", html or ""))
    match = _COUNT_RE.search(text)
    rows: list[dict] = []
    header_seen = False
    for cells in _leaf_rows(html):
        if len(cells) != 7:
            continue
        if tuple(c.lower() for c in cells) == _RESULTS_HEADER:
            header_seen = True
            continue
        status = _status(cells[2])
        amount = parse_amount(cells[6])
        if not cells[0].isdigit() or status is None or amount is None:
            continue
        rows.append({
            "tran_id": cells[0],
            "tran_date": cells[1],
            "status": status[0],
            "expired": status[1],
            "payee": cells[4] or None,
            "sub_type": cells[5] or None,
            "amount": amount,
        })
    if match is None:
        # A rendered search with nothing to show says so in words.
        return (0, []) if re.search(r"\bno (?:matching )?records\b", text, re.I) else None
    count = int(match.group(1).replace(",", ""))
    if count and not header_seen:
        return None
    return count, rows


def parse_history_page(html: str) -> list[dict] | None:
    """Every public version of one transaction, or None if unrendered."""
    versions: list[dict] = []
    header_seen = False
    for cells in _leaf_rows(html):
        if len(cells) != 8:
            continue
        if tuple(c.lower() for c in cells) == _HISTORY_HEADER:
            header_seen = True
            continue
        status = _status(cells[1])
        amount = parse_amount(cells[7])
        if not cells[0].isdigit() or status is None or amount is None:
            continue
        versions.append({
            "tran_id": cells[0],
            "status": status[0],
            "tran_date": cells[2],
            "filed_date": cells[3],
            "sub_type": cells[4] or None,
            "payee": cells[5] or None,
            "filed_by": cells[6] or None,
            "amount": amount,
        })
    return versions if header_seen and versions else None


def _filed_key(version: dict) -> tuple:
    text = str(version.get("filed_date") or "")
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return (datetime.strptime(text, fmt), int(version["tran_id"]))
        except ValueError:
            continue
    return (datetime.max, int(version["tran_id"]))


def counted_versions_2007(
    versions: list[dict],
    original_live: bool = False,
    live_ids: Iterable[str] = (),
) -> list[dict] | None:
    """The versions ORESTAR's early-era summary counts, or None if ambiguous.

    Every amendment counts; each deletion cancels one amendment of the same
    sub type and amount (or the original, when there are no amendments). The
    original counts only when it was never amended, or when ORESTAR still
    lists it as live (Oregon Firearms Federation PAC 2041931 — see
    _drop_superseded). A deletion that matches nothing is not guessed at.

    When several versions match a deletion, one ORESTAR marks expired is
    cancelled before the live one. The money is the same either way; this
    keeps the counted set on the row ORESTAR shows as current, so equal
    versions do not turn into an add-and-remove pair.
    """
    live = {str(v) for v in live_ids}
    ordered = sorted(versions, key=_filed_key)
    originals = [v for v in ordered if v["status"] == "Original"]
    if len(originals) != 1 or ordered[0] is not originals[0]:
        return None
    original = originals[0]
    amendments = [v for v in ordered if v["status"] == "Amended"]
    deletions = [v for v in ordered if v["status"] == "Deleted"]
    if len(originals) + len(amendments) + len(deletions) != len(ordered):
        return None
    counted = list(amendments) if amendments else [original]
    if amendments and original_live:
        counted.insert(0, original)
    for deletion in deletions:
        candidates = [v for v in reversed(counted)
                      if v["amount"] == deletion["amount"]
                      and v["sub_type"] == deletion["sub_type"]]
        match = next((v for v in candidates if v["tran_id"] not in live),
                     candidates[0] if candidates else None)
        if match is None:
            return None
        counted.remove(match)
    return counted


def bucket_of(sub_type: str | None) -> str | None:
    for bucket, sub_types in CASH_BUCKETS.items():
        if sub_type in sub_types:
            return bucket
    return None


def _year_of(tran_date: str | None) -> int | None:
    try:
        return datetime.strptime(str(tran_date or ""), "%m/%d/%Y").year
    except ValueError:
        return None


def summary_line(summary: dict, bucket: str) -> float | None:
    """ORESTAR's cash figure for one bucket: the line less its in-kind part."""
    total_key, inkind_key = SUMMARY_LINES[bucket]
    total = summary.get(total_key)
    inkind = (summary.get(inkind_key) or 0.0) if inkind_key else 0.0
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    return round(float(total) - float(inkind), 2)


def evaluate_target(
    entry: dict,
    summary: dict,
    held: dict[str, float],
    bucket: str,
) -> dict:
    """Decide one filer-year-bucket. Returns a decision dict, always.

    ``entry`` is the collected listing and chains for this filer, year and
    ORESTAR transaction type; ``held`` maps each tran_id we hold in this
    filer-year-bucket to its amount. The early-era counting is applied only
    when every check passes:

      * the listing is complete (as many rows as ORESTAR reported);
      * every expired, amended or deleted row belongs to a fetched chain, and
        every chain is unambiguous under the counting rule;
      * every version of every chain lies in this year;
      * ORESTAR's rows, counted that way, reproduce its summary line to the
        cent;
      * every row we hold is one ORESTAR lists, and every live row it lists
        outside a chain is one we hold (other mechanisms own those gaps).

    ``adjustments`` then lists the versions to add (counted, not held) and to
    take away (held, not counted), which move our total onto ORESTAR's line.
    """
    decision: dict[str, Any] = {"applied": False, "bucket": bucket,
                                "adjustments": [], "chains": []}

    def refuse(reason: str) -> dict:
        decision["reason"] = reason
        return decision

    if bucket not in CASH_BUCKETS:
        return refuse("unsupported_bucket")
    rows = entry.get("rows") or []
    if entry.get("reported") != len(rows) or len({r["tran_id"] for r in rows}) != len(rows):
        return refuse("listing_incomplete")
    year = entry.get("year")
    line = summary_line(summary or {}, bucket)
    if line is None:
        return refuse("no_summary_line")

    listed = {r["tran_id"]: r for r in rows}
    chains = entry.get("chains") or []
    in_chain: dict[str, int] = {}
    for index, chain in enumerate(chains):
        for version in chain.get("versions") or []:
            in_chain[version["tran_id"]] = index
    needs_chain = [r["tran_id"] for r in rows
                   if r["status"] != "Original" or r.get("expired")]
    if any(tid not in in_chain for tid in needs_chain):
        return refuse("chain_not_collected")

    model_total = 0.0
    counted_ids: set[str] = set()
    chain_versions: set[str] = set()
    for chain in chains:
        versions = chain.get("versions") or []
        if any(_year_of(v.get("tran_date")) != year for v in versions):
            return refuse("chain_crosses_year")
        original = next((v for v in versions if v["status"] == "Original"), None)
        live = bool(original) and not (listed.get(original["tran_id"]) or {}).get(
            "expired", True)
        live_ids = {v["tran_id"] for v in versions
                    if v["tran_id"] in listed and listed[v["tran_id"]]["status"] != "Deleted"
                    and not listed[v["tran_id"]].get("expired")}
        counted = counted_versions_2007(versions, original_live=live, live_ids=live_ids)
        if counted is None:
            return refuse("chain_ambiguous")
        chain_versions.update(v["tran_id"] for v in versions)
        for version in counted:
            if bucket_of(version.get("sub_type")) == bucket:
                counted_ids.add(version["tran_id"])
                model_total += version["amount"]
    standalone = [r for r in rows if r["tran_id"] not in chain_versions]
    for row in standalone:
        if bucket_of(row.get("sub_type")) == bucket:
            model_total += row["amount"]
    model_total = round(model_total, 2)
    decision.update(model_total=model_total, orestar_line=line,
                    held_total=round(sum(held.values()), 2))
    if abs(model_total - line) > 0.005:
        return refuse("model_does_not_reproduce_summary")

    if any(tid not in listed for tid in held):
        return refuse("held_row_not_listed")
    live_standalone = {r["tran_id"] for r in standalone
                       if bucket_of(r.get("sub_type")) == bucket}
    if live_standalone - set(held):
        return refuse("listed_row_not_held")

    versions_by_id = {v["tran_id"]: v for chain in chains
                      for v in chain.get("versions") or []}
    adjustments = []
    for tid in sorted(counted_ids - set(held), key=int):
        adjustments.append({"tran_id": tid, "effect": "add",
                            "amount": versions_by_id[tid]["amount"]})
    for tid in sorted((set(held) & chain_versions) - counted_ids, key=int):
        adjustments.append({"tran_id": tid, "effect": "remove",
                            "amount": round(float(held[tid]), 2)})
    moved = round(sum(a["amount"] if a["effect"] == "add" else -a["amount"]
                      for a in adjustments), 2)
    if abs(decision["held_total"] + moved - line) > 0.005:
        return refuse("adjustments_do_not_close")
    touched = {a["tran_id"] for a in adjustments}
    decision["chains"] = [
        {"versions": chain.get("versions") or []}
        for chain in chains
        if touched & {v["tran_id"] for v in chain.get("versions") or []}
    ]
    decision.update(applied=bool(adjustments), adjustments=adjustments, moved=moved)
    if not adjustments:
        decision["reason"] = "already_reconciled"
    return decision


def target_key(filer_id: str, year: int, tran_type: str) -> str:
    return f"{filer_id}:{year}:{tran_type}"


def targets_for_filer(payload: Any, filer_id: str) -> list[dict]:
    """Collected entries for one physical filer from the stored file."""
    if not isinstance(payload, dict):
        return []
    out = []
    for key, entry in (payload.get("targets") or {}).items():
        if not isinstance(entry, dict) or str(entry.get("filer_id")) != str(filer_id):
            continue
        if entry.get("tran_type") not in SUPPORTED_TYPES:
            continue
        out.append(entry)
    return sorted(out, key=lambda e: (e.get("year") or 0, e.get("tran_type") or ""))


def merge_targets(previous: Any, fetched: Iterable[dict], fetched_at: str) -> dict:
    """Fold one run's complete collections into the stored file.

    Only complete collections replace a stored one; a failed or partial read
    keeps whatever was there, so a bad night cannot remove an adjustment.
    """
    targets = dict(((previous or {}).get("targets") or {})) if isinstance(previous, dict) else {}
    for entry in fetched:
        if entry.get("reported") != len(entry.get("rows") or []):
            continue
        key = target_key(entry["filer_id"], entry["year"], entry["tran_type"])
        targets[key] = {**entry, "fetched_at": fetched_at}
    return {"version": FORMAT_VERSION, "targets": targets}
