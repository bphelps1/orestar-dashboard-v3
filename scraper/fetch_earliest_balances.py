#!/usr/bin/env python3
"""
fetch_earliest_balances.py — Scrape ORESTAR Account Summary data for all years.

Uses Playwright (headed browser) to navigate ORESTAR Account Summary pages
backwards via the "Prev" button, collecting ALL years' account summary data
and the earliest-year "Beginning Balance (Previous Year)" for each filer.

This is needed because:
  - ORESTAR's default Account Summary page shows the current year only
  - Year navigation uses POST forms with OWASP CSRF tokens
  - Plain HTTP requests can't navigate years; a real browser session is required
  - Back-calculating from the current year's beginning balance introduces errors
  - Per-year ORESTAR data enables tracing where discrepancies originate

Output:
  data/earliest_balances.json — { "filer_id": { "earliest_year": int, "beginning_balance": float, "ts": float }, ... }
  data/orestar_yearly_summaries.json — { "filer_id": { "years": { "2026": {...}, "2025": {...} }, "ts": float }, ... }

Usage:
    python scraper/fetch_earliest_balances.py [--filer-ids 19763 12345] [--max-filers 100]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys

import orestar_parse
import supabase_sync
import time
from datetime import datetime
from pathlib import Path

from balance_snapshot import (
    CALCULATION_VERSION,
    CAPTURE_KEY,
    FORMAT_VERSION,
    SOURCE_FILENAME,
    exact_evidence_identifier_is_valid,
    make_summary_capture,
    normalize_filer_id,
    scope_key,
    source_year_transaction_digest,
    transaction_snapshot_id,
    year_transaction_digest_map_is_valid,
)

log = logging.getLogger(__name__)

BASE_URL = "https://secure.sos.state.or.us/orestar"
DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "earliest_balances.json"
YEARLY_PATH = DATA_DIR / "orestar_yearly_summaries.json"
SNAPSHOT_SOURCE_PATH = DATA_DIR / "aggregated" / SOURCE_FILENAME
TRANSACTION_DIR = DATA_DIR / "transactions"
SWEEP_STATE_PATH = DATA_DIR / "account_summary_sweep_state.json"
PIPELINE_STATE_BASE_PATH = DATA_DIR.parent / ".pipeline-state-base.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Seconds to let F5's JavaScript challenge resolve into the real page before
# reloading. The challenge is quick; the old fixed 5-second sleep was both too
# long when it had already cleared and too short when it had not.
CHALLENGE_WAIT = 20
# How many times to re-request a page that never gets past the challenge.
PAGE_LOAD_ATTEMPTS = 3

# Three consecutive pages that exhaust every recognized F5 challenge attempt
# are a site-level refusal, not three coincidentally bad committees. Stop at the
# canonical scope boundary so the workflow can publish completed captures and
# retry on a fresh runner after a real cooldown. Without this guard, each
# refused filer consumes roughly seven minutes and a blocked batch burns its
# entire 150-minute budget before reaching the existing percentage breaker.
CONSECUTIVE_CHALLENGE_ABORT = 3

# Wait between "Prev" clicks
PREV_CLICK_WAIT = 1.5
# Retries for a single Prev click. A flaky navigation must not be able to
# pass itself off as the beginning of a committee's filing history.
PREV_CLICK_RETRIES = 3

# Fraction of a batch that may fail before the run is treated as blocked
# rather than merely unlucky.
BATCH_FAILURE_ABORT = 0.5

# Polite delay between filers
FILER_DELAY = 1.0

_YEAR_RE = re.compile(r"Account Summary Information for the year\s+(\d{4})")
SUMMARY_FIELD_VERSION = 2


class BotChallengeExhausted(RuntimeError):
    """The current account-summary page never escaped ORESTAR's F5 gate."""


_BOT_CHALLENGE_MARKERS = (
    "checking your browser",
    "/tspd/",
    "tspd_",
)


def _is_bot_challenge(html: str) -> bool:
    """Recognize F5's challenge without treating every bad page as retryable."""
    lowered = html.lower()
    return any(marker in lowered for marker in _BOT_CHALLENGE_MARKERS)


def _next_challenge_streak(previous: int, challenge_exhausted: bool) -> int:
    """Advance the site-refusal circuit, resetting it after a real response."""
    return previous + 1 if challenge_exhausted else 0


def _batch_block_state(
    attempted: int,
    failed: int,
    challenge_circuit_open: bool,
) -> tuple[bool, bool]:
    """Return (hard-stop, cooled-retry-authorized) for a completed batch."""
    generic_failure_stop = bool(
        attempted and failed / attempted >= BATCH_FAILURE_ABORT
    )
    return (
        challenge_circuit_open or generic_failure_stop,
        challenge_circuit_open,
    )


def _parse_year(html: str) -> int | None:
    m = _YEAR_RE.search(html)
    return int(m.group(1)) if m else None


def _parse_beginning_balance(html: str) -> float:
    """The anchor the whole rolling cash-on-hand calculation starts from.

    Worth its own function purely to mark how much rides on it: this figure
    seeds every subsequent year's balance for the filer, so a sign error here
    is not a display bug, it is a wrong number all the way down.
    """
    return orestar_parse.parse_dollar(html, "Beginning Balance (Previous Year)", 0.0)


def _parse_dollar(html: str, label: str) -> float | None:
    """Extract a dollar amount following a label in the ORESTAR HTML.

    Labels are passed as literals — the shared parser escapes them — so
    "Beginning Balance (Previous Year)" is written plainly here.
    """
    # ``None`` is evidence, not inconvenience.  Synthetic zeros in optional
    # loan/status fields can delete real transactions once a fresh scrape
    # timestamp makes those fields authoritative.
    return orestar_parse.parse_dollar(html, label, None)


def _parse_yearly_summary(html: str) -> dict | None:
    """Parse one complete Account Summary page.

    A year heading alone is not proof that F5/ORESTAR returned the financial
    table.  Missing labels used to default to zero, turning a partial page into
    a valid-looking paired $0 balance.  The core arithmetic rows are required;
    a genuine displayed zero still parses as ``0.0``.
    """
    required = {
        "beginning_balance": orestar_parse.parse_dollar(
            html, "Beginning Balance (Previous Year)", None),
        "contributions": orestar_parse.parse_dollar(
            html, "Total Contributions", None),
        "expenditures": orestar_parse.parse_dollar(
            html, "Total Expenditures", None),
        "other_receipts": orestar_parse.parse_dollar(
            html, "Other Receipts", None),
        "other_disbursements": orestar_parse.parse_dollar(
            html, "Other Disbursements", None),
        "balance_adjustments": orestar_parse.parse_dollar(
            html, "Balance Adjustments", None),
        "ending_cash_balance": orestar_parse.parse_dollar(
            html, "Ending Cash Balance", None),
    }
    missing = [label for label, value in required.items() if value is None]
    if missing:
        log.warning("Account Summary page is missing required fields: %s",
                    ", ".join(missing))
        return None

    return {
        **required,
        "summary_field_version": SUMMARY_FIELD_VERSION,
        # ORESTAR's own filer-type code. "IF" pages hide the balance section
        # above: an Independent Expenditure Filer has no published balance.
        "filer_type": orestar_parse.parse_filer_type(html),
        "loans_received": _parse_dollar(html, "Loans Received (Non-Exempt)"),
        "loans_received_exempt": _parse_dollar(html, "Loans Received (Exempt)"),
        "loan_payments": _parse_dollar(html, "Loan Payments (Non-Exempt)"),
        "loan_payments_exempt": _parse_dollar(html, "Loan Payments (Exempt)"),
        # Both rows are labelled just "In-Kind"; only the enclosing section
        # tells them apart. Asking for labels the page never prints returned
        # zero for all 45,938 yearly records.
        "inkind_contributions": orestar_parse.parse_dollar_between(
            html, "Cash Contributions", "Total Contributions", "In-Kind", None),
        "inkind_expenditures": orestar_parse.parse_dollar_between(
            html, "Cash Expenditures", "Total Expenditures", "In-Kind", None),
        "accounts_receivable": _parse_dollar(html, "Accounts Receivable"),
        "accounts_payable": _parse_dollar(html, "Accounts Payable"),
        "total_outstanding_loans": _parse_dollar(html, "Total Outstanding Loans"),
        "outstanding_personal_expenditures": _parse_dollar(html, "Outstanding Personal Expenditures"),
        "balance_deficit": _parse_dollar(html, "Balance Deficit"),
    }


def _safe_content(page, tries: int = 6, pause: float = 0.5) -> str:
    """page.content() that survives the challenge re-navigating underneath it.

    F5 replaces the document mid-flight — that is the whole mechanism of the
    bot challenge — so reading content is inherently racy:

        playwright._impl._errors.Error: Page.content: Unable to retrieve
        content because the page is navigating and changing the content

    The two reads inside the challenge-wait loops already tolerated this, since
    a failed read there simply costs another half-second pass. The read AFTER
    the paging loop did not, so an unlucky moment raised out of the scraper and
    ended a chained sweep with 565 filers left to do.

    Returning "" on exhaustion matches the callers, which already treat empty
    content as "did not resolve" rather than as an answer.
    """
    for _ in range(tries):
        try:
            return page.content()
        except Exception:
            time.sleep(pause)
    return ""


def _load_summary_page(page, url: str, filer_id: str) -> str | None:
    """Load an Account Summary page, sitting through F5's bot challenge.

    ORESTAR fronts every request with an F5/TSPD JavaScript challenge: it
    answers 200 with a ~7 KB script that computes a token, sets a cookie and
    re-requests the real page. The cookies carry Max-Age=30, so the challenge
    recurs constantly rather than once per session.

    The old code did `page.goto(url)` — which waits for the LOAD event — and
    called anything else a failure. That is the wrong success condition: the
    challenge document replaces itself mid-flight, so `load` may never fire on
    the document being waited for, and a 30-second timeout was reported as
    "Failed to load page" even when the browser went on to render the real
    thing. On 27 July that turned into 200 of 200 filers "failing" while a
    plain browser could open the very same URLs.

    So: return as soon as the DOM is parsed, then wait for the thing that
    actually matters — the year heading — reloading a few times to give the
    challenge room. A failure now means the data genuinely never arrived.
    """
    last_nonempty_html = ""
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            # Not fatal by itself — the challenge often lands the real page
            # anyway, so fall through and check the content before judging.
            log.debug("Filer %s: goto attempt %d raised: %s", filer_id, attempt, e)

        deadline = time.time() + CHALLENGE_WAIT
        while time.time() < deadline:
            try:
                html = page.content()
            except Exception:
                html = ""
            if _YEAR_RE.search(html):
                return html
            if html:
                last_nonempty_html = html
            time.sleep(0.5)

        log.debug("Filer %s: challenge not cleared on attempt %d/%d",
                  filer_id, attempt, PAGE_LOAD_ATTEMPTS)

    if _is_bot_challenge(last_nonempty_html):
        log.warning(
            "Filer %s: recognized bot challenge never resolved after %d attempts",
            filer_id,
            PAGE_LOAD_ATTEMPTS,
        )
        raise BotChallengeExhausted(filer_id)

    # Absence of the expected heading is not, by itself, evidence of F5. Keep
    # server errors, redirects, and malformed financial pages on the ordinary
    # failure path so they cannot authorize an automatic retry.
    log.warning("Filer %s: Account Summary never rendered after %d attempts",
                filer_id, PAGE_LOAD_ATTEMPTS)
    return None


def _scrape_filer_earliest(
    page,
    filer_id: str,
    *,
    current_only: bool = False,
) -> tuple[dict | None, dict, dict | None]:
    """Navigate to a filer's Account Summary and click Prev until earliest year.

    Returns (earliest_balance_info, yearly_summaries, current_capture) where
    yearly_summaries is
    {year_str: {beginning_balance, contributions, expenditures, ...}}.

    ``current_capture.captured_at`` is taken immediately after parsing the
    current page.  Paging through twenty historical years can take minutes;
    stamping at the end would claim the current balance was read much later
    than it actually was.
    """
    yearly = {}  # year_str → summary dict
    url = f"{BASE_URL}/publicAccountSummary.do?filerId={filer_id}"

    html = _load_summary_page(page, url, filer_id)
    if html is None:
        return None, yearly, None

    year = _parse_year(html)
    if not year:
        log.warning("No year found on page for filer %s", filer_id)
        return None, yearly, None

    log.info("Filer %s: starting at year %d", filer_id, year)

    # Collect the current year's data and timestamp THAT read, before paging
    # away from it.  The timestamp is paired with the app snapshot in main(),
    # where the exact transaction-set fingerprint is available.
    current_summary = _parse_yearly_summary(html)
    if current_summary is None:
        log.warning("Filer %s: current Account Summary was incomplete", filer_id)
        return None, yearly, None
    captured_at = time.time()
    current_summary["scrape_ts"] = captured_at
    yearly[str(year)] = current_summary
    current_capture = {
        "captured_at": captured_at,
        "orestar_year": year,
        "summary": current_summary,
    }

    # Current balances change constantly; historical opening anchors do not.
    # The weekly freshness sweep uses one page per filer and leaves the much
    # more expensive Prev crawl to its separate monthly pass.
    if current_only:
        log.info("Filer %s: captured current %d summary only", filer_id, year)
        return None, yearly, current_capture

    # Page back with "Prev" to the FIRST account statement ORESTAR holds. Its
    # "Beginning Balance (Previous Year)" is the anchor the whole rolling
    # cash-on-hand calculation starts from.
    #
    # Reaching that page is the entire point, so the two ways of stopping are
    # kept apart. Arriving at the first statement — no Prev button left, or the
    # year stops going down — is a RESULT. Running out of clicks, or a click
    # that fails, is a FAILURE that happens to leave the browser on some middle
    # year whose beginning balance is a mid-history figure, not an opening one.
    #
    # Both used to `break` into the same code path, so a timeout on one click
    # recorded whatever year it stopped on as "earliest". 155 filers carry a
    # balance collected that way, the worst of them 19 years short: filer 142
    # stopped at 2024 and banked $220,614.68 as an opening balance for a
    # committee that has been filing since 2006.
    prev_year = year
    max_clicks = 30                    # 2006–2026 is 21 years, so this is slack
    reached_earliest = False

    for click_num in range(max_clicks):
        prev_btn = page.locator('input[type="submit"][value="Prev"]').first
        if prev_btn.count() == 0:
            log.info("Filer %s: no Prev button at year %d — first statement reached",
                     filer_id, year)
            reached_earliest = True
            break

        # Retry the click: a single flaky navigation should not be allowed to
        # masquerade as the start of a committee's history.
        # Each Prev is a fresh request, so F5 can challenge again — its cookies
        # last 30 seconds. Waiting on "networkidle" is the same wrong success
        # condition as before: wait for the year heading to come back instead.
        html, new_year = None, None
        last_nonempty_html = ""
        for attempt in range(PREV_CLICK_RETRIES):
            try:
                prev_btn.click()
            except Exception as e:
                log.warning("Filer %s: Prev click failed at year %d (attempt %d/%d): %s",
                            filer_id, year, attempt + 1, PREV_CLICK_RETRIES, e)
                time.sleep(PREV_CLICK_WAIT * (attempt + 1))
                continue
            deadline = time.time() + CHALLENGE_WAIT
            while time.time() < deadline:
                try:
                    html = page.content()
                except Exception:
                    html = ""
                if html:
                    last_nonempty_html = html
                candidate_year = _parse_year(html)
                # The old DOM remains readable while navigation is in flight.
                # Seeing the SAME heading is therefore not the ORESTAR floor;
                # it is merely proof that the click has not completed yet.
                if candidate_year and candidate_year < prev_year:
                    new_year = candidate_year
                    break
                time.sleep(0.5)
            if new_year:
                break
            log.warning("Filer %s: year did not decrease after Prev at %d "
                        "(attempt %d/%d)",
                        filer_id, year, attempt + 1, PREV_CLICK_RETRIES)

        if not new_year:
            if _is_bot_challenge(last_nonempty_html):
                raise BotChallengeExhausted(filer_id)
            log.error("Filer %s: lost the year after Prev click %d — opening balance "
                      "NOT trustworthy", filer_id, click_num + 1)
            break

        historical_summary = _parse_yearly_summary(html)
        if historical_summary is None:
            log.error("Filer %s: Account Summary for year %d was incomplete; "
                      "opening balance NOT trustworthy", filer_id, new_year)
            break
        historical_summary["scrape_ts"] = time.time()
        yearly[str(new_year)] = historical_summary
        prev_year = new_year
        year = new_year
    else:
        log.error("Filer %s: still paging after %d clicks (year %d) — opening balance "
                  "NOT trustworthy", filer_id, max_clicks, year)

    html = _safe_content(page)
    final_year = _parse_year(html) or year
    # The page for ``year`` was already validated above. Reuse that parsed
    # value instead of letting a racing page.content() failure silently turn
    # the opening anchor into zero.
    beg_bal = float((yearly.get(str(year)) or {}).get("beginning_balance") or 0.0)

    log.info("Filer %s: earliest year = %d, beginning balance = $%.2f, years = %d, complete = %s",
             filer_id, final_year, beg_bal, len(yearly), reached_earliest)

    earliest = {
        "earliest_year": final_year,
        "beginning_balance": beg_bal,
        # Consumers must check this before using beginning_balance as an anchor.
        "reached_earliest": reached_earliest,
        "ts": datetime.now().timestamp(),
    }
    return earliest, yearly, current_capture


def get_all_filer_ids() -> list[str]:
    """Extract the filer IDs represented by the app balance snapshot.

    ``orestar_cash_balances.json`` is a retired, drifting cache and can miss
    committees that the app actually aggregates.  The paired snapshot source
    describes the exact comparison population.  Older checkouts fall back to
    the yearly-summary cache, then the legacy cash cache, so rollout does not
    require a flag day.
    """
    if supabase_sync.sync_enabled():
        source = supabase_sync.require_dashboard_cache("balance_snapshot_source")
        ids = {
            str(fid).strip()
            for scope in (source.get("scopes") or {}).values()
            for fid in (scope.get("filer_ids") or [])
            if str(fid).strip()
        }
        if ids:
            return sorted(ids)
        raise RuntimeError("Supabase balance_snapshot_source has no filer population")

    if SNAPSHOT_SOURCE_PATH.exists():
        try:
            source = json.loads(SNAPSHOT_SOURCE_PATH.read_text())
            ids = {
                str(fid).strip()
                for scope in (source.get("scopes") or {}).values()
                for fid in (scope.get("filer_ids") or [])
                if str(fid).strip()
            }
            if ids:
                return sorted(ids)
        except Exception as exc:
            log.warning("Could not read filer IDs from %s: %s",
                        SNAPSHOT_SOURCE_PATH, exc)

    for cache_path in (YEARLY_PATH, DATA_DIR / "orestar_cash_balances.json"):
        if not cache_path.exists():
            continue
        try:
            cache = json.loads(cache_path.read_text())
            if cache:
                log.warning("Using legacy filer population from %s", cache_path)
                return sorted(str(fid) for fid in cache)
        except Exception as exc:
            log.warning("Could not read filer IDs from %s: %s", cache_path, exc)

    log.error("No filer population found — run aggregation first")
    return []


def _pipeline_transaction_snapshot_id() -> str | None:
    """Read the transaction fingerprint carried by the hydrated state manifest."""
    try:
        state = json.loads(PIPELINE_STATE_BASE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = state.get("transaction_snapshot_id") if isinstance(state, dict) else None
    return value if exact_evidence_identifier_is_valid(value) else None


def _eligible_source_scopes(source: dict | None) -> dict[str, tuple[str, list[str]]]:
    """Map each unambiguous physical filer ID to its complete app scope."""
    occurrences: dict[str, list[tuple[str, list[str], dict]]] = {}
    for key, record in ((source or {}).get("scopes") or {}).items():
        members = sorted({normalize_filer_id(fid)
                          for fid in record.get("filer_ids", [])
                          if normalize_filer_id(fid)})
        for fid in members:
            occurrences.setdefault(fid, []).append((str(key), members, record))

    eligible: dict[str, tuple[str, list[str]]] = {}
    seen_scopes: set[str] = set()
    for matches in occurrences.values():
        for key, members, record in matches:
            if key in seen_scopes:
                continue
            seen_scopes.add(key)
            # Eligibility is a property of the whole canonical scope. If one
            # member also belongs to another scope, selecting an otherwise
            # unique sibling would expand back into an ID make_summary_capture
            # can never pair and the sweep would retry forever.
            if (record.get("status") == "ambiguous"
                    or not exact_evidence_identifier_is_valid(
                        record.get("app_scope_transaction_digest")
                    )
                    or not year_transaction_digest_map_is_valid(
                        record.get("app_year_transaction_digests")
                    )
                    or any(len(occurrences.get(member, [])) != 1
                           for member in members)):
                continue
            for member in members:
                eligible[member] = (key, members)
    return eligible


def _group_ids_by_source_scope(
    filer_ids: list[str],
    source: dict | None,
) -> list[list[str]]:
    """Keep every selected canonical scope together in one scrape batch.

    If one member of a multi-ID profile is stale or failed, every member must
    be recaptured against the same transaction snapshot. Refreshing only the
    stale member makes the scope unusable and can cause its members to alternate
    forever between otherwise-fresh but incompatible captures.
    """
    wanted = {normalize_filer_id(fid) for fid in filer_ids if normalize_filer_id(fid)}
    eligible = _eligible_source_scopes(source)
    selected_keys = {
        eligible[fid][0] for fid in wanted if fid in eligible
    }
    members_by_key = {
        key: members for key, members in eligible.values()
    }
    groups: list[list[str]] = []
    included: set[str] = set()
    for key in sorted(selected_keys):
        member = members_by_key[key]
        group = [fid for fid in member if fid not in included]
        if group:
            groups.append(group)
            included.update(group)
    for fid in sorted(wanted - included):
        groups.append([fid])
    return groups


def _load_sweep_state() -> dict:
    if not SWEEP_STATE_PATH.exists():
        return {}
    try:
        value = json.loads(SWEEP_STATE_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        log.warning("Could not read sweep state %s: %s", SWEEP_STATE_PATH, exc)
        return {}


def _save_sweep_state(state: dict) -> None:
    SWEEP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_STATE_PATH.write_text(json.dumps(state, separators=(",", ":")))


def _begin_or_resume_sweep(
    mode: str,
    requested_cutoff: float,
    now_ts: float,
    *,
    force: bool = False,
) -> tuple[float, dict]:
    """Persist one cutoff until a mode's full population reaches zero."""
    state = _load_sweep_state()
    existing = state.get(mode) or {}
    if existing.get("refresh_before_ts") and not force:
        cutoff = float(existing["refresh_before_ts"])
        log.info("Resuming %s sweep with fixed cutoff %.3f", mode, cutoff)
    else:
        cutoff = float(requested_cutoff)
        state[mode] = {"refresh_before_ts": cutoff, "started_at": now_ts}
        _save_sweep_state(state)
        log.info("Started %s sweep with fixed cutoff %.3f", mode, cutoff)
    return cutoff, state


def _current_capture_needs_refresh(
    entry: dict | None,
    cutoff: float,
    source_record: dict | None = None,
) -> bool:
    """Whether a current-page capture can satisfy this sweep generation."""
    capture = (entry or {}).get(CAPTURE_KEY) or {}
    if not capture.get("captured_at"):
        return True
    if (capture.get("version") != FORMAT_VERSION
            or capture.get("calculation_version") != CALCULATION_VERSION):
        return True
    if capture.get("status") != "paired":
        return True
    if not exact_evidence_identifier_is_valid(
        capture.get("app_scope_transaction_digest")
    ):
        return True
    if not exact_evidence_identifier_is_valid(
        capture.get("app_year_transaction_digest")
    ):
        return True
    if source_record is not None:
        if not isinstance(source_record, dict):
            return True
        source_digest = source_record.get("app_scope_transaction_digest")
        if (not exact_evidence_identifier_is_valid(source_digest)
                or source_digest != capture.get("app_scope_transaction_digest")):
            return True
        source_year_digest = source_year_transaction_digest(
            source_record, capture.get("orestar_year")
        )
        if (not exact_evidence_identifier_is_valid(source_year_digest)
                or source_year_digest
                != capture.get("app_year_transaction_digest")):
            return True
        try:
            source_cash = round(float(source_record["cash_on_hand"]), 2)
            captured_cash = round(float(capture["app_cash_on_hand"]), 2)
            source_count = int(source_record["tran_count"])
            captured_count = int(capture["app_tran_count"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return True
        if source_cash != captured_cash or source_count != captured_count:
            return True
    attempt = (entry or {}).get("comparison_capture_attempt") or {}
    if (attempt and float(attempt.get("captured_at") or 0.0)
            > float(capture.get("captured_at") or 0.0)):
        return True
    return float(capture.get("captured_at") or 0) < cutoff


def _historical_year_provenance_needs_refresh(
    entry: dict | None,
    source_record: dict | None = None,
) -> bool:
    """Whether any cached annual row lacks current exact app-year provenance.

    Legacy annual rows can be arbitrarily recent while still being unsafe for
    comparison: their timestamp does not identify the transaction rows they
    were read against.  Historical sweeps must replace that legacy state even
    when the ordinary age cutoff would skip it.  Current-only sweeps
    deliberately do not call this predicate, because refreshing one current
    page cannot repair older annual rows.  When a current source scope is
    available, a previously proven row must also match that source year's
    digest: late-filed historical transactions otherwise leave a recent cache
    looking current solely because its provenance fields are populated.
    """
    # Provenance can only be repaired from a current, eligible app source.
    # Returning True without one makes a chained historical sweep select the
    # same legacy rows forever: the crawl can refresh their source values, but
    # has no digest with which to stamp them.  Ordinary age, missing-cache and
    # incomplete-opening criteria remain independent callers of this helper.
    if (
        not isinstance(source_record, dict)
        or source_record.get("status") == "ambiguous"
        or not exact_evidence_identifier_is_valid(
            source_record.get("app_scope_transaction_digest")
        )
        or not year_transaction_digest_map_is_valid(
            source_record.get("app_year_transaction_digests")
        )
    ):
        return False

    years = (entry or {}).get("years") or {}
    if not isinstance(years, dict):
        return True
    for year, row in years.items():
        if (
            not isinstance(row, dict)
            or not exact_evidence_identifier_is_valid(
                row.get("app_year_transaction_digest")
            )
            or row.get("calculation_version") != CALCULATION_VERSION
            or not exact_evidence_identifier_is_valid(row.get("scope_capture_id"))
        ):
            return True
        source_digest = source_year_transaction_digest(source_record, year)
        if (
            not exact_evidence_identifier_is_valid(source_digest)
            or source_digest != row.get("app_year_transaction_digest")
        ):
            return True
    return False


def _merge_earliest_result(previous: dict | None, attempt: dict) -> dict:
    """Preserve a proven opening anchor across a failed historical recrawl."""
    old = previous or {}
    if not old.get("reached_earliest"):
        return dict(attempt)

    try:
        old_year = int(old.get("earliest_year"))
        new_year = int(attempt.get("earliest_year"))
    except (TypeError, ValueError):
        old_year = 9999
        new_year = 9999

    # A complete crawl may discover an older statement and safely improve the
    # anchor. It may not erase a previously proven older statement merely
    # because a truncated DOM lost its Prev control on a newer page.
    if attempt.get("reached_earliest") and new_year <= old_year:
        return dict(attempt)

    kept = dict(old)
    key = (
        "inconsistent_refresh_attempt"
        if attempt.get("reached_earliest")
        else "incomplete_refresh_attempt"
    )
    kept[key] = dict(attempt)
    return kept


def _snapshot_source_ready(source: dict | None, transaction_id: str | None) -> bool:
    return bool(
        source
        and source.get("version") == FORMAT_VERSION
        and source.get("calculation_version") == CALCULATION_VERSION
        and transaction_id
        and source.get("transaction_snapshot_id") == transaction_id
        and _eligible_source_scopes(source)
    )


def _commit_scope_captures(
    filer_ids: list[str],
    staged: dict[str, dict],
    yearly_cache: dict,
    fresh_year_digests: dict[str, dict[str, str | None]] | None = None,
) -> bool:
    """Atomically promote one canonical scope's comparison captures.

    A multi-ID profile represents one app balance, so captures from separate
    partial attempts cannot be mixed even when the transaction fingerprint did
    not change between attempts.  Successful component reads are staged until
    every member succeeds in this group.  A partial read leaves the last valid
    common pair intact and records one shared, newer unpaired attempt so the old
    result becomes refresh-only rather than actionable.
    """
    members = sorted({normalize_filer_id(fid) for fid in filer_ids
                      if normalize_filer_id(fid)})
    captures = {normalize_filer_id(fid): value for fid, value in staged.items()
                if normalize_filer_id(fid) and isinstance(value, dict)}
    expected_scope = scope_key(members)

    # A freshly scraped annual row is useful historical data even when another
    # physical member of the canonical scope fails. It is not, however, paired
    # app evidence until the complete scope promotes below. Clear any copied or
    # stale proof before evaluating the staged captures, then stamp only after
    # a successful atomic promotion.
    provenance_keys = (
        "app_scope_transaction_digest",
        "app_year_transaction_digest",
        "calculation_version",
        "scope_capture_id",
    )
    fresh_rows: list[tuple[dict, str | None]] = []
    fresh_provenance_valid = True
    for fid in members:
        entry = yearly_cache.get(fid) or {}
        years = entry.get("years") or {}
        for year, year_digest in (fresh_year_digests or {}).get(fid, {}).items():
            row = years.get(str(year))
            if not isinstance(row, dict):
                fresh_provenance_valid = False
                continue
            for key in provenance_keys:
                row.pop(key, None)
            if not exact_evidence_identifier_is_valid(year_digest):
                fresh_provenance_valid = False
            fresh_rows.append((row, year_digest))

    compatible = bool(
        members
        and set(captures) == set(members)
        and fresh_provenance_valid
    )
    if compatible:
        values = list(captures.values())
        compatible = bool(
            all(c.get("version") == FORMAT_VERSION and c.get("status") == "paired"
                for c in values)
            and {str(c.get("app_scope_key") or "") for c in values} == {expected_scope}
            and {scope_key(c.get("app_scope_filer_ids") or []) for c in values}
                == {expected_scope}
            and len({str(c.get("app_transaction_snapshot_id") or "")
                     for c in values}) == 1
            and "" not in {str(c.get("app_transaction_snapshot_id") or "")
                            for c in values}
            and len({str(c.get("calculation_version") or "") for c in values}) == 1
            and {str(c.get("calculation_version") or "") for c in values}
                == {CALCULATION_VERSION}
            and all(exact_evidence_identifier_is_valid(
                c.get("app_scope_transaction_digest")
            ) for c in values)
            and len({c.get("app_scope_transaction_digest") for c in values}) == 1
            and len({round(float(c.get("app_cash_on_hand") or 0.0), 2)
                     for c in values}) == 1
            and len({int(c.get("app_tran_count") or 0) for c in values}) == 1
        )

    if compatible:
        values = list(captures.values())
        captured_at = max(float(c.get("captured_at") or 0.0) for c in values)
        transaction_id = str(values[0]["app_transaction_snapshot_id"])
        # Shared identity is independently checked by paired_comparison.  It
        # prevents alternating partial runs against an unchanged app snapshot
        # from ever looking like one complete ORESTAR capture.
        capture_id = (
            f"{expected_scope}@{transaction_id.removeprefix('sha256:')[:16]}"
            f"@{captured_at:.6f}"
        )
        for fid in members:
            entry = yearly_cache.setdefault(fid, {"years": {}, "ts": 0})
            promoted = dict(captures[fid])
            promoted["scope_capture_id"] = capture_id
            entry[CAPTURE_KEY] = promoted
            entry.pop("comparison_capture_attempt", None)
        for row, year_digest in fresh_rows:
            row.update({
                "app_year_transaction_digest": year_digest,
                "calculation_version": values[0]["calculation_version"],
                "scope_capture_id": capture_id,
            })
        return True

    if not captures:
        # A page-load failure supplied no new ORESTAR evidence.  Keep an old
        # valid pair as-is; the failed member remains selected by the sweep.
        return False

    captured_at = max(float(c.get("captured_at") or 0.0)
                      for c in captures.values())
    reason = (
        "scope_capture_incomplete"
        if set(captures) != set(members)
        else "scope_capture_mismatch"
    )
    attempt = {
        "version": FORMAT_VERSION,
        "status": "unpaired",
        "reason": reason,
        "captured_at": captured_at,
        "app_scope_key": expected_scope,
        "app_scope_filer_ids": members,
        "captured_filer_ids": sorted(captures),
        "missing_filer_ids": sorted(set(members) - set(captures)),
        "component_statuses": {
            fid: {
                "status": capture.get("status"),
                "reason": capture.get("reason"),
                "captured_at": capture.get("captured_at"),
            }
            for fid, capture in sorted(captures.items())
        },
    }
    for fid in members:
        entry = yearly_cache.setdefault(fid, {"years": {}, "ts": 0})
        previous = entry.get(CAPTURE_KEY) or {}
        if previous.get("status") == "paired":
            entry["comparison_capture_attempt"] = dict(attempt)
        else:
            entry[CAPTURE_KEY] = dict(attempt)
            entry.pop("comparison_capture_attempt", None)
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Scrape earliest-year beginning balances from ORESTAR Account Summary pages."
    )
    parser.add_argument(
        "--max-minutes", type=int, default=0,
        help="stop after this long so the JOB's timeout never arrives mid-run "
             "(0 = no budget). A cancelled step is not a successful one, and the "
             "retrigger is gated on success, so a timeout silently ends the chain.",
    )
    parser.add_argument(
        "--filer-ids", nargs="+",
        help="Specific filer IDs to scrape (default: all from ORESTAR cache)",
    )
    parser.add_argument(
        "--max-filers", type=int, default=0,
        help="Max number of filers to process (0 = all)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-scrape even if already cached",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=30,
        help="Re-scrape entries older than this many days (default: 30)",
    )
    parser.add_argument(
        "--current-only", action="store_true",
        help="read only the current account-summary page (no historical Prev crawl)",
    )
    parser.add_argument(
        "--refresh-before-ts", type=float, default=0,
        help="fixed freshness cutoff shared by every batch in one sweep "
             "(Unix timestamp; default derives from --max-age-days)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Determine which filer IDs to process
    filer_ids = [normalize_filer_id(fid) for fid in (args.filer_ids or get_all_filer_ids())]
    if not filer_ids:
        log.error("No filer IDs to process")
        return

    # Load existing cache
    cache: dict = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        log.info("Loaded %d cached earliest balances", len(cache))

    # Load yearly summaries cache to check for missing yearly data
    yearly_cache: dict = {}
    if YEARLY_PATH.exists():
        with open(YEARLY_PATH) as f:
            yearly_cache = json.load(f)
        log.info("Loaded %d cached yearly summary entries", len(yearly_cache))

    # In CI the live dashboard cache describes what the app displays. Account-
    # summary batches hydrate only their small summary profile, so the hydrated
    # manifest independently identifies the persisted transaction profile. Both must
    # agree: using the database ID as its own comparator would turn this safety
    # check into a tautology if one side of a previous publication failed.
    snapshot_source = None
    snapshot_from_database = False
    if supabase_sync.sync_enabled():
        snapshot_source = supabase_sync.require_dashboard_cache(
            "balance_snapshot_source"
        )
        snapshot_from_database = True
    elif SNAPSHOT_SOURCE_PATH.exists():
        try:
            with open(SNAPSHOT_SOURCE_PATH) as f:
                snapshot_source = json.load(f)
        except Exception as exc:
            log.warning("Could not load app balance snapshot source: %s", exc)
    local_transaction_id = transaction_snapshot_id(TRANSACTION_DIR)
    manifest_transaction_id = _pipeline_transaction_snapshot_id()
    current_transaction_id = local_transaction_id or manifest_transaction_id
    if snapshot_from_database and not current_transaction_id:
        log.error("The hydrated pipeline-state manifest has no transaction snapshot ID; "
                  "refusing to pair account summaries")
        sys.exit(1)
    eligible_source_scopes = _eligible_source_scopes(snapshot_source)
    source_records_by_filer = {
        fid: (snapshot_source.get("scopes") or {}).get(key)
        for fid, (key, _members) in eligible_source_scopes.items()
    } if snapshot_source else {}
    source_ready = _snapshot_source_ready(snapshot_source, current_transaction_id)
    if not snapshot_source:
        log.warning("No app balance snapshot source; new summaries will be unpaired")
    elif snapshot_source.get("version") != FORMAT_VERSION:
        log.warning("App balance snapshot source has unsupported version %r; "
                    "new summaries will be unpaired", snapshot_source.get("version"))
    elif snapshot_source.get("calculation_version") != CALCULATION_VERSION:
        log.warning("App balance snapshot source uses calculation version %r, expected %r; "
                    "new summaries will be unpaired",
                    snapshot_source.get("calculation_version"), CALCULATION_VERSION)
    elif snapshot_source.get("transaction_snapshot_id") != current_transaction_id:
        log.warning("App balance snapshot source does not match the current ledger; "
                    "new summaries will be unpaired")
    elif not eligible_source_scopes:
        log.warning("App balance snapshot source has no unambiguous filer scopes; "
                    "new summaries will be unpaired")

    # A current-page sweep exists specifically to establish matched checks. If
    # its app source is missing or stale, every network request would produce
    # an unusable capture and the chain could churn through thousands of pages
    # without creating one comparison. Historical crawls may still proceed:
    # their opening-balance data remains independently useful.
    if args.current_only and not source_ready:
        log.error("Current-summary refresh requires a current app balance snapshot. "
                  "Run scraper/process.py first; refusing an unpaired sweep.")
        sys.exit(1)

    if args.current_only and not args.filer_ids:
        eligible_ids = set(eligible_source_scopes)
        skipped = len(set(filer_ids) - eligible_ids)
        if skipped:
            log.warning("Skipping %d filer IDs that do not map to exactly one app scope", skipped)
        filer_ids = [fid for fid in filer_ids if fid in eligible_ids]

    # Filter to only IDs that need fetching:
    # - Not in earliest_balances cache at all, OR
    # - Stale (older than max_age_days), OR
    # - Missing from yearly summaries (backfill yearly data for previously scraped filers)
    now_ts = datetime.now().timestamp()
    requested_cutoff = (
        now_ts if args.force
        else args.refresh_before_ts or (now_ts - args.max_age_days * 86_400)
    )
    sweep_mode = "current" if args.current_only else "historical"
    if not args.filer_ids:
        cutoff, sweep_state = _begin_or_resume_sweep(
            sweep_mode, requested_cutoff, now_ts, force=args.force
        )
    else:
        cutoff = float(requested_cutoff)
        sweep_state = _load_sweep_state()

    if args.force:
        ids_to_fetch = filer_ids
    elif args.current_only:
        ids_to_fetch = [
            fid for fid in filer_ids
            if (fid not in source_records_by_filer
                or _current_capture_needs_refresh(
                    yearly_cache.get(fid), cutoff,
                    source_record=source_records_by_filer[fid],
                ))
        ]
    else:
        ids_to_fetch = [
            fid for fid in filer_ids
            if fid not in cache
            or cache[fid].get("ts", 0) < cutoff
            or fid not in yearly_cache
            or not yearly_cache[fid].get("years")
            # A legacy row's cache timestamp cannot prove which transaction
            # year it described. Only a historical crawl can backfill exact
            # per-year provenance; current-only intentionally ignores these
            # older rows so the cheap daily/weekly sweep cannot loop on them.
            or _historical_year_provenance_needs_refresh(
                yearly_cache.get(fid),
                # Only a source proven to describe the transaction shards in
                # this checkout can establish that cached year provenance is
                # stale.  A historical crawl is allowed to run without such a
                # source, but must not churn on comparisons it cannot re-pair.
                source_record=(
                    source_records_by_filer.get(fid) if source_ready else None
                ),
            )
            # An entry that never reached the first statement holds a
            # mid-history balance, not an opening one. Retry those regardless
            # of age — freshness is not what is wrong with them.
            #
            # Scoped to entries carrying a non-zero balance, and to ones known
            # to have failed. A cached $0 anchor contributes nothing whether it
            # is trusted or not, so re-scraping all 7,366 to confirm zeros
            # would be thousands of page loads to change no number; those pick
            # the flag up on their normal refresh instead.
            or cache[fid].get("reached_earliest") is False
            or (not cache[fid].get("reached_earliest")
                and cache[fid].get("beginning_balance"))
        ]

    # Pair every physical ID behind a canonical profile against one snapshot.
    # Expanding after freshness selection also brings a freshly captured sibling
    # back into a retry batch when another sibling failed.
    scope_groups = _group_ids_by_source_scope(ids_to_fetch, snapshot_source)
    ids_to_fetch = [fid for group in scope_groups for fid in group]

    # Total remaining (before --max-filers cap) for retrigger logic.
    all_remaining = len(ids_to_fetch)

    if args.max_filers > 0:
        batch_groups = []
        batch_size = 0
        for group in scope_groups:
            if batch_groups and batch_size >= args.max_filers:
                break
            batch_groups.append(group)
            batch_size += len(group)
        ids_to_fetch = [fid for group in batch_groups for fid in group]
        groups_to_fetch = batch_groups
    else:
        groups_to_fetch = scope_groups

    if not ids_to_fetch:
        log.info("All %d filer IDs are already cached and fresh — nothing to do", len(filer_ids))
        if not args.filer_ids:
            sweep_state.pop(sweep_mode, None)
            _save_sweep_state(sweep_state)
        remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
        remaining_path.write_text("0")
        return

    if args.current_only:
        log.info("Will refresh current account summaries for %d filers", len(ids_to_fetch))
    else:
        log.info("Will scrape earliest balances for %d filers (%d already cached, %d need yearly backfill)",
                 len(ids_to_fetch), len(cache),
                 sum(1 for fid in ids_to_fetch if fid in cache and fid not in yearly_cache))

    # Publish the initial count before browser startup. If Playwright itself
    # fails, the workflow must not reuse a stale zero from an earlier sweep and
    # mistakenly run the final aggregation.
    remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
    remaining_path.write_text(str(all_remaining))

    # Launch Playwright
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        log.info("Launching browser (headed mode — required for ORESTAR)...")
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=False,
        )
        page = context.new_page()

        done = 0
        failed = 0
        completed = 0
        consecutive_challenge_failures = 0
        challenge_blocked = False
        # Stop on our own terms, before the job's timeout stops us.
        #
        # This sweep has no natural end inside one run — it walks thousands of
        # filers — so it ran until the job's 180-minute limit cancelled it. A
        # cancelled step is not a successful one, and "Retrigger for next batch"
        # is gated on success(), so the chain ended with 5,454 filers still to
        # do and nothing scheduled to resume. One partial batch, then silence.
        #
        # Finishing early and deliberately keeps the step successful, which is
        # what lets the chain continue. The gate stays as it is on purpose:
        # success() is also how cancelling a runaway chain stops it.
        _deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
        last_save_done = 0
        for group in groups_to_fetch:
            # A multi-ID canonical profile is one comparison unit. Once its
            # first member starts, finish the small group even if the soft time
            # budget passes; stopping between members creates incompatible
            # component captures and defeats the scope-level batching above.
            if _deadline and time.time() >= _deadline:
                log.info("Reached the %d-minute budget after %d filers — stopping so "
                         "this run counts as successful and the chain continues.",
                         args.max_minutes, done)
                break
            staged_captures: dict[str, dict] = {}
            fresh_year_digests: dict[str, dict[str, str | None]] = {}
            group_results: dict[str, dict | None] = {}
            group_had_data = False
            for fid in group:
                try:
                    result, yearly_data, current_capture = _scrape_filer_earliest(
                        page, fid, current_only=args.current_only
                    )
                except BotChallengeExhausted:
                    # Keep the failed member selected by the frozen sweep.
                    # The stable result marker below is the only condition that
                    # authorizes an automatic cooled retry; unrelated parse,
                    # state, or browser failures remain ordinary hard stops.
                    result, yearly_data, current_capture = None, {}, None
                    consecutive_challenge_failures = _next_challenge_streak(
                        consecutive_challenge_failures, True
                    )
                else:
                    consecutive_challenge_failures = _next_challenge_streak(
                        consecutive_challenge_failures, False
                    )
                done += 1
                group_results[fid] = result

                if result:
                    cache[fid] = _merge_earliest_result(cache.get(fid), result)
                    group_had_data = True

                # Historical rows may be persisted independently, but the
                # comparison capture itself remains staged until the complete
                # canonical scope succeeds below.
                if yearly_data and current_capture:
                    entry = yearly_cache.setdefault(fid, {"years": {}, "ts": 0})
                    previous_ts = float(entry.get("ts") or 0)
                    for old_summary in (entry.get("years") or {}).values():
                        if isinstance(old_summary, dict):
                            old_summary.setdefault("scrape_ts", previous_ts)
                    entry["years"].update(yearly_data)
                    source_record = source_records_by_filer.get(fid)
                    fresh_year_digests[fid] = {
                        str(year): (
                            source_year_transaction_digest(source_record, year)
                            if isinstance(source_record, dict) else None
                        )
                        for year in yearly_data
                    }
                    entry["ts"] = current_capture["captured_at"]
                    staged_captures[fid] = make_summary_capture(
                        fid,
                        current_capture["orestar_year"],
                        current_capture["summary"],
                        current_capture["captured_at"],
                        snapshot_source,
                        current_transaction_id,
                    )
                    group_had_data = True

                if done < len(ids_to_fetch):
                    time.sleep(FILER_DELAY)

            scope_paired = _commit_scope_captures(
                group, staged_captures, yearly_cache, fresh_year_digests
            )
            if args.current_only:
                if scope_paired:
                    completed += len(group)
                else:
                    failed += len(group)
            else:
                for fid in group:
                    if (group_results.get(fid) or {}).get("reached_earliest"):
                        completed += 1
                    else:
                        failed += 1

            # Cache writes happen only at group boundaries. A process killed
            # between members therefore cannot persist half of a scope.
            if group_had_data and done - last_save_done >= 10:
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(CACHE_PATH, "w") as f:
                    json.dump(cache, f, separators=(",", ":"))
                with open(YEARLY_PATH, "w") as f:
                    json.dump(yearly_cache, f, separators=(",", ":"))
                last_save_done = done
                remaining_now = max(0, all_remaining - completed)
                remaining_path.write_text(str(remaining_now))
                log.info("Progress: %d / %d done (%d failed), cache saved",
                         done, len(ids_to_fetch), failed)

            if consecutive_challenge_failures >= CONSECUTIVE_CHALLENGE_ABORT:
                challenge_blocked = True
                log.warning(
                    "ORESTAR bot challenge exhausted for %d consecutive filers; "
                    "stopping at the scope boundary for a cooled retry.",
                    consecutive_challenge_failures,
                )
                break

        browser.close()

    # Final cache save
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    with open(YEARLY_PATH, "w") as f:
        json.dump(yearly_cache, f, separators=(",", ":"))

    log.info("Done. Scraped %d filers (%d failed). Cache has %d total entries at %s",
             done, failed, len(cache), CACHE_PATH)

    # Report how many remain uncached (for workflow retrigger logic)
    # Failed or incomplete attempts remain work.  Counting attempts as
    # completions made a 200/200 refusal batch write zero remaining immediately
    # before failing, and smaller failure sets silently disappeared until the
    # next scheduled sweep.
    still_remaining = max(0, all_remaining - completed)
    if args.max_filers > 0 and still_remaining > 0:
        log.info("REMAINING: %d filers still need scraping", still_remaining)

    # The percentage breaker catches every kind of bad batch and remains a hard
    # stop. Only the consecutive, explicitly recognized challenge circuit may
    # authorize a cooled automatic retry.
    batch_blocked, f5_retryable = _batch_block_state(
        done, failed, challenge_blocked
    )

    # Write remaining count to a file for the workflow to read
    remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
    remaining_path.write_text(str(still_remaining))

    # Stable workflow contract.  A human warning is not permission to retry:
    # only this exact marker, emitted after truthful remaining/progress state is
    # persisted, may create a cooled child run.
    print(
        "ACCOUNT_SUMMARY_RESULT "
        f"attempted={done} completed={completed} failed={failed} "
        f"remaining={still_remaining} blocked={int(batch_blocked)} "
        f"f5_retryable={int(f5_retryable)}",
        flush=True,
    )

    # A batch where nearly everything failed is not a batch that ran.
    #
    # On 27 July one scraped 200 filers, failed all 200 on page-load timeouts,
    # logged them as warnings, exited 0 and retriggered itself — holding the
    # concurrency lane and evicting a scheduled job, to correct no balances at
    # all. ORESTAR was up; it was refusing us, most likely after the daily job
    # had just pulled 7,245 pages through it.
    #
    # Failing loudly here stops the chain instead of feeding it into a wall,
    # and the failure rate is the signal: a handful of bad filers is normal,
    # almost none succeeding is a blocked scraper.
    if batch_blocked:
        # The nonzero exit is the chain-stop signal: the workflow's retrigger
        # uses success(). Keep ``still_remaining`` truthful so a downstream
        # step cannot mistake an aborted batch for a completed sweep. Exit
        # before clearing sweep state, even if completion arithmetic changes.
        log.error("%d of %d filers failed (%.0f%%). ORESTAR is refusing this "
                  "scraper — stopping rather than retriggering.",
                  failed, done, 100 * failed / done)
        sys.exit(1)

    if still_remaining == 0 and not args.filer_ids:
        sweep_state.pop(sweep_mode, None)
        _save_sweep_state(sweep_state)


if __name__ == "__main__":
    main()
