#!/usr/bin/env python3
"""
fetch_amendment_chains.py — read amendment chains ORESTAR's search hides.

For each target (filer, year, transaction type) this runs ORESTAR's
transaction search with "Include Deleted Transactions" and "Show Expired
Transactions" ticked, reads every results page, and then opens the
Transaction History page of each row that was amended, expired or deleted, so
each chain of versions is known exactly. See orestar_amendment_chains.py for
what the chains mean and when they may move a balance.

Nothing here is guessed. A target is stored only when the listing holds as
many rows as ORESTAR reports and every chain it needs was read; anything less
leaves the stored file as it was.

Every page is a real navigation, so the browser can answer ORESTAR's F5
challenge, and success is judged by the results or history table rendering.

Usage:
    xvfb-run python scraper/fetch_amendment_chains.py --targets 3215:2006:C,E 21452:2021:OD
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))

from fetch_earliest_balances import (  # noqa: E402
    CHALLENGE_WAIT,
    PAGE_LOAD_ATTEMPTS,
    USER_AGENT,
    _safe_content,
)
from orestar_amendment_chains import (  # noqa: E402
    CHAINS_FILENAME,
    SUPPORTED_TYPES,
    merge_targets,
    parse_history_page,
    parse_results_page,
)

log = logging.getLogger("fetch_amendment_chains")

BASE_URL = "https://secure.sos.state.or.us/orestar"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = DATA_DIR / CHAINS_FILENAME

# ORESTAR's search codes and labels for each summary line checked.
TYPE_NAMES = {"C": "Contribution", "E": "Expenditure",
              "OR": "Other Receipt", "OD": "Other Disbursement"}
PAGE_ROWS = 50
# ORESTAR's results UI stops at 100 pages. A committee-year that large is not
# what this tool is for; refuse it rather than collect part of it.
MAX_ROWS = 5_000
REQUEST_DELAY = 2.0
CONSECUTIVE_FAILURE_ABORT = 3
_TOKEN_RE = re.compile(r"OWASP_CSRFTOKEN=([A-Z0-9-]+)")


def parse_target(text: str) -> list[tuple[str, int, str]]:
    """"3215:2006:C,E" -> [("3215", 2006, "C"), ("3215", 2006, "E")]."""
    parts = text.strip().split(":")
    if len(parts) != 3 or not parts[0].isdigit() or not re.fullmatch(r"\d{4}", parts[1]):
        raise ValueError(f"target must be FILER:YEAR:TYPES, got {text!r}")
    types = [t.strip().upper() for t in parts[2].split(",") if t.strip()]
    if not types or any(t not in SUPPORTED_TYPES for t in types):
        raise ValueError(f"types must be among {SUPPORTED_TYPES}, got {parts[2]!r}")
    return [(parts[0], int(parts[1]), t) for t in types]


def results_url(token: str, filer_id: str, year: int, tran_type: str, page: int) -> str:
    """Page 1 is the search itself; page n > 1 is "next" from index n - 2."""
    params = {
        "cneSearchButtonName": "search" if page == 1 else "next",
        "cneSearchContributorTxtSearchType": "C",
        "cneSearchFilerCommitteeId": filer_id,
        "cneSearchFilerCommitteeTxtSearchType": "C",
        "cneSearchPageIdx": str(0 if page == 1 else page - 2),
        "cneSearchTranEndDate": f"12/31/{year}",
        "cneSearchTranStartDate": f"01/01/{year}",
        "cneSearchTranType": tran_type,
        "cneSearchTranTypeName": TYPE_NAMES[tran_type],
        "viewDeletedTransactions": "on",
        "viewExpiredTransactions": "on",
        "OWASP_CSRFTOKEN": token,
    }
    return f"{BASE_URL}/gotoPublicTransactionSearchResults.do?{urlencode(params)}"


def history_url(token: str, tran_id: str) -> str:
    return (f"{BASE_URL}/ceTransactionHistory.do?"
            f"{urlencode({'tranRsn': tran_id, 'OWASP_CSRFTOKEN': token})}")


def _load(page, url: str, parse):
    """Navigate and wait for ``parse`` to recognise the page, or return None."""
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:  # the challenge often lands the page anyway
            log.debug("goto attempt %d raised: %s", attempt, exc)
        deadline = time.time() + CHALLENGE_WAIT
        while time.time() < deadline:
            parsed = parse(_safe_content(page))
            if parsed is not None:
                time.sleep(REQUEST_DELAY)
                return parsed
            time.sleep(0.5)
        log.debug("page never rendered on attempt %d/%d: %s", attempt,
                  PAGE_LOAD_ATTEMPTS, url.split("?")[0])
    return None


def _token(page) -> str | None:
    def find(html: str):
        match = _TOKEN_RE.search(html or "")
        return match.group(1) if match else None
    return _load(page, f"{BASE_URL}/gotoPublicTransactionSearch.do", find)


def collect_target(page, token: str, filer_id: str, year: int, tran_type: str) -> dict | None:
    """One complete listing plus every chain it needs, or None."""
    first = _load(page, results_url(token, filer_id, year, tran_type, 1), parse_results_page)
    if first is None:
        log.warning("%s %s %s: results never rendered", filer_id, year, tran_type)
        return None
    reported, rows = first
    if reported > MAX_ROWS:
        log.warning("%s %s %s: %d rows is over the %d the UI can page; refusing",
                    filer_id, year, tran_type, reported, MAX_ROWS)
        return None
    by_id = {r["tran_id"]: r for r in rows}
    pages = (reported + PAGE_ROWS - 1) // PAGE_ROWS
    for number in range(2, pages + 1):
        parsed = _load(page, results_url(token, filer_id, year, tran_type, number),
                       parse_results_page)
        if parsed is None or parsed[0] != reported:
            log.warning("%s %s %s: page %d unusable", filer_id, year, tran_type, number)
            return None
        for row in parsed[1]:
            by_id[row["tran_id"]] = row
    if len(by_id) != reported:
        log.warning("%s %s %s: collected %d of %d rows; refusing a partial listing",
                    filer_id, year, tran_type, len(by_id), reported)
        return None

    pending = sorted((tid for tid, r in by_id.items()
                      if r["status"] != "Original" or r.get("expired")), key=int)
    chains: list[dict] = []
    while pending:
        tran_id = pending[0]
        versions = _load(page, history_url(token, tran_id), parse_history_page)
        if versions is None or tran_id not in {v["tran_id"] for v in versions}:
            log.warning("%s %s %s: history for %s unusable", filer_id, year, tran_type, tran_id)
            return None
        chains.append({"versions": versions})
        seen = {v["tran_id"] for v in versions}
        pending = [tid for tid in pending if tid not in seen]
    log.info("%s %s %s: %d rows, %d chains", filer_id, year, tran_type, reported, len(chains))
    return {
        "filer_id": filer_id, "year": year, "tran_type": tran_type,
        "reported": reported,
        "rows": sorted(by_id.values(), key=lambda r: int(r["tran_id"])),
        "chains": chains,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--targets", nargs="+", required=True,
                        help="FILER:YEAR:TYPES, e.g. 3215:2006:C,E")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--max-minutes", type=float, default=30.0,
                        help="Stop starting new targets after this long")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s")

    targets = [t for text in args.targets for part in text.split()
               for t in parse_target(part)]
    previous = None
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text())
        except (OSError, ValueError) as exc:
            log.error("Existing %s is unreadable (%s); refusing to overwrite it",
                      args.output, exc)
            return 1

    from playwright.sync_api import sync_playwright

    collected: list[dict] = []
    failed: list[str] = []
    started = time.time()
    consecutive_failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,   # ORESTAR's F5 gate refuses headless browsers
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_context(user_agent=USER_AGENT,
                                   accept_downloads=False).new_page()
        token = _token(page)
        if token is None:
            log.error("Could not open ORESTAR's search page")
            browser.close()
            return 1
        for filer_id, year, tran_type in targets:
            label = f"{filer_id}:{year}:{tran_type}"
            if (time.time() - started) / 60 >= args.max_minutes:
                log.warning("Time budget reached before %s; stored targets are kept", label)
                failed.append(label)
                continue
            entry = collect_target(page, token, filer_id, year, tran_type)
            if entry is None:
                failed.append(label)
                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAILURE_ABORT:
                    log.warning("%d targets in a row refused; stopping", consecutive_failures)
                    break
                continue
            consecutive_failures = 0
            collected.append(entry)
        browser.close()

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = merge_targets(previous, collected, fetched_at)
    if collected:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(args.output)
    print(f"CHAINS_RESULT collected={len(collected)} failed={len(failed)} "
          f"stored_targets={len(payload['targets'])}")
    if failed:
        log.warning("Not collected: %s", " ".join(failed))
    return 0 if collected else 1


if __name__ == "__main__":
    sys.exit(main())
