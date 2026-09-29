#!/usr/bin/env python3
"""
audit_window_counts.py — find windows we believe we have but are actually short.

The fetch log records which windows were requested, not whether the response was
complete. Before the per-type change, an all-types request for a busy week hit
ORESTAR's 4,999-row export cap and returned a truncated file, and a single-day
window skipped the cap check entirely. Both recorded the window as fetched. So
"covered" in the log means "asked for", not "got".

That distinction is where the outstanding discrepancies live. Re-fetching every
window the log listed as MISSING for 2006-2016 recovered only 69 committees,
while those same years still show millions unaccounted for — the money is in
windows the log already calls complete.

This asks ORESTAR how many records a window really holds and compares that with
what the database holds. It reads the count off the results page and never
downloads the export, so it costs one request per window instead of a full
transfer, which matters when ORESTAR rate-limits at roughly 25 requests.

Output: data/window_audit.json — every window checked, with the shortfall.
Feed the short ones back to fetch.py; verify afterwards with
verify_completeness.py.

Usage:
    python scraper/audit_window_counts.py --start-year 2010 --end-year 2010
    python scraper/audit_window_counts.py --start-year 2010 --end-year 2014 --types C
    python scraper/audit_window_counts.py --max-windows 100     # respect the limiter
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fetch
import supabase_sync as s

DATA_DIR = Path(__file__).parent.parent / "data"
AUDIT_PATH = DATA_DIR / "window_audit.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

_COUNT_RE = re.compile(r"([\d,]+)\s+records found")


def orestar_count(page, start: date, end: date, tran_type: str, date_field: str) -> int | None:
    """Records ORESTAR reports for a window. None if the page did not resolve.

    Deliberately stops at the count. The export is the expensive part and the
    count is the only thing needed to decide whether a window is worth
    re-fetching at all.
    """
    fetch._return_to_search(page)
    if tran_type and tran_type != "ALL":
        page.select_option('select[name="cneSearchTranType"]', tran_type)
        page.wait_for_timeout(400)
    if date_field == "tran":
        page.fill('input[name="cneSearchTranStartDate"]', start.strftime("%m/%d/%Y"))
        page.fill('input[name="cneSearchTranEndDate"]',   end.strftime("%m/%d/%Y"))
    else:
        page.fill('input[name="cneSearchTranFiledStartDate"]', start.strftime("%m/%d/%Y"))
        page.fill('input[name="cneSearchTranFiledEndDate"]',   end.strftime("%m/%d/%Y"))
    page.click('input[name="search"]')
    try:
        page.wait_for_url(fetch.RESULTS_URL_PATTERN, timeout=30_000)
    except Exception:
        return None
    m = _COUNT_RE.search(page.inner_text("body"))
    return int(m.group(1).replace(",", "")) if m else None


def held(cur, start: date, end: date, tran_type: str, date_field: str) -> int:
    col = "tran_date" if date_field == "tran" else "filed_date"
    sql = f"select count(*) from transactions where {col} between %s and %s"
    args: list = [start, end]
    if tran_type and tran_type != "ALL":
        sql += " and tran_type = %s"; args.append(tran_type)
    cur.execute(sql, args)
    return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-year", type=int, required=True)
    ap.add_argument("--end-year", type=int)
    ap.add_argument("--types", nargs="+", default=fetch.TRAN_TYPES)
    ap.add_argument("--date-field", choices=["filed", "tran"], default="filed")
    ap.add_argument("--max-windows", type=int, default=0,
                    help="stop after this many (ORESTAR throttles around 25)")
    args = ap.parse_args()

    start = date(args.start_year, 1, 1)
    end = date(args.end_year or args.start_year, 12, 31)
    end = min(end, date.today())
    windows = list(fetch.week_windows(start, end))
    tasks = [(t, ws, we) for ws, we in windows for t in args.types]
    if args.max_windows:
        tasks = tasks[:args.max_windows]
    log.info("Auditing %d windows (%d weeks x %d types, date_field=%s)",
             len(tasks), len(windows), len(args.types), args.date_field)

    try:
        existing = json.loads(AUDIT_PATH.read_text()) if AUDIT_PATH.exists() else []
    except Exception:
        existing = []
    done = {(e["type"], e["start"], e["end"], e["date_field"]) for e in existing}

    conn = s._connect()
    cur = conn.cursor()
    from playwright.sync_api import sync_playwright
    checked = short = blocked = 0
    with sync_playwright() as p:
        browser, context, page = fetch.setup_browser(p)
        for i, (tran_type, ws, we) in enumerate(tasks, 1):
            if (tran_type, str(ws), str(we), args.date_field) in done:
                continue
            n = orestar_count(page, ws, we, tran_type, args.date_field)
            if n is None:
                blocked += 1
                log.warning("[%d/%d] %s %s→%s: no count (throttled or blocked) — stopping "
                            "to preserve results", i, len(tasks), tran_type, ws, we)
                break
            h = held(cur, ws, we, tran_type, args.date_field)
            gap = n - h
            existing.append({"type": tran_type, "start": str(ws), "end": str(we),
                             "date_field": args.date_field, "orestar": n, "held": h,
                             "missing": gap,
                             "checked_at": datetime.now().isoformat(timespec="seconds")})
            checked += 1
            if gap > 0:
                short += 1
                log.info("[%d/%d] %s %s→%s  ORESTAR %d, held %d  SHORT %d",
                         i, len(tasks), tran_type, ws, we, n, h, gap)
            time.sleep(fetch.REQUEST_DELAY)
        browser.close()
    conn.close()

    AUDIT_PATH.write_text(json.dumps(existing, indent=1))
    tot_missing = sum(e["missing"] for e in existing if e["missing"] > 0)
    print()
    print(f"  windows checked this run : {checked:,}")
    print(f"  short of ORESTAR         : {short:,}")
    print(f"  stopped early (throttled): {'yes' if blocked else 'no'}")
    print(f"  audit file now covers    : {len(existing):,} windows")
    print(f"  total rows missing       : {tot_missing:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
