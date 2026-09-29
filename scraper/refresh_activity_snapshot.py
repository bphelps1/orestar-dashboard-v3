"""
refresh_activity_snapshot.py — rebuild activity_snapshot from LIVE database data.

The Fundraising Pulse ("Who's Fundraising?") and Races to Watch are computed
over the last 30/90 days. data/aggregated/ lags the database, so generating the
snapshot from those files yields *empty lanes rather than an error* — the box
renders with "No data" and nothing looks broken. That happened once already.

This sources filer_index and every filer detail straight from Postgres and
regenerates the whole snapshot, then writes it back to dashboard_cache.

Usage:
    python scraper/refresh_activity_snapshot.py
    python scraper/refresh_activity_snapshot.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s
from generate_activity_snapshot import generate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BATCH = 500


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, but don't write")
    args = ap.parse_args()

    conn = s._connect()
    cur = conn.cursor()

    cur.execute("select data from dashboard_cache where key='filer_index'")
    row = cur.fetchone()
    if not row:
        log.error("filer_index missing from dashboard_cache")
        return 1
    index = row[0]
    log.info("filer_index: %d committees", len(index))

    # Stream details in batches — 7k jsonb blobs at once is a large single read.
    slugs = [r["slug"] for r in index if r.get("slug")]
    details = []
    for i in range(0, len(slugs), BATCH):
        cur.execute("select detail from filer_detail where slug = any(%s)",
                    (slugs[i:i + BATCH],))
        details.extend(d for (d,) in cur.fetchall())
    log.info("filer_detail: %d blobs", len(details))

    latest = max((e.get("month", "") for d in details
                  for e in (d.get("timeline") or [])), default="")
    log.info("latest month present in live data: %s", latest or "(none)")

    snapshot = generate(index=index, details=details)

    # Guard: the whole point is that empty lanes are silent. Refuse to publish
    # a snapshot whose current window has no activity at all.
    pulse = snapshot.get("periods", {}).get("30d", {})
    filled = sum(len(v or []) for v in (pulse.get("by_office_tier") or {}).values())
    log.info("30d lanes: %d entries, %d donors, %d growth", filled,
             len(pulse.get("top_donors") or []), len(pulse.get("top_growth") or []))
    if filled == 0:
        log.error("30d window is empty — refusing to publish (stale input?)")
        return 1

    if args.dry_run:
        log.info("--dry-run: not writing")
        return 0

    cur.execute(
        "update dashboard_cache set data = %s::jsonb, updated_at = now() "
        "where key = 'activity_snapshot'",
        (json.dumps(snapshot, default=str),),
    )
    conn.commit()
    conn.close()
    log.info("activity_snapshot refreshed from live data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
