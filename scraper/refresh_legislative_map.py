"""
refresh_legislative_map.py — rebuild the Races map from the candidate roster.

Reads the ballot roster (data/candidate_filings.json) and rebuilds only the
`legislative_map` key of the activity_snapshot blob in dashboard_cache.

Deliberately sources committees and cycle totals from the LIVE database
(dashboard_cache.filer_index + filer_detail) rather than data/aggregated/*.
Those local files lag the database, and generating the map from a stale copy
silently dropped 27 candidates from the map once already.

Usage:
    python scraper/refresh_legislative_map.py
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s
from generate_activity_snapshot import (
    build_legislative_map_from_filings, build_name_order_ref, hist_name_key,
    norm_district, speaking_name,
)

# Current district era only. The GeoJSON is TIGER 2024, so a pre-2022 race
# drawn on it would place old results on boundaries that no longer exist.
HISTORY_CYCLES = [2024, 2022]
CHAMBER_OF = {"State Representative": "house", "State Senator": "senate"}

ROOT = Path(__file__).parent.parent
FILINGS = ROOT / "data" / "candidate_filings.json"
CYCLE_START = "2025-01"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def cycle_sum(timeline, year) -> float:
    """Contributions in the cycle ending `year` (Dec of year-2 … Nov of year)."""
    start, end = f"{year - 2}-12", f"{year}-11"
    return round(sum(e.get("contributions", 0) for e in (timeline or [])
                     if start <= (e.get("month") or "") <= end), 2)


def build_district_history(cur, index, timelines) -> dict:
    """Per-district results for past cycles: who won, by how much, money raised.

    Margins come from race_margins and are always correct. Only the dollar
    figure depends on matching a candidate to a committee, so each entry
    records matched/total — an incomplete total is labelled rather than
    presented as fact.
    """
    # Three widening pools. A committee records its CURRENT office, so someone
    # who has since moved on (Elizabeth Steiner Hayward now reads "State
    # Treasurer") is missing from the district and office pools for the race
    # they actually ran in — hence the final all-committees tier.
    by_d, by_off, everyone = {}, {}, []
    for r in index:
        if r.get("committee_type") != "Candidate Committee":
            continue
        everyone.append(r)
        od = (r.get("office_district") or "").strip()
        if od and "," in od:
            off, dist = od.split(",", 1)
            by_d.setdefault(f"{off.strip()}|{norm_district(dist)}", []).append(r)
        off = (r.get("office") or "").strip()
        if off:
            by_off.setdefault(off, []).append(r)

    def best(pool, key):
        """Exact token-set match, or one name's tokens a strict subset of the
        other's (covers middle names and suffixes: 'floyd prozanski' ⊂ 'floyd
        prozanski jr').

        Deliberately NOT fuzzy string similarity. Character-level closeness
        matched 'Williamson James' to 'Jim Williams' — the nickname map made
        both start "james", and williams/williamson is a small edit distance
        but a different person. Set logic can't make that mistake.
        """
        kt = set(key.split())
        if not kt:
            return 0, None
        best_who, best_score = None, 0
        for f in pool:
            cn = hist_name_key(f.get("candidate_name"))
            if not cn:
                continue
            ct = set(cn.split())
            if ct == kt:
                return 100, f
            if (kt < ct or ct < kt) and len(kt & ct) >= 2 and best_score < 95:
                best_who, best_score = f, 95
        return best_score, best_who

    cur.execute("""
        select year, office_normalized, district, candidate, party_code, votes, won
        from election_results
        where election_type = 'General' and not is_measure
          and office_normalized in ('State Representative','State Senator')
          and year = any(%s)
          and lower(candidate) not in ('misc.','misc','write-in')
        order by year desc, district, votes desc
    """, (HISTORY_CYCLES,))
    contests = {}
    for yr, off, dist, cand, party, votes, won in cur.fetchall():
        contests.setdefault((yr, off, norm_district(dist)), []).append(
            {"candidate": cand, "party": party, "votes": votes, "won": won})

    cur.execute("""
        select year, office_normalized, district, winner, winner_party,
               margin_pts, unopposed
        from race_margins where year = any(%s) and era = '2022-'
    """, (HISTORY_CYCLES,))
    margins = {(y, o, norm_district(d)): (w, wp, m, u)
               for y, o, d, w, wp, m, u in cur.fetchall()}

    # Results file names as "Surname First"; the panel shows them as spoken.
    name_refs = build_name_order_ref(index)

    def say(n):
        return speaking_name(n, name_refs) if n else n

    out = {"house": {}, "senate": {}}
    stats = {y: [0, 0] for y in HISTORY_CYCLES}
    for (yr, off, dist), cands in contests.items():
        chamber = CHAMBER_OF.get(off)
        num = re.match(r"(\d+)", dist or "")
        if not chamber or not num:
            continue
        raised, matched = 0.0, 0
        rows = []
        for cd in cands:
            key = hist_name_key(cd["candidate"])
            sc, who = best(by_d.get(f"{off}|{dist}", []), key)
            ok = sc >= 95
            if not ok:                       # same office, any district
                sc, who = best(by_off.get(off, []), key)
                ok = sc >= 95
            if not ok:
                # Any candidate committee. Wider pool, so require a stricter
                # score — this tier exists for people who changed office.
                sc, who = best(everyone, key)
                ok = sc >= 100
            amt = 0.0
            if who and ok:
                amt = cycle_sum(timelines.get(who.get("slug")), yr)
                raised += amt
                matched += 1
            rows.append({"candidate": say(cd["candidate"]), "party": cd["party"],
                         "votes": cd["votes"], "won": cd["won"],
                         "slug": who.get("slug") if (who and ok) else None,
                         "raised": round(amt, 2)})
            stats[yr][1] += 1
            stats[yr][0] += 1 if (who and ok) else 0
        w, wp, m, unopp = margins.get((yr, off, dist), (None, None, None, None))
        out[chamber].setdefault(str(int(num.group(1))), []).append({
            "cycle": yr, "raised": round(raised, 2),
            "winner": say(w), "winner_party": wp,
            "margin_pts": float(m) if m is not None else None,
            "unopposed": bool(unopp),
            "matched": matched, "total": len(cands),
            "candidates": sorted(rows, key=lambda x: -(x["votes"] or 0)),
        })
    for ch in out:
        for d in out[ch]:
            out[ch][d].sort(key=lambda e: -e["cycle"])
    for y in HISTORY_CYCLES:
        got, tot = stats[y]
        log.info("  %s: matched %d/%d candidates to committees (%.0f%%)",
                 y, got, tot, 100 * got / max(tot, 1))
    return out


def main() -> int:
    if not FILINGS.exists():
        log.error("No %s — run fetch_candidates.py first", FILINGS)
        return 1
    filings = json.loads(FILINGS.read_text())
    if not filings.get("candidates"):
        log.error("Roster is empty — refusing to blank the Races map")
        return 1

    conn = s._connect()
    cur = conn.cursor()

    cur.execute("select data from dashboard_cache where key='filer_index'")
    row = cur.fetchone()
    if not row:
        log.error("filer_index not in dashboard_cache")
        return 1
    index = row[0]

    # cycle totals straight from the live per-filer timelines
    # Every candidate committee, not just current ones — a 2022 race often
    # involves people who have since left or changed office.
    slugs = [r["slug"] for r in index
             if r.get("committee_type") == "Candidate Committee" and r.get("slug")]
    cur.execute("select slug, detail->'timeline' from filer_detail where slug = any(%s)",
                (slugs,))
    # Keep the raw timelines: the current-cycle metrics and the historical
    # per-cycle sums both read them, and the cursor can only be drained once.
    timeline_rows = cur.fetchall()
    metrics = [
        {"slug": slug,
         "raised_cycle": round(sum(e.get("contributions", 0) for e in (tl or [])
                                   if (e.get("month") or "") >= CYCLE_START), 2)}
        for slug, tl in timeline_rows
    ]
    log.info("Loaded %d candidate committees, %d timelines", len(slugs), len(metrics))

    lm = build_legislative_map_from_filings(filings, metrics, index, cur)

    log.info("Building per-district history for %s…", HISTORY_CYCLES)
    tl_by_slug = {slug: tl for slug, tl in timeline_rows}
    lm["district_history"] = build_district_history(cur, index, tl_by_slug)

    cur.execute("select data from dashboard_cache where key='activity_snapshot'")
    snap_row = cur.fetchone()
    if not snap_row:
        log.error("activity_snapshot not in dashboard_cache")
        return 1
    snapshot = snap_row[0]
    snapshot["legislative_map"] = lm
    cur.execute(
        "update dashboard_cache set data = %s::jsonb, updated_at = now() "
        "where key = 'activity_snapshot'",
        (json.dumps(snapshot, default=str),),
    )
    conn.commit()
    conn.close()

    log.info("Races map refreshed — %s | %d districts (house) + %d (senate) | %s",
             lm.get("election"), len(lm["house"]), len(lm["senate"]), lm["match_stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
