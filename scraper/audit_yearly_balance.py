#!/usr/bin/env python3
"""
audit_yearly_balance.py — which committee-YEARS does our transaction history get wrong?

The coverage survey answered this for rows: it asked ORESTAR how many records
it holds per committee and compared. There was no equivalent for money, so a
committee could hold every row ORESTAR lists and still disagree on the balance,
with nothing to say which year went wrong.

ORESTAR states a beginning AND an ending balance for every year, so the
difference between them is that year's movement according to the audited
record. Comparing our own net against it isolates a single year:

    delta_movement = our_net - (orestar_ending - orestar_beginning)

That is deliberately NOT the same as the stored `discrepancy`, which compares
our rolled-forward ending against ORESTAR's. Our rolled-forward ending carries
every earlier year's error with it, so one bad year in 2008 makes 2009 through
2026 all look wrong and hides which one actually is. delta_movement inherits
nothing: a year is guilty only of its own transactions.

Usage:
    python scraper/audit_yearly_balance.py                 # summary
    python scraper/audit_yearly_balance.py --min 1000      # only material years
    python scraper/audit_yearly_balance.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync
from audit_consistency import current_yearly_discrepancies


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=float, default=0.01,
                    help="only count years off by at least this much")
    ap.add_argument("--include-artifacts", action="store_true",
                    help="count filing-period artifacts as divergence too "
                         "(they reconcile on ORESTAR's own basis, so they are "
                         "excluded by default)")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    conn = supabase_sync._connect()
    cur = conn.cursor()
    cur.execute("select filer_id, name, detail from filer_detail")
    rows = cur.fetchall()
    conn.close()

    artifacts = 0
    artifact_amt = 0.0
    lags = 0
    lag_amt = 0.0
    by_year_count: Counter = Counter()
    by_year_amt: defaultdict = defaultdict(float)
    offenders = []
    checked = matched = 0

    for fid, name, detail in rows:
        if isinstance(detail, str):
            detail = json.loads(detail)
        yd = current_yearly_discrepancies(detail)
        for yr, d in yd.items():
            dm = d.get("delta_movement")
            if dm is None:
                continue
            checked += 1
            if abs(dm) < args.min:
                matched += 1
                continue
            # No exclusions. An earlier version dropped any year that would
            # reconcile on a different basis, which left the calculation
            # disagreeing with ORESTAR and hid the difference behind a 5%
            # tolerance. Where another basis is right, process.py now computes
            # on it (2006, filing basis); where it is not, the difference is
            # real and is counted here.
            by_year_count[yr] += 1
            by_year_amt[yr] += abs(dm)
            offenders.append({
                "filer_id": fid, "name": name, "year": yr,
                "our_net": d.get("our_net"),
                "orestar_movement": d.get("orestar_movement"),
                "delta_movement": dm,
            })

    offenders.sort(key=lambda o: -abs(o["delta_movement"]))
    total = sum(abs(o["delta_movement"]) for o in offenders)

    print()
    print(f"  committee-years with both figures : {checked:,}")
    print(f"  our transactions agree            : {matched:,}  ({100*matched/checked:.1f}%)" if checked else "")
    print(f"  DIVERGE                           : {len(offenders):,}")
    print(f"  total absolute divergence         : ${total:,.0f}")

    if by_year_count:
        print()
        print("  worst years (where our history is wrong, not where it shows up):")
        print(f"    {'year':6} {'committees':>11} {'divergence':>14}")
        for yr, amt in sorted(by_year_amt.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {yr:6} {by_year_count[yr]:>11,} {amt:>14,.0f}")

    if offenders:
        print()
        print("  largest single committee-years:")
        print(f"    {'filer':>7} {'year':>5} {'our net':>13} {'ORESTAR moved':>14} {'off by':>12}  name")
        for o in offenders[:12]:
            print(f"    {o['filer_id']:>7} {o['year']:>5} {o['our_net']:>13,.0f} "
                  f"{o['orestar_movement']:>14,.0f} {o['delta_movement']:>12,.0f}  {o['name'][:24]}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(offenders, indent=1))
        print(f"\n  wrote {len(offenders):,} findings to {args.json_out}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
