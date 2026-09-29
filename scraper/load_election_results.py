"""
load_election_results.py — load official SoS results into Postgres.

Reads the extractor's `results_totals.csv` (one row per contest x candidate)
and loads it into `election_results`, which backs the `race_margins` view used
by the recommendation engine.

Usage:
    python scraper/load_election_results.py --csv "/path/to/results_totals.csv"
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

COLS = ["election", "year", "election_type", "office_normalized", "district",
        "ballot_party", "candidate", "candidate_party", "party_code",
        "votes", "pct", "won", "is_measure", "source"]


def _bool(v: str) -> str:
    return "true" if str(v).strip().lower() in ("y", "yes", "true", "1") else "false"


def _int(v: str) -> str:
    v = (v or "").replace(",", "").strip()
    return v if v.lstrip("-").isdigit() else "0"


def _num(v: str) -> str:
    v = (v or "").strip()
    try:
        float(v)
        return v
    except ValueError:
        return ""


def load(csv_path: Path) -> int:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    log.info("Read %s rows from %s", f"{len(rows):,}", csv_path.name)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLS)
    kept = 0
    for r in rows:
        year = _int(r.get("year"))
        if not year or year == "0":
            continue
        w.writerow([
            r.get("election", ""), year, r.get("election_type", ""),
            r.get("office_normalized", ""), r.get("district", ""),
            r.get("ballot_party", ""), r.get("candidate", ""),
            r.get("candidate_party", ""), r.get("party_code", ""),
            _int(r.get("votes")), _num(r.get("pct")),
            _bool(r.get("won")), _bool(r.get("is_measure")),
            r.get("source", ""),
        ])
        kept += 1
    body = buf.getvalue().splitlines()
    header, data_lines = body[0], body[1:]

    conn = s._connect()
    cur = conn.cursor()
    cur.execute("truncate election_results restart identity")
    conn.commit()

    # Chunked COPY with per-chunk retry: this link drops larger TLS payloads
    # ("SSL error: bad record mac"), and a single 2 MB COPY fails outright.
    copy_sql = (f"copy election_results ({', '.join(COLS)}) from stdin "
                f"with (format csv, header true, null '')")
    CHUNK = int(__import__("os").environ.get("RESULTS_COPY_CHUNK", "2000"))
    sent = 0
    for i in range(0, len(data_lines), CHUNK):
        piece = "\n".join([header] + data_lines[i:i + CHUNK]) + "\n"
        for attempt in range(1, 6):
            try:
                cur.copy_expert(copy_sql, io.StringIO(piece))
                conn.commit()
                break
            except Exception as e:                      # noqa: BLE001
                conn.rollback()
                if attempt == 5:
                    raise
                log.warning("  chunk at %d failed (%s) — retry %d",
                            i, str(e).split("\n")[0][:50], attempt)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = s._connect()
                cur = conn.cursor()
        sent += len(data_lines[i:i + CHUNK])
        log.info("  loaded %s/%s rows", f"{sent:,}", f"{len(data_lines):,}")

    cur.execute("select count(*) from election_results")
    n = cur.fetchone()[0]
    cur.execute("select count(*) from race_margins")
    m = cur.fetchone()[0]
    cur.execute("analyze election_results")
    conn.commit()
    conn.close()
    log.info("Loaded %s result rows; race_margins has %s general contests",
             f"{n:,}", f"{m:,}")
    return 0 if n == kept else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="path to results_totals.csv")
    args = ap.parse_args()
    path = Path(args.csv).expanduser()
    if not path.exists():
        log.error("no such file: %s", path)
        return 1
    return load(path)


if __name__ == "__main__":
    sys.exit(main())
