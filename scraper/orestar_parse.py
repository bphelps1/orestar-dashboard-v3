"""Shared parsing of ORESTAR's Account Summary pages.

One canonical dollar parser, because there were three copies and all three
carried the same bug: ORESTAR renders negative amounts in ACCOUNTING
PARENTHESES — ($6,441.60) — and every copy matched only a leading minus sign.
The parenthesised amount matched as a plain positive, so a committee $6,441.60
in the red was recorded as $6,441.60 in the black.

That produced a discrepancy of exactly twice the true balance (our correct
calculation minus a sign-flipped reference), which the dashboard then flagged
as a data-quality warning against a figure that was in fact right.
"""

from __future__ import annotations

import re

# label … then an amount in any of:
#     $1,234.00        →  1234.00
#     -$1,234.00       → -1234.00     (leading minus, with or without a space)
#     ($1,234.00)      → -1234.00     (accounting parentheses)
_AMOUNT = r"(\()?\s*(-\s*)?\$\s*([\d,]+\.\d{2})\s*(\))?"


def _amount_value(match: re.Match[str]) -> float:
    """Convert one ``_AMOUNT`` match, including accounting signs."""
    value = float(match.group(3).replace(",", ""))
    negative = (
        bool(match.group(2) and match.group(2).strip() == "-")
        or bool(match.group(1) and match.group(4))
    )
    return -value if negative else value


def parse_dollar(html: str, label: str, default: float | None = None) -> float | None:
    """First dollar amount following `label`, signed correctly.

    `label` is treated as a literal, so callers pass "Beginning Balance
    (Previous Year)" rather than a pre-escaped pattern.
    """
    # IGNORECASE, because ORESTAR's casing is not what a caller guesses.
    #
    # The page prints "Loans Received (non-exempt)"; every caller asked for
    # "Loans Received (Non-Exempt)". A case-sensitive literal matched nothing,
    # and parse_dollar returns the default rather than raising — so all four
    # loan fields read $0.00 in all 46,945 yearly records, silently, forever.
    #
    # That is the same failure as the in-kind labels in #56, and it produced a
    # $3.08M phantom discrepancy across 66 committees in 2006 that was twice
    # explained away as a difference between ORESTAR's accounting and ours.
    # There is no difference: we were not reading the figures.
    #
    # Matching case-insensitively removes the whole class rather than the four
    # instances of it. No label on this page differs only by case.
    document = html or ""
    # Never let a missing/truncated value borrow the next table row's amount.
    # ORESTAR occasionally returns a partially rendered page through its F5
    # layer.  The old ``label + .*? + amount`` expression crossed ``</tr>`` and
    # turned this:
    #
    #   Total Contributions  [missing]
    #   Total Expenditures    $500.00
    #
    # into $500 for BOTH fields.  Iterate label occurrences (some pages contain
    # hidden duplicates), but bound each lookup to the current row whenever row
    # markup is present.
    for label_match in re.finditer(re.escape(label), document, re.IGNORECASE):
        tail = document[label_match.end():]
        boundary = re.search(r"</?tr\b", tail, re.IGNORECASE)
        field = tail[:boundary.start()] if boundary else tail
        amount = re.search(_AMOUNT, field, re.DOTALL | re.IGNORECASE)
        if amount:
            return _amount_value(amount)
    return default

def parse_dollar_between(html: str, start_label: str, end_label: str,
                         label: str, default: float | None = None) -> float | None:
    """Dollar amount for `label`, looked up only BETWEEN two section anchors.

    ORESTAR's account summary labels both in-kind rows simply "In-Kind" — once
    under Contributions, once under Expenditures:

        Contributions
          Cash Contributions            $0.00
          Loans Received (non-exempt)   $0.00
          In-Kind                       $0.00      <- contributions
          Total Contributions           $0.00
        Expenditures
          Cash Expenditures         $1,440.59
          Loan Payments (non-exempt)    $0.00
          In-Kind                       $0.00      <- expenditures
          Total Expenditures        $1,440.59

    A plain search for "In-Kind" finds the contributions row both times, which
    is why asking for "In-Kind Contributions" and "In-Kind Expenditures" — the
    labels the page never uses — returned nothing at all: every one of 45,938
    yearly records had both fields at zero while committees plainly had in-kind.
    Scoping to the enclosing section is what tells the two rows apart.
    """
    a = html.find(start_label) if html else -1
    if a == -1:
        return default
    b = html.find(end_label, a)
    if b == -1:
        return default
    return parse_dollar(html[a:b], label, default)


_FILER_TYPE_RE = re.compile(r'var\s+filerType\s*=\s*"([A-Za-z]*)"')

# ORESTAR's code for an Independent Expenditure Filer. Its account-summary page
# carries the full committee template, balances included, but its own script
# hides that section and shows only expenditures: ORESTAR publishes no cash
# balance for these filers. Tyler Miller (19990): hidden "Ending Cash Balance
# ($9,640.00)", visible page "Cash Expenditures / Accounts Payable" only.
INDEPENDENT_FILER_TYPE = "IF"


def parse_filer_type(html: str) -> str | None:
    """ORESTAR's filer-type code from an account-summary page, or None.

    The page script decides what to display from ``var filerType = "..."``:
    "IF" for an Independent Expenditure Filer, "PAC" or "CC" and so on for
    committees. It is set server-side, so it reads the same from raw HTML.
    """
    match = _FILER_TYPE_RE.search(html or "")
    return match.group(1).upper() if match and match.group(1) else None
