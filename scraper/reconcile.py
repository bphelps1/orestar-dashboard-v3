"""
reconcile.py — Compare our transaction data against ORESTAR per-filer transaction counts.

For a set of filers, downloads the ORESTAR search results page (by filer committee ID)
to get the official transaction count, then compares against our aggregated data.

Also does a detailed breakdown of our transaction sub-types vs ORESTAR's Account Summary
to identify where discrepancies arise.

Usage:
    python reconcile.py                          # reconcile top 20 longest-active filers
    python reconcile.py --filer-ids 4792 5152    # reconcile specific filers
"""

import argparse
import json
import logging
import re

import orestar_parse
import sys
import urllib.request
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
AGGREGATED_DIR = DATA_DIR / "aggregated"


def get_orestar_tran_count(filer_id: str) -> int | None:
    """Fetch the total transaction count from ORESTAR's committee search page."""
    url = (
        "https://secure.sos.state.or.us/orestar/cneSearch.do"
        f"?cneSearchButtonName=search&cneSearchFilerCommitteeId={filer_id}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ORESTAR-reconcile/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("  Failed to fetch ORESTAR page for filer %s: %s", filer_id, e)
        return None

    # Look for pattern like "1 - 50 of 25213" or "Transactions found: 25213"
    m = re.search(r"of\s+([\d,]+)\s*(?:transactions|</span>|<)", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    # Alternative: count from the "Found X" pattern
    m = re.search(r"(\d[\d,]*)\s+transactions?\s+found", html, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))

    # Try another common pattern
    m = re.search(r"Displaying.*?of\s+([\d,]+)", html)
    if m:
        return int(m.group(1).replace(",", ""))

    return None


def get_orestar_account_summary(filer_id: str) -> dict | None:
    """Fetch the Account Summary page and extract all line items."""
    url = (
        "https://secure.sos.state.or.us/orestar/"
        f"publicAccountSummary.do?filerId={filer_id}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ORESTAR-reconcile/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    def parse_dollar(label):
        return orestar_parse.parse_dollar(html, label)

    year_m = re.search(r"Account Summary Information for the year\s+(\d{4})", html)
    if not year_m:
        return None

    return {
        "year": int(year_m.group(1)),
        "beginning_balance": parse_dollar("Beginning Balance (Previous Year)") or 0.0,
        "ending_cash_balance": parse_dollar("Ending Cash Balance") or 0.0,
        "cash_contributions": parse_dollar("Cash Contributions") or 0.0,
        "other_receipts": parse_dollar("Other Receipts") or 0.0,
        "cash_expenditures": parse_dollar("Cash Expenditures") or 0.0,
        "other_disbursements": parse_dollar("Other Disbursements") or 0.0,
        "balance_adjustments": parse_dollar("Balance Adjustments") or 0.0,
    }


_all_transactions_cache = None

def load_all_transactions_cached() -> pd.DataFrame:
    """Load all transactions from raw Excel files (cached across calls)."""
    global _all_transactions_cache
    if _all_transactions_cache is None:
        sys.path.insert(0, str(Path(__file__).parent))
        from process import _load_all_transactions
        _all_transactions_cache = _load_all_transactions()
    return _all_transactions_cache

def load_our_transactions(filer_name: str) -> pd.DataFrame:
    """Load our raw transaction data for a specific filer."""
    df = load_all_transactions_cached()

    # Use canonical filer name if available
    filer_col = "filer_canonical" if "filer_canonical" in df.columns else "filer"
    filer_df = df[df[filer_col].str.lower() == filer_name.lower()].copy()
    if filer_df.empty and filer_col == "filer_canonical":
        filer_df = df[df["filer"].str.lower() == filer_name.lower()].copy()

    # Ensure numeric amount
    if not filer_df.empty:
        filer_df["amount"] = pd.to_numeric(filer_df["amount"], errors="coerce").fillna(0)

    # Ensure year column
    if not filer_df.empty and "year" not in filer_df.columns:
        if "filed_date" in filer_df.columns:
            filer_df["year"] = pd.to_numeric(filer_df["filed_date"].str[:4], errors="coerce")
    return filer_df


def reconcile_filer(filer_id: str, filer_name: str, our_data: dict, df: pd.DataFrame | None = None):
    """Run full reconciliation for a single filer."""
    log.info("=" * 80)
    log.info("RECONCILING: %s (filer ID: %s)", filer_name, filer_id)
    log.info("=" * 80)

    # 1. Get ORESTAR transaction count
    orestar_count = get_orestar_tran_count(filer_id)
    our_count = our_data.get("tran_count", 0)
    log.info("\n  Transaction counts:")
    log.info("    Our data:   %s", f"{our_count:,}")
    log.info("    ORESTAR:    %s", f"{orestar_count:,}" if orestar_count else "UNAVAILABLE")
    if orestar_count:
        diff = our_count - orestar_count
        log.info("    Difference: %s%s", "+" if diff > 0 else "", f"{diff:,}")

    # 2. Get ORESTAR Account Summary
    acct = get_orestar_account_summary(filer_id)
    if acct:
        log.info("\n  ORESTAR Account Summary (%d):", acct["year"])
        log.info("    Beginning Balance:     $%12.2f", acct["beginning_balance"])
        log.info("    Cash Contributions:    $%12.2f", acct["cash_contributions"])
        log.info("    Other Receipts:        $%12.2f", acct["other_receipts"])
        log.info("    Cash Expenditures:     $%12.2f", acct["cash_expenditures"])
        log.info("    Other Disbursements:   $%12.2f", acct["other_disbursements"])
        log.info("    Balance Adjustments:   $%12.2f", acct["balance_adjustments"])
        log.info("    Ending Cash Balance:   $%12.2f", acct["ending_cash_balance"])

        # Compare our data for the same year
        year_str = str(acct["year"])
        timeline = our_data.get("timeline", [])
        year_rows = [t for t in timeline if t["month"].startswith(year_str)]

        our_c = sum(t.get("contributions", 0) for t in year_rows)
        our_e = sum(t.get("expenditures", 0) for t in year_rows)
        our_or = sum(t.get("other_receipts", 0) for t in year_rows)
        our_od = sum(t.get("other_disbursements", 0) for t in year_rows)
        our_begin = our_data.get("beginning_balances", {}).get(year_str, 0)

        log.info("\n  Our data for %s:", year_str)
        log.info("    Beginning Balance:     $%12.2f  (from ORESTAR anchor)", our_begin)
        log.info("    Cash Contributions:    $%12.2f  (diff: %+.2f)", our_c, our_c - acct["cash_contributions"])
        log.info("    Other Receipts:        $%12.2f  (diff: %+.2f)", our_or, our_or - acct["other_receipts"])
        log.info("    Cash Expenditures:     $%12.2f  (diff: %+.2f)", our_e, our_e - acct["cash_expenditures"])
        log.info("    Other Disbursements:   $%12.2f  (diff: %+.2f)", our_od, our_od - acct["other_disbursements"])
        our_ending = our_begin + our_c + our_or - our_e - our_od
        log.info("    Our Ending Balance:    $%12.2f  (diff: %+.2f)", our_ending, our_ending - acct["ending_cash_balance"])

    # 3. If we have the raw DataFrame, do sub-type breakdown
    if df is not None and not df.empty and acct:
        year_str = str(acct["year"])
        year_df = df[df["year"] == acct["year"]] if "year" in df.columns else pd.DataFrame()
        if not year_df.empty:
            log.info("\n  Sub-type breakdown for %s:", year_str)

            for ttype in ["C", "E", "O", "OA", "OD", "OR"]:
                type_df = year_df[year_df["tran_type"].str.strip().str.upper() == ttype]
                if not type_df.empty:
                    log.info("\n    Type %s (%d transactions, $%.2f total):",
                             ttype, len(type_df), type_df["amount"].sum())
                    for st, grp in type_df.groupby("sub_type"):
                        log.info("      %-45s  %5d txns  $%12.2f", st, len(grp), grp["amount"].sum())

    log.info("")


def _build_filer_id_map() -> dict[str, str]:
    """Build filer_id → canonical_name mapping from raw transaction data.

    Falls back to a quick scan of CSV files for the 'filer id' and 'filer' columns.
    """
    fid_to_name: dict[str, str] = {}

    # Try loading from consolidated parquet/csv
    consolidated = DATA_DIR / "transactions.parquet"
    csv_dir = DATA_DIR / "_raw"

    # Fastest: scan the consolidated CSV if it exists
    for p in [DATA_DIR / "all_transactions.csv", DATA_DIR / "transactions.csv"]:
        if p.exists():
            try:
                df = pd.read_csv(p, usecols=["filer", "filer id"], dtype=str).dropna()
                for _, row in df.drop_duplicates(subset=["filer id"]).iterrows():
                    fid = str(row["filer id"]).strip()
                    if fid:
                        fid_to_name[fid] = row["filer"].strip()
                return fid_to_name
            except Exception:
                pass

    # Fallback: scan raw Excel files
    if csv_dir.exists():
        from process import _load_all_transactions as load_all_transactions
        try:
            df = load_all_transactions()
            if "filer id" in df.columns:
                pairs = df[["filer", "filer id"]].dropna().drop_duplicates(subset=["filer id"])
                for _, row in pairs.iterrows():
                    fid = str(row["filer id"]).strip()
                    if fid:
                        fid_to_name[fid] = row["filer"].strip()
        except Exception as e:
            log.warning("Could not load transactions: %s", e)

    return fid_to_name


def find_longest_active_filers(n=20) -> list[dict]:
    """Find the N filers with the most transactions (proxy for longest active)."""
    index_path = AGGREGATED_DIR / "filer_index.json"

    with open(index_path) as f:
        index = json.load(f)

    # Load each filer's data and sort by transaction count
    filers = []
    for entry in index:
        slug = entry["slug"]
        fpath = AGGREGATED_DIR / "filers" / f"{slug}.json"
        if not fpath.exists():
            continue
        with open(fpath) as f:
            data = json.load(f)
        filer_id = data.get("filer_id")
        if filer_id:
            filers.append({
                "name": data["name"],
                "slug": slug,
                "filer_id": str(filer_id),
                "tran_count": data.get("tran_count", 0),
                "data": data,
            })

    filers.sort(key=lambda x: x["tran_count"], reverse=True)
    return filers[:n]


def main():
    parser = argparse.ArgumentParser(description="Reconcile transaction data against ORESTAR")
    parser.add_argument("--filer-ids", nargs="+", help="Specific filer IDs to reconcile")
    parser.add_argument("--top", type=int, default=20, help="Number of top filers to reconcile")
    parser.add_argument("--with-raw", action="store_true", help="Load raw transactions for sub-type breakdown (slow)")
    args = parser.parse_args()

    # Build filer_id → name mapping from process.py's _filer_id_to_name
    fid_to_name = _build_filer_id_map()

    if args.filer_ids:
        # Load filer index
        index_path = AGGREGATED_DIR / "filer_index.json"
        with open(index_path) as f:
            index = json.load(f)

        slug_map = {e["name"].lower(): e["slug"] for e in index}

        for fid in args.filer_ids:
            name = fid_to_name.get(fid)
            if not name:
                log.warning("Filer ID %s not found in our ID mapping", fid)
                continue

            slug = slug_map.get(name.lower())
            if not slug:
                log.warning("Filer '%s' (ID %s) not found in filer index", name, fid)
                continue

            fpath = AGGREGATED_DIR / "filers" / f"{slug}.json"
            if not fpath.exists():
                log.warning("No JSON file for %s at %s", name, fpath)
                continue

            with open(fpath) as f:
                data = json.load(f)

            df = None
            if args.with_raw:
                df = load_our_transactions(name)
            reconcile_filer(fid, name, data, df)

    else:
        log.info("Finding top %d filers by transaction count...\n", args.top)
        filers = find_longest_active_filers(args.top)

        results = []
        for filer in filers:
            reconcile_filer(
                filer["filer_id"],
                filer["name"],
                filer["data"],
            )
            results.append(filer)

    log.info("\nReconciliation complete.")


if __name__ == "__main__":
    main()
