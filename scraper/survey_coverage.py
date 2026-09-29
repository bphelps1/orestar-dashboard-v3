#!/usr/bin/env python3
"""
survey_coverage.py — which committees are actually missing transactions?

The backfill recovers missing rows. For a large share of flagged committees
that is not the problem, and pointing the backfill at them spends the request
budget to learn nothing.

Committee for SAIF Keeping is the clearest case: it tops the discrepancy list
at $665,242, ORESTAR holds SEVEN transactions for it in total, we hold all
seven, and ORESTAR's last account summary (2008) says the committee ended with
nothing. There are no rows to fetch. Its delta comes from a transaction-derived
balance disagreeing with a summary for a committee that predates the
transaction record — a different question, with a different answer.

Local 48 Electricians is the opposite: ORESTAR reports 18,968 rows for 2023
against the 9,937 we hold, and a narrowed re-fetch has already recovered rows
that were genuinely absent.

Telling those two apart costs ONE search per committee. ORESTAR prints the
number of matching records on the results page and does not cap it, so a single
query settles "are we missing rows, and how many" without downloading anything.
This surveys that, cheaply, and ranks committees by what the backfill can
actually fix.

Deliberately does not export. A survey that downloaded would cost the same as
the backfill it is meant to target.

Usage:
    python scraper/survey_coverage.py                    # flagged committees, worst delta first
    python scraper/survey_coverage.py --filer-ids 4572 416
    python scraper/survey_coverage.py --limit 40         # stop after N committees
    python scraper/survey_coverage.py --report           # print findings, fetch nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import fetch as F
import supabase_sync
from search_budget import SearchBudget
from balance_snapshot import evidence_is_current

DATA_DIR = Path(__file__).parent.parent / "data"
SURVEY_PATH = DATA_DIR / "coverage_survey.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

FIRST_YEAR = 2006


@dataclass
class SearchSubmissionCounter:
    """Observed submissions for one collection, independent of any budget."""

    count: int = 0


_submission_counter: ContextVar[SearchSubmissionCounter | None] = ContextVar(
    "orestar_search_submission_counter", default=None,
)


@contextmanager
def measure_search_submissions():
    """Measure real search clicks without changing collection or retry rules."""
    counter = SearchSubmissionCounter()
    token = _submission_counter.set(counter)
    try:
        yield counter
    finally:
        _submission_counter.reset(token)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _expand_actionable_rows(rows: list[dict]) -> list[dict]:
    """Expand aggregate comparison scopes into physical IDs for surveying."""
    expanded: dict[str, dict] = {}
    for row in rows or []:
        if (row.get("comparison_status") != "paired"
                or row.get("newer_app_data") or row.get("closed")):
            continue
        ids = row.get("filer_ids") or [row.get("filer_id")]
        for raw_fid in ids:
            fid = str(raw_fid or "").strip()
            if not fid:
                continue
            candidate = {**row, "filer_id": fid}
            previous = expanded.get(fid)
            if previous is None or abs(candidate.get("delta") or 0) > abs(previous.get("delta") or 0):
                expanded[fid] = candidate
    return list(expanded.values())


def _evidence_is_current(evidence: dict | None, comparison: dict | None) -> bool:
    """Whether a count was measured after the paired summary it supports."""
    comparison = comparison or {}
    return evidence_is_current(
        evidence,
        comparison.get("scrape_ts") or comparison.get("captured_at"),
    )

def _flagged_committees() -> list[dict]:
    """Committees with a balance discrepancy, worst first.

    Read from the database rather than data/aggregated/filers/*.json. Those
    files are only as fresh as the last local pull, and ranking from a stale
    copy produced a priority list whose top entry had no discrepancy at all —
    it had been fixed days earlier.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("select data from dashboard_cache where key = 'balance_discrepancies'")
            row = cur.fetchone()
            if not row:
                return []
            d = row[0]
            if isinstance(d, str):
                d = json.loads(d)
    finally:
        conn.close()
    if isinstance(d, dict):
        rows = d.get("rows")
        if rows is None:  # compatibility with the older cache envelope
            rows = d.get("filers", [])
    else:
        rows = d
    out = _expand_actionable_rows(rows or [])
    out.sort(key=lambda r: -abs(r.get("delta", 0) or 0))
    return out


def _our_counts(filer_ids: list[str], start: date, end: date) -> dict[str, int]:
    """Rows we hold per filer, over exactly the window ORESTAR is asked about.

    This counted every row for a filer regardless of date, while orestar_count
    searches 2006 → today. Any row outside that window inflated our side and so
    understated the shortfall — Citizens for Mannix holds 4 such rows and was
    recorded as complete at 343 vs 339, when inside the window it holds exactly
    the 339 ORESTAR reports.

    Small in this dataset (14 rows across 9 committees) but wrong in the
    direction that hides missing data, which is the direction that matters:
    comparing two different populations and calling the difference a finding is
    how eight earlier "discrepancies" turned out to be nothing at all.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select filer_id, count(*) from transactions "
                "where filer_id = any(%s) and tran_date between %s and %s "
                "group by filer_id",
                (list(filer_ids), str(start), str(end)),
            )
            return {str(k): int(v) for k, v in cur.fetchall()}
    finally:
        conn.close()


def _load_survey() -> dict:
    if SURVEY_PATH.exists():
        try:
            return {str(e["filer_id"]): e for e in json.loads(SURVEY_PATH.read_text())}
        except Exception:
            return {}
    return {}


def _save_survey(surveyed: dict) -> None:
    """Written after every committee, not at the end.

    A run that is cut off by F5 mid-survey must keep what it learned; the whole
    point is that progress accumulates across runs.
    """
    SURVEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(surveyed.values(), key=lambda e: -(e.get("missing") or 0))
    SURVEY_PATH.write_text(json.dumps(rows, indent=1))


# ---------------------------------------------------------------------------
# The one query
# ---------------------------------------------------------------------------

class SearchDeadlineExceeded(RuntimeError):
    """The caller's absolute search deadline elapsed."""


def _timeout_ms(deadline: float | None, ceiling_ms: int) -> int:
    """A Playwright timeout that cannot extend past ``deadline``."""
    if deadline is None:
        return ceiling_ms
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise SearchDeadlineExceeded("ORESTAR search deadline reached")
    return min(ceiling_ms, remaining)


def _return_to_form(page, deadline: float | None = None) -> None:
    """Back to the search form, without the flat seven-second sleep.

    fetch.py's _return_to_search clicks Reset when it is already on the form,
    but a survey is always standing on a results page, so it took the slow path
    every single time: a full navigation plus PAGE_RENDER_WAIT = 7 seconds,
    whether or not the form was ready sooner.

    Polling for the form's CSRF field is both faster and more correct. The
    sleep existed because F5's JavaScript challenge needs time to run, and
    waiting for the element it produces handles that properly: if the challenge
    is still going the field is not there yet and we keep waiting, and if the
    page is ready early we proceed immediately.
    """
    if "gotoPublicTransactionSearch.do" not in page.url:
        page.goto(
            F.SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=_timeout_ms(deadline, 60_000),
        )
    if "secure.sos.state.or.us/orestar" not in page.url:
        raise F.SessionExpiredError(f"Session expired — redirected to {page.url}")
    # state="attached", NOT the default "visible".
    #
    # OWASP_CSRFTOKEN is <input type="hidden">, so it is never visible and the
    # default state can never be satisfied. The first version of this waited on
    # the default, timed out after 30 seconds on a perfectly healthy page, and
    # reported the timeout as "F5 challenge or session expiry" — inventing a
    # rate limit that was not happening and surveying zero committees where the
    # slow path had managed 96. _load_search_form gets this right by using
    # .count(), which ignores visibility.
    try:
        page.wait_for_selector(
            'input[name="OWASP_CSRFTOKEN"]',
            state="attached",
            timeout=_timeout_ms(deadline, 30_000),
        )
    except PlaywrightTimeout:
        if deadline is not None and time.monotonic() >= deadline - 0.05:
            raise SearchDeadlineExceeded("ORESTAR search deadline reached")
        raise F.SessionExpiredError(
            "Search form never rendered — F5 challenge or session expiry"
        )


def _count_failure_page_diagnostics(url: str, body: str) -> dict:
    """Return only safe page metadata and recognized text signals, not a verdict.

    Body text can omit a script-only challenge. False signals therefore do not
    establish a healthy page, and none of these flags changes count semantics.
    """
    host, path = "unknown", "unknown"
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname)):
            raise ValueError("Invalid HTTP authority")
        # Validate the authority before using any path: malformed URLs can put
        # credentials where a parser expects a relative path.
        _ = parsed.port
        segments = []
        for raw_segment in parsed.path.split("/"):
            segment = unquote(raw_segment)
            # Strip literal and nested percent-encoded delimiters within each
            # original segment, before encoded slashes can expose session data
            # as a new segment. Ambiguous residual encodings are redacted below.
            segment = re.split(
                r"[;?#]|%(?:25)*(?:3b|3f|23)", segment, maxsplit=1, flags=re.I,
            )[0]
            segments.append(segment)
        safe_path = "/".join(segments)
        host = hostname.lower()
        if re.fullmatch(r"[A-Za-z0-9._~/-]*", safe_path):
            path = safe_path
    except (TypeError, ValueError):
        pass
    lowered = " ".join(body.lower().split())
    return {
        "url_host": host,
        "url_path": path,
        "body_length": len(body),
        "challenge_signal": any(marker in lowered for marker in (
            "checking your browser", "verify you are human",
            "please enable javascript to view the page content",
            "the requested url was rejected", "request rejected",
        )),
        "maintenance_signal": any(marker in lowered for marker in (
            "scheduled maintenance", "undergoing maintenance",
            "down for maintenance", "system maintenance",
        )),
        "server_error_signal": any(marker in lowered for marker in (
            "internal server error", "service unavailable", "bad gateway",
            "gateway timeout",
        )) or bool(re.search(
            r"\b(?:http(?: status)?|status code)\s*[:=-]?\s*(?:500|502|503|504)\b",
            lowered,
        )),
    }


def orestar_count(
    page,
    filer_id: str,
    start: date,
    end: date,
    tran_type: str = "ALL",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
    deadline: float | None = None,
) -> int | None:
    """How many records ORESTAR holds for one precisely filtered window.

    When an absolute deadline is supplied, any Playwright operation whose
    clipped timeout reaches that boundary is translated into the explicit
    deadline signal expected by the identity-diff runner.
    """
    try:
        return _orestar_count(
            page,
            filer_id,
            start,
            end,
            tran_type,
            amt_from,
            amt_to,
            payee_prefix,
            deadline,
        )
    except PlaywrightTimeout as exc:
        # Integer millisecond timeouts can fire just before monotonic reaches
        # the exact float deadline.  A small tolerance prevents that rounding
        # edge from escaping as an unhandled Playwright exception.
        if deadline is not None and time.monotonic() >= deadline - 0.05:
            raise SearchDeadlineExceeded("ORESTAR search deadline reached") from exc
        raise


def _orestar_count(
    page,
    filer_id: str,
    start: date,
    end: date,
    tran_type: str = "ALL",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
    deadline: float | None = None,
) -> int | None:
    """How many records ORESTAR holds for one precisely filtered window.

    Returns None if the count could not be read, which the caller must treat as
    "unknown" — recording it as 0 would mark a committee complete on the
    strength of a parse failure.

    The survey uses only filer and date.  The identity diff also uses type,
    amount and contributor prefix when a single day exceeds ORESTAR's display
    cap.  Keeping the form submission here gives both callers one definition of
    a search window instead of two copies that can drift.
    """
    budget = SearchBudget.from_environment()
    if budget is not None:
        budget.require_capacity(1)
    _return_to_form(page, deadline)
    page.fill(
        'input[name="cneSearchFilerCommitteeId"]',
        str(filer_id),
        timeout=_timeout_ms(deadline, 30_000),
    )
    page.wait_for_timeout(250)
    if tran_type != "ALL":
        page.select_option(
            'select[name="cneSearchTranType"]',
            tran_type,
            timeout=_timeout_ms(deadline, 30_000),
        )
        page.wait_for_timeout(400)
    page.fill(
        'input[name="cneSearchTranStartDate"]',
        start.strftime("%m/%d/%Y"),
        timeout=_timeout_ms(deadline, 30_000),
    )
    page.fill(
        'input[name="cneSearchTranEndDate"]',
        end.strftime("%m/%d/%Y"),
        timeout=_timeout_ms(deadline, 30_000),
    )
    if amt_from is not None:
        page.fill(
            'input[name="cneSearchTranAmountFrom"]',
            str(amt_from),
            timeout=_timeout_ms(deadline, 30_000),
        )
    if amt_to is not None:
        page.fill(
            'input[name="cneSearchTranAmountTo"]',
            str(amt_to),
            timeout=_timeout_ms(deadline, 30_000),
        )
    if payee_prefix:
        page.fill(
            'input[name="cneSearchContributorTxt"]',
            payee_prefix,
            timeout=_timeout_ms(deadline, 30_000),
        )
        page.select_option(
            'select[name="cneSearchContributorTxtSearchType"]',
            "S",
            timeout=_timeout_ms(deadline, 30_000),
        )
    # An atomic workflow shares this ledger across collector subprocesses.
    # Reserve durably before the submission: a timeout may still reach the
    # server, and neither a fresh browser nor a later pass resets the count.
    if budget is not None:
        budget.consume(str(filer_id), {
            "tran_type": tran_type, "start": start.isoformat(), "end": end.isoformat(),
            "amt_from": amt_from, "amt_to": amt_to, "payee_prefix": payee_prefix,
        })
    timeout = _timeout_ms(deadline, 30_000)
    counter = _submission_counter.get()
    if counter is not None:
        # Count at the submission boundary, including a click that times out
        # after possibly reaching ORESTAR. Polls and budget refusals add none.
        counter.count += 1
    page.click('input[name="search"]', timeout=timeout)
    try:
        page.wait_for_url(
            F.RESULTS_URL_PATTERN,
            timeout=_timeout_ms(deadline, 30_000),
        )
    except PlaywrightTimeout:
        if deadline is not None and time.monotonic() >= deadline - 0.05:
            raise SearchDeadlineExceeded("ORESTAR search deadline reached")
        log.warning("Timed out reading count for filer %s", filer_id)
        return None

    if "secure.sos.state.or.us/orestar" not in page.url:
        raise F.SessionExpiredError(f"Session expired — redirected to {page.url}")

    # Poll for the count instead of reading the body once.
    #
    # wait_for_url resolves the moment the URL changes, which is before the
    # results have rendered. Reading inner_text right then returns a page that
    # has no count in it yet, and this reported "no count read" for committees
    # that were perfectly fine — filer 24173 answers "104 records found" in a
    # browser and was skipped anyway.
    #
    # The flat PAGE_RENDER_WAIT this replaced was hiding the race by sleeping
    # through it. Waiting for the content itself is the fix; it also returns as
    # soon as the page is ready rather than always paying the full delay.
    poll_started = time.monotonic()
    poll_deadline = poll_started + 20
    if deadline is not None:
        poll_deadline = min(poll_deadline, deadline)
    text = ""
    while time.monotonic() < poll_deadline:
        text = page.inner_text(
            "body", timeout=_timeout_ms(deadline, 30_000)
        )
        m = re.search(r"([\d,]+)\s+records found", text)
        if m:
            return int(m.group(1).replace(",", ""))
        # "No records found" is a real answer, not a failure.
        if "no records found" in text.lower():
            return 0
        page.wait_for_timeout(500)
    if deadline is not None and time.monotonic() >= deadline:
        raise SearchDeadlineExceeded("ORESTAR search deadline reached")
    # Reuse the final poll body. Diagnostics must never issue another search or
    # DOM read, extend a deadline, or turn an unreadable count into zero.
    poll_elapsed = time.monotonic() - poll_started
    diagnostic = _count_failure_page_diagnostics(page.url, text)
    diagnostic.update({
        "filer_id": str(filer_id),
        "window": {"tran_type": tran_type, "start": start.isoformat(),
                   "end": end.isoformat(), "amt_from": amt_from,
                   "amt_to": amt_to, "payee_prefix": payee_prefix},
        "poll_elapsed_seconds": round(poll_elapsed, 3),
    })
    log.warning("COUNT_READ_EXHAUSTED %s", json.dumps(diagnostic, sort_keys=True))
    return None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report() -> int:
    if not SURVEY_PATH.exists():
        log.error("No %s yet — run the survey first.", SURVEY_PATH)
        return 1
    rows = json.loads(SURVEY_PATH.read_text())
    short = [r for r in rows if (r.get("missing") or 0) > 0]
    over  = [r for r in rows if (r.get("surplus") or 0) > 0]
    # "clean" means the counts AGREE. A committee holding rows ORESTAR does not
    # have is not clean; it was previously counted as such because `missing` is
    # clamped at zero.
    clean = [r for r in rows
             if (r.get("missing") or 0) == 0 and (r.get("surplus") or 0) == 0]
    d_short = sum(abs(r.get("delta") or 0) for r in short)
    d_clean = sum(abs(r.get("delta") or 0) for r in clean)

    print()
    print(f"  committees surveyed          : {len(rows):,}")
    print(f"  SURPLUS ROWS (we hold extra) : {len(over):,}   "
          f"{sum(r.get('surplus') or 0 for r in over):,} rows")
    print(f"  MISSING ROWS (backfill helps): {len(short):,}   ${d_short:,.0f} of delta")
    print(f"  complete (backfill cannot)   : {len(clean):,}   ${d_clean:,.0f} of delta")
    print(f"  rows missing in total        : {sum(r['missing'] for r in short):,}")
    if short:
        print()
        print(f"    {'filer':>7} {'ORESTAR':>9} {'held':>9} {'missing':>9} {'delta':>12}  name")
        for r in short[:25]:
            print(f"    {r['filer_id']:>7} {r['orestar']:>9,} {r['held']:>9,} "
                  f"{r['missing']:>9,} {(r.get('delta') or 0):>12,.0f}  {r['name'][:34]}")
    if clean:
        print()
        print("  largest deltas with NOTHING to fetch (a different problem):")
        for r in sorted(clean, key=lambda e: -abs(e.get("delta") or 0))[:10]:
            print(f"    {r['filer_id']:>7} {r['orestar']:>9,} held {r['held']:>9,} "
                  f"delta {(r.get('delta') or 0):>12,.0f}  {r['name'][:34]}")
    print()
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filer-ids", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many committees (0 = until blocked)")
    ap.add_argument("--max-minutes", type=int, default=70,
                    help="stop after this long, so the job's own timeout never "
                         "arrives mid-run (0 = no budget)")
    ap.add_argument("--report", action="store_true", help="print findings, fetch nothing")
    ap.add_argument("--recheck", action="store_true",
                    help="re-survey committees already recorded")
    ap.add_argument("--drop", nargs="*", metavar="FILER_ID",
                    help="forget these committees' recorded results so they are "
                         "surveyed again; with no ids, drops every committee "
                         "holding rows outside the surveyed window, whose "
                         "counts predate the date-bounded comparison")
    args = ap.parse_args()

    if args.report:
        return report()

    if args.drop is not None:
        surveyed = _load_survey()
        if args.drop:
            targets_to_drop = {str(f) for f in args.drop}
        else:
            # Exactly the committees the old unbounded count could have got
            # wrong: those holding rows outside the surveyed window. Dropping
            # only these re-surveys 3 committees instead of all 300 — the whole
            # blast radius is 14 rows across 9 committees dataset-wide.
            conn = supabase_sync._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "select distinct filer_id from transactions "
                        "where tran_date < %s or tran_date > current_date "
                        "   or tran_date is null",
                        (f"{FIRST_YEAR}-01-01",),
                    )
                    targets_to_drop = {str(r[0]) for r in cur.fetchall()}
            finally:
                conn.close()
        gone = [f for f in targets_to_drop if f in surveyed]
        for f in gone:
            del surveyed[f]
        _save_survey(surveyed)
        log.info("Dropped %d recorded result(s) — they will be surveyed again: %s",
                 len(gone), " ".join(sorted(gone)) or "(none were recorded)")
        return 0

    flagged = _flagged_committees()
    by_id = {str(r["filer_id"]): r for r in flagged}
    if args.filer_ids:
        targets = [str(f) for f in args.filer_ids]
    else:
        targets = [str(r["filer_id"]) for r in flagged]
    if not targets:
        log.info("Nothing flagged — nothing to survey.")
        return 0

    # --recheck forgets the TARGETS, not the whole survey.
    #
    # It used to start from an empty dict, and _save_survey writes whatever
    # dict it is handed — so a recheck of 365 committees would have replaced a
    # 1,617-committee file with 365 entries and silently discarded the other
    # 1,252, each of which cost a real ORESTAR request to obtain. The flag is
    # for re-measuring a subset whose counts have gone stale; that is what it
    # does now.
    surveyed = _load_survey()
    if args.recheck:
        _forgotten = [t for t in targets if t in surveyed]
        for _t in _forgotten:
            del surveyed[_t]
        log.info("--recheck: forgetting %d recorded result(s) of the %d targeted; "
                 "%d other committees keep theirs",
                 len(_forgotten), len(targets), len(surveyed))
    if args.filer_ids:
        todo = [t for t in targets if t not in surveyed]
    else:
        todo = [
            t for t in targets
            if not _evidence_is_current(surveyed.get(t), by_id.get(t))
        ]
    log.info("%d committees flagged, %d already surveyed, %d to go",
             len(targets), len(targets) - len(todo), len(todo))
    if not todo:
        log.info("Survey complete for every flagged committee.")
        return report()

    today = date.today()
    held = _our_counts(todo, date(FIRST_YEAR, 1, 1), today)
    done_this_run = 0

    # Stop on our own terms, before the job's timeout stops us.
    #
    # A GitHub job timeout is reported as a cancellation, which skipped the
    # commit step and discarded 90 minutes and 96 committees of measured
    # progress. Finishing early and deliberately means the commit and the
    # chain both run normally.
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None

    unreadable = 0
    with sync_playwright() as p:
        browser, context, page = F.setup_browser(p)
        consecutive_failures = 0
        for fid in todo:
            if args.limit and done_this_run >= args.limit:
                log.info("Reached --limit %d for this run.", args.limit)
                break
            if deadline and time.monotonic() >= deadline:
                log.info("Reached the %d-minute budget after %d committees — "
                         "stopping so this run's progress is committed.",
                         args.max_minutes, done_this_run)
                break
            meta = by_id.get(fid, {})
            try:
                n = orestar_count(page, fid, date(FIRST_YEAR, 1, 1), today)
                # Reset only on a real read. Resetting here unconditionally
                # counted an unreadable page as a success: the counter went to
                # zero and straight back to one on every iteration, so the
                # give-up threshold below could never be reached.
                if n is not None:
                    consecutive_failures = 0
            except F.SessionExpiredError as exc:
                consecutive_failures += 1
                log.warning("Blocked at filer %s (%d/2): %s — restarting browser",
                            fid, consecutive_failures, exc)
                try:
                    browser.close()
                except Exception:
                    pass
                if consecutive_failures >= 2:
                    log.warning("Rate-limited — stopping. %d surveyed this run; "
                                "the next run continues from here.", done_this_run)
                    break
                browser, context, page = F.setup_browser(p)
                continue
            except Exception as exc:
                log.warning("Filer %s failed: %s", fid, exc)
                n = None

            if n is None:
                # Not recorded at all: an unknown count must never be filed as
                # a result, or the committee is written off on a parse failure.
                #
                # It DOES count as a failure, though. This used to `continue`
                # without touching the failure counter, so once something went
                # wrong the loop tore through every remaining committee at a
                # second each, logging 1,500 warnings and stopping for nothing.
                # A run that cannot read any page should give up and let the
                # next one try, not sprint to the end of the list.
                unreadable += 1
                consecutive_failures += 1
                log.warning("Filer %s: no count read — leaving unsurveyed", fid)
                if consecutive_failures >= 5:
                    log.warning("Five committees in a row unreadable — stopping. "
                                "%d surveyed this run.", done_this_run)
                    break
                continue

            ours = held.get(fid, 0)
            surveyed[fid] = {
                "filer_id": fid,
                "name": meta.get("name", ""),
                "orestar": n,
                "held": ours,
                "missing": max(n - ours, 0),
                # Recorded rather than discarded. `missing` is clamped at zero
                # so the backfill's sort and "how many rows to recover" stay
                # meaningful, but that clamp made a SURPLUS invisible — we hold
                # rows ORESTAR no longer returns, because withdrawn and
                # superseded filings vanish from ORESTAR and nothing in this
                # pipeline ever removes them. Plumbers & Steamfitters PAC held
                # 16 such rows worth $32,284.04 and surveyed as "missing: 0".
                "surplus": max(ours - n, 0),
                "delta": meta.get("delta"),
                "dormant": meta.get("dormant"),
                "checked": today.isoformat(),       # compatibility
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            _save_survey(surveyed)
            done_this_run += 1
            verdict = (f"MISSING {n - ours:,}" if n > ours else "complete")
            log.info("[%d] filer %s %-34s ORESTAR %7d  held %7d  %s",
                     done_this_run, fid, (meta.get("name") or "")[:34], n, ours, verdict)
            time.sleep(F.REQUEST_DELAY)
        try:
            browser.close()
        except Exception:
            pass

    log.info("Surveyed %d committees this run (%d unreadable); %d of %d recorded overall.",
             done_this_run, unreadable, len(surveyed), len(targets))
    return report()


if __name__ == "__main__":
    sys.exit(main())
