"""
fetch.py — Download campaign finance data from Oregon ORESTAR system.

ORESTAR uses F5 bot defense that requires JavaScript execution, so we use
Playwright with a real (headed) browser. In GitHub Actions, xvfb-run provides
a virtual display so headed mode works without a physical screen.

Usage:
    python fetch.py --mode=incremental          # last 14 days (default)
    python fetch.py --mode=backfill             # 2017-01-01 to today
    python fetch.py --mode=backfill --start-year=2020
    python fetch.py --mode=test --days=7        # single week, verify connectivity

Local:    python3 fetch.py --mode=test
CI:       xvfb-run --auto-servernum python fetch.py --mode=incremental
"""

import argparse
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from search_budget import SearchBudget, SearchBudgetError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL   = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/gotoPublicTransactionSearch.do"
EXPORT_URL = f"{BASE_URL}/XcelCNESearch"

RESULTS_URL_PATTERN = "**/gotoPublicTransactionSearchResults**"

# Raw Excel files land here temporarily (never committed to git)
RAW_DIR = Path(__file__).parent.parent / "data" / "_raw"

# Tracks which windows have been successfully fetched across runs (committed to git)
# Two separate logs: one per date-field mode so filed-date and tran-date searches
# don't share state (different searches, different result sets).
FETCHED_LOG     = RAW_DIR.parent / "fetched_windows.json"       # filed-date mode
FETCHED_LOG_TRN = RAW_DIR.parent / "fetched_windows_tran.json"  # tran-date mode
# Progress of the history-wide re-read of lumped "Miscellaneous" rows. Kept
# apart from the fetch logs above: those record windows as done for good, and
# a re-read exists precisely to revisit windows that are already done.
FETCHED_LOG_MISC = RAW_DIR.parent / "fetched_windows_misc.json"
# Deletion records the merge has applied (process.DELETIONS_PATH). Read here
# only to count them when deciding whether a window is already held.
DELETIONS_LOG = RAW_DIR.parent / "orestar_deletions.json"

# ORESTAR edits lumped "Miscellaneous ... $100 and under" rows in place: the
# Tran ID, status and filed date stay the same while the amount changes (Eli
# for Portland 5675002: $340 -> $830). A window is never fetched twice, so
# those edits were invisible. Re-reading just these rows by filed date, with
# ORESTAR's "Contributor/Payee starts with" filter, brings the current
# amounts in; the merge keeps the newest copy of each Tran ID.
MISC_PREFIX = "Miscellaneous"
# The types lumped rows occur in; OA has none.
MISC_TYPES = ["C", "E", "O", "OD", "OR"]
# Rolling re-read: rows filed in the last eight weeks, in four-week windows,
# about ten searches a day.
MISC_RECENT_DAYS = 56
MISC_RECENT_WINDOW_DAYS = 28
# History-wide re-read: quarter-year windows, split further if one overruns.
MISC_REREAD_WINDOW_DAYS = 91
# Stop starting windows after this long. Without a budget, a run the F5 defense
# doesn't block keeps going until the job's own timeout kills it, and a killed
# job never merges or publishes: every window it read is lost and the chain
# stops. 90 minutes fits a 150-minute job even after a full 25-minute
# coordination wait; the workflow passes its own figure.
MISC_REREAD_MAX_MINUTES = 90.0

# Identity remediation deliberately ignores the ordinary count/held-row skips:
# a withdrawn row can cancel a genuinely missing row inside the same window.
# Large filers still need to converge across rate-limited runners, though, so
# forced runs keep a separate ledger containing ONLY windows queried during the
# current remediation chain. A leaf enters the ledger only after its fresh
# export row count exactly matches ORESTAR's reported count; capped parents enter
# only as routing hints after they have been freshly measured.
IDENTITY_PROGRESS_FILE = RAW_DIR.parent / "identity_remediation_windows.json"
IDENTITY_FAILURE_FILE = RAW_DIR.parent / "identity_remediation_failures.json"

TRAN_TYPES = ["C", "E", "O", "OA", "OD", "OR"]
# C  = Contribution,          E  = Expenditure
# O  = Other,                 OA = Other Account Receivable
# OD = Other Disbursement,    OR = Other Receipt
# (OR: Return/Refund of Contribution, Refunds & Rebates, Misc Other Receipt
#  OD: Misc Other Disbursement, Refunds & Rebates)

# Time (seconds) to wait after page.goto() for F5 JS challenge + app to render
PAGE_RENDER_WAIT = 7

# Polite pause between downloads
REQUEST_DELAY = 1.5

# ORESTAR truncates Excel exports at this many data rows. A file at the cap is
# split unless the freshly rendered result count proves 4,999 is the complete
# result rather than a truncated one.
ORESTAR_ROW_CAP = 4999

# {(type, start, end, amt_from, amt_to, payee_prefix): true_record_count}
# Filled from each results page and flushed to disk by _flush_record_counts.
RECORD_COUNTS: dict = {}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Magic bytes for file type detection
_XLS_MAGIC  = b"\xd0\xcf\x11\xe0"   # OLE2 Compound Document (old .xls)
_XLSX_MAGIC = b"PK\x03\x04"         # ZIP archive (new .xlsx)


class SessionExpiredError(Exception):
    """Raised when the ORESTAR session has expired and the browser was redirected away."""
    pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def setup_browser(playwright):
    """
    Launch a headed Chromium browser and load the ORESTAR search form.
    headed=True is required — F5 bot defense blocks headless browsers.
    In GitHub Actions, use:  xvfb-run --auto-servernum python fetch.py ...
    """
    log.info("Launching browser (headed mode)...")
    browser = playwright.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
    )
    try:
        context = browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=True,
            no_viewport=True,
        )
        page = context.new_page()
        _load_search_form(page)
        return browser, context, page
    except Exception:
        # setup_browser_retrying may immediately try again. Do not leave a
        # headed Chromium process behind for every failed setup attempt.
        browser.close()
        raise


def _load_search_form(page) -> None:
    """Navigate to ORESTAR search form and wait for JS to render it."""
    log.info("Loading ORESTAR search form...")
    page.goto(SEARCH_URL, timeout=60_000)
    time.sleep(PAGE_RENDER_WAIT)

    # Session expiry redirects to sos.oregon.gov — detect and raise
    if "secure.sos.state.or.us/orestar" not in page.url:
        raise SessionExpiredError(
            f"Session expired — redirected to {page.url}"
        )

    if page.locator('input[name="OWASP_CSRFTOKEN"]').count() == 0:
        raise RuntimeError(
            "ORESTAR search form did not load. "
            "F5 may have blocked the browser, or the site is down."
        )
    log.info("Search form ready.")


def _validate_download(path: Path) -> int:
    """
    Validate that a downloaded file is a real Excel file with data rows.

    Returns the number of data rows (excluding header), or -1 if the file is
    invalid (HTML error page, corrupted, empty, etc.).
    """
    if not path.exists() or path.stat().st_size == 0:
        return -1

    header = path.read_bytes()[:8]

    # HTML error page (F5 bot defense, session expired, etc.)
    if header[:5].lower() in (b"<!doc", b"<html", b"<head", b"<?xml"):
        return -1

    try:
        if header[:4] == _XLS_MAGIC:
            import xlrd as _xlrd
            _wb = _xlrd.open_workbook(str(path), on_demand=True)
            nrows = _wb.sheet_by_index(0).nrows
            _wb.release_resources()
            return max(nrows - 1, 0)  # subtract header row
        elif header[:4] == _XLSX_MAGIC:
            wb = load_workbook(path, read_only=True)
            count = 0
            for _ in wb.active.rows:
                count += 1
                if count > ORESTAR_ROW_CAP + 1:
                    break
            wb.close()
            return max(count - 1, 0)
        else:
            # Unknown format — treat as invalid
            return -1
    except Exception:
        return -1


def _include_deleted(page) -> None:
    """Tick "view deleted transactions" so the export carries deletion records.

    Deleting a transaction files a new record (status "Deleted") whose
    Original Id names the deleted row. ORESTAR's default search hides it, so a
    row deleted after its window was fetched stayed counted here forever. The
    expired original itself stays out (expired transactions are not asked for):
    the export is ORESTAR's live rows plus its deletion records, and the merge
    turns each deletion record into the removal of the row it names.
    """
    try:
        page.check('input[name="viewDeletedTransactions"]', timeout=5_000)
    except Exception as exc:          # never fail a fetch over the option
        log.warning("Could not ask ORESTAR for deletion records: %s", exc)


def _return_to_search(page) -> None:
    """
    Reset the form for the next search.
    Clicks the Reset button if already on the search form (fast),
    otherwise does a full page reload (slow, only needed after errors).
    """
    if "gotoPublicTransactionSearch.do" in page.url:
        # Already on search form — just click Reset to clear fields
        reset = page.locator('input[type="button"][value="Reset"]')
        if reset.count() > 0:
            reset.first.click()
            page.wait_for_timeout(300)
            return
    # Not on search form (e.g. after a results page or error) — full reload
    _load_search_form(page)


def _read_results_count(page, timeout_seconds: int = 20) -> int | None:
    """Poll the current results page until its record count has rendered."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        text = page.inner_text("body", timeout=30_000)
        match = re.search(r"([\d,]+)\s+records found", text)
        if match:
            return int(match.group(1).replace(",", ""))
        if "no records found" in text.lower():
            return 0
        page.wait_for_timeout(250)
    return None


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------

def download_week(
    page,
    context,
    start: date,
    end: date,
    tran_type: str,
    raw_dir: Path,
    date_field: str = "filed",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
) -> Path | None:
    """
    Fill the ORESTAR search form for one week, submit, then download
    the Excel export.

    If tran_type is "ALL", no type filter is applied (downloads all types
    in one request). Otherwise, filters to the specific transaction type.

    Returns the path to the saved .xlsx file, or None on failure.
    """
    budget = SearchBudget.from_environment()
    # The narrowing dimensions belong in the filename, or two different
    # sub-windows of the same day would collide on disk and the second would be
    # skipped as "already downloaded".
    _suffix = ""
    if amt_from is not None or amt_to is not None:
        _suffix += f"_amt{amt_from or ''}-{amt_to or ''}"
    if payee_prefix:
        _suffix += f"_p{payee_prefix}"
    filename = raw_dir / f"{tran_type}_{start.isoformat()}_{end.isoformat()}{_suffix}.xlsx"
    if filename.exists():
        log.debug("Already downloaded: %s", filename.name)
        return filename

    if budget is not None:
        budget.require_capacity(1)
    try:
        # Ensure we're on the search form
        _return_to_search(page)

        # ── Fill the form ────────────────────────────────────────────────────
        # Transaction type (skip for ALL — leave default which returns everything)
        if tran_type != "ALL":
            page.select_option('select[name="cneSearchTranType"]', tran_type)
            page.wait_for_timeout(600)  # brief wait for any dynamic field updates

        # Date range (MM/DD/YYYY) — filed date or transaction date depending on mode
        if date_field == "tran":
            page.fill('input[name="cneSearchTranStartDate"]',     start.strftime("%m/%d/%Y"))
            page.fill('input[name="cneSearchTranEndDate"]',        end.strftime("%m/%d/%Y"))
        else:
            page.fill('input[name="cneSearchTranFiledStartDate"]', start.strftime("%m/%d/%Y"))
            page.fill('input[name="cneSearchTranFiledEndDate"]',   end.strftime("%m/%d/%Y"))
        _include_deleted(page)

        # Narrowing filters, used only when a window has already been split as
        # far as date and type allow.
        if amt_from is not None:
            page.fill('input[name="cneSearchTranAmountFrom"]', str(amt_from))
        if amt_to is not None:
            page.fill('input[name="cneSearchTranAmountTo"]', str(amt_to))
        if payee_prefix:
            page.fill('input[name="cneSearchContributorTxt"]', payee_prefix)
            page.select_option('select[name="cneSearchContributorTxtSearchType"]', "S")

        # ── Submit search ─────────────────────────────────────────────────────
        if budget is not None:
            budget.consume("ALL", {
                "collector": "fetch", "date_field": date_field,
                "start": start.isoformat(), "end": end.isoformat(),
                "tran_type": tran_type, "amt_from": amt_from,
                "amt_to": amt_to, "payee_prefix": payee_prefix,
            })
        page.click('input[name="search"]')
        try:
            page.wait_for_url(RESULTS_URL_PATTERN, timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timed out waiting for results: %s %s→%s", tran_type, start, end)
            _return_to_search(page)
            return None

        # ── Extract session info from results page ────────────────────────────
        results_url = page.url

        # ORESTAR prints the true number of matching records here, and that
        # number is NOT capped — the page says "58366 records found ... A
        # maximum 5000 records are displayed". It is the only source of truth
        # for how many rows a window should yield, so capture it while we are
        # standing on the page. RECORD_COUNTS is what the completeness
        # verifier compares the downloaded rows against.
        try:
            _txt = page.inner_text("body")
            _m = re.search(r"([\d,]+)\s+records found", _txt)
            if _m:
                RECORD_COUNTS[(tran_type, str(start), str(end),
                               str(amt_from), str(amt_to), str(payee_prefix))] = \
                    int(_m.group(1).replace(",", ""))
        except Exception:
            pass                      # a missing count must never fail the fetch

        # CSRF token from the Export link on the results page
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")

        if not csrf:
            log.warning("No CSRF token on results page for %s %s→%s", tran_type, start, end)
            _return_to_search(page)
            return None

        # JSESSIONID is embedded in the URL path (not a cookie) — must be included
        session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
        session_path  = f";{session_match.group(1)}" if session_match else ""

        # ── Download via requests (faster than browser download) ──────────────
        raw_dir.mkdir(parents=True, exist_ok=True)
        cookies = {c["name"]: c["value"] for c in context.cookies()}

        export_url = f"{EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"
        resp = requests.get(
            export_url,
            cookies=cookies,
            headers={"User-Agent": USER_AGENT, "Referer": results_url},
            timeout=120,
        )

        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower():
            # Fallback: navigate to the export URL directly in the browser
            log.debug("requests returned HTML; falling back to browser download...")
            with page.expect_download(timeout=60_000) as dl_info:
                page.goto(
                    f"{EXPORT_URL}?OWASP_CSRFTOKEN={csrf}",
                    wait_until="commit",
                    timeout=60_000,
                )
            dl = dl_info.value
            dl.save_as(filename)
            log.info(
                "Saved (browser) %s  (%s → %s, %d bytes)",
                filename.name, start, end, filename.stat().st_size,
            )
        else:
            filename.write_bytes(resp.content)
            log.info(
                "Saved (requests) %s  (%s → %s, %d bytes)",
                filename.name, start, end, len(resp.content),
            )

        # ── Validate the downloaded file ──────────────────────────────────
        row_count = _validate_download(filename)
        if row_count < 0:
            log.warning(
                "Invalid download for %s %s→%s — file is not valid Excel "
                "(HTML error page, corrupted, or empty). Deleting and will retry.",
                tran_type, start, end,
            )
            filename.unlink(missing_ok=True)
            _return_to_search(page)
            return None

        if row_count == 0:
            log.info(
                "Empty result for %s %s→%s (0 data rows) — valid but no transactions",
                tran_type, start, end,
            )

        # Return to search form for next iteration
        _return_to_search(page)
        time.sleep(REQUEST_DELAY)
        return filename

    except SearchBudgetError:
        raise  # A refusal cannot become a session retry or partial success.
    except SessionExpiredError:
        raise  # propagate up to _fetch_range for browser restart
    except Exception as exc:
        # If the page redirected off ORESTAR during a form interaction, the session
        # is broken — raise SessionExpiredError so _fetch_range can restart the browser.
        # (goto(SEARCH_URL) can succeed with an expired session, so we can't rely solely
        # on the _load_search_form URL check — we must catch it here too.)
        if "secure.sos.state.or.us/orestar" not in page.url:
            raise SessionExpiredError(
                f"Session expired during interaction — redirected to {page.url}"
            ) from exc
        log.warning("Failed %s %s→%s: %s", tran_type, start, end, exc)
        try:
            _return_to_search(page)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Mode drivers
# ---------------------------------------------------------------------------

# Amount bands used when a window cannot be split by date any further. Chosen
# to match where Oregon contributions actually cluster — small recurring payroll
# deductions dominate, so the low end needs fine bands and the top can be one
# open bucket.
AMOUNT_BANDS = [
    (None, "9.99"), ("10", "14.99"), ("15", "15.99"), ("16", "19.99"),
    ("20", "24.99"), ("25", "49.99"), ("50", "99.99"), ("100", "249.99"),
    ("250", "999.99"), ("1000", "4999.99"), ("5000", None),
]

# Contributor-name prefixes, the last resort. A payroll batch is thousands of
# rows sharing ONE filer and ONE amount — 4,969 rows of exactly $17.50 on
# 2023-03-02, all from a single committee — so neither amount nor filer can
# partition it. The contributors are all different (4,957 of them), which makes
# their names the only dimension that splits such a batch.
PAYEE_PREFIXES = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + [str(d) for d in range(10)]
    # Names really do start with punctuation — 92 rows in this dataset begin
    # with one of these. A-Z0-9 alone would drop them from any narrowed window,
    # which would be a fresh silent gap of exactly the kind this code exists to
    # close.
    #
    # This list is drawn from characters actually observed in the data, so a
    # first character nobody has seen yet would still be dropped. Forced
    # identity remediation catches that case by reconciling every set of child
    # counts against the parent's uncapped count. Ordinary backfill remains
    # thorough rather than provably complete at this tier.
    + ["(", '"', "[", "'", ".", "-", "@", "?"]
)


def _narrow_filer(tran_type, start, end, amt_from, amt_to, payee_prefix):
    """Next set of sub-queries for a capped FILER window.

    The filer path had no cascade at all. It asked for one filer, one year, all
    transaction types, and when that overran the cap it halved the date range —
    and nothing else. At a single day it logged "cannot split further" and kept
    the truncated 4,999 rows, silently.

    That is where the missing data came from. Local 48 Electricians 2023 holds
    18,968 rows in ORESTAR and 9,937 here; the shortfall is the cap, hit
    repeatedly by a union-dues payroll batch that no date split can break up
    because thousands of rows share one day, one filer and one amount.

    The date-range path already solved this. It narrows type → date → amount →
    contributor prefix, and that cascade is what got a 4,969-row single-amount
    batch down to fetchable pieces. This gives the filer path the same ladder,
    starting one rung higher: an un-typed window splits by type first, which is
    both the cheapest split and the one ORESTAR answers most reliably.

    Order: type → date → amount → contributor. Returns [] only when every
    dimension is spent, which is the honest answer.
    """
    # Tier 1 — split all-types into the six real types.
    if tran_type == "ALL":
        return [(t, start, end, amt_from, amt_to, payee_prefix) for t in TRAN_TYPES]

    # Tier 2 — halve the window. Narrowing dimensions carry through both halves.
    span_days = (end - start).days
    if span_days > 0:
        mid = start + timedelta(days=span_days // 2)
        return [
            (tran_type, start, mid, amt_from, amt_to, payee_prefix),
            (tran_type, mid + timedelta(days=1), end, amt_from, amt_to, payee_prefix),
        ]

    # Tiers 3 and 4 — a single day, single type. Same rungs the date-range path
    # uses, reached through the same helper so the two can never drift apart.
    return _narrow_further(tran_type, start, amt_from, amt_to, payee_prefix)


def _narrow_further(tran_type, day, amt_from, amt_to, payee_prefix):
    """Next set of sub-queries for a single-day window that still hits the cap.

    Returns [] when every dimension is spent, which is the honest answer — the
    caller then records the window as incomplete rather than pretending.

    Tiers, in order:
      1. amount bands      — splits a mixed day into ranges
      2. contributor prefix — splits a single-amount batch, the only thing that can

    Filer is not a tier. Every capped cluster measured on this data has exactly
    one filer (4,969 rows of $17.50 on 2023-03-02, all one committee), so a
    filer split would return the same oversized set.
    """
    if amt_from is None and amt_to is None:
        return [(tran_type, day, day, af, at, None) for af, at in AMOUNT_BANDS]
    if not payee_prefix:
        return [(tran_type, day, day, amt_from, amt_to, pre) for pre in PAYEE_PREFIXES]
    return []


def _task_key(task):
    """Log key for a task.

    Un-narrowed windows keep the original 3-part key so the existing fetch log
    (8,000+ entries) stays valid; narrowed sub-windows get a longer key.
    """
    tran_type, ws, we, af, at, pp = task
    if af is None and at is None and pp is None:
        return (tran_type, str(ws), str(we))
    return (tran_type, str(ws), str(we), str(af), str(at), str(pp))


def _narrowing_label(af, at, pp) -> str:
    bits = []
    if af is not None or at is not None:
        bits.append(f"${af or '0'}-{at or 'max'}")
    if pp:
        bits.append(f"payee~{pp}*")
    return ("  [" + ", ".join(bits) + "]") if bits else ""


def _flush_record_counts() -> None:
    """Persist captured record counts next to the fetch log.

    Kept as a plain list of rows rather than a nested structure so
    verify_completeness.py can read it without knowing how windows were split.
    """
    if not RECORD_COUNTS:
        return
    path = RAW_DIR.parent / "record_counts.json"
    try:
        existing = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        existing = []
    # These are observations, not immutable facts. A force-refresh must be
    # able to replace a stale count for the same window.
    merged = {tuple(e["key"]): int(e["reported"]) for e in existing}
    merged.update({tuple(key): int(n) for key, n in RECORD_COUNTS.items()})
    existing = [
        {"key": list(key), "reported": n}
        for key, n in sorted(merged.items(), key=lambda item: tuple(map(str, item[0])))
    ]
    path.write_text(json.dumps(existing, indent=1))
    log.info("Recorded ORESTAR's own counts for %d windows -> %s",
             len(RECORD_COUNTS), path.name)


def _save_identity_progress(progress: dict[tuple, int]) -> None:
    """Persist the resumable forced-remediation window ledger."""
    rows = [
        {"key": list(key), "reported": int(count)}
        for key, count in sorted(
            progress.items(), key=lambda item: tuple(map(str, item[0]))
        )
    ]
    IDENTITY_PROGRESS_FILE.write_text(json.dumps(rows, indent=1) + "\n")


def _save_identity_failures(failures: dict[tuple, int]) -> None:
    """Persist forced partition failures so one bad split cannot loop forever."""
    rows = [
        {"key": list(key), "failures": int(count)}
        for key, count in sorted(
            failures.items(), key=lambda item: tuple(map(str, item[0]))
        )
    ]
    IDENTITY_FAILURE_FILE.write_text(json.dumps(rows, indent=1) + "\n")


def _identity_progress(*, reset_filers=()) -> dict[tuple, int]:
    """Load forced-remediation progress, optionally resetting named filers."""
    try:
        rows = json.loads(IDENTITY_PROGRESS_FILE.read_text()) \
            if IDENTITY_PROGRESS_FILE.exists() else []
    except Exception:
        rows = []
    progress = {
        tuple(row["key"]): int(row["reported"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("key"), list)
        and len(row["key"]) == 7
    }
    reset = {str(fid) for fid in reset_filers}
    if reset:
        progress = {
            key: count for key, count in progress.items()
            if str(key[-1]) not in reset
        }
        _save_identity_progress(progress)
    return progress


def _identity_failures(*, reset_filers=()) -> dict[tuple, int]:
    """Load per-partition reconciliation failures, optionally resetting filers."""
    try:
        rows = json.loads(IDENTITY_FAILURE_FILE.read_text()) \
            if IDENTITY_FAILURE_FILE.exists() else []
    except Exception:
        rows = []
    failures = {
        tuple(row["key"]): int(row["failures"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("key"), list)
        and len(row["key"]) == 7
    }
    reset = {str(fid) for fid in reset_filers}
    if reset:
        failures = {
            key: count for key, count in failures.items()
            if str(key[-1]) not in reset
        }
        _save_identity_failures(failures)
    return failures


def clear_identity_progress(filer_ids) -> None:
    """Forget completed remediation chains for the named filer IDs."""
    _identity_progress(reset_filers=filer_ids)
    _identity_failures(reset_filers=filer_ids)
    log.info("Cleared identity-remediation progress for %d filer(s)",
             len({str(fid) for fid in filer_ids}))


def _quarantine_identity_cache(filer_ids) -> None:
    """Move every requested filer's old top-level Excel outside the merge glob."""
    stale_dir = RAW_DIR / ".identity_stale"
    stale_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for fid in {str(value) for value in filer_ids}:
        for pattern in (f"filer{fid}_*.xls*", f"verified_filer{fid}_*.xls*"):
            for path in RAW_DIR.glob(pattern):
                destination = stale_dir / path.name
                destination.unlink(missing_ok=True)
                path.replace(destination)
                moved += 1
    if moved:
        log.info("Quarantined %d stale filer exports before forced refresh", moved)


def _record_truncated(tran_type: str, day: date, rows: int) -> None:
    """Note a window that came back at the cap and cannot be split any further.

    Written to disk rather than only logged, so an incomplete window is
    discoverable after the run ends instead of living in a CI log nobody
    re-reads. The completeness audit reads this file.
    """
    path = RAW_DIR.parent / "truncated_windows.json"
    try:
        existing = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        existing = []
    entry = {"tran_type": tran_type, "date": str(day), "rows": rows,
             "noticed": datetime.now().isoformat(timespec="seconds")}
    if not any(e.get("tran_type") == tran_type and e.get("date") == str(day) for e in existing):
        existing.append(entry)
        path.write_text(json.dumps(existing, indent=1))


def week_windows(start: date, end: date, days: int = 7):
    """Yield (window_start, window_end) chunks of ``days`` covering [start, end]."""
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=days - 1), end)
        cur += timedelta(days=days)


def _load_fetched(log_file: Path = FETCHED_LOG) -> set:
    """Load the set of already-fetched (tran_type, start, end) keys from disk."""
    if log_file.exists():
        try:
            with open(log_file) as f:
                return {tuple(x) for x in json.load(f)}
        except Exception:
            return set()
    return set()


def _save_fetched(fetched: set, log_file: Path | None = FETCHED_LOG) -> None:
    """Persist the fetched-windows set to disk immediately (no-op without a log)."""
    if log_file is None:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        json.dump(sorted([list(x) for x in fetched]), f, indent=2)


_DEFAULT_LOG = object()


def _range_tasks(start: date, end: date, types, window_days: int = 7,
                 payee_prefix: str | None = None) -> list[tuple]:
    """Every (type, window) task for a range. The fetcher and every
    remaining-count must build tasks here, or a chain can never finish."""
    return [(t, ws, we, None, None, payee_prefix)
            for ws, we in week_windows(start, end, window_days) for t in types]


def _fetch_range(
    start: date,
    end: date,
    date_field: str = "filed",
    *,
    payee_prefix: str | None = None,
    types: list[str] | None = None,
    window_days: int = 7,
    log_file=_DEFAULT_LOG,
    deadline: float | None = None,
) -> None:
    """Fetch every window of [start, end], one request per transaction type.

    ``payee_prefix`` restricts every request to contributors/payees starting
    with it (the lumped-row re-read). ``log_file=None`` re-fetches every
    window and records nothing, for the rolling re-read; the default is the
    permanent fetch log for ``date_field``. ``deadline`` (a
    ``time.monotonic()`` value) stops the run before its next window, leaving
    the rest for the next run.
    """
    SearchBudget.from_environment()  # Refuse malformed configuration before setup.
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    types = list(types or TRAN_TYPES)
    windows = list(week_windows(start, end, window_days))
    tasks = _range_tasks(start, end, types, window_days, payee_prefix)
    # One request PER TRANSACTION TYPE, not one request for all of them.
    #
    # An all-types request bundles every category into a single export, and a
    # busy week overruns ORESTAR's 4,999-row cap: 2016-10-02..08 holds 6,355
    # rows. Date-splitting is supposed to rescue that, and it partly does — an
    # all-types fetch of that week left us with 2,224 expenditure rows, while
    # requesting type E alone returned 2,517, matching ORESTAR's own reported
    # count exactly. An ID-level diff put all 293 absentees in the "genuinely
    # missing" column: not one was a superseded original.
    #
    # Six smaller requests are far less likely to reach the cap at all, and the
    # per-type result is the one that provably matches ORESTAR. This is also
    # what the original tran-date backfill did, which is why the fetch log's
    # older entries are per-type.
    total   = len(tasks)
    log.info(
        "Fetching %d windows (%d windows of %d days x %d types, date_field=%s%s)",
        total, len(windows), window_days, len(types), date_field,
        f", payee~{payee_prefix}*" if payee_prefix else "",
    )

    # Windows recorded here are skipped on every run — they have already been
    # fetched, processed, and committed to git in a previous run.  This is the
    # key mechanism that lets us make progress across runs despite F5 rate-limiting
    # each runner IP after ~25 requests.
    if log_file is _DEFAULT_LOG:
        log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file) if log_file is not None else set()
    skipped = sum(1 for t in tasks if _task_key(t) in fetched)
    if skipped:
        log.info("Skipping %d already-fetched windows (recorded in %s)", skipped, log_file.name)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        i = 0
        consecutive_restarts = 0
        while i < total:
            tran_type, w_start, w_end, amt_from, amt_to, payee_prefix = tasks[i]
            key = _task_key(tasks[i])

            # Skip windows already processed in a previous run
            if key in fetched:
                i += 1
                continue

            if deadline is not None and time.monotonic() >= deadline:
                log.warning(
                    "Time budget reached at [%d/%d] — stopping so this run's windows "
                    "are merged and published. The next run resumes here.",
                    i + 1, total,
                )
                break

            log.info("[%d/%d] %s  %s → %s%s", i + 1, total, tran_type, w_start, w_end,
                     _narrowing_label(amt_from, amt_to, payee_prefix))
            try:
                result = download_week(page, context, w_start, w_end, tran_type, RAW_DIR,
                                       date_field, amt_from, amt_to, payee_prefix)
                consecutive_restarts = 0

                # ── Check for ORESTAR row-cap truncation ─────────────────────
                #
                # The check used to be skipped for single-day windows, on the
                # apparent assumption that a day could not overrun the cap. It
                # can: 15 filed-dates in this dataset already exceed 4,999 rows,
                # topping out at 7,965 on 2018-10-02, and those are the counts
                # we HOLD — the true ones are higher. Splitting bottoms out at
                # one day, so those windows truncated with nothing logged.
                #
                # A single day cannot be split further by date, so detection is
                # all we can offer here — but a named, recorded gap beats a
                # silent one, and it gives the audit something to find.
                span_days = (w_end - w_start).days
                cap_hit = False
                if result is not None and result.exists():
                    row_count = _validate_download(result)
                    if row_count >= ORESTAR_ROW_CAP:
                        if span_days > 0:
                            cap_hit = True
                        else:
                            # Date is exhausted; narrow on another dimension.
                            # Order: amount, then contributor name. Filer is
                            # deliberately NOT a tier — every capped cluster
                            # measured here belongs to a single committee, so
                            # splitting on filer would put every row in one
                            # bucket and change nothing.
                            subs = _narrow_further(tran_type, w_start,
                                                   amt_from, amt_to, payee_prefix)
                            if subs:
                                ins = i + 1
                                for sub in subs:
                                    if _task_key(sub) not in fetched:
                                        tasks.insert(ins, sub); total += 1; ins += 1
                                log.warning(
                                    "Cap hit at %s %s%s — narrowing into %d sub-queries",
                                    tran_type, w_start,
                                    _narrowing_label(amt_from, amt_to, payee_prefix), len(subs),
                                )
                            else:
                                log.error(
                                    "TRUNCATED: %s %s%s returned %d rows (cap %d) and no "
                                    "dimension is left to split on — INCOMPLETE",
                                    tran_type, w_start,
                                    _narrowing_label(amt_from, amt_to, payee_prefix),
                                    row_count, ORESTAR_ROW_CAP,
                                )
                                _record_truncated(tran_type, w_start, row_count)

                if cap_hit:
                    half = span_days // 2
                    mid  = w_start + timedelta(days=half)
                    log.warning(
                        "ORESTAR cap hit for %s %s→%s — splitting at %s",
                        tran_type, w_start, w_end, mid,
                    )
                    # Six fields, like every other task. When tasks grew to
                    # carry the narrowing dimensions, this branch kept building
                    # 3-tuples, so the first cap-triggered date split crashed
                    # the fetcher on the next iteration:
                    #   ValueError: not enough values to unpack (expected 6, got 3)
                    # It broke the daily refresh outright, and the cascade test
                    # missed it because that test only exercised a single-day
                    # window, where the date branch never runs.
                    #
                    # The narrowing dimensions carry through the split: a window
                    # already narrowed by amount stays narrowed in both halves.
                    sub1 = (tran_type, w_start, mid, amt_from, amt_to, payee_prefix)
                    sub2 = (tran_type, mid + timedelta(days=1), w_end,
                            amt_from, amt_to, payee_prefix)
                    ins = i + 1
                    for sub in (sub1, sub2):
                        if _task_key(sub) not in fetched:
                            tasks.insert(ins, sub)
                            total += 1
                            ins += 1
                    # Only mark the original window as fetched if both
                    # sub-windows are already done.  This prevents data loss
                    # when the scraper stops (rate-limit) before finishing
                    # the second sub-window — the original stays "unfetched"
                    # so the next run will re-split and pick up the remainder.
                    sub1_key = _task_key(sub1)
                    sub2_key = _task_key(sub2)
                    if sub1_key in fetched and sub2_key in fetched:
                        fetched.add(key)
                        _save_fetched(fetched, log_file)
                    i += 1
                    continue

                i += 1
                if result is not None:
                    # Only mark as fetched when a file was actually saved.
                    # download_week() returns None on failure (timeout, CSRF
                    # error, etc.) — leave those windows unfetched so the next
                    # run retries them.
                    fetched.add(key)
                    _save_fetched(fetched, log_file)
                else:
                    log.warning(
                        "Download returned None for %s %s→%s — will retry next run",
                        tran_type, w_start, w_end,
                    )
            except SessionExpiredError as exc:
                consecutive_restarts += 1
                log.warning(
                    "Blocked (attempt %d/3) at [%d/%d] %s %s→%s: %s — restarting browser",
                    consecutive_restarts, i + 1, total, tran_type, w_start, w_end, exc,
                )
                try:
                    browser.close()
                except Exception:
                    pass
                if consecutive_restarts >= 3:
                    # Persistent IP-level block — stop now so process.py and the
                    # git commit still run.  Next run will skip already-fetched windows
                    # and get a fresh runner IP, so the next batch will succeed.
                    log.warning(
                        "Rate-limited after 3 attempts at [%d/%d] — stopping fetch to "
                        "preserve partial results. Re-run the workflow to continue.",
                        i + 1, total,
                    )
                    break
                browser, context, page = setup_browser(p)
                # Don't increment i — retry the same window with the fresh session
        browser.close()

    _flush_record_counts()
    log.info("Fetch complete. Raw files in: %s", RAW_DIR)


# Returned when ORESTAR's own count says the window is over the cap, so no
# export was requested. Distinct from None, which means "the request failed,
# retry it" — conflating the two would either lose the window or spin on it.
CAPPED = Path("__capped__")

# Returned when we already hold every row ORESTAR reports for the window.
# Also distinct from None: nothing was downloaded, but nothing needs to be.
COMPLETE = Path("__complete__")

# Returned when a fresh query explicitly says "No records found". Empty
# narrowing children are part of a complete partition and must be recorded as
# zero, not retried forever because there is no export link.
EMPTY = Path("__empty__")


# ---------------------------------------------------------------------------
# What we already hold
# ---------------------------------------------------------------------------
#
# The fetcher used to re-request every year for a filer regardless of whether
# we already had it, because it had no way to ask. ORESTAR's results page
# reports the true, uncapped number of matching records, so one search answers
# "is this window already complete?" — and if it is, the multi-megabyte export
# is pure waste. On a filer with twenty years and two bad ones that turns
# twenty exports into two.
#
# Degrades to the old behaviour when there is no database to ask: a fetcher
# that refuses to run without Postgres would be worse than one that occasionally
# re-downloads.

_HELD_CONN = None
_HELD_UNAVAILABLE = False


def _held_rows(filer_id, tran_type, start, end, amt_from, amt_to, payee_prefix):
    """How many rows we hold for exactly the window ORESTAR was asked about.

    Returns None when the database cannot be consulted, which callers must
    treat as "unknown", never as zero — reading it as zero would make every
    window look missing and re-download the entire dataset.

    The filters mirror the search precisely. Filer searches fill the
    transaction-date fields, so this counts on tran_date; counting a different
    set would produce differences that are the checker's own fault.
    """
    global _HELD_CONN, _HELD_UNAVAILABLE
    if _HELD_UNAVAILABLE:
        return None
    try:
        if _HELD_CONN is None:
            import supabase_sync
            if not supabase_sync.sync_enabled():
                _HELD_UNAVAILABLE = True
                log.info("No SUPABASE_DB_URL — cannot skip windows we already hold; "
                         "every window will be downloaded as before.")
                return None
            _HELD_CONN = supabase_sync._connect()
        sql = ["select count(*) from transactions where tran_date between %s and %s",
               "and filer_id = %s"]
        args: list = [str(start), str(end), str(filer_id)]
        if tran_type and tran_type != "ALL":
            sql.append("and tran_type = %s"); args.append(tran_type)
        if amt_from is not None:
            sql.append("and amount >= %s"); args.append(float(amt_from))
        if amt_to is not None:
            sql.append("and amount <= %s"); args.append(float(amt_to))
        if payee_prefix:
            sql.append("and upper(contributor_payee) like %s")
            args.append(payee_prefix.upper() + "%")
        with _HELD_CONN.cursor() as cur:
            cur.execute(" ".join(sql), args)
            held = cur.fetchone()[0]
        # ORESTAR's count includes the deletion records we ask for, and the
        # merge never stores those, so add the ones already applied for this
        # window. Without them a window with a deletion looks short forever.
        return held + _recorded_deletions(filer_id, tran_type, start, end,
                                          amt_from, amt_to, payee_prefix)
    except Exception as exc:
        log.warning("Could not read held rows (%s) — proceeding without the skip", exc)
        _HELD_UNAVAILABLE = True
        _HELD_CONN = None
        return None


_DELETIONS_CACHE: list[dict] | None = None


def _recorded_deletions(filer_id, tran_type, start, end, amt_from, amt_to,
                        payee_prefix) -> int:
    """Applied deletion records inside exactly the window _held_rows counts."""
    global _DELETIONS_CACHE
    if _DELETIONS_CACHE is None:
        try:
            loaded = json.loads(DELETIONS_LOG.read_text()) if DELETIONS_LOG.exists() else []
            _DELETIONS_CACHE = [e.get("deletion") or {} for e in loaded
                                if isinstance(e, dict)] if isinstance(loaded, list) else []
        except (OSError, ValueError):
            _DELETIONS_CACHE = []
    count = 0
    for row in _DELETIONS_CACHE:
        if str(row.get("filer id") or "").strip() != str(filer_id):
            continue
        if tran_type and tran_type != "ALL" and row.get("tran_type") != tran_type:
            continue
        try:
            when = datetime.strptime(str(row.get("tran_date"))[:10], "%m/%d/%Y").date()
        except ValueError:
            try:
                when = date.fromisoformat(str(row.get("tran_date"))[:10])
            except ValueError:
                continue
        if not (start <= when <= end):
            continue
        try:
            amount = float(str(row.get("amount")).replace("$", "").replace(",", ""))
        except ValueError:
            amount = None
        if amt_from is not None and (amount is None or amount < float(amt_from)):
            continue
        if amt_to is not None and (amount is None or amount > float(amt_to)):
            continue
        if payee_prefix and not str(row.get("contributor_payee") or "").upper() \
                .startswith(payee_prefix.upper()):
            continue
        count += 1
    return count


def _prior_counts() -> dict:
    """ORESTAR counts recorded by earlier runs, keyed like RECORD_COUNTS.

    Lets a window be skipped with NO request at all when a previous run already
    learned its size and we hold that many rows. This is what makes the retrigger
    chain converge instead of re-walking the same ground every time.
    """
    path = RAW_DIR.parent / "record_counts.json"
    if not path.exists():
        return {}
    try:
        return {tuple(e["key"]): int(e["reported"]) for e in json.loads(path.read_text())}
    except Exception:
        return {}


def _filer_window_path(raw_dir: Path, filer_id: str, tran_type: str,
                       start: date, end: date, amt_from, amt_to, payee_prefix) -> Path:
    """On-disk name for one filer sub-query.

    The un-narrowed all-types window keeps its ORIGINAL name. Those files are
    committed to git, and renaming them would make every past filer download
    look absent and re-fetch from scratch. Narrowed sub-queries get the extra
    dimensions in the name — without them two different splits of the same day
    collide on disk and the second is skipped as "already downloaded", which is
    the same trap download_week fell into.
    """
    stem = f"filer{filer_id}_{start.isoformat()}_{end.isoformat()}"
    if tran_type != "ALL":
        stem += f"_{tran_type}"
    if amt_from is not None or amt_to is not None:
        stem += f"_amt{amt_from or ''}-{amt_to or ''}"
    if payee_prefix:
        stem += f"_p{payee_prefix}"
    return raw_dir / f"{stem}.xlsx"


def _filer_progress_key(window, filer_id: str) -> tuple:
    """Stable ledger key for one filer narrowing window."""
    tran_type, start, end, amt_from, amt_to, payee_prefix = window
    return (tran_type, str(start), str(end), str(amt_from), str(amt_to),
            str(payee_prefix), str(filer_id))


def _identity_tree_failures(progress: dict[tuple, int], branches: dict):
    """Return (parent, kind, message) failures for capped partitions."""
    failures = []
    for parent, children in branches.items():
        absent = [child for child in children if child not in progress]
        if absent:
            failures.append(
                (parent, "missing",
                 f"{parent}: {len(absent)} child windows lack fresh evidence")
            )
            continue
        parent_count = progress.get(parent)
        child_count = sum(progress[child] for child in children)
        if parent_count != child_count:
            failures.append(
                (parent, "mismatch",
                 f"{parent}: parent reported {parent_count}, children sum to {child_count}")
            )
    return failures


def _identity_tree_errors(progress: dict[tuple, int], branches: dict) -> list[str]:
    """Backward-compatible messages for partition reconciliation tests/reporting."""
    return [
        message for _parent, _kind, message
        in _identity_tree_failures(progress, branches)
    ]


def _discard_identity_subtrees(progress: dict[tuple, int], branches: dict,
                               parents) -> int:
    """Remove invalid parents and every known descendant from the resume ledger."""
    pending = list(parents)
    discarded = set()
    while pending:
        key = pending.pop()
        if key in discarded:
            continue
        discarded.add(key)
        pending.extend(branches.get(key, ()))
    removed = 0
    for key in discarded:
        if key in progress:
            progress.pop(key)
            removed += 1
    return removed


def _discard_identity_filer(progress: dict[tuple, int], filer_id: str) -> int:
    """Remove every resumable window for a filer after a repeated tree failure."""
    doomed = [key for key in progress if str(key[-1]) == str(filer_id)]
    for key in doomed:
        progress.pop(key)
    return len(doomed)


def _needs_filer_split(result: Path, rows: int, reported: int | None) -> bool:
    """Whether a returned filer window still needs narrowing."""
    return result is CAPPED or (
        rows >= ORESTAR_ROW_CAP
        and not (reported is not None and rows == reported)
    )


def download_filer_window(
    page,
    context,
    filer_id: str,
    start: date,
    end: date,
    raw_dir: Path,
    tran_type: str = "ALL",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """
    Download transactions for a specific filer in a date range.

    tran_type "ALL" applies no type filter. Splitting is NOT done here — the
    caller checks the row count and queues sub-queries via _narrow_filer(). The
    recursion this function used to do could only split on date, raised
    SessionExpiredError to signal an ordinary partial result, and returned None
    on success in one branch and a path in another; the queue driver in
    backfill_filers() handles all four narrowing tiers with one code path.
    """
    budget = SearchBudget.from_environment()
    filename = _filer_window_path(raw_dir, filer_id, tran_type, start, end,
                                  amt_from, amt_to, payee_prefix)
    stale_path = None
    download_path = filename
    if force:
        # Never let process.py merge an old cached export after a failed forced
        # refresh. Move it outside RAW_DIR's non-recursive Excel glob first,
        # and download to a second hidden directory so replacement is atomic.
        stale_dir = raw_dir / ".identity_stale"
        refresh_dir = raw_dir / ".identity_refresh"
        stale_dir.mkdir(parents=True, exist_ok=True)
        refresh_dir.mkdir(parents=True, exist_ok=True)
        stale_path = stale_dir / filename.name
        download_path = refresh_dir / filename.name
        stale_path.unlink(missing_ok=True)
        download_path.unlink(missing_ok=True)
        if filename.exists():
            filename.replace(stale_path)
    elif filename.exists():
        rows = _validate_download(filename)
        if rows >= 0:
            # A cap file is returned too, not skipped: the driver reads its row
            # count and narrows. Previously this branch tried to resume the
            # split itself and could return a path having downloaded nothing.
            log.debug("Already downloaded: %s (%d rows)", filename.name, rows)
            return filename

    if budget is not None:
        budget.require_capacity(1)
    try:
        _return_to_search(page)

        # Fill filer committee ID
        page.fill('input[name="cneSearchFilerCommitteeId"]', str(filer_id))
        page.wait_for_timeout(300)

        if tran_type != "ALL":
            page.select_option('select[name="cneSearchTranType"]', tran_type)
            page.wait_for_timeout(600)

        # Transaction date range
        page.fill('input[name="cneSearchTranStartDate"]', start.strftime("%m/%d/%Y"))
        page.fill('input[name="cneSearchTranEndDate"]', end.strftime("%m/%d/%Y"))
        _include_deleted(page)

        # Narrowing filters, used only once type and date are spent.
        if amt_from is not None:
            page.fill('input[name="cneSearchTranAmountFrom"]', str(amt_from))
        if amt_to is not None:
            page.fill('input[name="cneSearchTranAmountTo"]', str(amt_to))
        if payee_prefix:
            page.fill('input[name="cneSearchContributorTxt"]', payee_prefix)
            page.select_option('select[name="cneSearchContributorTxtSearchType"]', "S")

        # Submit
        if budget is not None:
            budget.consume(str(filer_id), {
                "collector": "fetch", "date_field": "tran",
                "start": start.isoformat(), "end": end.isoformat(),
                "tran_type": tran_type, "amt_from": amt_from,
                "amt_to": amt_to, "payee_prefix": payee_prefix,
            })
        page.click('input[name="search"]')
        try:
            page.wait_for_url(RESULTS_URL_PATTERN, timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timed out waiting for results: filer %s %s→%s", filer_id, start, end)
            _return_to_search(page)
            return None

        # Extract CSRF + session for Excel export
        results_url = page.url

        # ORESTAR's own record count for this query — uncapped, and the only
        # thing that can tell us afterwards whether the cascade actually got
        # everything. The filer path never captured it, which is why the
        # shortfalls here had to be measured by hand in a browser.
        reported = None
        try:
            reported = _read_results_count(page)
            if reported is not None:
                RECORD_COUNTS[(tran_type, str(start), str(end), str(amt_from),
                               str(amt_to), str(payee_prefix), str(filer_id))] = reported
        except Exception:
            pass                      # a missing count must never fail the fetch

        if reported == 0:
            log.info("Filer %s %s %s→%s%s: no records", filer_id, tran_type,
                     start, end, _narrowing_label(amt_from, amt_to, payee_prefix))
            _return_to_search(page)
            time.sleep(REQUEST_DELAY)
            return EMPTY

        # The count is on the page BEFORE we export, and it tells us the export
        # will be truncated. Downloading it anyway buys nothing: the first 4,999
        # rows come back again under a narrower query, and the window has to be
        # split regardless.
        #
        # It is most of the work. The Local 48 run managed 24 windows in 8.6
        # minutes before F5 cut it off; 12 were capped, and they accounted for
        # 34.7 MB of the 40.4 MB downloaded — 86% of the bytes spent on files
        # whose contents we already had. Skipping them roughly doubles the
        # useful windows per run, and requests-before-block is the binding
        # constraint on this whole recovery.
        # Completeness BEFORE the cap, and the order matters enormously.
        #
        # These checks used to run the other way round, so a window over the cap
        # was narrowed before anyone asked whether we needed it. Local 48 holds
        # 8,000-22,000 rows in every single year, so every year window is over
        # the cap: the backfill split years we already held in full into dozens
        # of sub-queries each, and a whole run's request budget disappeared into
        # 2006-2009 without downloading a single row.
        #
        # Being over the cap only matters if the data is actually wanted. If we
        # already hold everything ORESTAR reports for the window, there is
        # nothing to fetch and nothing to narrow — one search settles it.
        #
        # The comparison is deliberately ">=", not "==". We delete originals and
        # older amendments that ORESTAR no longer counts, so holding FEWER rows
        # than it reports is normal and does not prove anything is missing.
        # Treating that as "incomplete" only costs a download; treating it as
        # "complete" could skip a window that really is short, so the
        # conservative direction is the only safe one.
        held = None
        if reported is not None and not force:
            held = _held_rows(filer_id, tran_type, start, end,
                              amt_from, amt_to, payee_prefix)
            if held is not None and held >= reported:
                log.info("Filer %s %s %s→%s%s: hold %d of %d — already complete, "
                         "skipping download", filer_id, tran_type, start, end,
                         _narrowing_label(amt_from, amt_to, payee_prefix), held, reported)
                _return_to_search(page)
                time.sleep(REQUEST_DELAY)
                return COMPLETE

        if reported is not None and reported > ORESTAR_ROW_CAP:
            log.info("Filer %s %s %s→%s%s: %d records, hold %s — over the cap, "
                     "narrowing without downloading", filer_id, tran_type, start, end,
                     _narrowing_label(amt_from, amt_to, payee_prefix), reported,
                     "unknown" if held is None else f"{held}")
            _return_to_search(page)
            time.sleep(REQUEST_DELAY)
            return CAPPED
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")
        if not csrf:
            log.warning("No CSRF token for filer %s %s→%s", filer_id, start, end)
            _return_to_search(page)
            return None

        session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
        session_path = f";{session_match.group(1)}" if session_match else ""

        # Download Excel
        raw_dir.mkdir(parents=True, exist_ok=True)
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        export_url = f"{EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"
        resp = requests.get(
            export_url,
            cookies=cookies,
            headers={"User-Agent": USER_AGENT, "Referer": results_url},
            timeout=120,
        )

        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower():
            log.debug("requests returned HTML; falling back to browser download...")
            with page.expect_download(timeout=60_000) as dl_info:
                page.goto(f"{EXPORT_URL}?OWASP_CSRFTOKEN={csrf}",
                          wait_until="commit", timeout=60_000)
            dl_info.value.save_as(download_path)
        else:
            download_path.write_bytes(resp.content)

        # Validate
        row_count = _validate_download(download_path)
        if row_count < 0:
            log.warning("Invalid download for filer %s %s→%s — deleting", filer_id, start, end)
            download_path.unlink(missing_ok=True)
            _return_to_search(page)
            return None

        # Forced evidence is useful only if the export reconciles with the
        # count on the page we just queried. A short export must not enter the
        # merge or the resumable ledger as though the window were complete.
        if force and (reported is None or row_count != reported):
            log.warning(
                "Forced filer %s %s %s→%s%s: export contained %d rows, "
                "reported count was %s — refusing partial refresh",
                filer_id, tran_type, start, end,
                _narrowing_label(amt_from, amt_to, payee_prefix), row_count,
                "unknown" if reported is None else str(reported),
            )
            download_path.unlink(missing_ok=True)
            _return_to_search(page)
            return None

        if force:
            download_path.replace(filename)
            if stale_path is not None:
                stale_path.unlink(missing_ok=True)
        if row_count == ORESTAR_ROW_CAP and reported == row_count:
            # process.py conservatively skips every filer*.xlsx at 4,999 rows
            # because legacy files at the cap were usually truncated. This one
            # is different: the fresh page count proved that 4,999 is the
            # complete leaf. Mark the filename so the merger can distinguish it.
            verified = filename.with_name(f"verified_{filename.name}")
            verified.unlink(missing_ok=True)
            filename.replace(verified)
            filename = verified

        log.info("Filer %s %s %s→%s%s: %d rows (%d bytes)",
                 filer_id, tran_type, start, end,
                 _narrowing_label(amt_from, amt_to, payee_prefix),
                 row_count, filename.stat().st_size)

        # No cap handling here — the driver reads the row count off the returned
        # file and queues the next narrowing tier.
        _return_to_search(page)
        time.sleep(REQUEST_DELAY)
        return filename

    except SearchBudgetError:
        raise
    except SessionExpiredError:
        raise
    except Exception as exc:
        if "secure.sos.state.or.us/orestar" not in page.url:
            raise SessionExpiredError(
                f"Session expired during filer fetch — redirected to {page.url}"
            ) from exc
        log.warning("Failed filer %s %s→%s: %s", filer_id, start, end, exc)
        try:
            _return_to_search(page)
        except Exception:
            pass
        return None


def setup_browser_retrying(playwright, attempts: int = 3):
    """Open the browser, surviving a slow ORESTAR.

    setup_browser() ends in page.goto(SEARCH_URL, timeout=60_000), and when
    ORESTAR failed to answer within the minute the PlaywrightTimeout escaped
    every handler and killed the run outright:

        playwright._impl._errors.TimeoutError: Page.goto: Timeout 60000ms exceeded

    The per-window handlers below already restart the browser on this exact
    condition; it was only the FIRST open, outside the loop, that had no cover.
    A site too slow to answer once is usually fine a minute later, and a run
    that dies here has done no work at all.
    """
    for attempt in range(1, attempts + 1):
        try:
            return setup_browser(playwright)
        except Exception as exc:
            log.warning("Browser setup failed (attempt %d/%d): %s",
                        attempt, attempts, exc)
            if attempt == attempts:
                raise
            time.sleep(15 * attempt)


def _is_transient_orestar_startup_failure(exc: Exception) -> bool:
    """Return whether initial setup reached ORESTAR but got no usable form.

    Browser installation/launch errors are structural and must fail normally.
    Navigation failures and known session redirects are transient failures for
    which a quiet cooldown can help. A rendered page without the expected form
    is deliberately excluded: that can also mean a structural selector change.
    """
    message = str(exc)
    return (
        "Page.goto" in message
        or isinstance(exc, SessionExpiredError)
    )


def _setup_initial_filer_browser(playwright, identity_remediation: bool):
    """Set up the initial filer browser and mark retryable startup exhaustion.

    Keep this marker around the initial setup only. ``setup_browser_retrying``
    is also called after session expiry, when a filer may already have made
    progress; replaying the whole command at that point is not safe.
    """
    try:
        return setup_browser_retrying(playwright)
    except Exception as exc:
        if identity_remediation and _is_transient_orestar_startup_failure(exc):
            print("REMEDIATION_STARTUP_EXHAUSTED attempts=3", flush=True)
        raise


def backfill_filers(
    filer_ids: list[str],
    start_year: int = 2006,
    *,
    end_date: date | None = None,
    identity_remediation: bool = False,
    reset_identity_progress: bool = False,
) -> None:
    """Fetch all transactions for specific filers through the narrowing queue.

    Normal backfills use count and database shortcuts. Identity remediation
    cannot: a surplus row can cancel a missing row in every count comparison.
    Forced runs therefore query fresh data and resume only from their own
    validated progress ledger.
    """
    SearchBudget.from_environment()  # Refuse before cache/progress mutations or setup.
    target_end = end_date or date.today()
    current_year = target_end.year
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    incomplete_filers: list[str] = []
    # Filers this run actually FINISHED. The caller cannot infer this from the
    # list it passed in: when the loop below stops early on rate-limiting, the
    # filers it never reached are neither completed nor "incomplete" — they are
    # untouched. The workflow used to mark every requested filer done unless it
    # appeared in incomplete_backfills.txt, so a batch of ten that stopped after
    # three recorded all ten as backfilled. 3,639 of the 4,128 filers on that
    # list have no downloaded file of any kind, and filer 4572 — marked done,
    # nothing on disk — is 9,031 rows short of ORESTAR for 2023 alone.
    completed_filers: list[str] = []
    prior_counts = _prior_counts() if not identity_remediation else {}
    progress = _identity_progress(
        reset_filers=filer_ids if identity_remediation and reset_identity_progress else ()
    ) if identity_remediation else {}
    failure_counts = _identity_failures(
        reset_filers=filer_ids if identity_remediation and reset_identity_progress else ()
    ) if identity_remediation else {}
    initial_progress_keys = set(progress)
    if identity_remediation:
        progressed_filers = {str(key[-1]) for key in progress}
        quarantine = [
            fid for fid in filer_ids
            if reset_identity_progress or str(fid) not in progressed_filers
        ]
        _quarantine_identity_cache(quarantine)
    retryable_tree_failure = False
    skipped_windows = 0

    log.info("Backfilling %d filers from %d to %d (%d ordinary counts, %d forced "
             "windows known; identity remediation=%s)", len(filer_ids), start_year,
             current_year, len(prior_counts), len(progress),
             "yes" if identity_remediation else "no")

    with sync_playwright() as p:
        browser, context, page = _setup_initial_filer_browser(
            p, identity_remediation,
        )
        consecutive_failures = 0
        for fid in filer_ids:
            log.info("=== Backfilling filer %s ===", fid)
            filer_had_error = False
            forced_branches = {}
            # ONE window per filer, not one per year.
            #
            # This used to seed twenty-one year windows per committee, which
            # made sense when the target was Local 48 — 8,000 to 22,000 rows in
            # every single year, so every year needed splitting anyway.
            #
            # It is exactly backwards for the tail that remains. Of the 658
            # committees still short, 656 hold fewer than 4,999 rows in TOTAL:
            # their entire twenty-year history fits in one request. Asking about
            # each year separately spent 21 searches to learn what one search
            # answers, and searches are the scarce resource — F5 stops a runner
            # after roughly 25 of them, so a committee missing three rows could
            # consume most of a run.
            #
            # Measured across what is left: 13,818 searches (~92 hours) becomes
            # 1,356 (~9 hours). The cascade is unchanged and still splits by
            # type, then date, then amount, then contributor whenever a window
            # comes back at the cap — a big filer just pays one extra level of
            # splitting, and #77 means that tree is only ever derived once.
            queue = [("ALL", date(start_year, 1, 1), target_end,
                      None, None, None)]
            qi = 0
            while qi < len(queue):
                tran_type, yr_start, yr_end, af, at, pp = queue[qi]
                year = yr_start.year

                # Skip with NO request at all when an earlier run already
                # learned this window's size and we hold that many rows. Without
                # this the retrigger chain re-walks the same ground every run and
                # spends its whole request budget rediscovering what it knew.
                _pk = _filer_progress_key(queue[qi], fid)
                if identity_remediation and _pk in progress:
                    _fresh = progress[_pk]
                    if _fresh > ORESTAR_ROW_CAP:
                        subs = _narrow_filer(tran_type, yr_start, yr_end, af, at, pp)
                        if not subs:
                            log.error(
                                "Forced progress contains terminal capped window for "
                                "filer %s %s %s→%s%s — INCOMPLETE",
                                fid, tran_type, yr_start, yr_end,
                                _narrowing_label(af, at, pp),
                            )
                            filer_had_error = True
                        else:
                            queue[qi + 1:qi + 1] = subs
                            forced_branches[_pk] = [
                                _filer_progress_key(child, fid) for child in subs
                            ]
                    skipped_windows += 1
                    qi += 1
                    continue

                _prior = prior_counts.get(_pk)
                if not identity_remediation and _prior is not None:
                    _held = _held_rows(fid, tran_type, yr_start, yr_end, af, at, pp)
                    if _held is not None and _held >= _prior:
                        log.debug("Filer %s %s %s→%s: hold %d of %d from a previous "
                                  "run — skipping entirely", fid, tran_type,
                                  yr_start, yr_end, _held, _prior)
                        skipped_windows += 1
                        qi += 1
                        continue

                    # A recorded cap is as durable a fact as a recorded count.
                    #
                    # Capped windows used to be excluded from this skip, so every
                    # run re-asked ORESTAR about a window it had already measured,
                    # got the same "over the cap" answer, and rebuilt the same
                    # narrowing tree. With 48 capped windows recorded for Local 48,
                    # that consumed the entire per-run request budget before
                    # reaching any window small enough to download: 59 consecutive
                    # runs, every one of them green, and not one row recovered.
                    #
                    # Knowing a window is over the cap is exactly what is needed to
                    # narrow it, so narrow it and spend the request on a sub-window
                    # nobody has measured yet. Completeness is still checked first,
                    # because an over-cap window we already hold in full needs
                    # neither a download nor a split.
                    if _prior >= ORESTAR_ROW_CAP:
                        subs = _narrow_filer(tran_type, yr_start, yr_end, af, at, pp)
                        if subs:
                            log.debug("Filer %s %s %s→%s: %d records recorded earlier "
                                      "— narrowing without asking again", fid,
                                      tran_type, yr_start, yr_end, _prior)
                            queue[qi + 1:qi + 1] = subs
                            skipped_windows += 1
                            qi += 1
                            continue
                result = None
                try:
                    result = download_filer_window(
                        page, context, fid, yr_start, yr_end, RAW_DIR,
                        tran_type, af, at, pp, force=identity_remediation,
                    )
                except SearchBudgetError:
                    raise
                except SessionExpiredError:
                    log.warning("Session expired during filer %s year %d — restarting browser", fid, year)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    try:
                        browser, context, page = setup_browser_retrying(p)
                        result = download_filer_window(
                            page, context, fid, yr_start, yr_end, RAW_DIR,
                            tran_type, af, at, pp, force=identity_remediation,
                        )
                        if result is None:
                            consecutive_failures += 1
                    except SearchBudgetError:
                        raise
                    except Exception as exc:
                        log.error("Failed filer %s year %d after restart: %s", fid, year, exc)
                        filer_had_error = True
                        consecutive_failures += 1
                except Exception as exc:
                    filer_had_error = True
                    log.error("Failed filer %s year %d: %s", fid, year, exc)
                    consecutive_failures += 1

                if consecutive_failures >= 2:
                    log.warning("Rate-limited — stopping filer %s at year %d", fid, year)
                    break

                if result is None:
                    # A window that returned nothing is not done. Leave it
                    # unmarked so the next run retries it.
                    filer_had_error = True
                    if identity_remediation:
                        consecutive_failures += 1
                        if consecutive_failures >= 2:
                            log.warning("Forced refresh produced no result twice — "
                                        "stopping filer %s", fid)
                            break
                    qi += 1
                    continue

                consecutive_failures = 0

                # Cap check — a capped result is routing evidence, never a
                # completed leaf. COMPLETE is possible only in normal mode.
                reported = RECORD_COUNTS.get(_pk)
                if result is COMPLETE:
                    rows = 0
                    skipped_windows += 1
                elif result is EMPTY:
                    rows = 0
                elif result is CAPPED:
                    rows = reported if reported is not None else ORESTAR_ROW_CAP
                else:
                    rows = _validate_download(result)

                needs_split = _needs_filer_split(result, rows, reported)
                if needs_split:
                    subs = _narrow_filer(tran_type, yr_start, yr_end, af, at, pp)
                    if subs:
                        log.warning(
                            "Cap hit for filer %s %s %s→%s%s (%d rows) — "
                            "narrowing into %d sub-queries",
                            fid, tran_type, yr_start, yr_end,
                            _narrowing_label(af, at, pp), rows, len(subs),
                        )
                        queue[qi + 1:qi + 1] = subs
                        if identity_remediation:
                            forced_branches[_pk] = [
                                _filer_progress_key(child, fid) for child in subs
                            ]
                            if reported is None or reported <= ORESTAR_ROW_CAP:
                                log.error("Forced capped window for filer %s has no "
                                          "fresh reported count — INCOMPLETE", fid)
                                filer_had_error = True
                            else:
                                progress[_pk] = reported
                    else:
                        log.error(
                            "TRUNCATED: filer %s %s %s%s returned %d rows "
                            "(cap %d) and no dimension is left — INCOMPLETE",
                            fid, tran_type, yr_start,
                            _narrowing_label(af, at, pp), rows, ORESTAR_ROW_CAP,
                        )
                        _record_truncated(
                            f"filer{fid}:{tran_type}:{yr_start}:{yr_end}:"
                            f"{af}:{at}:{pp}",
                            yr_start,
                            rows,
                        )
                        filer_had_error = True
                elif identity_remediation:
                    if result is COMPLETE or reported is None or rows != reported:
                        log.error(
                            "Forced leaf for filer %s %s %s→%s%s did not reconcile "
                            "(%d exported, %s reported) — INCOMPLETE",
                            fid, tran_type, yr_start, yr_end,
                            _narrowing_label(af, at, pp), rows,
                            "unknown" if reported is None else str(reported),
                        )
                        if result is not COMPLETE:
                            result.unlink(missing_ok=True)
                        filer_had_error = True
                    else:
                        progress[_pk] = reported
                qi += 1
            else:
                # Queue drained without breaking — filer is done
                if identity_remediation:
                    tree_failures = _identity_tree_failures(progress, forced_branches)
                    failed_parents = {
                        parent for parent, _kind, _message in tree_failures
                    }
                    for parent in set(forced_branches) - failed_parents:
                        failure_counts.pop(parent, None)
                    mismatch_parents = set()
                    for parent, kind, tree_error in tree_failures:
                        if kind == "missing":
                            # A leaf request failed, but every sibling remains
                            # valid. Preserve the subtree so the next resume
                            # skips directly to the absent key.
                            log.error("Forced partition remains incomplete: %s",
                                      tree_error)
                            continue
                        mismatch_parents.add(parent)
                        failure_counts[parent] = failure_counts.get(parent, 0) + 1
                        attempt = failure_counts[parent]
                        log.error(
                            "Forced partition reconciliation failed (attempt %d): %s",
                            attempt, tree_error,
                        )
                        if attempt == 1:
                            retryable_tree_failure = True
                        else:
                            log.error(
                                "Partition %s failed reconciliation repeatedly; "
                                "leaving the filer incomplete for diagnosis",
                                parent,
                            )
                    if mismatch_parents:
                        repeated = any(
                            failure_counts[parent] > 1 for parent in mismatch_parents
                        )
                        if repeated:
                            # Do not leave an ancestor root that makes the auto
                            # selector treat this diagnosed-bad filer as an
                            # active resume forever, starving every healthy
                            # candidate behind it. It remains on the deferred
                            # exact-missing list and can start a new snapshot
                            # after the other work drains.
                            removed = _discard_identity_filer(progress, fid)
                        else:
                            removed = _discard_identity_subtrees(
                                progress, forced_branches, mismatch_parents,
                            )
                        log.warning(
                            "Invalidated %d forced window(s) so the bad subtree "
                            "cannot be trusted on resume", removed,
                        )
                    if failed_parents:
                        filer_had_error = True
                if not filer_had_error:
                    log.info("Filer %s: all %d windows downloaded successfully", fid, len(queue))
                    completed_filers.append(fid)
                else:
                    incomplete_filers.append(fid)
                continue

            # Broke out of year loop — filer is incomplete
            filer_had_error = True

            if filer_had_error:
                incomplete_filers.append(fid)

            if consecutive_failures >= 2:
                log.warning(
                    "Rate-limited: %d consecutive failures — stopping early. "
                    "Remaining filers will be retried on the next run.",
                    consecutive_failures,
                )
                break
        browser.close()

    # Persist ORESTAR's own counts so verify_completeness can check this run's
    # windows. Only _fetch_range flushed them, so every filer-targeted fetch
    # threw its counts away and left nothing to verify against.
    _flush_record_counts()
    if identity_remediation:
        _save_identity_progress(progress)
        _save_identity_failures(failure_counts)

    # What this run finished, for the workflow to tick off. Rewritten each run,
    # never appended: it describes THIS run, and the durable record is
    # backfilled_filers.txt.
    completed_path = RAW_DIR.parent / "completed_backfills.txt"
    completed_path.write_text("\n".join(completed_filers) + ("\n" if completed_filers else ""))
    log.info("Completed %d of %d requested filers (%d incomplete, %d never reached); "
             "%d windows skipped as already held",
             len(completed_filers), len(filer_ids), len(incomplete_filers),
             len(filer_ids) - len(completed_filers) - len(incomplete_filers),
             skipped_windows)
    if _HELD_CONN is not None:
        try:
            _HELD_CONN.close()
        except Exception:
            pass

    # Write incomplete filers with retry counts so auto-backfill can defer
    # filers that keep failing (e.g. huge filers that always get rate-limited).
    # Format: "filer_id:count" per line.
    incomplete_path = RAW_DIR.parent / "incomplete_backfills.txt"
    existing: dict[str, int] = {}
    if incomplete_path.exists():
        for line in incomplete_path.read_text().strip().split("\n"):
            if ":" in line:
                fid_str, cnt = line.split(":", 1)
                existing[fid_str.strip()] = int(cnt)
            elif line.strip():
                existing[line.strip()] = 1
    for fid in completed_filers:
        existing.pop(str(fid), None)
    if incomplete_filers:
        log.info("Incomplete filers (will retry next run): %s", " ".join(incomplete_filers))
        for fid in incomplete_filers:
            existing[fid] = existing.get(fid, 0) + 1
            log.info("  Filer %s: retry count now %d", fid, existing[fid])
    if existing:
        incomplete_path.write_text(
            "\n".join(f"{fid}:{cnt}" for fid, cnt in sorted(existing.items())) + "\n"
        )
    else:
        incomplete_path.unlink(missing_ok=True)
    log.info("Filer backfill complete. Raw files in: %s", RAW_DIR)
    if identity_remediation:
        retained_progress = len(set(progress) - initial_progress_keys)
        print(f"REMEDIATION_RESULT progress={retained_progress} "
              f"retry={1 if retryable_tree_failure else 0} "
              f"completed={len(completed_filers)} incomplete={len(incomplete_filers)}")


def run_misc_recent(days: int = MISC_RECENT_DAYS) -> None:
    """Re-read lumped rows filed recently, every time (no progress log)."""
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Lumped-row re-read (recent): %s → %s", start, today)
    _fetch_range(start, today, "filed", payee_prefix=MISC_PREFIX, types=MISC_TYPES,
                 window_days=MISC_RECENT_WINDOW_DAYS, log_file=None)


def run_misc_reread(start_year: int = 2006, end_year: int | None = None,
                    max_minutes: float | None = MISC_REREAD_MAX_MINUTES) -> None:
    """History-wide re-read of lumped rows, resumable across chained runs."""
    start = date(start_year, 1, 1)
    end = min(date(end_year, 12, 31) if end_year else date.today(), date.today())
    log.info("Lumped-row re-read (history): %s → %s, budget %s minutes",
             start, end, max_minutes if max_minutes else "unlimited")
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    _fetch_range(start, end, "filed", payee_prefix=MISC_PREFIX, types=MISC_TYPES,
                 window_days=MISC_REREAD_WINDOW_DAYS, log_file=FETCHED_LOG_MISC,
                 deadline=deadline)


def count_misc_remaining(start_year: int = 2006, end_year: int | None = None) -> int:
    """Windows of the history-wide re-read not yet done; mirrors run_misc_reread."""
    start = date(start_year, 1, 1)
    end = min(date(end_year, 12, 31) if end_year else date.today(), date.today())
    fetched = _load_fetched(FETCHED_LOG_MISC)
    tasks = _range_tasks(start, end, MISC_TYPES, MISC_REREAD_WINDOW_DAYS, MISC_PREFIX)
    remaining = sum(1 for task in tasks if _task_key(task) not in fetched)
    print(remaining)
    return remaining


def run_incremental(days: int = 14) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Incremental mode: %s → %s", start, today)
    _fetch_range(start, today, date_field="filed")


def run_backfill(start_year: int = 2017, end_year: int | None = None,
                 date_field: str = "filed") -> None:
    start = date(start_year, 1, 1)
    end   = date(end_year, 12, 31) if end_year else date.today()
    end   = min(end, date.today())
    log.info("Backfill mode: %s → %s (date_field=%s)", start, end, date_field)
    _fetch_range(start, end, date_field=date_field)


def run_test(days: int = 7) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Test mode: %s → %s", start, today)
    _fetch_range(start, today, date_field="filed")


def count_remaining(start_year: int = 2017, end_year: int | None = None,
                    date_field: str = "filed") -> int:
    """
    Count standard 7-day windows not yet fetched.
    Prints the count to stdout and returns it.
    Used by the backfill workflow to decide whether to re-trigger itself.
    """
    start = date(start_year, 1, 1)
    end   = date(end_year, 12, 31) if end_year else date.today()
    end   = min(end, date.today())
    # MUST mirror how _fetch_range enumerates work, or the backfill chain
    # cannot terminate. This counted ("ALL", start, end) keys while the fetcher
    # records one key per TYPE, so the keys it looked for were never written
    # and "remaining" stayed permanently above zero — an endless retrigger.
    windows = list(week_windows(start, end))
    tasks = [(t, str(ws), str(we)) for ws, we in windows for t in TRAN_TYPES]
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    remaining = sum(1 for key in tasks if key not in fetched)
    print(remaining)
    return remaining


def check_split_gaps(date_field: str = "filed") -> int:
    """
    Detect windows that were split due to the ORESTAR row cap but whose
    second (or first) sub-window was never fetched.  Removes incomplete
    parent windows from the fetched log so they are retried on the next run.

    Returns the number of gaps found and repaired.
    """
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    if not fetched:
        log.info("No fetched windows to check.")
        return 0

    to_remove = set()
    for key in fetched:
        tt, ws_str, we_str = key
        ws = date.fromisoformat(ws_str)
        we = date.fromisoformat(we_str)
        span = (we - ws).days
        if span < 2:
            continue
        half = span // 2
        mid = ws + timedelta(days=half)
        first_key = (tt, str(ws), str(mid))
        second_key = (tt, str(mid + timedelta(days=1)), str(we))
        has_first = first_key in fetched
        has_second = second_key in fetched
        # If one half exists but not the other, the parent was prematurely
        # marked done.  Remove it so the next run re-splits and fetches
        # the missing half.
        if (has_first and not has_second) or (has_second and not has_first):
            to_remove.add(key)
            missing = second_key if has_first else first_key
            log.warning(
                "Split gap: %s %s→%s is missing sub-window %s→%s — "
                "removing parent so it will be re-fetched",
                tt, ws_str, we_str, missing[1], missing[2],
            )

    if to_remove:
        cleaned = fetched - to_remove
        _save_fetched(cleaned, log_file)
        log.info(
            "Removed %d incomplete split windows from %s",
            len(to_remove), log_file.name,
        )
    else:
        log.info("No split gaps found in %s", log_file.name)

    print(len(to_remove))
    return len(to_remove)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download ORESTAR campaign finance Excel exports."
    )
    parser.add_argument(
        "--mode",
        choices=["incremental", "backfill", "test", "count-remaining", "check-gaps",
                 "misc-recent", "misc-reread", "count-misc-remaining"],
        default="incremental",
    )
    parser.add_argument("--days",        type=int,  default=14,   dest="days")
    parser.add_argument("--start-year",  type=int,  default=None, dest="start_year")
    parser.add_argument("--end-year",    type=int,  default=None, dest="end_year")
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        dest="end_date",
        help="Freeze filer-targeted searches at YYYY-MM-DD across chained runs",
    )
    parser.add_argument(
        "--date-field",
        choices=["filed", "tran"],
        default="filed",
        dest="date_field",
        help="Search by filed date (default) or transaction date (use 'tran' for pre-2017 backfill)",
    )
    parser.add_argument(
        "--filer-ids",
        nargs="+",
        help="Fetch all transactions for specific filer committee IDs (bypasses --mode)",
    )
    parser.add_argument(
        "--identity-remediation",
        action="store_true",
        help=("Force fresh filer exports without count/held-row shortcuts; "
              "requires --filer-ids"),
    )
    parser.add_argument(
        "--reset-identity-progress",
        action="store_true",
        help="Start a new forced-remediation chain for the requested filer IDs",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        dest="max_minutes",
        help=f"misc-reread: stop starting windows after this long "
             f"(default {MISC_REREAD_MAX_MINUTES:g})",
    )
    parser.add_argument(
        "--clear-identity-progress",
        action="store_true",
        help="Clear completed forced-remediation progress for --filer-ids and exit",
    )

    args = parser.parse_args()

    if (args.identity_remediation or args.reset_identity_progress
            or args.clear_identity_progress) and not args.filer_ids:
        parser.error("identity remediation options require --filer-ids")
    if args.reset_identity_progress and not args.identity_remediation:
        parser.error("--reset-identity-progress requires --identity-remediation")
    if args.identity_remediation and args.end_date is None:
        parser.error("--identity-remediation requires a frozen --end-date")

    start_year = args.start_year
    if start_year is None:
        start_year = 2006 if args.filer_ids else 2017
    if args.identity_remediation and start_year != 2006:
        parser.error("--identity-remediation requires --start-year=2006")

    if args.clear_identity_progress:
        clear_identity_progress(args.filer_ids)
        return

    if args.filer_ids:
        backfill_filers(
            args.filer_ids,
            start_year=start_year,
            end_date=args.end_date,
            identity_remediation=args.identity_remediation,
            reset_identity_progress=args.reset_identity_progress,
        )
    elif args.mode == "incremental":
        run_incremental(days=args.days)
    elif args.mode == "backfill":
        run_backfill(start_year=start_year, end_year=args.end_year,
                     date_field=args.date_field)
    elif args.mode == "test":
        run_test(days=args.days)
    elif args.mode == "count-remaining":
        count_remaining(start_year=start_year, end_year=args.end_year,
                        date_field=args.date_field)
    elif args.mode == "check-gaps":
        check_split_gaps(date_field=args.date_field)
    elif args.mode == "misc-recent":
        run_misc_recent(days=args.days if args.days != 14 else MISC_RECENT_DAYS)
    elif args.mode == "misc-reread":
        run_misc_reread(start_year=args.start_year or 2006, end_year=args.end_year,
                        max_minutes=(args.max_minutes if args.max_minutes is not None
                                     else MISC_REREAD_MAX_MINUTES))
    elif args.mode == "count-misc-remaining":
        count_misc_remaining(start_year=args.start_year or 2006, end_year=args.end_year)


if __name__ == "__main__":
    main()
