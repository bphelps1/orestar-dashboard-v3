#!/usr/bin/env python3
"""
fetch_certificates.py — ORESTAR's Certificates of Limited Contributions and
Expenditures, one search per year.

See orestar_certificates.py for why these matter: a certificate year is a year
ORESTAR's itemized totals cannot see, and the certificate list is the only
place that says which years those are.

The search is a plain GET per year, but ORESTAR fronts it with the same F5
challenge as every other page, and the challenge cookies expire after about
thirty seconds. So each year is a real page navigation that lets the browser
answer the challenge, exactly as fetch_earliest_balances.py loads account
summaries. Success is judged by the results table rendering, never by the
absence of challenge markers — F5's scripts are embedded in real pages too.

Usage:
    xvfb-run python scraper/fetch_certificates.py
    python scraper/fetch_certificates.py --years 2013 2014 2015
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fetch_earliest_balances import (  # noqa: E402
    CHALLENGE_WAIT,
    PAGE_LOAD_ATTEMPTS,
    USER_AGENT,
    _safe_content,
)
from orestar_certificates import (  # noqa: E402
    CERTIFICATES_FILENAME,
    merge_certificates,
    parse_certificate_page,
)

log = logging.getLogger("fetch_certificates")

BASE_URL = "https://secure.sos.state.or.us/orestar"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = DATA_DIR / CERTIFICATES_FILENAME

# ORESTAR's certificate search starts here; earlier years are not offered.
FIRST_YEAR = 2007
# Polite gap between searches.
YEAR_DELAY = 1.0
# A run of failed years means the runner is being refused, not that several
# years are coincidentally broken. Stop rather than spend ~20 minutes per year
# failing, and keep the stored certificates for everything not reached.
CONSECUTIVE_FAILURE_ABORT = 3


def _load_year(page, year: int) -> list[dict] | None:
    """One year's certificates, or None if the results table never rendered."""
    url = f"{BASE_URL}/cneCertificateSearch.do?yearSelected={year}&search=Submit"
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:  # the challenge often lands the page anyway
            log.debug("Year %d: goto attempt %d raised: %s", year, attempt, exc)
        deadline = time.time() + CHALLENGE_WAIT
        while time.time() < deadline:
            rows = parse_certificate_page(_safe_content(page), year)
            if rows is not None:
                return rows
            time.sleep(0.5)
        log.debug("Year %d: results never rendered on attempt %d/%d",
                  year, attempt, PAGE_LOAD_ATTEMPTS)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", nargs="+", type=int,
                        help="Specific years (default: every year ORESTAR offers)")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--max-minutes", type=float, default=15.0,
                        help="Stop starting new years after this long")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s")

    this_year = datetime.now(timezone.utc).year
    years = sorted(set(args.years or range(FIRST_YEAR, this_year + 1)))

    previous = None
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text())
        except (OSError, ValueError) as exc:
            # A corrupt file must not be silently replaced by a partial run.
            log.error("Existing %s is unreadable (%s); refusing to overwrite it",
                      args.output, exc)
            return 1

    from playwright.sync_api import sync_playwright

    fetched: dict[int, list[dict] | None] = {}
    started = time.time()
    consecutive_failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,   # ORESTAR's F5 gate refuses headless browsers
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_context(user_agent=USER_AGENT,
                                   accept_downloads=False).new_page()
        for year in years:
            if (time.time() - started) / 60 >= args.max_minutes:
                log.warning("Time budget reached before %d; stored years are kept", year)
                break
            rows = _load_year(page, year)
            fetched[year] = rows
            if rows is None:
                consecutive_failures += 1
                log.warning("Year %d: results did not render", year)
                if consecutive_failures >= CONSECUTIVE_FAILURE_ABORT:
                    log.warning("%d consecutive years refused; stopping", consecutive_failures)
                    break
            else:
                consecutive_failures = 0
                log.info("Year %d: %d certificates", year, len(rows))
            time.sleep(YEAR_DELAY)
        browser.close()

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload, kept = merge_certificates(previous, fetched, fetched_at)
    ok = sorted(y for y, rows in fetched.items() if rows is not None)
    failed = sorted(y for y, rows in fetched.items() if rows is None)

    # Write only if something new was learned; a run that read nothing must
    # not even touch the file's metadata.
    if ok:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(args.output)

    total = sum(len((v or {}).get("certificates") or []) for v in payload["years"].values())
    print(f"CERTIFICATES_RESULT fetched={len(ok)} failed={len(failed)} "
          f"kept_from_previous={len(kept)} stored_years={len(payload['years'])} "
          f"stored_certificates={total}")
    if failed:
        log.warning("Years not refreshed: %s", " ".join(map(str, failed)))
    # Nonzero only when nothing at all was read, so a partial run still
    # publishes what it learned while a fully refused run is visibly red.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
