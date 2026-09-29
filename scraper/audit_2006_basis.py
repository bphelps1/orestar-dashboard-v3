#!/usr/bin/env python3
"""
audit_2006_basis.py — what rule does ORESTAR's 2006 statement actually follow?

2006 has been explained wrongly four times: a pre-ORESTAR boundary effect, a
difference in loan treatment, a parser bug, and a uniform filing basis. The
first three were disproved by measurement; the fourth was implemented, made
2006 worse across 440 additional committees, and was reverted.

What is known:

  - Our figures match ORESTAR from 2007 on, often to within tens of dollars
    across a hundred committees a year. The calculation is not broadly wrong.
  - 73 committees diverge in 2006, by $3.17M on the transaction basis.
  - Those 73 reconcile better on a filing basis, but imposing that basis on
    everyone breaks committees that previously reconciled — so ORESTAR's 2006
    statement is neither purely transaction-dated nor purely filing-dated.

This identifies the rule per committee instead of assuming one, by scoring
candidate bases against the single number ORESTAR states for the year.

The candidates are computed in process.py, NOT here. Every attempt to
reimplement the cash net in ad-hoc SQL has produced a different quantity —
once wrong by $457,723 on one committee, once making a diverging committee
appear to reconcile — because the pipeline's net includes balance adjustments
and treats loans through frames this file cannot see. A candidate basis is
only meaningful if it differs from the live figure in the BASIS alone.

Usage:
    python scraper/audit_2006_basis.py
    python scraper/audit_2006_basis.py --diverging
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync
from audit_consistency import current_yearly_discrepancies

LABELS = {
    "tran_2006":                     "transaction date in 2006 (current)",
    "tran_2006_filed_by_2006":       "tran 2006, filed by 2006-12-31",
    "tran_2006_filed_by_2007_01_31": "tran 2006, filed by 2007-01-31",
    "tran_2006_filed_by_2007_06_30": "tran 2006, filed by 2007-06-30",
    "tran_2006_filed_by_2007_12_31": "tran 2006, filed by 2007-12-31",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diverging", action="store_true")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    conn = supabase_sync._connect()
    cur = conn.cursor()
    cur.execute("select filer_id, name, detail from filer_detail")
    rows = cur.fetchall()
    conn.close()

    explains = Counter()
    cases = unexplained = 0
    misses = []
    out = []
    for fid, name, detail in rows:
        if isinstance(detail, str):
            detail = json.loads(detail)
        basis = detail.get("basis_2006") or {}
        if not basis:
            continue
        # Candidate app bases may be judged only against an annual ORESTAR row
        # captured for the same transaction scope.  Falling back to a legacy
        # source-only row made late backfills look like evidence for a different
        # 2006 accounting rule.
        y = current_yearly_discrepancies(detail).get("2006") or {}
        if not y:
            continue
        mv = y.get("orestar_movement")
        if mv is None:
            continue
        diverging = y.get("delta_movement") is not None and abs(y["delta_movement"]) > 0.01
        if args.diverging and not diverging:
            continue
        cases += 1
        fits = [k for k, v in basis.items() if abs(float(v) - float(mv)) <= 1.0]
        for k in fits:
            explains[k] += 1
        if not fits:
            unexplained += 1
            misses.append((fid, name, float(mv), basis))
        out.append({"filer_id": fid, "name": name, "orestar_movement": float(mv),
                    "diverging": diverging, "basis": basis, "fits": fits})

    print()
    print(f"  committees with a 2006 statement: {cases:,}"
          f"{'  (diverging only)' if args.diverging else ''}")
    print()
    print(f"    {'candidate rule':<40} {'reconciles':>10}")
    for k, lab in LABELS.items():
        print(f"    {lab:<40} {explains[k]:>10,}")
    print(f"    {'NO candidate reconciles':<40} {unexplained:>10,}")

    if misses:
        print()
        print("  committees no candidate explains (first 8):")
        for fid, name, mv, basis in misses[:8]:
            print(f"    {fid:>7} ORESTAR moved {mv:>12,.0f}   {name[:32]}")
            for k, lab in LABELS.items():
                print(f"           {lab:<40} {float(basis.get(k, 0)):>12,.0f}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=1))
        print(f"\n  wrote {len(out):,} rows to {args.json_out}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
