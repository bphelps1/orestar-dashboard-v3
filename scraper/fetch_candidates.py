"""
fetch_candidates.py — scrape ORESTAR's Candidate Filing Search for the ballot roster.

Why this exists: committees self-report "Election/Office" on their Statement of
Organization and update it inconsistently, so a committee can sit in a current
race while still claiming a past election (Emerson Levy's committee still says
"2024 General Election" while she is nominated for HD 53 in 2026). Driving the
Races map off that field silently drops candidates. The candidate *filing*
record is authoritative for who is actually on the ballot.

Notes on the site:
  • CFSearchPage.do is behind F5 bot defense. Plain HTTP and HEADLESS Chromium
    are both blocked ("Please Contact Us"); a HEADED browser works. CI runs this
    under `xvfb-run`, exactly like fetch.py.
  • cfElection / cfOfficeGrp are AJAX-populated, so selections must be made in
    order (year -> election -> office) with waits between.
  • The named submit button is replaced during those re-renders, so we submit
    the form directly.
  • One query per chamber returns every district — no need to iterate districts.

Usage:
    python scraper/fetch_candidates.py                # current year, auto-advance
    python scraper/fetch_candidates.py --year 2026
    python scraper/fetch_candidates.py --election-id 1451   # force (e.g. primary)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "candidate_filings.json"

SEARCH_URL = "https://secure.sos.state.or.us/orestar/CFSearchPage.do"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGE_RENDER_WAIT = 7_000     # ms — F5 challenge + form JS
AJAX_WAIT = 3_000            # ms — dependent dropdown population
RESULTS_WAIT = 10_000        # ms — search submit

CHAMBERS = {"SR": "house", "SS": "senate"}
# Statewide offices have no district — their `Candidate Office` is just the
# office name ("Governor"). Superintendent of Public Instruction is
# deliberately not included.
STATEWIDE_OFFICES = {
    "GOV": "Governor",
    "SOS": "Secretary of State",
    "AG": "Attorney General",
    "TR": "State Treasurer",
    "BOLI": "Commissioner of the Bureau of Labor and Industries",
}
ALL_OFFICES = list(CHAMBERS) + list(STATEWIDE_OFFICES)
FILING_NOMINATED = "NOM"
DISTRICT_PAT = re.compile(r"(\d+)\w*\s+District", re.I)
FORM_ATTEMPTS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _open(playwright):
    """Headed browser — headless is blocked by F5."""
    browser = playwright.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    page = browser.new_context(user_agent=USER_AGENT, no_viewport=True,
                               accept_downloads=True).new_page()
    page.goto(SEARCH_URL, timeout=90_000)
    page.wait_for_timeout(PAGE_RENDER_WAIT)
    if page.locator("select[name=cfyearActive]").count() == 0:
        raise RuntimeError(
            f"Filing search form did not load (title={page.title()!r}). "
            "F5 likely blocked the browser — headless mode is always blocked; "
            "CI must run this under xvfb-run."
        )
    return browser, page


def oregon_primary_date(year: int) -> date:
    """Third Tuesday in May (ORS 254.056)."""
    d = date(year, 5, 1)
    d += timedelta(days=(1 - d.weekday()) % 7)   # first Tuesday
    return d + timedelta(days=14)                # third Tuesday


def elections_for_year(page, year: str) -> list[tuple[str, str]]:
    """[(election_id, label)] ordered by which election is currently live.

    The switch point is the primary date, not the size of the field or whether
    nominees exist:
      • The primary has MORE candidates than the general (186 vs 139 in 2026),
        because several candidates compete per party — so "bigger roster" would
        wrongly keep showing a finished primary.
      • Unopposed candidates can be "Automatically nominated to General
        Election" before the primary is held, so "has nominees" leaks early.
    Before the primary is held the primary field is the live race; after it, the
    general is. Whichever is preferred, the caller still falls back to the other
    if it returns nothing.
    """
    page.select_option("select[name=cfyearActive]", year)
    page.wait_for_timeout(AJAX_WAIT)
    opts = page.evaluate(
        """() => [...document.querySelector('select[name=cfElection]').options]
             .map(o => [o.value, o.text.trim()]).filter(([v]) => v)"""
    )
    primary_over = date.today() >= oregon_primary_date(int(year))
    want = "general" if primary_over else "primary"
    log.info("Oregon %s primary: %s — %s, preferring the %s election",
             year, oregon_primary_date(int(year)),
             "held" if primary_over else "not yet held", want)
    opts.sort(key=lambda kv: (0 if want in kv[1].lower() else 1, kv[1]))
    return [(v, t) for v, t in opts]


def available_offices(page, year: str, election_id: str) -> set[str]:
    """Office codes ORESTAR offers for this election.

    The dropdown lists only offices actually on that ballot. Oregon's statewide
    offices are staggered: Governor is up in 2026, while Secretary of State,
    Attorney General and Treasurer were elected in 2024 and don't return until
    2028 — so they are simply absent from the 2026 list. Querying them anyway
    just times out against an option that doesn't exist, which previously looked
    like scraper flakiness. Enumerating instead keeps this correct in any cycle
    without a code change.
    """
    page.goto(SEARCH_URL, timeout=90_000)
    page.wait_for_timeout(PAGE_RENDER_WAIT)
    page.select_option("select[name=cfyearActive]", year, timeout=20_000)
    page.wait_for_timeout(AJAX_WAIT)
    page.select_option("select[name=cfElection]", election_id, timeout=20_000)
    page.wait_for_timeout(AJAX_WAIT)
    codes = page.evaluate(
        """() => [...document.querySelector('select[name=cfOffice]').options]
                   .map(o => (o.value || '').trim()).filter(Boolean)"""
    )
    return set(codes)


def _download_export(page) -> list[list[str]]:
    """Click Export and parse the workbook — returns rows as lists of strings.

    The results page caps the HTML table at 50 rows per page; the export has no
    such cap, so this is the only complete source.
    """
    import pandas as pd

    link = page.locator("a:has-text('Export')").first
    if link.count() == 0:
        log.warning("No Export link on the results page")
        return []
    import tempfile
    with page.expect_download(timeout=60_000) as dl_info:
        link.click()
    # Playwright's temp file has no extension, so pandas can't infer an engine.
    # ORESTAR serves legacy BIFF .xls here (OLE2), which needs xlrd.
    dest = Path(tempfile.gettempdir()) / "orestar_cf_export.xls"
    dl_info.value.save_as(dest)

    try:
        df = pd.read_excel(dest, dtype=str)
    except Exception as e:
        log.debug("read_excel failed (%s) — trying HTML table", e)
        try:
            tables = pd.read_html(dest)
        except Exception:
            tables = []
        if not tables:
            log.warning("Could not parse the export workbook")
            return []
        df = tables[0].astype(str)

    df = df.fillna("")
    # Find the header row (export sometimes carries title rows above it)
    cols = [str(c).strip().lower() for c in df.columns]
    if "ballot name" not in " ".join(cols):
        for i in range(min(6, len(df))):
            if any("ballot name" in str(v).strip().lower() for v in df.iloc[i]):
                df.columns = [str(v).strip() for v in df.iloc[i]]
                df = df.iloc[i + 1:]
                break
    return df


def _run_search(page, year: str, election_id: str, office: str) -> None:
    """Drive the search form once: year -> election -> office -> submit.

    Retried by the caller: running several searches back to back destabilises
    the form and `select_option` starts timing out roughly every other query.
    Reloading the page and re-selecting clears it.
    """
    page.goto(SEARCH_URL, timeout=90_000)          # fresh form per query
    page.wait_for_timeout(PAGE_RENDER_WAIT)
    page.select_option("select[name=cfyearActive]", year, timeout=20_000)
    page.wait_for_timeout(AJAX_WAIT)
    page.select_option("select[name=cfElection]", election_id, timeout=20_000)
    page.wait_for_timeout(AJAX_WAIT)
    page.select_option("select[name=cfOffice]", office, timeout=20_000)
    page.wait_for_timeout(AJAX_WAIT)
    # The named submit button is swapped out by the AJAX re-renders.
    page.evaluate("() => document.forms[0].submit()")
    page.wait_for_timeout(RESULTS_WAIT)


def scrape_office(page, year: str, election_id: str, office: str) -> list[dict]:
    """Ballot candidates for one office (a chamber or a statewide office).

    Deliberately does NOT filter by filing type at the query level. "Nominated"
    only exists after a primary is decided — 2026 Primary filings are all method
    "Fee" — so filtering on NOM would return an empty roster for the whole
    pre-primary phase. Instead we take everything and let the caller prefer
    nominees when they exist.
    """
    is_statewide = office in STATEWIDE_OFFICES

    last_err = None
    for attempt in range(1, FORM_ATTEMPTS + 1):
        try:
            _run_search(page, year, election_id, office)
            last_err = None
            break
        except Exception as e:                      # noqa: BLE001 - retry any form flakiness
            last_err = e
            log.warning("  %s: form attempt %d/%d failed (%s)", office, attempt,
                        FORM_ATTEMPTS, str(e).split("\n")[0][:70])
            page.wait_for_timeout(3_000 * attempt)
    if last_err is not None:
        raise last_err

    # The HTML table caps at "Maximum of 50 records ... in a page", which
    # silently truncated the roster (Emerson Levy and 8 others were lost). The
    # Excel export returns the complete set, the same way fetch.py pulls
    # transactions.
    df = _download_export(page)
    if df is None or not len(df):
        return []

    def col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_name = col("Cand Ballot Name Txt", "Ballot Name")
    c_off = col("Candidate Office", "Office")
    c_type = col("Filetype Descr", "Filing Method")
    c_party = col("Party Descr", "Party")
    c_filed = col("Filed Date")
    c_qlf = col("Qlf Ind", "Qualified")
    if not (c_name and c_off):
        log.warning("Export missing expected columns: %s", list(df.columns)[:8])
        return []

    # A candidate can be cross-nominated (Emerson Levy appears as both
    # Nominated/Democrat and Minor Party/Independent). Collapse to one entry
    # per person per race, keeping every party they appear under.
    merged: dict[tuple, dict] = {}
    for _, r in df.iterrows():
        ballot = str(r[c_name]).strip()
        office_txt = str(r[c_off]).strip()
        ftype = str(r[c_type]).strip() if c_type else ""
        if not ballot or not office_txt:
            continue
        if ftype.lower() == "write in":     # not on the printed ballot
            continue

        if is_statewide:
            # No district: the race IS the office.
            district = None
            race_key = STATEWIDE_OFFICES[office]
            chamber = "statewide"
        else:
            m = DISTRICT_PAT.search(office_txt)
            if not m:
                log.warning("No district parsed from %r (%s) — skipped", office_txt, ballot)
                continue
            district = int(m.group(1))
            race_key = district
            chamber = CHAMBERS[office]

        key = (ballot.lower(), race_key)
        party = str(r[c_party]).strip() if c_party else ""
        if key in merged:
            if party and party not in merged[key]["parties"]:
                merged[key]["parties"].append(party)
            continue
        merged[key] = {
            "ballot_name": ballot,
            "party": party,
            "parties": [party] if party else [],
            "chamber": chamber,
            "district": district,
            "office": STATEWIDE_OFFICES[office] if is_statewide else CHAMBERS[office],
            "office_district": office_txt,   # matches filer_index.office_district
            "election": str(r[col("Election Txt") or c_off]).strip(),
            "filing_method": ftype,
            "filing_date": str(r[c_filed]).strip() if c_filed else "",
            "qualified": str(r[c_qlf]).strip() if c_qlf else "",
        }
    return list(merged.values())


def scrape(year: str | None = None, election_id: str | None = None) -> dict:
    year = year or str(datetime.now().year)
    with sync_playwright() as pw:
        browser, page = _open(pw)
        try:
            elections = elections_for_year(page, year)
            if not elections:
                raise RuntimeError(f"No elections listed for {year}")
            log.info("Elections for %s: %s", year, [t for _, t in elections])

            # Auto-advance: prefer the General; fall back to the Primary if it
            # has no nominees yet (nominations post after the primary).
            tries = ([(election_id, next((t for v, t in elections if v == election_id),
                                         election_id))]
                     if election_id else elections)

            for eid, label in tries:
                log.info("Trying election %s (%s)", label, eid)
                # Only query offices this ballot actually has — see
                # available_offices(). Statewide offices are staggered across
                # cycles, so a missing one is correct, not an error.
                offered = available_offices(page, year, eid)
                to_scrape = [o for o in ALL_OFFICES if o in offered]
                skipped = [o for o in ALL_OFFICES if o not in offered]
                if skipped:
                    log.info("  not on this ballot (skipped): %s",
                             [STATEWIDE_OFFICES.get(o, o) for o in skipped])

                candidates, per_chamber = [], {}
                for office in to_scrape:
                    try:
                        rows = scrape_office(page, year, eid, office)
                    except Exception as e:          # noqa: BLE001
                        if office in CHAMBERS:
                            raise                   # a chamber failing is fatal
                        log.warning("  %s: giving up after retries (%s) — continuing",
                                    office, str(e).split("\n")[0][:60])
                        per_chamber[office] = 0
                        continue
                    # Everything filed for this election is on its ballot:
                    # "Nominated" (major-party nominees) AND "Minor Party".
                    # Filtering to Nominated alone would drop minor-party
                    # candidates; write-ins are already excluded in parsing.
                    per_chamber[office] = len(rows)
                    candidates.extend(rows)
                    types = Counter(r["filing_method"] for r in rows)
                    log.info("  %s: %d candidates %s", office, len(rows), dict(types))
                if candidates:
                    return {
                        "election": label,
                        "election_id": eid,
                        "year": year,
                        "scraped": datetime.now(timezone.utc)
                                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "counts": per_chamber,
                        "candidates": candidates,
                    }
                log.warning("No nominated candidates for %s — trying next election", label)
            raise RuntimeError(
                f"No nominated candidates found for any {year} election. "
                "Refusing to write an empty roster (it would blank the Races map)."
            )
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year")
    ap.add_argument("--election-id", help="force a specific election (skips auto-advance)")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    data = scrape(args.year, args.election_id)

    # A CHAMBER returning zero is a scrape failure — the legislature always has
    # a ballot. A STATEWIDE office legitimately can be zero (not every office is
    # up every cycle), so it only warns.
    missing = [o for o, n in data["counts"].items() if n == 0 and o in CHAMBERS]
    if missing:
        log.error("Chamber(s) returned zero candidates: %s — not writing output", missing)
        return 1
    empty_statewide = [o for o, n in data["counts"].items()
                       if n == 0 and o in STATEWIDE_OFFICES]
    if empty_statewide:
        log.warning("No candidates for statewide office(s): %s (not on this ballot?)",
                    [STATEWIDE_OFFICES[o] for o in empty_statewide])

    Path(args.out).write_text(json.dumps(data, indent=2))
    log.info("Wrote %s — %s, %d candidates (%s)", args.out, data["election"],
             len(data["candidates"]), data["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
