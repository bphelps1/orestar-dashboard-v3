#!/usr/bin/env python3
"""
verify_filer.py — Verify transaction completeness for specific filers.

Downloads ALL transactions for a filer directly from ORESTAR using the
"Committees/Filers by Name" search flow:
  1. Navigate to GotoSearchByName.do
  2. Search by filer committee ID
  3. Click "Campaign Finance Activity" for the filer
  4. Submit search with no filters (all types, all dates)
  5. Export Excel — if under 4,999 rows, done
  6. If capped, scrape paginated HTML results instead

Then compares against our local transaction data to identify discrepancies:
  - Transactions in ORESTAR that we're missing
  - Transactions we have that ORESTAR doesn't show
  - Amount or date mismatches on matching transaction IDs

Usage:
    python scraper/verify_filer.py --filer-id 19763
    python scraper/verify_filer.py --filer-id 19763 --skip-fetch   # re-diff only
    python scraper/verify_filer.py --discrepancy-threshold 100      # auto-select filers
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

# Ensure the repo root is on sys.path so "from scraper.process import ..." works
# when invoked as "python scraper/verify_filer.py"
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from scraper import supabase_sync

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
VERIFY_DIR = DATA_DIR / "_verify"

BASE_URL = "https://secure.sos.state.or.us/orestar"
SEARCH_BY_NAME_URL = f"{BASE_URL}/GotoSearchByName.do"
EXPORT_URL = f"{BASE_URL}/XcelCNESearch"

RESULTS_URL_PATTERN = "**/gotoPublicTransactionSearchResults**"

PAGE_RENDER_WAIT = 7
REQUEST_DELAY = 1.5
ORESTAR_ROW_CAP = 4999

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_XLS_MAGIC  = b"\xd0\xcf\x11\xe0"
_XLSX_MAGIC = b"PK\x03\x04"


class SessionExpiredError(Exception):
    pass


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def setup_browser(playwright):
    log.info("Launching browser (headed mode)...")
    browser = playwright.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        accept_downloads=True,
        no_viewport=True,
    )
    page = context.new_page()
    return browser, context, page


def _validate_download(path: Path) -> int:
    """Validate downloaded file is real Excel. Returns row count or -1."""
    if not path.exists() or path.stat().st_size == 0:
        return -1
    header = path.read_bytes()[:8]
    if header[:5].lower() in (b"<!doc", b"<html", b"<head", b"<?xml"):
        return -1
    try:
        if header[:4] == _XLS_MAGIC:
            import xlrd
            wb = xlrd.open_workbook(str(path), on_demand=True)
            nrows = wb.sheet_by_index(0).nrows
            wb.release_resources()
            return max(nrows - 1, 0)
        elif header[:4] == _XLSX_MAGIC:
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True)
            count = 0
            for _ in wb.active.rows:
                count += 1
                if count > ORESTAR_ROW_CAP + 1:
                    break
            wb.close()
            return max(count - 1, 0)
        else:
            return -1
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Filer-scoped ORESTAR navigation
# ---------------------------------------------------------------------------

def navigate_to_filer_search(page, filer_id: str) -> bool:
    """Navigate to a filer's Campaign Finance Activity search page.

    Flow: GotoSearchByName.do → search by committee ID → click
    "Campaign Finance Activity" → lands on cneSearch.do scoped to filer.
    """
    log.info("Navigating to filer %s via Committees/Filers by Name...", filer_id)

    page.goto(SEARCH_BY_NAME_URL, timeout=60_000)
    time.sleep(PAGE_RENDER_WAIT)

    if "secure.sos.state.or.us/orestar" not in page.url:
        raise SessionExpiredError(f"Session expired — redirected to {page.url}")

    # Fill committee ID field
    id_field = page.locator('input[name="committeeId"]')
    if id_field.count() == 0:
        id_field = page.locator('input[name="cneSearchFilerCommitteeId"]')
    if id_field.count() == 0:
        log.warning("No committee ID field found on search-by-name page")
        return False

    id_field.fill(filer_id)
    page.wait_for_timeout(300)

    # Click search
    search_btn = page.locator('input[type="submit"][value="Submit"]')
    if search_btn.count() == 0:
        search_btn = page.locator('input[type="submit"][value="Search"]')
    if search_btn.count() == 0:
        search_btn = page.locator('input[name="search"]')
    if search_btn.count() == 0:
        log.warning("No search button found")
        return False

    search_btn.first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeout:
        log.warning("Timeout waiting for filer search results")
        return False
    time.sleep(2)

    # Click "Campaign Finance Activity"
    cfa_link = page.locator('a:has-text("Campaign Finance Activity")')
    if cfa_link.count() == 0:
        cfa_link = page.locator(f'a[href*="cneSearchFilerCommitteeId={filer_id}"]')
    if cfa_link.count() > 0:
        cfa_link.first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timeout waiting for Campaign Finance Activity page")
        time.sleep(2)
    else:
        # Fallback: navigate directly to the filer-scoped search page
        log.info("No 'Campaign Finance Activity' link — navigating directly to cneSearch for filer %s", filer_id)
        direct_url = f"{BASE_URL}/cneSearch.do?cneSearchButtonName=search&cneSearchFilerCommitteeId={filer_id}"
        page.goto(direct_url, timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeout:
            pass
        time.sleep(2)

    if "cneSearch" not in page.url and "TransactionSearch" not in page.url:
        log.warning("Expected cneSearch page, got: %s", page.url)
        return False

    log.info("On filer %s search page: %s", filer_id, page.url)
    return True


def _return_to_filer_search(page, filer_id: str) -> bool:
    """Return to the filer-scoped search form."""
    if "cneSearch" in page.url:
        # Try JS resetSearch() first (matches the page's own button)
        has_reset_fn = page.evaluate("typeof resetSearch === 'function'")
        if has_reset_fn:
            page.evaluate("resetSearch()")
            page.wait_for_timeout(500)
            return True
        reset = page.locator('input[type="button"][value="Reset"]')
        if reset.count() > 0:
            reset.first.click()
            page.wait_for_timeout(300)
            return True
    return navigate_to_filer_search(page, filer_id)


# ---------------------------------------------------------------------------
# Submit search and get to results page
# ---------------------------------------------------------------------------

def _submit_filer_search(page, filer_id: str) -> tuple[bool, int]:
    """Submit the filer-scoped search with no filters.

    Returns (on_results_page, expected_record_count).
    expected_record_count is parsed from the "Results :N records found" header.
    Returns -1 if the count can't be parsed.
    """
    _return_to_filer_search(page, filer_id)

    # Check if we're already on a results page (some filer pages show
    # results immediately after navigation)
    existing_count = _parse_record_count(page)
    if existing_count > 0:
        log.info("Already on results page with %d records for filer %s", existing_count, filer_id)
        return True, existing_count

    # The cneSearch page uses JavaScript doNewSearch() instead of a
    # standard HTML submit button. Try that first, then fall back to
    # looking for submit buttons.
    has_js_search = page.evaluate("typeof doNewSearch === 'function'")
    if has_js_search:
        log.debug("Using doNewSearch() JavaScript function")
        page.evaluate("doNewSearch()")
    else:
        search_btn = page.locator('input[type="submit"][value="Submit"]')
        if search_btn.count() == 0:
            search_btn = page.locator('input[name="search"]')
        if search_btn.count() == 0:
            search_btn = page.locator('input[type="submit"][value="Search"]')
        if search_btn.count() == 0:
            log.warning("No search button or doNewSearch() on filer search form")
            return False, -1
        search_btn.first.click()

    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except PlaywrightTimeout:
        pass
    time.sleep(2)

    # Parse expected record count from page header
    expected = _parse_record_count(page)
    if expected >= 0:
        log.info("ORESTAR reports %d records for filer %s", expected, filer_id)
    else:
        log.warning("Could not parse record count from results page for filer %s", filer_id)

    return True, expected


_RECORD_COUNT_RE = re.compile(r"Results\s*:\s*([\d,]+)\s*records?\s*found", re.IGNORECASE)


def _parse_record_count(page) -> int:
    """Extract the record count from the results page header.

    Looks for text like "Results :25324 records found for the above search criteria."
    Returns the count, or -1 if not found.
    """
    text = page.evaluate("""() => {
        // Look for the results count in common locations
        const body = document.body.innerText;
        const match = body.match(/Results\\s*:\\s*([\\d,]+)\\s*records?\\s*found/i);
        return match ? match[1] : null;
    }""")
    if text:
        return int(text.replace(",", ""))
    return -1


# ---------------------------------------------------------------------------
# Method 1: Excel export (works when total rows < 4,999)
# ---------------------------------------------------------------------------

def _try_excel_export(page, context, filer_id: str, raw_dir: Path) -> tuple[Path | None, int, int]:
    """Try to export all transactions as Excel.

    Returns (path, row_count, expected_count).
    expected_count is from the "Results :N records found" header.
    """
    import requests as req

    filename = raw_dir / f"filer{filer_id}_ALL.xlsx"

    ok, expected_count = _submit_filer_search(page, filer_id)
    if not ok:
        return None, -1, -1

    # Extract CSRF token
    csrf = page.evaluate("""() => {
        const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
        if (!links.length) return null;
        const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
        return m ? m[1] : null;
    }""")
    if not csrf:
        log.warning("No CSRF token on results page")
        return None, -1

    results_url = page.url
    session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
    session_path = f";{session_match.group(1)}" if session_match else ""

    raw_dir.mkdir(parents=True, exist_ok=True)
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    export_url = f"{EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"

    resp = req.get(
        export_url,
        cookies=cookies,
        headers={"User-Agent": USER_AGENT, "Referer": results_url},
        timeout=120,
    )

    ct = resp.headers.get("Content-Type", "")
    if "html" in ct.lower():
        log.debug("requests returned HTML; falling back to browser download...")
        with page.expect_download(timeout=60_000) as dl_info:
            page.goto(
                f"{EXPORT_URL}?OWASP_CSRFTOKEN={csrf}",
                wait_until="commit",
                timeout=60_000,
            )
        dl = dl_info.value
        dl.save_as(filename)
    else:
        filename.write_bytes(resp.content)

    row_count = _validate_download(filename)
    if row_count < 0:
        log.warning("Invalid Excel download — deleting")
        filename.unlink(missing_ok=True)
        return None, -1

    log.info("Excel export: %s (%d rows, expected %s, %d bytes)",
             filename.name, row_count,
             str(expected_count) if expected_count >= 0 else "unknown",
             filename.stat().st_size)
    return filename, row_count, expected_count


# ---------------------------------------------------------------------------
# Method 2: Paginated HTML scraping (fallback for large filers)
# ---------------------------------------------------------------------------

def _scrape_results_table(page) -> list[dict]:
    """Scrape the transaction table from the current ORESTAR results page."""
    return page.evaluate("""() => {
        // Find the results table — try common patterns
        let table = document.querySelector('table.datatable, table.resultstable');
        if (!table) {
            // Look for any table whose headers mention transactions
            for (const t of document.querySelectorAll('table')) {
                const ths = [...t.querySelectorAll('th')];
                const hdrs = ths.map(th => th.textContent.trim().toLowerCase());
                if (hdrs.some(h => h.includes('tran') || h.includes('amount'))) {
                    table = t;
                    break;
                }
            }
        }
        if (!table) return [];

        const ths = [...table.querySelectorAll('thead th, tr:first-child th')];
        const headers = ths.map(th => th.textContent.trim());
        if (!headers.length) return [];

        const rows = [...table.querySelectorAll('tbody tr')];
        return rows.map(tr => {
            const tds = [...tr.querySelectorAll('td')];
            const obj = {};
            tds.forEach((td, i) => {
                if (i < headers.length) obj[headers[i]] = td.textContent.trim();
            });
            return obj;
        }).filter(o => Object.keys(o).length > 0);
    }""") or []


def _scrape_all_pages(page, filer_id: str, raw_dir: Path, expected_count: int = -1) -> Path | None:
    """Scrape all paginated HTML results for a filer.

    Submits search with no filters, then paginates through all result
    pages scraping the HTML table. Saves to CSV.

    If expected_count is provided, verifies the scraped total matches.
    """
    log.info("Falling back to HTML pagination scraping for filer %s...", filer_id)

    ok, page_expected = _submit_filer_search(page, filer_id)
    if not ok:
        return None

    # Use the freshly-parsed expected count (may differ if expected_count was from
    # a previous submit; prefer the one from this submit)
    if page_expected >= 0:
        expected_count = page_expected

    all_rows: list[dict] = []
    page_num = 1

    while True:
        rows = _scrape_results_table(page)
        if not rows:
            if page_num == 1:
                log.warning("No rows found on first results page")
            break

        all_rows.extend(rows)
        log.info("Page %d: %d rows (total: %d%s)", page_num, len(rows), len(all_rows),
                 f" / {expected_count}" if expected_count >= 0 else "")

        # Look for Next button/link
        next_btn = page.locator('input[type="submit"][value="Next"]')
        if next_btn.count() == 0:
            next_btn = page.locator('a:has-text("Next")')
        if next_btn.count() == 0:
            log.info("Last page reached (page %d, %d total rows)", page_num, len(all_rows))
            break

        next_btn.first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timeout on Next at page %d", page_num)
            break
        time.sleep(0.5)
        page_num += 1

        if page_num > 2000:
            log.warning("Safety limit: stopped at 2000 pages (%d rows)", len(all_rows))
            break

    if not all_rows:
        return None

    # Verify count
    if expected_count >= 0:
        if len(all_rows) == expected_count:
            log.info("VERIFIED: scraped %d rows matches ORESTAR expected %d", len(all_rows), expected_count)
        else:
            log.warning(
                "COUNT MISMATCH: scraped %d rows but ORESTAR header says %d records",
                len(all_rows), expected_count,
            )

    df = pd.DataFrame(all_rows)
    csv_path = raw_dir / f"filer{filer_id}_ALL_html.csv"
    df.to_csv(csv_path, index=False)
    log.info("Saved %s (%d rows)", csv_path.name, len(df))
    return csv_path


# ---------------------------------------------------------------------------
# Orchestrator: fetch all transactions for one filer
# ---------------------------------------------------------------------------

def fetch_filer_transactions(filer_id: str) -> Path:
    """Download all ORESTAR transactions for a filer.

    Strategy:
      1. Navigate to filer via "Committees/Filers by Name" search
      2. Try Excel export (all types, no date filter)
      3. If Excel hits the 4,999-row cap, scrape paginated HTML instead

    Returns the path to the directory containing data files.
    """
    raw_dir = VERIFY_DIR / filer_id / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Check if already fetched
    done_marker = VERIFY_DIR / filer_id / "fetch_done"
    if done_marker.exists():
        log.info("Filer %s already fetched (done marker exists)", filer_id)
        return raw_dir

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)

        try:
            if not navigate_to_filer_search(page, filer_id):
                log.error("Failed to navigate to filer %s search page", filer_id)
                browser.close()
                return raw_dir

            # Try Excel export first
            filepath, row_count, expected_count = _try_excel_export(
                page, context, filer_id, raw_dir,
            )

            use_html = False

            if filepath is not None and row_count >= ORESTAR_ROW_CAP:
                # Hit the Excel cap — must use HTML
                log.warning("Filer %s: Excel capped at %d rows — switching to HTML",
                            filer_id, row_count)
                filepath.unlink(missing_ok=True)
                use_html = True

            elif filepath is not None and expected_count >= 0 and row_count != expected_count:
                # Excel didn't cap but count doesn't match header — use HTML
                log.warning(
                    "Filer %s: Excel has %d rows but ORESTAR says %d — switching to HTML",
                    filer_id, row_count, expected_count,
                )
                filepath.unlink(missing_ok=True)
                use_html = True

            elif filepath is not None:
                # Excel worked and count is verified (or header wasn't parseable)
                log.info("Filer %s: Excel export complete (%d rows)", filer_id, row_count)
                done_marker.write_text(
                    json.dumps({"method": "excel", "rows": row_count,
                                "expected": expected_count})
                )

            else:
                # Excel export failed entirely — try HTML
                log.warning("Filer %s: Excel export failed — trying HTML", filer_id)
                use_html = True

            if use_html:
                csv_path = _scrape_all_pages(page, filer_id, raw_dir, expected_count)
                if csv_path:
                    df = pd.read_csv(csv_path)
                    done_marker.write_text(
                        json.dumps({"method": "html", "rows": len(df),
                                    "expected": expected_count})
                    )
                else:
                    log.error("HTML scraping also failed for filer %s", filer_id)

        except SessionExpiredError as e:
            log.error("Session expired for filer %s: %s", filer_id, e)
        except Exception as e:
            log.error("Unexpected error for filer %s: %s", filer_id, e)
        finally:
            browser.close()

    return raw_dir


# ---------------------------------------------------------------------------
# Load and compare transactions
# ---------------------------------------------------------------------------

def load_orestar_exports(raw_dir: Path) -> pd.DataFrame:
    """Load all Excel and CSV files from a verification directory."""
    frames = []

    # Load Excel files
    for ext in ("*.xls", "*.xlsx"):
        for f in raw_dir.glob(ext):
            try:
                header = f.read_bytes()[:4]
                if header == _XLS_MAGIC:
                    import xlrd
                    wb = xlrd.open_workbook(str(f), on_demand=True)
                    sheet = wb.sheet_by_index(0)
                    headers = [sheet.cell_value(0, c) for c in range(sheet.ncols)]
                    data = []
                    for r in range(1, sheet.nrows):
                        data.append([sheet.cell_value(r, c) for c in range(sheet.ncols)])
                    wb.release_resources()
                    frames.append(pd.DataFrame(data, columns=headers))
                elif header == _XLSX_MAGIC:
                    frames.append(pd.read_excel(f, engine="openpyxl"))
            except Exception as e:
                log.warning("Failed to load %s: %s", f.name, e)

    # Load HTML-scraped CSV files
    for f in raw_dir.glob("*_html.csv"):
        try:
            frames.append(pd.read_csv(f))
        except Exception as e:
            log.warning("Failed to load %s: %s", f.name, e)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.strip().lower() for c in df.columns]

    # Normalize tran_id column
    for candidate in ["tran id", "tran_id", "transaction id"]:
        if candidate in df.columns:
            if candidate != "tran_id":
                df = df.rename(columns={candidate: "tran_id"})
            break

    # Parse amount
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(
            df["amount"].astype(str).str.replace(",", "").str.replace("$", ""),
            errors="coerce",
        ).fillna(0.0)

    # Deduplicate
    if "tran_id" in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=["tran_id"], keep="first")
        if len(df) < before:
            log.info("Deduplicated: %d → %d rows", before, len(df))

    return df


def load_local_transactions(filer_id: str) -> pd.DataFrame:
    """Load our local transaction data for a specific filer ID."""
    from scraper.process import _load_all_transactions
    df = _load_all_transactions()
    fid_col = "filer id" if "filer id" in df.columns else None
    if not fid_col:
        log.error("No 'filer id' column in local transaction data")
        return pd.DataFrame()
    df = df[df[fid_col].astype(str).str.strip() == str(filer_id)]
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    return df


def diff_transactions(
    orestar_df: pd.DataFrame,
    local_df: pd.DataFrame,
    filer_id: str,
) -> dict:
    """Compare ORESTAR vs local transactions. Returns summary dict."""
    empty_result = {
        "filer_id": filer_id, "orestar_count": 0, "local_count": 0,
        "missing_count": 0, "extra_count": 0, "mismatch_count": 0,
        "missing_amount": 0, "extra_amount": 0, "mismatch_amount": 0,
        "net_discrepancy": 0, "missing_df": pd.DataFrame(),
        "extra_df": pd.DataFrame(), "mismatched": [],
    }

    if orestar_df.empty:
        empty_result["error"] = "No ORESTAR data fetched"
        return empty_result

    if "tran_id" in orestar_df.columns:
        orestar_df["tran_id"] = orestar_df["tran_id"].astype(str).str.strip()
    if "tran_id" in local_df.columns:
        local_df["tran_id"] = local_df["tran_id"].astype(str).str.strip()

    orestar_ids = set(orestar_df["tran_id"]) if "tran_id" in orestar_df.columns else set()
    local_ids = set(local_df["tran_id"]) if "tran_id" in local_df.columns else set()

    missing_ids = orestar_ids - local_ids
    extra_ids = local_ids - orestar_ids
    common_ids = orestar_ids & local_ids

    # Amount mismatches
    mismatched = []
    if common_ids and "amount" in orestar_df.columns and "amount" in local_df.columns:
        ore_amounts = orestar_df.set_index("tran_id")["amount"]
        if ore_amounts.index.duplicated().any():
            ore_amounts = ore_amounts[~ore_amounts.index.duplicated(keep="first")]
        ore_amounts = ore_amounts.to_dict()

        loc_amounts = local_df.set_index("tran_id")["amount"]
        if loc_amounts.index.duplicated().any():
            loc_amounts = loc_amounts[~loc_amounts.index.duplicated(keep="first")]
        loc_amounts = loc_amounts.to_dict()

        for tid in common_ids:
            ore_amt = ore_amounts.get(tid, 0.0)
            loc_amt = loc_amounts.get(tid, 0.0)
            if abs(ore_amt - loc_amt) > 0.005:
                mismatched.append({
                    "tran_id": tid,
                    "orestar_amount": ore_amt,
                    "local_amount": loc_amt,
                    "diff": round(ore_amt - loc_amt, 2),
                })

    missing_df = orestar_df[orestar_df["tran_id"].isin(missing_ids)] if missing_ids else pd.DataFrame()
    extra_df = local_df[local_df["tran_id"].isin(extra_ids)] if extra_ids else pd.DataFrame()

    missing_amount = float(missing_df["amount"].sum()) if not missing_df.empty and "amount" in missing_df.columns else 0.0
    extra_amount = float(extra_df["amount"].sum()) if not extra_df.empty and "amount" in extra_df.columns else 0.0
    mismatch_amount = sum(m["diff"] for m in mismatched)

    return {
        "filer_id": filer_id,
        "orestar_count": len(orestar_ids),
        "local_count": len(local_ids),
        "missing_count": len(missing_ids),
        "extra_count": len(extra_ids),
        "mismatch_count": len(mismatched),
        "missing_amount": round(missing_amount, 2),
        "extra_amount": round(extra_amount, 2),
        "mismatch_amount": round(mismatch_amount, 2),
        "net_discrepancy": round(missing_amount - extra_amount + mismatch_amount, 2),
        "missing_df": missing_df,
        "extra_df": extra_df,
        "mismatched": mismatched,
    }


def print_report(result: dict) -> None:
    print(f"\n{'='*60}")
    print(f"Verification Report — Filer {result['filer_id']}")
    print(f"{'='*60}")
    print(f"  ORESTAR transactions:  {result['orestar_count']:>8,}")
    print(f"  Local transactions:    {result['local_count']:>8,}")
    print(f"  Missing (in ORESTAR):  {result['missing_count']:>8,}   ${result['missing_amount']:>12,.2f}")
    print(f"  Extra (in local):      {result['extra_count']:>8,}   ${result['extra_amount']:>12,.2f}")
    print(f"  Amount mismatches:     {result['mismatch_count']:>8,}   ${result['mismatch_amount']:>12,.2f}")
    print(f"  Net discrepancy:                      ${result['net_discrepancy']:>12,.2f}")
    print(f"{'='*60}")

    if result["missing_count"] > 0:
        missing = result["missing_df"]
        print(f"\n  Top missing transactions (by amount):")
        cols = [c for c in ["tran_id", "tran date", "amount", "tran_type", "sub_type",
                            "contributor/payee", "contributor payee"] if c in missing.columns]
        if "amount" in missing.columns:
            top = missing.nlargest(10, "amount")
        else:
            top = missing.head(10)
        print(top[cols].to_string(index=False))

    if result["mismatched"]:
        print(f"\n  Amount mismatches:")
        for m in sorted(result["mismatched"], key=lambda x: abs(x["diff"]), reverse=True)[:10]:
            print(f"    tran_id={m['tran_id']}  ORESTAR=${m['orestar_amount']:,.2f}  "
                  f"Local=${m['local_amount']:,.2f}  Diff=${m['diff']:,.2f}")


def get_filers_with_discrepancies(threshold: float = 100.0) -> list[tuple[str, str, float]]:
    """Find filers whose cash-on-hand discrepancy exceeds the threshold."""
    if supabase_sync.sync_enabled():
        candidates = [
            (entry.get("name") or entry.get("slug") or "", entry)
            for entry in supabase_sync.require_filer_comparison_details()
        ]
    else:
        filer_index_path = DATA_DIR / "aggregated" / "filer_index.json"
        if not filer_index_path.exists():
            raise RuntimeError(
                "No filer_index is available locally or from Supabase"
            )
        with open(filer_index_path) as f:
            index = json.load(f)
        candidates = []
        for entry in index:
            slug = entry.get("slug", "")
            detail_path = DATA_DIR / "aggregated" / "filers" / f"{slug}.json"
            if not detail_path.exists():
                continue
            with open(detail_path) as f:
                candidates.append((entry.get("name", slug), json.load(f)))

    results = []
    for display_name, detail in candidates:
        # Automatic verification must be driven by an immutable paired
        # capture, not by the legacy scalar.  Old generated detail files can
        # still contain ``orestar_discrepancy`` even though their ORESTAR
        # balance was not captured alongside the app state.  Fail closed on
        # missing/stale/closed comparison metadata and read the delta from the
        # paired evidence itself.
        comparison = detail.get("orestar_comparison")
        if (not isinstance(comparison, dict)
                or comparison.get("status") != "paired"
                or comparison.get("actionable") is not True
                or detail.get("closed")):
            continue
        try:
            disc = abs(float(comparison["delta_at_capture"]))
        except (KeyError, TypeError, ValueError):
            continue
        if disc >= threshold:
            scope_ids = sorted({str(fid) for fid in (detail.get("filer_ids") or [])
                                if str(fid)})
            # The aggregate delta does not identify which component filer is
            # responsible. Automatic threshold selection waits for per-ID
            # count/identity evidence instead of guessing a representative.
            if len(scope_ids) != 1:
                continue
            fid = scope_ids[0]
            if fid:
                results.append((str(fid), display_name, disc))

    results.sort(key=lambda x: x[2], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Verify transaction completeness for specific ORESTAR filers."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--filer-id", type=str, help="Single filer ID to verify")
    group.add_argument("--discrepancy-threshold", type=float,
                       help="Auto-select filers with discrepancy above this amount")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip downloading — only diff already-fetched data")
    parser.add_argument("--max-filers", type=int, default=0,
                        help="Max filers when using --discrepancy-threshold (0 = all)")
    parser.add_argument("--save-report", action="store_true",
                        help="Save diff results to CSV files")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if already done")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.filer_id:
        filers = [(args.filer_id, f"Filer {args.filer_id}", 0.0)]
    else:
        filers = get_filers_with_discrepancies(args.discrepancy_threshold)
        if not filers:
            log.info("No filers found with discrepancy >= $%.2f", args.discrepancy_threshold)
            return
        log.info("Found %d filers with discrepancy >= $%.2f", len(filers), args.discrepancy_threshold)
        for fid, name, disc in filers[:10]:
            log.info("  %s (%s): $%.2f", fid, name, disc)
        if args.max_filers > 0:
            filers = filers[:args.max_filers]

    for fid, name, disc in filers:
        log.info("Verifying filer %s (%s)...", fid, name)

        # Remove done marker if forcing re-fetch
        if args.force:
            done_marker = VERIFY_DIR / fid / "fetch_done"
            done_marker.unlink(missing_ok=True)

        # Phase 1: Fetch from ORESTAR
        raw_dir = VERIFY_DIR / fid / "raw"
        if not args.skip_fetch:
            raw_dir = fetch_filer_transactions(fid)

        # Phase 2: Load both datasets
        orestar_df = load_orestar_exports(raw_dir)
        local_df = load_local_transactions(fid)

        # Phase 3: Diff
        result = diff_transactions(orestar_df, local_df, fid)
        result["filer_name"] = name
        print_report(result)

        if args.save_report:
            report_dir = VERIFY_DIR / fid
            report_dir.mkdir(parents=True, exist_ok=True)
            if not result.get("missing_df", pd.DataFrame()).empty:
                result["missing_df"].to_csv(report_dir / "missing.csv", index=False)
            if not result.get("extra_df", pd.DataFrame()).empty:
                result["extra_df"].to_csv(report_dir / "extra.csv", index=False)
            if result.get("mismatched"):
                pd.DataFrame(result["mismatched"]).to_csv(report_dir / "mismatched.csv", index=False)
            log.info("Reports saved to %s", report_dir)


if __name__ == "__main__":
    main()
