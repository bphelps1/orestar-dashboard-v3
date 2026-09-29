#!/usr/bin/env python3
"""
refresh_leadership.py — Refresh Oregon legislative leadership roles.

Scrapes the four Oregon Legislature caucus pages for current leadership
positions and updates the Supabase leadership_roles table.

Sources (each has a different DOM structure):
  - House Democrats:  SharePoint list-view table (gridcells)
  - House Republicans: Rich-text <p> tags: "Name (R-City)—Role"
  - Senate Democrats:  Rich-text <ul>/<li>: <strong>Role:</strong> Name (City)
  - Senate Republicans: Rich-text <h2> headings: "Role:Name(R-City)"

Can be run:
  - Via GitHub Actions monthly cron (SUPABASE_URL / SUPABASE_SERVICE_KEY env vars)
  - Manually: python scraper/refresh_leadership.py --dry-run

Usage:
  python scraper/refresh_leadership.py [--dry-run]
"""

import argparse
import logging
import os
import re
import sys
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

HEADERS = {
    "User-Agent": "OrestarDashboard/1.0 (github.com/bphelps1/orestar-dashboard)"
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _strip_unicode(s):
    """Remove zero-width spaces and other unicode junk."""
    return re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)


def clean_name(raw):
    """Strip prefixes, party/district info, and whitespace from a legislator name."""
    name = _strip_unicode(raw).strip()
    name = re.sub(r"^(Rep\.|Sen\.|Representative|Senator)\s+", "", name)
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name)  # remove (R-City) etc.
    name = re.sub(r"\xa0", " ", name)  # non-breaking space
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _find_leadership_div(soup):
    """
    Find the ExternalClass div that contains leadership data.
    Oregon Legislature SharePoint pages have multiple ExternalClass divs —
    the first is typically contact info, the second has the leadership list.
    We score each div and return the best match.
    """
    divs = soup.find_all("div", class_=re.compile(r"ExternalClass"))
    best = None
    best_score = -1

    for div in divs:
        text = div.get_text().lower()
        score = 0
        # Role keywords
        if any(kw in text for kw in ["leader", "speaker", "president", "whip"]):
            score += 1
        # Structural elements that indicate a leadership list
        if div.find("ul") and div.find_all("li"):
            score += 3
        if div.find("h2"):
            score += 2
        # Many paragraphs (leadership entries)
        p_count = len(div.find_all("p"))
        if p_count > 5:
            score += 2
        elif p_count > 2:
            score += 1

        if score > best_score:
            best_score = score
            best = div

    return best if best else (divs[-1] if divs else soup)


def normalize_role(raw, chamber, party):
    """Normalize a scraped role title to canonical form."""
    r = _strip_unicode(raw).strip().rstrip(":").strip().lower()
    r = re.sub(r"\xa0", " ", r)

    prefix = chamber  # "House" or "Senate"
    # D is majority in current Oregon session
    majority = "Majority" if party == "D" else "Minority"

    if "speaker" in r and "pro" in r:
        return "House Speaker Pro Tem"
    if "speaker" in r:
        return "Speaker of the House"
    if "president" in r and "pro" in r:
        return "Senate President Pro Tem"
    if "president" in r:
        return "Senate President"

    if "republican leader" in r and "deputy" not in r:
        return f"{prefix} Minority Leader"
    if "majority leader" in r and "deputy" not in r and "assistant" not in r:
        return f"{prefix} Majority Leader"
    if "minority leader" in r and "deputy" not in r and "assistant" not in r:
        return f"{prefix} Minority Leader"

    if "deputy" in r:
        return f"{prefix} Deputy {majority} Leader"
    if "assistant" in r:
        return f"{prefix} Assistant {majority} Leader"
    if "floor manager" in r:
        return f"{prefix} {majority} Floor Manager"
    if "whip" in r:
        return f"{prefix} {majority} Whip"
    if "ex-officio" in r or "ex officio" in r:
        return f"{prefix} {majority} Ex-Officio"

    if "ways and means" in r:
        return "Ways and Means Co-Chair"
    if "revenue" in r and "chair" in r:
        return "Revenue Committee Chair"

    return raw.strip().rstrip(":")


def is_valid_name(name):
    """Check that a string looks like a person's name, not junk."""
    if not name or len(name) < 4 or " " not in name:
        return False
    if "@" in name or "Phone" in name or "Capitol" in name:
        return False
    if "oregonlegislature" in name.lower():
        return False
    if name.lower() in ("content", "details", "biography", "edit"):
        return False
    return bool(re.search(r"[A-Za-z]", name))


# ── Page-specific scrapers ─────────────────────────────────────────────────

def scrape_house_dems():
    """
    House Democrats: SharePoint list-view table.
    Columns: [Legislator link] [Role title] [Area]
    """
    url = "https://www.oregonlegislature.gov/housedemocrats/Pages/members.aspx"
    roles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tr in soup.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue

            link = cells[0].find("a")
            if not link:
                continue

            name = clean_name(link.get_text(strip=True))
            raw_role = _strip_unicode(cells[1].get_text(strip=True))
            district = cells[2].get_text(strip=True) if len(cells) > 2 else None

            if not raw_role or not is_valid_name(name):
                continue

            roles.append({
                "filer_name": name,
                "role_title": normalize_role(raw_role, "House", "D"),
                "chamber": "House",
                "party": "D",
                "district": district or None,
                "source": "scraped",
            })

    except requests.RequestException as e:
        log.warning("Failed to fetch House Democrats: %s", e)

    log.info("  House Democrats: %d roles", len(roles))
    return roles


def scrape_house_repubs():
    """
    House Republicans: <p> tags with pattern:
      "Representative Name (R-City)—Role Title"
    """
    url = "https://www.oregonlegislature.gov/houserepublicans/Pages/leaders.aspx"
    roles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        content = _find_leadership_div(soup)

        for p in content.find_all("p"):
            text = _strip_unicode(p.get_text(strip=True))
            text = re.sub(r"\xa0", " ", text)

            # Pattern: "Representative Name (R-City)—Role"
            m = re.match(
                r"(?:Representative\s+)?(.+?)\s*\(([RD])-([^)]+)\)\s*[—–\-]+\s*(.+)",
                text,
            )
            if m:
                name = clean_name(m.group(1))
                city = m.group(3).strip()
                raw_role = m.group(4).strip()

                if is_valid_name(name):
                    roles.append({
                        "filer_name": name,
                        "role_title": normalize_role(raw_role, "House", "R"),
                        "chamber": "House",
                        "party": "R",
                        "district": city or None,
                        "source": "scraped",
                    })

    except requests.RequestException as e:
        log.warning("Failed to fetch House Republicans: %s", e)

    log.info("  House Republicans: %d roles", len(roles))
    return roles


def scrape_senate_dems():
    """
    Senate Democrats: <ul>/<li> with pattern:
      <li><strong>Role Title:</strong> Name (City)</li>
    """
    url = "https://www.oregonlegislature.gov/senatedemocrats/Pages/leadership.aspx"
    roles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        content = _find_leadership_div(soup)

        for li in content.find_all("li"):
            strong = li.find(["strong", "b"])
            if not strong:
                continue

            raw_role = _strip_unicode(strong.get_text(strip=True))
            full_text = _strip_unicode(li.get_text(strip=True))
            full_text = re.sub(r"\xa0", " ", full_text)

            # Remove the role portion to get the name
            name_part = full_text.replace(raw_role, "", 1).strip()
            name_part = re.sub(r"^[\s:—–\-]+", "", name_part)

            # Extract city in parens
            city_match = re.search(r"\(([^)]+)\)", name_part)
            city = city_match.group(1) if city_match else None
            name = re.sub(r"\s*\([^)]*\)\s*", " ", name_part).strip()
            name = clean_name(name)

            if is_valid_name(name) and raw_role:
                roles.append({
                    "filer_name": name,
                    "role_title": normalize_role(raw_role, "Senate", "D"),
                    "chamber": "Senate",
                    "party": "D",
                    "district": city or None,
                    "source": "scraped",
                })

    except requests.RequestException as e:
        log.warning("Failed to fetch Senate Democrats: %s", e)

    log.info("  Senate Democrats: %d roles", len(roles))
    return roles


def scrape_senate_repubs():
    """
    Senate Republicans: <h2> headings with pattern:
      "Role Title:Name(R-City)"
    """
    url = "https://www.oregonlegislature.gov/senaterepublicans/Pages/leadership.aspx"
    roles = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        content = _find_leadership_div(soup)

        for h2 in content.find_all("h2"):
            text = _strip_unicode(h2.get_text(strip=True))
            text = re.sub(r"\xa0", " ", text)

            # Skip title headings without a colon
            if ":" not in text:
                continue

            # Pattern: "Role:Name(R-City)" or "Role: Name (R-City)"
            m = re.match(r"(.+?):\s*(.+?)\s*\(([RD])-([^)]+)\)", text)
            if m:
                raw_role = m.group(1).strip()
                name = clean_name(m.group(2))
                city = m.group(4).strip()

                if is_valid_name(name):
                    roles.append({
                        "filer_name": name,
                        "role_title": normalize_role(raw_role, "Senate", "R"),
                        "chamber": "Senate",
                        "party": "R",
                        "district": city or None,
                        "source": "scraped",
                    })
                continue

            # Fallback: "Role: Name (City)" without party letter
            m2 = re.match(r"(.+?):\s*(.+?)\s*\(([^)]+)\)", text)
            if m2:
                raw_role = m2.group(1).strip()
                name = clean_name(m2.group(2))
                city = m2.group(3).strip()

                if is_valid_name(name):
                    roles.append({
                        "filer_name": name,
                        "role_title": normalize_role(raw_role, "Senate", "R"),
                        "chamber": "Senate",
                        "party": "R",
                        "district": city or None,
                        "source": "scraped",
                    })

    except requests.RequestException as e:
        log.warning("Failed to fetch Senate Republicans: %s", e)

    log.info("  Senate Republicans: %d roles", len(roles))
    return roles


# ── Main scrape logic ──────────────────────────────────────────────────────

def scrape_leadership():
    """Scrape all four caucus leadership pages."""
    log.info("Scraping Oregon Legislature caucus pages…")

    all_roles = []
    all_roles.extend(scrape_house_dems())
    all_roles.extend(scrape_house_repubs())
    all_roles.extend(scrape_senate_dems())
    all_roles.extend(scrape_senate_repubs())

    # Deduplicate by (name, role)
    seen = set()
    deduped = []
    for r in all_roles:
        key = (r["filer_name"].lower(), r["role_title"].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    log.info("Total: %d unique leadership roles scraped", len(deduped))
    return deduped


# ── Supabase integration ──────────────────────────────────────────────────

def get_supabase_client():
    """Get Supabase client from environment variables."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables required")
        sys.exit(1)

    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        log.error("supabase package not installed. Run: pip install supabase")
        sys.exit(1)


def update_supabase(sb, roles):
    """Update leadership_roles table in Supabase."""
    today = date.today().isoformat()
    now = datetime.utcnow().isoformat()
    inserted = 0
    updated = 0

    for role in roles:
        existing = (
            sb.table("leadership_roles")
            .select("id")
            .eq("filer_name", role["filer_name"])
            .eq("role_title", role["role_title"])
            .is_("end_date", "null")
            .execute()
        )

        if existing.data:
            sb.table("leadership_roles").update({
                "updated_at": now,
                "party": role.get("party"),
                "district": role.get("district"),
            }).eq("id", existing.data[0]["id"]).execute()
            updated += 1
        else:
            sb.table("leadership_roles").insert({
                "filer_name": role["filer_name"],
                "role_title": role["role_title"],
                "chamber": role.get("chamber"),
                "party": role.get("party"),
                "district": role.get("district"),
                "effective_date": today,
                "source": "scraped",
            }).execute()
            inserted += 1

    sb.table("leadership_refresh_log").insert({
        "source": "github_action",
        "roles_updated": inserted,
        "notes": f"Processed {len(roles)} roles, {inserted} new, {updated} refreshed",
    }).execute()

    log.info("Leadership refresh: %d processed, %d new, %d refreshed",
             len(roles), inserted, updated)
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Refresh Oregon legislative leadership roles")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and print roles without writing to Supabase")
    args = parser.parse_args()

    log.info("Starting leadership role refresh…")
    roles = scrape_leadership()

    if args.dry_run:
        log.info("\n=== DRY RUN — %d roles found ===", len(roles))
        for r in roles:
            log.info("  %-35s  %-40s  %-7s  %s  %s",
                     r["filer_name"], r["role_title"], r["chamber"], r["party"],
                     r.get("district") or "")
        return

    sb = get_supabase_client()

    if roles:
        update_supabase(sb, roles)
    else:
        sb.table("leadership_refresh_log").insert({
            "source": "github_action",
            "roles_updated": 0,
            "notes": "No roles scraped — page structure may have changed",
        }).execute()
        log.warning("No roles scraped! Oregon Legislature website structure may have changed.")


if __name__ == "__main__":
    main()
