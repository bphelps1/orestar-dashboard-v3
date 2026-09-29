"""ORESTAR's balance after a committee is discontinued.

When a committee files a Discontinuation, ORESTAR stops producing real
account summaries for it. The statements that follow are blank: no activity,
and a beginning balance of $0.00 whatever the last real statement closed at.
No transaction or balance adjustment accounts for the drop. Verified on
ORESTAR 2026-09-19 for every committee where it happens:

  committee                       last close     discontinued   blank from
  Total Recall PAC                $8,459.85      02/07/2026     2025
  Recall Ted Wheeler              $100.00        02/07/2026     2025
  Friends of Rick Harrington      $100.00        03/03/2026     2025
  Our Portland PAC                -$285.93       03/06/2026     2025
  Friends of Peggy Stevens        $100.00        11/19/2020     2021
  Promote Oregon Leadership PAC   $968.02        12/20/2021     2022

Each committee's full ORESTAR record, deleted and expired rows included,
matches our rows, with nothing after the reset that could explain it except
Promote Oregon Leadership PAC's final $968.02 payment, which ORESTAR's blank
2022 statement ignores but our rows carry. None held a certificate, and no
other committee opened with the vanished balance.

The rule mirrors ORESTAR: the balance ends at $0.00 with the first blank
statement. It is measured so that neither our own rows in the blank years
nor any gap from before the reset are absorbed:

    amount = -(ORESTAR's last real close) - (our net in the blank years)

so Promote Oregon Leadership PAC, whose own rows already reach $0.00, gets
nothing, and a real data gap from earlier years stays visible.

Independent Expenditure Filers look similar in our stored summaries but are
a different thing. ORESTAR publishes no balance for them at all (see
orestar_parse.parse_filer_type), so they are handled by not showing one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

CLOSURE_ID_PREFIX = "closure-reset-"
CLOSURE_SUB_TYPE = "Closure Restatement (derived)"
DISCONTINUATION = "Discontinuation"

_ACTIVITY_KEYS = (
    "contributions", "expenditures", "other_receipts", "other_disbursements",
    "balance_adjustments", "loans_received", "loan_payments",
    "loans_received_exempt", "loan_payments_exempt",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def statement_is_blank(summary: Any) -> bool:
    """A statement with no activity at all and a zero balance throughout."""
    if not isinstance(summary, dict):
        return False
    for key in ("beginning_balance", "ending_cash_balance", *_ACTIVITY_KEYS):
        value = summary.get(key)
        if value is None:
            continue
        number = _number(value)
        if number is None or abs(number) > 0.005:
            return False
    return (_number(summary.get("beginning_balance")) is not None
            and _number(summary.get("ending_cash_balance")) is not None)


def discontinued_on(metadata: Any) -> str | None:
    """The Discontinuation filing's effective date, from filer metadata."""
    if not isinstance(metadata, dict):
        return None
    if str(metadata.get("filing_type") or "").strip() != DISCONTINUATION:
        return None
    text = str(metadata.get("filing_effective_from") or "").strip()
    try:
        datetime.strptime(text, "%m/%d/%Y")
    except ValueError:
        return None
    return text


def closure_reset(
    orestar_years: Any,
    certificate_years: Iterable[int],
    our_nets: dict[int, float] | None,
    discontinued: str | None,
) -> dict | None:
    """The one restatement a discontinued committee's record implies, or None.

    Applies only when all of these hold:

      * ORESTAR records a Discontinuation filing for the committee;
      * its statements end in a run of blank ones, and the statement before
        that run closed at a non-zero balance;
      * neither of those two years held a certificate (that step belongs to
        orestar_certificates);
      * the discontinuation is no earlier than the last real statement's year.

    The row is dated 1 January of the first blank year, where ORESTAR's
    balance becomes $0.00.
    """
    if not discontinued or not isinstance(orestar_years, dict):
        return None
    years = sorted(int(y) for y in orestar_years if str(y).isdigit())
    if len(years) < 2:
        return None
    tail: list[int] = []
    for year in reversed(years):
        if not statement_is_blank(orestar_years.get(str(year))):
            break
        tail.insert(0, year)
    if not tail or len(tail) == len(years):
        return None
    first_blank = tail[0]
    last_real = years[years.index(first_blank) - 1]
    if first_blank - last_real != 1:
        return None
    certs = {int(y) for y in certificate_years or ()}
    if last_real in certs or first_blank in certs:
        return None
    closing = _number((orestar_years.get(str(last_real)) or {}).get("ending_cash_balance"))
    if closing is None or abs(closing) <= 0.005:
        return None
    if datetime.strptime(discontinued, "%m/%d/%Y").year < last_real:
        return None
    ours = {int(k): float(v) for k, v in (our_nets or {}).items()}
    tail_offset = round(sum(ours.get(year, 0.0) for year in tail), 2)
    amount = round(-closing - tail_offset, 2)
    if abs(amount) <= 0.005:
        return None
    return {
        "date": f"{first_blank}-01-01",
        "year": first_blank,
        "amount": amount,
        "boundary": [last_real, first_blank],
        "orestar_prior_ending": round(closing, 2),
        "tail_offset": tail_offset,
        "discontinued_on": discontinued,
    }
