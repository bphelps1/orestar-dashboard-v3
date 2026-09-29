#!/usr/bin/env python3
"""Generate activity_snapshot.json from existing aggregated data.

Reads filer_index.json, per-filer detail files, and recent_transactions.json
to compute recent fundraising metrics grouped by office tier, growth rates,
and top donors. Designed to run quickly from already-aggregated data.

Can be run standalone or called from process.py.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

ROOT = Path(__file__).parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
FILERS_DIR = AGG_DIR / "filers"

STATEWIDE_OFFICES = {
    "Governor", "Secretary of State", "Attorney General",
    "State Treasurer", "Superintendent of Public Instruction",
    "Commissioner of the Bureau of Labor and Industries",
}
LEGISLATIVE_OFFICES = {"State Senator", "State Representative"}


def office_tier(office: str) -> str | None:
    if not office:
        return None
    if office in STATEWIDE_OFFICES:
        return "statewide"
    if office in LEGISLATIVE_OFFICES:
        return "legislative"
    return "local"


def months_in_range(start_ym: str, end_ym: str) -> list[str]:
    """Return list of YYYY-MM strings from start to end inclusive."""
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def sum_timeline_months(timeline: list[dict], target_months: set[str]) -> float:
    """Sum contributions from timeline entries matching target months."""
    total = 0.0
    for entry in timeline:
        if entry.get("month") in target_months:
            total += entry.get("contributions", 0)
    return total


def aggregate_top_donors_from_filers(
    filer_details: list[dict],
    target_months: set[str],
) -> list[dict]:
    """Aggregate top donors across all filer detail files for given months.

    Uses by_contributor_type_by_month data from per-filer detail files,
    which captures the full month's data (not limited to recent transactions).
    Returns top 10 donors sorted by total amount.
    """
    donor_totals: dict[str, float] = defaultdict(float)
    donor_filers: dict[str, set] = defaultdict(set)
    donor_filer_amounts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    donor_filer_slugs: dict[str, dict[str, str]] = {}  # donor_name → {filer_name: slug}

    for detail in filer_details:
        filer_name = detail.get("name", "")
        filer_slug = detail.get("slug", "")
        by_month = detail.get("by_contributor_type_by_month", {})
        for month, type_rows in by_month.items():
            if month not in target_months:
                continue
            for tr in type_rows:
                for d in tr.get("top_donors", []):
                    name = d.get("name", "")
                    if not name or name.startswith("Miscellaneous Cash"):
                        continue
                    amt = d.get("total", 0)
                    donor_totals[name] += amt
                    donor_filers[name].add(filer_name)
                    donor_filer_amounts[name][filer_name] += amt
                    if name not in donor_filer_slugs:
                        donor_filer_slugs[name] = {}
                    donor_filer_slugs[name][filer_name] = filer_slug

    # Sort by total, take top 10
    ranked = sorted(donor_totals.items(), key=lambda x: -x[1])[:10]
    return [
        {
            "name": name,
            "total": round(total, 2),
            "committees": len(donor_filers[name]),
            "details": sorted(
                [{"filer": fn, "slug": donor_filer_slugs[name].get(fn, ""), "amount": round(amt, 2)}
                 for fn, amt in donor_filer_amounts[name].items()],
                key=lambda x: -x["amount"]
            )[:10],
        }
        for name, total in ranked
    ]


def build_races(filer_metrics: list[dict], index: list[dict]) -> list[dict]:
    """Group candidates by office_district to find contested races.

    ONLY includes candidates whose ORESTAR 'election' field (scraped from
    the Statement of Organization 'Election/Office' field) contains a 2026
    election. Candidates without this field or from previous cycles are
    excluded entirely — no fallbacks or guessing.
    """
    import re as _re
    metric_by_slug = {fm["slug"]: fm for fm in filer_metrics}

    # Show races for the current year and next year. This covers:
    # - Even years (2026): statewide + legislative elections
    # - Odd years (2027): local/municipal elections
    # The window automatically advances as time passes.
    now = datetime.now()
    valid_election_years = {now.year, now.year + 1}

    races = defaultdict(list)
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        od = row.get("office_district", "")
        if not od:
            continue

        # STRICT: only include candidates with an upcoming election
        # field from ORESTAR (e.g., "2026 Primary Election")
        election = row.get("election", "")
        if not election:
            continue  # No election data scraped yet — skip
        yr_match = _re.match(r"(\d{4})", election)
        if not yr_match:
            continue
        election_year = int(yr_match.group(1))
        if election_year not in valid_election_years:
            continue

        slug = row.get("slug", "")
        fm = metric_by_slug.get(slug, {})
        raised = fm.get("raised_cycle", 0)
        races[od].append({
            "slug": slug,
            "name": row.get("name", ""),
            "party": row.get("party", ""),
            "candidate_name": row.get("candidate_name", ""),
            "raised_cycle": round(raised, 2),
            "cash_on_hand": row.get("cash_on_hand", 0),
        })

    contested = []
    for office, candidates in races.items():
        if len(candidates) < 2:
            continue
        candidates.sort(key=lambda c: -c["raised_cycle"])
        total = sum(c["raised_cycle"] for c in candidates)
        # Determine if statewide
        base_office = office.split(",")[0].strip()
        is_statewide = base_office in STATEWIDE_OFFICES
        contested.append({
            "office": office,
            "total_raised": round(total, 2),
            "is_statewide": is_statewide,
            "candidates": candidates,  # All candidates (frontend shows top 5 + expand)
        })

    # Sort: statewide first (by total raised), then non-statewide (by total raised)
    statewide = sorted([r for r in contested if r["is_statewide"]], key=lambda r: -r["total_raised"])
    other = sorted([r for r in contested if not r["is_statewide"]], key=lambda r: -r["total_raised"])[:5]
    return statewide + other


CANDIDATE_FILINGS = ROOT / "data" / "candidate_filings.json"


CANDIDATE_OVERRIDES = ROOT / "scraper" / "candidate_overrides.json"


def _load_candidate_overrides(cur=None) -> dict:
    """{"ballot name|district": filer_id} — manual candidate→committee links.

    For pairings no matcher should be asked to infer (a committee named nothing
    like its candidate, or two plausible people in one district).

    Two sources, both human decisions: the checked-in JSON file, and the
    candidate_committee_links table written by the admin page. The table wins
    on conflict — it is the one a reviewer can correct without a commit.
    A 'none' row records "this candidate really has no committee" and maps to
    None, which is not the same as an absent key: it means reviewed, not
    unreviewed, and keeps the candidate off the review queue.
    """
    out = {}
    if CANDIDATE_OVERRIDES.exists():
        try:
            raw = json.loads(CANDIDATE_OVERRIDES.read_text())
            out = {str(k).strip().lower(): v for k, v in raw.items()
                   if not str(k).startswith("_")}
        except Exception as e:                   # noqa: BLE001
            print(f"  WARNING: could not read {CANDIDATE_OVERRIDES.name}: {e}")

    if cur is not None:
        try:
            cur.execute("select candidate_key, filer_id, decision "
                        "from candidate_committee_links")
            rows = cur.fetchall()
            for key, filer_id, decision in rows:
                out[str(key).strip().lower()] = filer_id if decision == "link" else None
            if rows:
                print(f"  Loaded {len(rows)} reviewed candidate link(s) from the database")
        except Exception as e:                   # noqa: BLE001
            # The map is still correct without them, just less complete.
            print(f"  WARNING: could not read candidate_committee_links: {e}")
    return out


def _override_key(cand: dict) -> str:
    race = cand.get("office_district") or cand.get("office") or ""
    return f"{cand.get('ballot_name', '')}|{race}".strip().lower()


# Honorifics and post-nominals. ORESTAR records them inline in the committee's
# candidate name ("Sue R Rieke Smith, Dr", "Floyd F. Prozanski, Jr."), where
# they land in the LAST token — the one the matcher treats as the surname. That
# alone kept Sue Rieke Smith off HD 26 despite an otherwise exact name and a
# matching district. Dropped from both sides so the comparison stays symmetric.
NAME_NOISE = {
    "dr", "mr", "mrs", "ms", "miss", "rev", "hon", "sen", "rep", "gov",
    "jr", "sr", "ii", "iii", "iv", "vi", "md", "phd", "dds", "esq", "cpa", "rn",
}


def _cand_key(name: str) -> str:
    """Normalize a personal name for matching: lowercase, strip punctuation,
    drop single-letter middle initials ('Kevin L Mannix' ~ 'Kevin Mannix') and
    honorifics/suffixes ('Floyd Prozanski Jr' ~ 'Floyd Prozanski')."""
    import re as _re
    s = _re.sub(r"[^a-z ]", " ", (name or "").lower())
    return " ".join(t for t in s.split() if len(t) > 1 and t not in NAME_NOISE)


# ── Historical candidate matching ────────────────────────────────────────────
#
# Deliberately separate from the current-cycle matcher above. Official results
# print names "Last First" ("Gomberg David") while committees record them
# "First Last" ("David Gomberg"), so the surname-anchored rule used for the
# live roster matches nothing here (measured: 0%). This compares sorted token
# sets instead, which is looser — hence its confinement to the historical
# layer, where a miss only understates a past dollar figure and never
# re-points a current candidate.
#
# Nicknames are the largest remaining gap: "Wagner Rob" vs "Robert Wagner",
# "Patterson Deb" vs "Deborah". Folding them in took matching from 76% to 84%.
NICKNAMES = {
    "rob": "robert", "bob": "robert", "bobby": "robert",
    "deb": "deborah", "debbie": "deborah",
    "rich": "richard", "rick": "richard", "dick": "richard",
    "bill": "william", "will": "william", "billy": "william",
    "jim": "james", "jimmy": "james", "mike": "michael",
    "dan": "daniel", "danny": "daniel", "dave": "david",
    "tom": "thomas", "tommy": "thomas", "chris": "christopher",
    "kim": "kimberly", "ben": "benjamin", "ed": "edward", "eddie": "edward",
    "jeff": "jeffrey", "sue": "susan", "susie": "susan",
    "liz": "elizabeth", "beth": "elizabeth", "matt": "matthew",
    "nick": "nicholas", "tony": "anthony", "pat": "patricia",
    "patty": "patricia", "greg": "gregory", "steve": "steven",
    "stephen": "steven", "joe": "joseph", "tim": "timothy",
    "ron": "ronald", "don": "donald", "ken": "kenneth",
    "andy": "andrew", "drew": "andrew", "sam": "samuel",
    "charlie": "charles", "chuck": "charles",
    "kate": "katherine", "kathy": "katherine", "cathy": "catherine",
    "jen": "jennifer", "jenny": "jennifer", "becky": "rebecca",
    "peggy": "margaret", "meg": "margaret",
}


def hist_name_key(name: str) -> str:
    """Order-independent, nickname-normalised name key for historical matching."""
    toks = [NICKNAMES.get(t, t) for t in _cand_key(name).split()]
    return " ".join(sorted(t for t in toks if len(t) > 1))


def norm_district(d: str) -> str:
    """'18th District (2 year term' -> '18th District'.

    Results carry parentheticals for special/short terms; the committee index
    does not, so the raw string never matches.
    """
    import re as _re
    return _re.sub(r"\s*\(.*$", "", (d or "")).strip()


def _nick(key: str) -> str:
    """Nickname-normalised token string ('bob niemeyer' -> 'robert niemeyer')."""
    return " ".join(NICKNAMES.get(t, t) for t in key.split())


# ── Speaking-order names ─────────────────────────────────────────────────────
#
# Election results file names as "Surname(s) First Middle" — "Munoz Lesly M",
# "Boshart Davis Shelly". Printing those as-is reads as the wrong person.
#
# No positional rule can undo it, because "Boshart Davis Shelly" (compound
# surname) and "Newgard Michael Steven" (middle name) have identical shapes.
# The committee records in filer_index write names in speaking order, so they
# are used to identify WHICH token is the given name; the rest follows, since
# everything before it is surname and everything after is middle name.
#
# Note this reference is looser than the one that attaches money: borrowing a
# name's ORDER from a same-named different person is still correct, so a
# false positive here is harmless in a way a false money link never is.

def build_name_order_ref(index: list[dict]) -> list[tuple]:
    """(token set, given name) per candidate committee, for name-order lookup."""
    refs = []
    for r in index:
        if r.get("committee_type") != "Candidate Committee":
            continue
        k = _nick(_cand_key(r.get("candidate_name")))
        if k:
            refs.append((set(k.split()), k.split()[0]))
    return refs


def speaking_name(raw: str, refs: list[tuple]) -> str:
    """'Munoz Lesly M' -> 'Lesly Munoz'; 'Boshart Davis Shelly' -> 'Shelly Boshart Davis'."""
    import re as _re
    toks = [t for t in (raw or "").split()
            if len(_re.sub(r"[^A-Za-z]", "", t)) != 1]     # drop middle initials
    if len(toks) < 2:
        return (raw or "").strip()
    norm = [_nick(_cand_key(t)) or t.lower() for t in toks]
    tset = set(norm)

    # Best reference: exact token set beats partial, more overlap beats less,
    # a tie goes to the record that differs least, and a remaining tie to the
    # earlier token. That last rule is not arbitrary — the source orders names
    # "Surname First Middle", so the given name precedes the middle one. Two
    # unrelated Newgards ("Steve Newgard", "Michael S Newgard") both match
    # "Newgard Michael Steven" on every other measure; position settles it.
    given, best = None, None
    for rs, gn in refs:
        if gn not in tset:
            continue
        overlap = len(tset & rs)
        if overlap < 2:
            continue
        rank = (rs == tset, overlap, -len(rs ^ tset), -norm.index(gn))
        if best is None or rank > best:
            given, best = gn, rank

    # Surname sits first in the source, so absent a reference take token 0 as
    # the surname — right for every two-token name and for "Last First Middle".
    i = norm.index(given) if given else 0
    if not given:
        return " ".join(toks[1:] + toks[:1])
    return " ".join([toks[i]] + toks[i + 1:] + toks[:i])


def _match_in_pool(key: str, pool: list[dict]) -> tuple[dict | None, str]:
    """Best committee for a ballot name, within one race's committees.

    The pool is already the candidates filed for THIS office and district —
    usually one to four committees — so the district has done most of the
    identification before a name is compared at all. That corroboration is what
    licenses the looser tiers below; none of them would be safe index-wide.

    Ordered strongest first, and each tier says which real failure it exists
    for. Everything that misses every tier is reported as unmatched rather than
    guessed at.
    """
    from rapidfuzz import fuzz

    rows = [(r, _cand_key(r.get("candidate_name"))) for r in pool]
    rows = [(r, cn) for r, cn in rows if cn]

    # 1. Exact, then exact once nicknames are folded in.
    #    "Bob Niemeyer" on the ballot, "Robert H Niemeyer, III" on the committee.
    for r, cn in rows:
        if cn == key:
            return r, "exact"
    nk = _nick(key)
    for r, cn in rows:
        if _nick(cn) == nk:
            return r, "nickname"

    # 2. One name's tokens contained in the other's, sharing at least two.
    #    Added and maiden names: the ballot reads "Courtney Neron Misslin" while
    #    the committee is still "Courtney Neron", and "Sue R Rieke Smith" vs
    #    "Sue R Rieke Smith, Dr". A surname check cannot see these — the token
    #    it calls the surname is precisely the one that differs — so this tier
    #    deliberately runs before any surname rule.
    kt = set(key.split())
    for r, cn in rows:
        ct = set(cn.split())
        if (kt <= ct or ct <= kt) and len(kt & ct) >= 2:
            return r, "subset"

    # 3. Surname agrees and the names are close. The surname guard belongs
    #    HERE and nowhere earlier: it exists to stop fuzzy similarity linking
    #    people who merely share a first name ("Michael Summers" once matched
    #    "Michael Sipe"), which is a fuzzy-tier failure, not a subset one.
    best, best_sc = None, 0
    for r, cn in rows:
        if cn.split()[-1] != key.split()[-1]:
            continue
        sc = fuzz.token_sort_ratio(key, cn)
        if sc > best_sc:
            best, best_sc = r, sc
    if best is not None and best_sc >= 80:
        return best, "fuzzy"

    # 4. Surname agrees and the first names begin with the same letter.
    #    Diminutives no nickname table will ever cover: "Ricki Ruiz" for
    #    "Ricardo Ruiz", "Jules Walters" for "Julianna Walters". Weak on its
    #    own — it is only offered because the district already agreed.
    for r, cn in rows:
        if cn.split()[-1] == key.split()[-1] and cn[0] == key[0]:
            return r, "district+initial"

    return (best, "fuzzy") if best is not None and best_sc >= 80 else (None, "none")


def build_legislative_map_from_filings(filings: dict, filer_metrics: list[dict],
                                       index: list[dict], cur=None) -> dict:
    """Race-map data driven by the ORESTAR candidate FILING record.

    The roster is who is actually on the ballot; committees supply only the
    money. Committees self-report "Election/Office" on their Statement of
    Organization and update it inconsistently — Emerson Levy's committee still
    says "2024 General Election" while she is nominated for HD 53 in 2026 — so
    that field must not decide who appears in a race.

    Candidates with no committee are kept with raised_cycle 0: "nominated but
    not fundraising" is real information, and dropping them would recreate the
    incomplete picture this replaced.
    """
    from rapidfuzz import fuzz

    metric_by_slug = {fm["slug"]: fm for fm in filer_metrics}
    # candidate committees grouped by their office_district string, and by bare
    # office for statewide races (which have no district to match on)
    by_district: dict[str, list] = defaultdict(list)
    by_office: dict[str, list] = defaultdict(list)
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        od = (row.get("office_district") or "").strip()
        if od:
            by_district[od].append(row)
        off = (row.get("office") or "").strip()
        if off:
            by_office[off].append(row)

    out = {"house": {}, "senate": {}, "statewide": {}, "cycle_start": "2025-01",
           "election": filings.get("election", ""),
           "scraped": filings.get("scraped", "")}
    stats = {"exact": 0, "subset": 0, "fuzzy": 0, "override": 0, "none": 0}
    overrides = _load_candidate_overrides(cur)
    unmatched: list[str] = []

    for cand in filings.get("candidates", []):
        chamber = cand["chamber"]
        is_statewide = chamber == "statewide"
        # Statewide races are keyed by office name; districts by number.
        district = cand.get("office", "") if is_statewide else str(cand["district"])
        key = _cand_key(cand["ballot_name"])

        # Constrain the pool: same district, or same office when statewide.
        # Still a small pool (Governor is the largest at 69), so fuzzy is safe.
        pool = (by_office.get(cand.get("office", ""), []) if is_statewide
                else by_district.get(cand.get("office_district", ""), []))
        best, score, method = None, 0, "none"

        # 1. Manual override wins outright — for pairings no algorithm should
        #    be asked to guess (see scraper/candidate_overrides.json).
        ok = _override_key(cand)
        reviewed = ok in overrides
        ov_filer = overrides.get(ok)
        if ov_filer:
            forced = next((r for r in pool if str(r.get("filer_id")) == str(ov_filer)), None)
            if forced is None:   # override may point outside the district pool
                forced = next((r for r in index
                               if str(r.get("filer_id")) == str(ov_filer)), None)
            if forced is not None:
                best, score, method = forced, 100, "override"

        if best is None:
            best, method = _match_in_pool(key, pool)
        if reviewed and method == "none":
            method = "reviewed-none"       # a human confirmed there is none
        stats[method] = stats.get(method, 0) + 1
        if method == "none":
            # Structured, not a display string: this is the admin page's review
            # queue, and it needs the same key the override lookup uses.
            unmatched.append({
                "key": ok,
                "ballot_name": cand["ballot_name"],
                "office_district": cand.get("office_district") or cand.get("office") or "",
                "party": cand.get("party", ""),
                "chamber": chamber,
                "district": district,
            })

        fm = metric_by_slug.get(best.get("slug", ""), {}) if best else {}
        entry = out[chamber].setdefault(district, (
            {"office": district, "total_raised": 0.0, "candidates": []}
            if is_statewide else
            {"district": int(district), "total_raised": 0.0, "candidates": []}))
        row_out = {
            "slug": best.get("slug") if best else None,
            "name": best.get("name") if best else None,       # committee name
            "candidate_name": cand["ballot_name"],
            "party": cand.get("party", ""),
            "election": cand.get("election", ""),
            "raised_cycle": round(fm.get("raised_cycle", 0), 2) if best else 0.0,
            "cash_on_hand": best.get("cash_on_hand", 0) if best else 0,
            "match_method": method,
        }
        entry["candidates"].append(row_out)
        entry["total_raised"] = round(entry["total_raised"] + row_out["raised_cycle"], 2)

    for chamber in ("house", "senate", "statewide"):
        for entry in out[chamber].values():
            entry["candidates"].sort(key=lambda c: -c["raised_cycle"])
    out["match_stats"] = stats
    out["matched_count"] = sum(v for k, v in stats.items() if k != "none")
    out["unmatched"] = unmatched          # surfaced in /admin for manual linking
    matched = ", ".join(f"{stats[m]} {m}" for m in
                        ("exact", "nickname", "subset", "fuzzy", "district+initial", "override")
                        if stats.get(m))
    print(f"  Legislative map: {len(filings.get('candidates', []))} filed candidates — "
          f"{matched}, {stats['none']} without a committee")
    # List them: a candidate silently sitting at $0 is indistinguishable from
    # one who genuinely hasn't raised anything.
    for u in unmatched:
        print(f"    unmatched: {u['ballot_name']} ({u['office_district']})")
    return out


def build_legislative_map(filer_metrics: list[dict], index: list[dict]) -> dict:
    """Fallback race-map builder, used only when candidate_filings.json is absent.

    Gates on the committee's self-reported election field, which is exactly the
    unreliable signal the filing-driven builder above replaces — kept so a
    missing/failed scrape degrades instead of blanking the map.
    """
    import re as _re
    metric_by_slug = {fm["slug"]: fm for fm in filer_metrics}
    now = datetime.now()
    valid_election_years = {now.year, now.year + 1}
    chambers = {"State Representative": "house", "State Senator": "senate"}
    dist_pat = _re.compile(r"(\d+)\w*\s+District", _re.I)

    out = {"house": {}, "senate": {}, "cycle_start": "2025-01",
           "election_years": sorted(valid_election_years)}
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        chamber = chambers.get(row.get("office", ""))
        if not chamber:
            continue
        m = dist_pat.search(row.get("office_district", "") or "")
        if not m:
            continue
        election = row.get("election", "")
        yr = _re.match(r"(\d{4})", election or "")
        if not yr or int(yr.group(1)) not in valid_election_years:
            continue
        district = str(int(m.group(1)))
        fm = metric_by_slug.get(row.get("slug", ""), {})
        entry = out[chamber].setdefault(district, {
            "district": int(district), "total_raised": 0.0, "candidates": []})
        cand = {
            "slug": row.get("slug", ""),
            "name": row.get("name", ""),
            "party": row.get("party", ""),
            "candidate_name": row.get("candidate_name", ""),
            "election": election,
            "raised_cycle": round(fm.get("raised_cycle", 0), 2),
            "cash_on_hand": row.get("cash_on_hand", 0),
        }
        entry["candidates"].append(cand)
        entry["total_raised"] = round(entry["total_raised"] + cand["raised_cycle"], 2)
    for chamber in ("house", "senate"):
        for entry in out[chamber].values():
            entry["candidates"].sort(key=lambda c: -c["raised_cycle"])
    return out


def generate(agg_dir: Path = AGG_DIR, filers_dir: Path = FILERS_DIR,
             index: list | None = None, details: list | None = None) -> dict:
    """Generate the activity snapshot data structure.

    `index` / `details` let callers inject data straight from Postgres instead
    of reading data/aggregated/. That matters: the local copies lag the
    database, and the Fundraising Pulse windows are the *last 30/90 days* — so
    generating from stale files silently produces empty lanes rather than an
    error. See scraper/refresh_activity_snapshot.py.
    """
    if index is None:
        with open(agg_dir / "filer_index.json") as f:
            index = json.load(f)

    # Determine time periods
    now = datetime.now()
    current_ym = now.strftime("%Y-%m")

    # Last 3 months (current + 2 prior)
    recent_months = []
    y, m = now.year, now.month
    for _ in range(3):
        recent_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    recent_3m = set(recent_months)

    # Prior 3 months (the 3 months before that)
    prior_months = []
    for _ in range(3):
        prior_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    prior_3m = set(prior_months)

    # Last 1 month
    recent_1m = {current_ym}
    y1, m1 = now.year, now.month - 1
    if m1 < 1:
        m1 = 12
        y1 -= 1
    recent_1m.add(f"{y1:04d}-{m1:02d}")

    # Prior 1 month (for 30d growth)
    prior_1m_months = set()
    for _ in range(2):
        m1 -= 1
        if m1 < 1:
            m1 = 12
            y1 -= 1
        prior_1m_months.add(f"{y1:04d}-{m1:02d}")

    # Cycle: Jan 2025 onward (post-2024 election)
    cycle_start = "2025-01"
    cycle_months = set(months_in_range(cycle_start, current_ym))

    # Prior cycle equivalent (same number of months before cycle start)
    cycle_len = len(cycle_months)
    prior_cycle_months = []
    y_pc, m_pc = 2025, 0
    if m_pc < 1:
        m_pc = 12
        y_pc -= 1
    for _ in range(cycle_len):
        prior_cycle_months.append(f"{y_pc:04d}-{m_pc:02d}")
        m_pc -= 1
        if m_pc < 1:
            m_pc = 12
            y_pc -= 1
    prior_cycle = set(prior_cycle_months)

    # Filter to candidate committees with offices (for Raising/Momentum lanes)
    candidates = []
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        tier = office_tier(row.get("office", ""))
        if not tier:
            continue
        candidates.append((row, tier))

    print(f"Processing {len(candidates)} candidate committees...")

    # Load ALL filer detail files for donor aggregation (Biggest Donors
    # lane should capture donations to PACs, party committees, etc.)
    all_filer_slugs = [row["slug"] for row in index]
    if details is not None:
        all_filer_details = details
        print(f"Using {len(all_filer_details)} injected filer details")
    else:
        all_filer_details = []
        print(f"Loading {len(all_filer_slugs)} filer detail files for donor aggregation...")
        for slug in all_filer_slugs:
            detail_path = filers_dir / f"{slug}.json"
            if not detail_path.exists():
                continue
            with open(detail_path) as f:
                all_filer_details.append(json.load(f))

    # Build candidate-specific detail lookup for timeline metrics
    candidate_details = {}  # slug -> detail
    for detail in all_filer_details:
        candidate_details[detail.get("slug", "")] = detail

    # Collect metrics per candidate filer
    filer_metrics = []
    for row, tier in candidates:
        slug = row["slug"]
        detail = candidate_details.get(slug)
        if not detail:
            continue

        timeline = detail.get("timeline", [])

        # Compute period totals
        raised_1m = sum_timeline_months(timeline, recent_1m)
        raised_prior_1m = sum_timeline_months(timeline, prior_1m_months)
        raised_3m = sum_timeline_months(timeline, recent_3m)
        raised_prior_3m = sum_timeline_months(timeline, prior_3m)
        raised_cycle = sum_timeline_months(timeline, cycle_months)
        raised_prior_cycle = sum_timeline_months(timeline, prior_cycle)

        # Growth rates
        def growth(current, prior):
            if prior <= 0:
                return 999 if current > 0 else 0
            return round((current - prior) / prior * 100)

        growth_1m = growth(raised_1m, raised_prior_1m)
        growth_3m = growth(raised_3m, raised_prior_3m)
        growth_cycle = growth(raised_cycle, raised_prior_cycle)

        filer_metrics.append({
            "slug": row["slug"],
            "name": row["name"],
            "office": row.get("office", ""),
            "party": row.get("party", ""),
            "tier": tier,
            "raised_1m": round(raised_1m, 2),
            "raised_3m": round(raised_3m, 2),
            "raised_cycle": round(raised_cycle, 2),
            "growth_1m": growth_1m,
            "growth_3m": growth_3m,
            "growth_cycle": growth_cycle,
            # Prior-window totals: the growth denominator. Kept so the Momentum
            # lane can require a real base — a % off near-zero is noise.
            "prior_1m": round(raised_prior_1m, 2),
            "prior_3m": round(raised_prior_3m, 2),
            "prior_cycle": round(raised_prior_cycle, 2),
            "cash_on_hand": row.get("cash_on_hand", 0),
            "total_in": row.get("total_in", 0),
        })

    # Collect metrics for non-candidate committees (PACs, party committees, etc.)
    committee_metrics = []
    for row in index:
        if row.get("committee_type") == "Candidate Committee":
            continue
        slug = row["slug"]
        detail = candidate_details.get(slug)
        if not detail:
            continue
        timeline = detail.get("timeline", [])
        raised_1m = sum_timeline_months(timeline, recent_1m)
        raised_prior_1m = sum_timeline_months(timeline, prior_1m_months)
        raised_3m = sum_timeline_months(timeline, recent_3m)
        raised_prior_3m = sum_timeline_months(timeline, prior_3m)
        raised_cycle = sum_timeline_months(timeline, cycle_months)
        raised_prior_cycle = sum_timeline_months(timeline, prior_cycle)
        if raised_1m <= 0 and raised_3m <= 0 and raised_cycle <= 0:
            continue

        def _cgrowth(current, prior):
            if prior <= 0:
                return 999 if current > 0 else 0
            return round((current - prior) / prior * 100)

        committee_metrics.append({
            "slug": slug,
            "name": row["name"],
            "committee_type": row.get("committee_type", ""),
            "raised_1m": round(raised_1m, 2),
            "raised_3m": round(raised_3m, 2),
            "raised_cycle": round(raised_cycle, 2),
            "growth_1m": _cgrowth(raised_1m, raised_prior_1m),
            "growth_3m": _cgrowth(raised_3m, raised_prior_3m),
            "growth_cycle": _cgrowth(raised_cycle, raised_prior_cycle),
            "prior_1m": round(raised_prior_1m, 2),
            "prior_3m": round(raised_prior_3m, 2),
            "prior_cycle": round(raised_prior_cycle, 2),
        })

    # Aggregate top donors from per-filer monthly data (full, not limited)
    print(f"Aggregating top donors from {len(all_filer_details)} filer detail files...")
    top_donors_30d = aggregate_top_donors_from_filers(all_filer_details, recent_1m)
    top_donors_90d = aggregate_top_donors_from_filers(all_filer_details, recent_3m)
    top_donors_cycle = aggregate_top_donors_from_filers(all_filer_details, cycle_months)

    # Build output per period
    def build_period(metric_key, growth_key, top_donors, min_raised=5000,
                     prior_key=None):
        by_tier = {"statewide": [], "legislative": [], "local": [], "committees": []}
        growth_candidates = []

        for fm in filer_metrics:
            raised = fm[metric_key]
            if raised <= 0:
                continue

            by_tier[fm["tier"]].append({
                "slug": fm["slug"],
                "name": fm["name"],
                "office": fm["office"],
                "party": fm["party"],
                "raised": raised,
                "cash_on_hand": fm["cash_on_hand"],
            })

            # Growth: include all actively raising candidates
            if raised >= min_raised:
                growth_candidates.append({
                    "slug": fm["slug"],
                    "name": fm["name"],
                    "office": fm["office"],
                    "party": fm["party"],
                    "tier": fm["tier"],
                    "raised": raised,
                    "growth_pct": fm[growth_key],
                    "prior": fm.get(prior_key, 0) if prior_key else 0,
                    "is_new": fm[growth_key] == 999,
                })

        # Add non-candidate committees (raising + growth)
        for cm in committee_metrics:
            raised = cm[metric_key]
            if raised <= 0:
                continue
            by_tier["committees"].append({
                "slug": cm["slug"],
                "name": cm["name"],
                "office": cm.get("committee_type", ""),
                "party": "",
                "raised": raised,
            })
            # Committee growth
            if raised >= min_raised:
                growth_candidates.append({
                    "slug": cm["slug"],
                    "name": cm["name"],
                    "office": cm.get("committee_type", ""),
                    "party": "",
                    "tier": "committees",
                    "raised": raised,
                    "growth_pct": cm[growth_key],
                    "prior": cm.get(prior_key, 0) if prior_key else 0,
                    "is_new": cm[growth_key] == 999,
                })

        # Sort each tier by raised amount, take top entries
        for tier in by_tier:
            by_tier[tier].sort(key=lambda x: -x["raised"])
            by_tier[tier] = by_tier[tier][:3]

        # Top growth: existing filers with positive growth (no new entrants).
        # The prior window must ALSO clear min_raised — otherwise the percentage
        # is arithmetic noise off a near-zero base ($0.06 -> $1,050 reads as
        # "+1,750,033%") and those artifacts crowd out real momentum.
        top_growth_list = sorted(
            [c for c in growth_candidates
             if not c.get("is_new")
             and c["growth_pct"] > 0
             and c.get("prior", 0) >= min_raised],
            key=lambda x: -x["growth_pct"],
        )[:12]

        return {
            "by_office_tier": by_tier,
            "top_growth": top_growth_list,
            "top_donors": top_donors,
        }

    snapshot = {
        "generated": now.isoformat(),
        "periods": {
            "30d": {
                "label": "Last 30 Days",
                "months": sorted(recent_1m),
                **build_period("raised_1m", "growth_1m", top_donors_30d, min_raised=500, prior_key="prior_1m"),
            },
            "90d": {
                "label": "Last 90 Days",
                "months": sorted(recent_3m),
                **build_period("raised_3m", "growth_3m", top_donors_90d, min_raised=1000, prior_key="prior_3m"),
            },
            "cycle": {
                "label": "This Cycle",
                "months": sorted(cycle_months),
                **build_period("raised_cycle", "growth_cycle", top_donors_cycle, min_raised=5000, prior_key="prior_cycle"),
            },
        },
        "meta": {
            "total_candidates": len(candidates),
            "statewide_count": sum(1 for _, t in candidates if t == "statewide"),
            "legislative_count": sum(1 for _, t in candidates if t == "legislative"),
            "local_count": sum(1 for _, t in candidates if t == "local"),
        },
    }

    snapshot["races"] = build_races(filer_metrics, index)

    # Prefer the ORESTAR candidate filing roster; fall back to the committee's
    # self-reported election only if the scrape output is missing.
    if CANDIDATE_FILINGS.exists():
        filings = json.loads(CANDIDATE_FILINGS.read_text())
        snapshot["legislative_map"] = build_legislative_map_from_filings(
            filings, filer_metrics, index)
    else:
        print("  WARNING: no candidate_filings.json — falling back to "
              "committee-reported elections (roster may be incomplete)")
        snapshot["legislative_map"] = build_legislative_map(filer_metrics, index)

    return snapshot


def main():
    snapshot = generate()
    out_path = AGG_DIR / "activity_snapshot.json"
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Wrote {out_path}")
    print(f"  Candidates: {snapshot['meta']['total_candidates']}")
    print(f"  Statewide: {snapshot['meta']['statewide_count']}")
    print(f"  Legislative: {snapshot['meta']['legislative_count']}")
    print(f"  Local: {snapshot['meta']['local_count']}")

    for period_key, period_data in snapshot["periods"].items():
        print(f"\n  {period_data['label']}:")
        for tier in ["statewide", "legislative", "local"]:
            entries = period_data["by_office_tier"][tier]
            if entries:
                top = entries[0]
                print(f"    {tier}: {top['name']} — ${top['raised']:,.0f}")
        growth = period_data["top_growth"]
        if growth:
            top = growth[0]
            print(f"    Top growth: {top['name']} — +{top['growth_pct']}%")
        donors = period_data["top_donors"]
        if donors:
            top = donors[0]
            print(f"    Top donor: {top['name']} — ${top['total']:,.0f} ({top['committees']} committees)")


if __name__ == "__main__":
    main()
