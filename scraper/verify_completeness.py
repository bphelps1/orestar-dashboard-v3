#!/usr/bin/env python3
"""
verify_completeness.py — prove, or disprove, that a fetch actually got everything.

Every other check in this pipeline compares our data against itself: row counts,
fetch-log coverage, per-year sums. Those can all agree while the data is wrong,
because they share the same blind spot — anything never fetched is invisible to
all of them. That is how ~207 missing expenditure rows for one committee stayed
hidden behind a fetch log that looked fully covered.

This compares against ORESTAR instead. Its results page prints the number of
matching records and, unlike the Excel export, that number is NOT capped:

    58366 records found for the above search criteria.
    A maximum 5000 records are displayed.

fetch.py captures that figure for every window it requests (data/record_counts.json).
Holding it next to what actually landed in the database gives a per-window answer
to "did we get it all", which no amount of internal consistency can provide.

Usage:
    python scraper/verify_completeness.py                 # every recorded window
    python scraper/verify_completeness.py --min-gap 50    # only material shortfalls
    python scraper/verify_completeness.py --json out.json # machine-readable
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s

DATA_DIR = Path(__file__).parent.parent / "data"
COUNTS_PATH = DATA_DIR / "record_counts.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def _held(cur, tran_type, start, end, amt_from, amt_to, payee_prefix, date_field,
          filer_id=None) -> int:
    """How many rows we hold for exactly the window ORESTAR was asked about.

    The filters must mirror the search precisely — a verifier that counts a
    slightly different set reports differences that are its own fault, which is
    worse than no verifier at all.
    """
    # A filer-targeted window is always searched by transaction date — the
    # filer backfill fills cneSearchTranStartDate, never the filed-date pair.
    # Counting those against filed_date would compare two different sets and
    # blame the difference on missing rows.
    col = "tran_date" if (date_field == "tran" or filer_id) else "filed_date"
    sql = [f"select count(*) from transactions where {col} between %s and %s"]
    args: list = [start, end]
    if filer_id not in (None, "None", ""):
        sql.append("and filer_id = %s"); args.append(str(filer_id))
    if tran_type and tran_type != "ALL":
        sql.append("and tran_type = %s"); args.append(tran_type)
    if amt_from not in (None, "None"):
        sql.append("and amount >= %s"); args.append(float(amt_from))
    if amt_to not in (None, "None"):
        sql.append("and amount <= %s"); args.append(float(amt_to))
    if payee_prefix not in (None, "None", ""):
        sql.append("and upper(contributor_payee) like %s"); args.append(payee_prefix.upper() + "%")
    cur.execute(" ".join(sql), args)
    return cur.fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-gap", type=int, default=1,
                    help="only report windows short by at least this many rows")
    ap.add_argument("--date-field", choices=["filed", "tran"], default="filed")
    ap.add_argument("--json", dest="json_out", help="write findings to this path")
    args = ap.parse_args()

    if not COUNTS_PATH.exists():
        log.error("No %s — run a fetch first; it records ORESTAR's counts as it goes.",
                  COUNTS_PATH)
        return 1
    rows = json.loads(COUNTS_PATH.read_text())
    log.info("Verifying %d recorded windows against the database…", len(rows))

    conn = s._connect()
    cur = conn.cursor()
    findings, complete, checked_rows = [], 0, 0
    for entry in rows:
        key = entry["key"]
        # Filer-targeted windows carry a 7th element. Older entries have six,
        # and the padding keeps them reading exactly as before.
        (tran_type, start, end, amt_from, amt_to,
         payee_prefix, filer_id) = (list(key) + [None] * 7)[:7]
        reported = int(entry["reported"])
        held = _held(cur, tran_type, start, end, amt_from, amt_to, payee_prefix,
                     args.date_field, filer_id)
        checked_rows += reported
        gap = reported - held
        if gap >= args.min_gap:
            findings.append({"type": tran_type, "start": start, "end": end,
                             "amt_from": amt_from, "amt_to": amt_to,
                             "payee_prefix": payee_prefix, "filer_id": filer_id,
                             "orestar": reported, "held": held, "missing": gap})
        else:
            complete += 1
    conn.close()

    findings.sort(key=lambda f: -f["missing"])
    print()
    print(f"  windows verified            : {len(rows):,}")
    print(f"  complete                    : {complete:,}")
    print(f"  SHORT of ORESTAR            : {len(findings):,}")
    print(f"  rows missing in total       : {sum(f['missing'] for f in findings):,}")
    print(f"  ORESTAR rows covered        : {checked_rows:,}")
    if findings:
        print(f"\n  worst {min(len(findings), 15)}:")
        print(f"    {'type':5} {'window':24} {'ORESTAR':>9} {'held':>9} {'missing':>9}")
        for f in findings[:15]:
            w = f["start"] if f["start"] == f["end"] else f"{f['start']}..{f['end']}"
            extra = ""
            if f.get("filer_id") not in (None, "None"): extra += f" filer{f['filer_id']}"
            if f["amt_from"] not in (None, "None"): extra += f" ${f['amt_from']}-{f['amt_to']}"
            if f["payee_prefix"] not in (None, "None"): extra += f" ~{f['payee_prefix']}*"
            print(f"    {f['type'] or '-':5} {(w + extra)[:24]:24} "
                  f"{f['orestar']:>9,} {f['held']:>9,} {f['missing']:>9,}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=1))
        log.info("Wrote %d findings to %s", len(findings), args.json_out)

    # Non-zero when anything is short, so CI can gate on it.
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
