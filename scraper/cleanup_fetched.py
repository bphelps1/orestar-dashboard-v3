"""
cleanup_fetched.py — Identify and remove empty windows from fetched_windows.json.

An "empty window" is one marked as fetched but whose source file contributed ZERO
transactions to the dashboard.  This is detected by cross-referencing the fetched
log against the _source_file column in the per-year transaction CSVs.

These empty windows are typically the result of failed downloads where the scraper
received an HTML error page, an empty file, or a truncated response but still
recorded the window as done.

Usage:
    python cleanup_fetched.py              # dry run (report only)
    python cleanup_fetched.py --apply      # remove empty windows and save
    python cleanup_fetched.py --log tran   # check fetched_windows_tran.json instead

Reads:
    data/fetched_windows.json (or fetched_windows_tran.json)
    data/transactions/txn_YYYY.csv.gz

Writes (with --apply):
    data/fetched_windows.json  (cleaned)
    data/fetched_windows_backup.json  (original backup)
"""

import argparse
import gzip
import json
import shutil
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
FETCHED_LOG = DATA_DIR / "fetched_windows.json"
FETCHED_LOG_TRN = DATA_DIR / "fetched_windows_tran.json"
TRANS_DIR = DATA_DIR / "transactions"


def collect_source_files() -> set[str]:
    """Scan all per-year CSV.gz files and return the set of _source_file values."""
    source_files: set[str] = set()
    for f in sorted(TRANS_DIR.glob("txn_*.csv.gz")):
        try:
            with gzip.open(f, "rt") as gz:
                import csv
                reader = csv.DictReader(gz)
                for row in reader:
                    src = row.get("_source_file", "").strip()
                    if src:
                        source_files.add(src)
        except Exception as exc:
            print(f"  Warning: could not read {f.name}: {exc}")
    print(f"Found {len(source_files):,} unique source files in transaction data")
    return source_files


def main():
    parser = argparse.ArgumentParser(
        description="Find and remove empty windows from fetched_windows.json"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually remove empty windows (default is dry-run report only)"
    )
    parser.add_argument(
        "--log", choices=["filed", "tran"], default="filed",
        help="Which log to clean: 'filed' (default) or 'tran'"
    )
    args = parser.parse_args()

    log_file = FETCHED_LOG_TRN if args.log == "tran" else FETCHED_LOG
    backup_file = DATA_DIR / f"{log_file.stem}_backup.json"

    # 1. Load fetched windows
    with open(log_file) as f:
        windows = json.load(f)
    print(f"Fetched windows in {log_file.name}: {len(windows)} total")

    # 2. Collect all source files referenced in transaction data
    source_files = collect_source_files()

    # 3. Check each window against source files
    empty = []
    has_data = []

    for entry in windows:
        ttype, start_str, end_str = entry
        expected_file = f"{ttype}_{start_str}_{end_str}.xlsx"
        if expected_file in source_files:
            has_data.append(entry)
        else:
            empty.append(entry)

    print(f"\nResults:")
    print(f"  Windows with data (source file exists): {len(has_data)}")
    print(f"  Empty windows (no source file):         {len(empty)}")
    pct = len(empty) / len(windows) * 100 if windows else 0
    print(f"  Empty percentage:                       {pct:.1f}%")

    if empty:
        # Breakdown by type
        type_counts = Counter(e[0] for e in empty)
        type_totals = Counter(e[0] for e in windows)
        print(f"\nEmpty windows by type:")
        for t in sorted(type_totals):
            ec = type_counts.get(t, 0)
            tc = type_totals[t]
            print(f"  {t:>2}: {ec:>5} empty / {tc:>5} total ({ec/tc*100:.1f}%)")

        # Date range
        dates = sorted(date.fromisoformat(e[1]) for e in empty)
        print(f"\nDate range of empty windows: {dates[0]} to {dates[-1]}")

        # Sample
        print(f"\nFirst 15 empty windows:")
        for entry in empty[:15]:
            print(f"  {entry}")
        if len(empty) > 15:
            print(f"  ... and {len(empty) - 15} more")

    if args.apply and empty:
        # Save backup
        shutil.copy2(log_file, backup_file)
        print(f"\nBackup saved to: {backup_file}")

        # Remove empty windows
        empty_set = {tuple(e) for e in empty}
        cleaned = [e for e in windows if tuple(e) not in empty_set]
        with open(log_file, "w") as f:
            json.dump(sorted(cleaned), f, indent=2)
        print(f"Cleaned {log_file.name}: {len(cleaned)} entries (removed {len(empty)})")
    elif empty and not args.apply:
        print(f"\nDry run — no changes made. Re-run with --apply to remove empty windows.")


if __name__ == "__main__":
    main()
