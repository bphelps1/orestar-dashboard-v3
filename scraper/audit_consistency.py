#!/usr/bin/env python3
"""
audit_consistency.py — Validate internal consistency of filer financial data.

Checks each filer's aggregated JSON for:
  1. total_in matches sum of timeline[].contributions
  2. total_out matches sum of timeline[].expenditures
  3. cash_on_hand matches beginning_balance + net flow from timeline
  4. Beginning balance continuity across years
  5. Per-year ORESTAR line-item comparison (if yearly data available)

Usage:
    python scraper/audit_consistency.py                     # audit all filers
    python scraper/audit_consistency.py --filer some-slug   # audit one filer
    python scraper/audit_consistency.py --threshold 100     # only report deltas > $100
    python scraper/audit_consistency.py --output report.csv # save to CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "aggregated"
FILERS_DIR = DATA_DIR / "filers"


def _timeline_has_exact_cash(timeline: list[dict]) -> bool:
    """Whether one whole timeline can use the new authoritative cash lane."""
    return bool(timeline) and all(
        isinstance(row.get("cash_balance_net"), (int, float))
        and not isinstance(row.get("cash_balance_net"), bool)
        and math.isfinite(row["cash_balance_net"])
        for row in timeline
    )


def _legacy_cash_net(row: dict) -> float:
    """Cash movement encoded by pre-cash_balance_net timeline rows."""
    return (
        row.get("contributions", 0)
        + row.get("other_receipts", 0)
        + row.get("balance_adjustments", 0)
        - row.get("expenditures", 0)
        - row.get("other_disbursements", 0)
    )


def current_yearly_discrepancies(detail: dict) -> dict[str, dict]:
    """Return annual comparisons proven current for this transaction scope.

    Annual ORESTAR pages and the app's transaction rows are independent
    snapshots.  A cached yearly delta is diagnostic only when aggregation has
    explicitly verified the annual row's capture provenance against the current
    scope.  Missing and legacy flags fail closed; truthy stand-ins such as ``1``
    are not accepted as evidence.
    """
    yearly = detail.get("yearly_discrepancies") or {}
    if not isinstance(yearly, dict):
        return {}
    return {
        str(year): row
        for year, row in yearly.items()
        if isinstance(row, dict) and row.get("comparison_current") is True
    }


def audit_filer(slug: str, detail: dict, threshold: float = 0.01) -> list[dict]:
    """Run all consistency checks on a single filer. Returns list of findings."""
    findings = []
    name = detail.get("name", slug)
    timeline = detail.get("timeline", [])

    # 1. total_in matches sum of timeline contributions
    total_in = detail.get("total_in", 0)
    tl_contributions = round(sum(r.get("contributions", 0) for r in timeline), 2)
    delta = round(total_in - tl_contributions, 2)
    if abs(delta) > threshold:
        findings.append({
            "filer_slug": slug, "filer_name": name,
            "check": "total_in vs timeline contributions",
            "year": "all",
            "our_value": total_in, "expected_value": tl_contributions,
            "delta": delta,
            "severity": "HIGH" if abs(delta) > 1000 else "MEDIUM" if abs(delta) > 10 else "LOW",
        })

    # 2. total_out matches sum of timeline expenditures
    total_out = detail.get("total_out", 0)
    tl_expenditures = round(sum(r.get("expenditures", 0) for r in timeline), 2)
    delta = round(total_out - tl_expenditures, 2)
    if abs(delta) > threshold:
        findings.append({
            "filer_slug": slug, "filer_name": name,
            "check": "total_out vs timeline expenditures",
            "year": "all",
            "our_value": total_out, "expected_value": tl_expenditures,
            "delta": delta,
            "severity": "HIGH" if abs(delta) > 1000 else "MEDIUM" if abs(delta) > 10 else "LOW",
        })

    # 3. cash_on_hand matches beginning_balance + net flow
    coh = detail.get("cash_on_hand", 0)
    beginning_balances = detail.get("beginning_balances", {})
    first_year = sorted(beginning_balances.keys())[0] if beginning_balances else None
    begin_bal = beginning_balances.get(first_year, 0) if first_year else 0
    exact_cash = _timeline_has_exact_cash(timeline)
    timeline_net = round(sum(
        r["cash_balance_net"] if exact_cash else _legacy_cash_net(r)
        for r in timeline
    ), 2)
    expected_coh = round(begin_bal + timeline_net, 2)
    delta = round(coh - expected_coh, 2)
    if abs(delta) > threshold:
        findings.append({
            "filer_slug": slug, "filer_name": name,
            "check": "cash_on_hand vs begin + net flow",
            "year": "all",
            "our_value": coh, "expected_value": expected_coh,
            "delta": delta,
            "severity": "HIGH" if abs(delta) > 1000 else "MEDIUM" if abs(delta) > 10 else "LOW",
        })

    # 4. Beginning balance continuity
    sorted_years = sorted(beginning_balances.keys())
    for i in range(len(sorted_years) - 1):
        yr = sorted_years[i]
        next_yr = sorted_years[i + 1]
        begin_this = beginning_balances[yr]
        begin_next = beginning_balances[next_yr]
        # Compute year's net from timeline
        yr_months = [r for r in timeline if r["month"].startswith(yr)]
        yr_net = sum(
            r["cash_balance_net"] if exact_cash else _legacy_cash_net(r)
            for r in yr_months
        )
        expected_next = round(begin_this + yr_net, 2)
        delta = round(begin_next - expected_next, 2)
        if abs(delta) > threshold:
            findings.append({
                "filer_slug": slug, "filer_name": name,
                "check": f"begin_balance continuity {yr}→{next_yr}",
                "year": yr,
                "our_value": begin_next, "expected_value": expected_next,
                "delta": delta,
                "severity": "HIGH" if abs(delta) > 1000 else "MEDIUM" if abs(delta) > 10 else "LOW",
            })

    # 5. ORESTAR annual-flow comparison.  A year-local digest proves that
    # year's transactions and therefore its line items / movement.  It cannot
    # prove the rolled opening or ending position: a late row in an earlier
    # year changes both without changing this year's digest.  Keep those raw
    # fields in the profile for inspection, but do not diagnose them here.
    yearly_disc = current_yearly_discrepancies(detail)
    for yr, d in yearly_disc.items():
        for field, label, our_field, orestar_field in [
            ("delta_contributions", "contributions",
             "our_contributions", "orestar_contributions"),
            ("delta_expenditures", "expenditures",
             "our_expenditures", "orestar_expenditures"),
            ("delta_other_receipts", "other_receipts",
             "our_other_receipts", "orestar_other_receipts"),
            ("delta_other_disbursements", "other_disbursements",
             "our_other_disbursements", "orestar_other_disbursements"),
            ("delta_movement", "movement", "our_net", "orestar_movement"),
        ]:
            delta = d.get(field)
            if delta is not None and abs(delta) > threshold:
                findings.append({
                    "filer_slug": slug, "filer_name": name,
                    "check": f"ORESTAR {label}",
                    "year": yr,
                    "our_value": d.get(our_field, ""),
                    "expected_value": d.get(orestar_field, ""),
                    "delta": delta,
                    "severity": "HIGH" if abs(delta) > 1000 else "MEDIUM" if abs(delta) > 10 else "LOW",
                })

    return findings


def main():
    parser = argparse.ArgumentParser(description="Audit filer data consistency")
    parser.add_argument("--filer", help="Audit a specific filer slug")
    parser.add_argument("--threshold", type=float, default=0.01, help="Min delta to report (default $0.01)")
    parser.add_argument("--output", help="Write CSV report to file")
    parser.add_argument("--summary-only", action="store_true", help="Only show summary counts")
    args = parser.parse_args()

    if args.filer:
        path = FILERS_DIR / f"{args.filer}.json"
        if not path.exists():
            print(f"Filer not found: {path}")
            sys.exit(1)
        with open(path) as f:
            detail = json.load(f)
        filers = [(args.filer, detail)]
    else:
        filers = []
        for path in sorted(FILERS_DIR.glob("*.json")):
            with open(path) as f:
                filers.append((path.stem, json.load(f)))
        print(f"Auditing {len(filers)} filers...")

    all_findings = []
    filers_clean = 0
    filers_with_issues = 0

    for slug, detail in filers:
        findings = audit_filer(slug, detail, args.threshold)
        all_findings.extend(findings)
        if findings:
            filers_with_issues += 1
        else:
            filers_clean += 1

    # Summary
    high = sum(1 for f in all_findings if f["severity"] == "HIGH")
    medium = sum(1 for f in all_findings if f["severity"] == "MEDIUM")
    low = sum(1 for f in all_findings if f["severity"] == "LOW")

    print(f"\n{'='*60}")
    print(f"AUDIT SUMMARY")
    print(f"{'='*60}")
    print(f"Filers audited:     {filers_clean + filers_with_issues}")
    print(f"Filers clean:       {filers_clean}")
    print(f"Filers with issues: {filers_with_issues}")
    print(f"Total findings:     {len(all_findings)}")
    print(f"  HIGH (>$1000):    {high}")
    print(f"  MEDIUM ($10-1000):{medium}")
    print(f"  LOW (<$10):       {low}")

    if not args.summary_only and all_findings:
        print(f"\n{'─'*60}")
        for f in sorted(all_findings, key=lambda x: -abs(x["delta"])):
            print(f"[{f['severity']:6s}] {f['filer_name'][:40]:40s} | {f['check']:40s} | yr={f['year']} | Δ=${f['delta']:>12,.2f}")

    if args.output:
        with open(args.output, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=[
                "filer_slug", "filer_name", "check", "year",
                "our_value", "expected_value", "delta", "severity",
            ])
            writer.writeheader()
            writer.writerows(all_findings)
        print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
