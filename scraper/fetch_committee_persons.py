#!/usr/bin/env python3
"""
fetch_committee_persons.py — scrape ORESTAR "Persons Associated with Committee".

Every committee's Statement of Organization links to a page naming its
treasurer, correspondence recipient and directors, with addresses, phones,
emails and (for directors) occupation/employer.

Navigation mirrors fetch_filer_metadata.py (search by committee id → SOO page)
and then follows the "Persons Associated with Committee" link. ORESTAR's F5
shield challenges background fetch() calls, so each step is a real page
navigation in a headed browser.

Output: data/committee_persons.json   { filer_id: {committee_name, statement_from,
          status, persons: [{role, seq, name, address, phone, email,
          occupation, employer, effective_from, effective_to}], ts} }

Which filers: by default every donor committee in lobby_donor_pool
(non-individual donors since 2021), skipping ones scraped in the last
--max-age-days. --filer-ids overrides.

Usage:
    python scraper/fetch_committee_persons.py --filer-ids 161 33
    python scraper/fetch_committee_persons.py --max-filers 400 --sync
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
import supabase_sync  # noqa: E402
from fetch_filer_metadata import (  # noqa: E402
    BATCH_FAILURE_ABORT, NAV_WAIT, PAGE_RENDER_WAIT, SEARCH_URL, USER_AGENT,
)

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "committee_persons.json"
FILER_DELAY = 0.5

_ROLE_HEADINGS = {
    "treasurer name": "treasurer",
    "correspondence recipient": "correspondence",
    "director name": "director",
    "candidate name": "candidate",
    "chief petitioner name": "chief_petitioner",
    "multi-committee director name": "director",
}


def _cell_lines(td) -> list[str]:
    """A table cell's text split on <br> (source newlines are just layout)."""
    if td is None:
        return []
    for br in td.find_all("br"):
        br.replace_with("\x00")
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in td.get_text().split("\x00"))
    return [re.sub(r"\s+,", ",", ln) for ln in lines if ln]


def _contact_pairs(td) -> dict[str, str]:
    """The nested Work Phone / Home Phone / Fax / Email Address table."""
    out = {}
    if td is None:
        return out
    for tr in td.select("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) >= 2:
            label = re.sub(r"\s+", " ", cells[0].get_text()).strip().rstrip(":").lower()
            out[label] = re.sub(r"\s+", " ", cells[1].get_text()).strip()
    return out


def parse_persons_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    result = {"committee_name": "", "statement_from": "", "persons": []}

    for tr in soup.select("tr"):
        cells = tr.find_all("td", recursive=False)
        for i, td in enumerate(cells[:-1]):
            label = td.get_text(" ", strip=True).rstrip(":")
            if label == "Name" and not result["committee_name"]:
                result["committee_name"] = cells[i + 1].get_text(" ", strip=True)
            elif label == "Statement Effective From" and not result["statement_from"]:
                result["statement_from"] = re.sub(r"\s+", " ", cells[i + 1].get_text(" ", strip=True))

    seq: dict[str, int] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr", recursive=False) or table.select(":scope > tbody > tr")
        role = None
        for tr in rows:
            cells = tr.find_all("td", recursive=False)
            if not cells:
                continue
            h5 = cells[0].find("h5")
            if h5:
                role = _ROLE_HEADINGS.get(h5.get_text(" ", strip=True).lower())
                continue
            if role is None:
                continue
            name = " ".join(_cell_lines(cells[0]))
            if not name:
                continue
            p = {"role": role, "name": name, "address": "", "phone": "", "email": "",
                 "occupation": "", "employer": "", "effective_from": "", "effective_to": ""}
            if role in ("director", "chief_petitioner") and len(cells) >= 6:
                p["effective_from"] = cells[1].get_text(" ", strip=True)
                p["effective_to"] = cells[2].get_text(" ", strip=True)
                p["address"] = ", ".join(_cell_lines(cells[3]))
                p["phone"] = " ".join(_cell_lines(cells[4]))
                occ = _cell_lines(cells[5])
                p["occupation"] = occ[0] if occ else ""
                p["employer"] = occ[1] if len(occ) > 1 else ""
            elif len(cells) >= 3:
                p["address"] = ", ".join(_cell_lines(cells[1]))
                contact = _contact_pairs(cells[2])
                p["phone"] = contact.get("work phone") or contact.get("home phone") or ""
                p["email"] = (contact.get("email address") or "").lower()
            else:
                continue
            p["seq"] = seq.get(role, 0)
            seq[role] = p["seq"] + 1
            result["persons"].append(p)
    return result


def _open_committee(page, filer_id: str, first_load: bool) -> str | None:
    """Search by committee id → SOO page. Returns SOO text, or None on failure."""
    for discontinued in (False, True):
        page.goto(SEARCH_URL, timeout=60_000)
        time.sleep(PAGE_RENDER_WAIT if first_load else NAV_WAIT)
        first_load = False
        field = page.locator('input[name="committeeId"]')
        if field.count() == 0:
            log.warning("no committeeId field (bot challenge?) for %s", filer_id)
            return None
        field.fill(filer_id)
        box = page.locator('input[name="discontinuedSOO"][type="checkbox"]')
        if box.count() and box.first.is_checked() != discontinued:
            box.first.set_checked(discontinued)
        page.locator('input[type="submit"][value="Submit"]').first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            log.warning("timeout loading SOO for %s", filer_id)
            return None
        text = page.inner_text("body")
        if "0 found for the above search criteria" not in text:
            return text
    return ""   # searched cleanly; no committee record


def scrape_one(page, filer_id: str, first_load: bool) -> dict | None:
    text = _open_committee(page, filer_id, first_load)
    if text is None:
        return None
    if text == "":
        return {"status": "not_found", "persons": [], "committee_name": "", "statement_from": ""}
    link = page.locator('a:has-text("Persons Associated with Committee")')
    if link.count() == 0:
        return {"status": "no_link", "persons": [], "committee_name": "", "statement_from": ""}
    link.first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        log.warning("timeout loading persons page for %s", filer_id)
        return None
    html = page.content()
    if "Persons Associated with Committee" not in html:
        return None
    parsed = parse_persons_page(html)
    parsed["status"] = "ok"
    return parsed


def default_filer_ids() -> list[str]:
    conn = supabase_sync._connect()
    cur = conn.cursor()
    cur.execute("""select committee_id from lobby_donor_pool
                   where committee_id is not null and committee_id <> ''
                   order by total_since_2021 desc""")
    ids = [r[0] for r in cur.fetchall()]
    conn.close()
    return ids


def scraped_times() -> dict[str, float]:
    if not supabase_sync.sync_enabled():
        return {}
    conn = supabase_sync._connect()
    cur = conn.cursor()
    cur.execute("select filer_id, extract(epoch from scraped_at) from committee_persons_scrapes")
    out = {fid: float(ts) for fid, ts in cur.fetchall()}
    conn.close()
    return out


def sync(cache: dict, filer_ids) -> None:
    """Replace the listed committees' contacts with what was just scraped."""
    from psycopg2.extras import execute_values
    ids = [f for f in dict.fromkeys(filer_ids) if f in cache]
    if not ids:
        return
    persons, scrapes = [], []
    for fid in ids:
        entry = cache[fid]
        for p in entry["persons"]:
            persons.append((fid, p["role"], p["seq"], p["name"], p["address"] or None, p["phone"] or None,
                            p["email"] or None, p["occupation"] or None, p["employer"] or None,
                            p["effective_from"] or None, p["effective_to"] or None))
        scrapes.append((fid, entry.get("committee_name") or None, len(entry["persons"]),
                        entry.get("statement_from") or None, entry["status"], entry["ts"]))
    conn = supabase_sync._connect()
    cur = conn.cursor()
    cur.execute("delete from committee_persons where filer_id = any(%s)", (ids,))
    if persons:
        execute_values(cur, """insert into committee_persons (filer_id, role, seq, name, address, phone,
                                 email, occupation, employer, effective_from, effective_to) values %s""",
                       persons, page_size=500)
    execute_values(cur, """insert into committee_persons_scrapes (filer_id, committee_name, persons,
                             statement_from, status, scraped_at)
                           values %s
                           on conflict (filer_id) do update set committee_name = excluded.committee_name,
                             persons = excluded.persons, statement_from = excluded.statement_from,
                             status = excluded.status, scraped_at = excluded.scraped_at""",
                   scrapes, template="(%s, %s, %s, %s, %s, to_timestamp(%s))", page_size=500)
    conn.commit()
    conn.close()
    log.info("synced contacts for %d committees (%d persons)", len(ids), len(persons))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--filer-ids", nargs="+")
    ap.add_argument("--max-filers", type=int, default=0)
    ap.add_argument("--max-age-days", type=float, default=30)
    ap.add_argument("--sync", action="store_true", help="write results to Supabase")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    if args.filer_ids:
        todo = args.filer_ids
    else:
        cutoff = time.time() - args.max_age_days * 86400
        # CI starts without the local cache; the scrape log in Supabase is
        # the durable record of what was read and when.
        last = {f: e.get("ts", 0) for f, e in cache.items()}
        for f, ts in scraped_times().items():
            last[f] = max(last.get(f, 0), ts)
        todo = [f for f in default_filer_ids() if last.get(f, 0) < cutoff]
    if args.max_filers:
        todo = todo[:args.max_filers]
    log.info("committees to scrape: %d", len(todo))
    if not todo:
        return

    from playwright.sync_api import sync_playwright
    done, errors = [], 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_context(user_agent=USER_AGENT, no_viewport=True).new_page()
        for i, fid in enumerate(todo):
            try:
                res = scrape_one(page, fid, first_load=(i == 0))
            except Exception as e:          # one bad page must not end the batch
                log.error("filer %s: %s", fid, e)
                res = None
            if res is None:
                errors += 1
                log.warning("[%d/%d] %s: failed", i + 1, len(todo), fid)
            else:
                res["ts"] = time.time()
                cache[fid] = res
                done.append(fid)
                log.info("[%d/%d] %s %s: %d persons (%s)", i + 1, len(todo), fid,
                         res.get("committee_name", "")[:40], len(res["persons"]), res["status"])
            if (i + 1) % 25 == 0:
                CACHE_PATH.write_text(json.dumps(cache, indent=1))
                if args.sync and supabase_sync.sync_enabled():
                    sync(cache, done[-25:])
            time.sleep(FILER_DELAY)
        browser.close()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=1))
    if args.sync and supabase_sync.sync_enabled():
        sync(cache, done)
    log.info("done: %d scraped, %d failed", len(done), errors)
    attempted = len(done) + errors
    if attempted and errors / attempted >= BATCH_FAILURE_ABORT:
        log.error("%d of %d committees failed — is ORESTAR refusing this scraper?", errors, attempted)
        sys.exit(1)


if __name__ == "__main__":
    main()
