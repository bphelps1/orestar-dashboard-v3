"""
supabase_sync.py — push processed data into Supabase Postgres.

The dashboard reads its data from Postgres (not static files), so after
process.py computes everything it syncs three things here:

  • transactions        — the full queryable table (source of truth for the
                          Explore / SQL / download surface)
  • dashboard_cache     — the curated aggregate blobs (summary, timeline, …)
  • filer_detail        — one jsonb row per filer slug

All functions no-op when SUPABASE_DB_URL is not set, so local runs without
credentials still produce the CSV/JSON files as before.

Env:
  SUPABASE_DB_URL            postgres connection string (service/postgres role)
  SUPABASE_URL               https://<project>.supabase.co   (for Storage upload)
  SUPABASE_SERVICE_ROLE_KEY  service role key                (for Storage upload)
  SUPABASE_STORAGE_BUCKET    bucket name for the full-CSV download (default: 'exports')
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
import hashlib
import tempfile
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# ── df/CSV column name → transactions table column name ─────────────────────
COLUMN_MAP = {
    "tran_id": "tran_id",
    "original id": "original_id",
    "tran_date": "tran_date",
    "tran status": "tran_status",
    "filer": "filer",
    "contributor_payee": "contributor_payee",
    "sub_type": "sub_type",
    "payer of personal expenditure": "payer_of_personal_expenditure",
    "amount": "amount",
    "aggregate amount": "aggregate_amount",
    "contributor/payee committee id": "contributor_payee_committee_id",
    "filer id": "filer_id",
    "attest by name": "attest_by_name",
    "attest date": "attest_date",
    "review by name": "review_by_name",
    "review date": "review_date",
    "due date": "due_date",
    "occptn ltr date": "occptn_ltr_date",
    "pymt sched txt": "pymt_sched_txt",
    "purpose": "purpose",
    "intrst rate": "intrst_rate",
    "check nbr": "check_nbr",
    "tran stsfd ind": "tran_stsfd_ind",
    "filed by name": "filed_by_name",
    "filed_date": "filed_date",
    "addr book agent name": "addr_book_agent_name",
    "book type": "book_type",
    "book_type": "book_type",
    "title txt": "title_txt",
    "occupation": "occupation",
    "employer": "employer",
    "emp city": "emp_city",
    "emp state": "emp_state",
    "employ ind": "employ_ind",
    "self employ ind": "self_employ_ind",
    "addr line1": "addr_line1",
    "addr line2": "addr_line2",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "zip plus four": "zip_plus_four",
    "county": "county",
    "country": "country",
    "foreign postal code": "foreign_postal_code",
    "purpose_codes": "purpose_codes",
    "exp date": "exp_date",
    "_source_file": "source_file",
    "tran_type": "tran_type",
    "contributor_type": "contributor_type",
    "office": "office",
    "party": "party",
    "contributor_payee_canonical": "contributor_payee_canonical",
    "filer_canonical": "filer_canonical",
    "contributor_type_label": "contributor_type_label",
}

DATE_COLS = {"tran_date", "attest_date", "review_date", "due_date",
             "occptn_ltr_date", "filed_date", "exp_date"}
NUMERIC_COLS = {"amount", "aggregate_amount"}
BIGINT_COLS = {"tran_id", "original_id"}

# Ordered list of target columns (table order not required, but stable helps).
TARGET_COLS = list(dict.fromkeys(COLUMN_MAP.values()))


# ── Connection ──────────────────────────────────────────────────────────────
def _load_dotenv() -> None:
    """Populate os.environ from a repo-root .env (local dev convenience).
    Never overrides values already set (e.g. GitHub Actions secrets)."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def sync_enabled() -> bool:
    _load_dotenv()
    return bool(os.environ.get("SUPABASE_DB_URL"))


def _parse_dsn(dsn: str) -> dict:
    """Parse postgres://user:password@host:port/dbname into psycopg2 kwargs.

    Done manually (not urllib) so passwords containing URL-special characters
    like '?', '#', '/', '@' work without the caller having to percent-encode."""
    body = dsn.split("://", 1)[1] if "://" in dsn else dsn
    userpass, _, hostpart = body.rpartition("@")          # host is after the last @
    user, _, password = userpass.partition(":")           # password is after the first :
    hostport, _, dbname = hostpart.partition("/")
    dbname = dbname.split("?", 1)[0] or "postgres"         # drop any ?query suffix
    host, _, port = hostport.rpartition(":")
    return {
        "host": host,
        "port": port or "5432",
        "user": user,
        "password": password,
        "dbname": dbname,
    }


def _connect(attempts: int = 6):
    """Connect, retrying transient pool-checkout failures with backoff.

    Supabase's session pooler has a finite slot count; a cancelled job or a
    burst of activity can exhaust it, and every connect then fails with
    ECHECKOUTTIMEOUT for minutes. Without a retry a long batch job dies on its
    very first connection, throwing away all the work that would follow."""
    import time
    import psycopg2  # imported lazily so local runs without the dep still work
    _load_dotenv()
    params = _parse_dsn(os.environ["SUPABASE_DB_URL"])
    # TLS + TCP keepalives keep long bulk-load connections from being dropped.
    # They cannot detect a Postgres backend that dies behind the Supavisor
    # pooler: the pooler keeps this socket open and answers the probes, and
    # statement_timeout dies with the server. libpq's tcp_user_timeout does not
    # cover it either, so it is deliberately not set. On a CI runner
    # (2026-09-23), a 75 s query through the pooler outlived a 15 s
    # tcp_user_timeout. Against a simulated dead backend the option fired only
    # when the pooler stopped reading in the middle of a send, never while the
    # client waited for a result. The donor re-key that hung for 1h44m that day
    # was waiting on a one-line CREATE TABLE AS. The workflows' step-level
    # timeout-minutes are what end such a hang.
    params.update(sslmode="require", keepalives=1, keepalives_idle=30,
                  keepalives_interval=10, keepalives_count=5)
    # Give up on a connection attempt that never answers; the retry loop below
    # treats "timeout expired" as transient and backs off.
    params["connect_timeout"] = 15
    last = None
    for attempt in range(1, attempts + 1):
        try:
            conn = psycopg2.connect(**params)
            break
        except psycopg2.OperationalError as e:
            msg = str(e)
            transient = ("ECHECKOUTTIMEOUT" in msg              # pooler slots exhausted
                         or "authentication did not complete" in msg
                         or "too many clients" in msg
                         or "timeout expired" in msg
                         or "SSL connection has been closed" in msg   # dropped mid-handshake
                         or "server closed the connection" in msg
                         or "Connection refused" in msg)
            last = e
            if not transient or attempt == attempts:
                raise
            wait = min(60, 5 * 2 ** (attempt - 1))
            log.warning("DB connect attempt %d/%d failed (%s) — retrying in %ds",
                        attempt, attempts, msg.strip().split("\n")[0][:80], wait)
            time.sleep(wait)
    else:  # pragma: no cover — loop always breaks or raises
        raise last
    # Supabase enforces a 2-min statement_timeout by default, which cancels
    # building a GIN trigram index over 3M rows. This is a trusted maintenance
    # connection (service role), so disable the per-statement timeout. Single
    # long statements are routine for its callers: the weekly resolve's donor
    # aggregate UPDATE has run 75-121 s, and concurrent index builds longer.
    # Dashboard publication bounds its own transaction with SET LOCAL instead.
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


# ── DataFrame → COPY-ready CSV ───────────────────────────────────────────────
def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Rename to table columns and coerce dates/numerics so COPY into the typed
    transactions table succeeds. Empty strings become NULL via COPY(NULL '')."""
    present = {src: dst for src, dst in COLUMN_MAP.items() if src in df.columns}
    out = df[list(present.keys())].rename(columns=present).copy()

    # Collapse any duplicate target columns (e.g. both "book type" and
    # "book_type" present) keeping the first non-empty.
    out = out.loc[:, ~out.columns.duplicated()]

    for col in out.columns:
        if col in DATE_COLS:
            s = pd.to_datetime(out[col], errors="coerce")
            out[col] = s.dt.strftime("%Y-%m-%d").where(s.notna(), "")
        elif col in NUMERIC_COLS:
            s = pd.to_numeric(out[col], errors="coerce")
            out[col] = s.map(lambda x: "" if pd.isna(x) else repr(float(x)))
        elif col in BIGINT_COLS:
            s = pd.to_numeric(out[col], errors="coerce")
            out[col] = s.map(lambda x: "" if pd.isna(x) else str(int(x)))
        else:
            out[col] = out[col].fillna("").astype(str)

    return out


# COPY payload size per round-trip. Large default for clean networks (CI);
# override to a small value (e.g. 500) on flaky/inspected TLS links where big
# payloads get dropped: export SUPABASE_COPY_CHUNK=500
_load_dotenv()
COPY_CHUNK_ROWS = int(os.environ.get("SUPABASE_COPY_CHUNK", "25000"))


def _copy_frame(cur, table: str, frame: pd.DataFrame) -> None:
    """COPY a frame in row-chunks so no single COPY payload is huge."""
    cols = list(frame.columns)
    col_list = ", ".join(f'"{c}"' for c in cols)
    copy_sql = f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    for start in range(0, len(frame), COPY_CHUNK_ROWS):
        buf = io.StringIO()
        frame.iloc[start:start + COPY_CHUNK_ROWS].to_csv(buf, index=False, header=True)
        buf.seek(0)
        cur.copy_expert(copy_sql, buf)


# ── Transactions ─────────────────────────────────────────────────────────────
def upsert_transactions(df: pd.DataFrame) -> None:
    """Insert-or-update the given rows (used for the daily changed window)."""
    if not sync_enabled() or df is None or df.empty:
        return
    frame = _prepare_frame(df)
    frame = frame[frame["tran_id"] != ""]  # rows without a PK can't be stored
    if frame.empty:
        return
    cols = list(frame.columns)
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != "tran_id")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _txn_stage (LIKE transactions INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        _copy_frame(cur, "_txn_stage", frame)
        col_list = ", ".join(f'"{c}"' for c in cols)
        cur.execute(
            f"INSERT INTO transactions ({col_list}) "
            f"SELECT {col_list} FROM _txn_stage "
            f"ON CONFLICT (tran_id) DO UPDATE SET {updates}"
        )
        conn.commit()
    log.info("Supabase: upserted %d transactions", len(frame))


def delete_transactions(tran_ids) -> None:
    """Remove rows no longer present (e.g. originals superseded by amendments)."""
    ids = [int(t) for t in tran_ids if str(t).strip().isdigit()]
    if not sync_enabled() or not ids:
        return
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM transactions WHERE tran_id = ANY(%s)", (ids,))
        conn.commit()
    log.info("Supabase: deleted %d superseded transactions", len(ids))


def _load_frame_chunked(frame: pd.DataFrame, attempts: int = 6) -> int:
    """COPY a shard into transactions committing every COPY_CHUNK_ROWS rows, so
    progress is durable and a dropped TLS link only costs one small chunk.
    Reuses one connection; reconnects only when a chunk fails."""
    import time
    import psycopg2
    cols = list(frame.columns)
    col_list = ", ".join(f'"{c}"' for c in cols)
    copy_sql = f"COPY transactions ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    conn = _connect()
    loaded = 0
    try:
        for start in range(0, len(frame), COPY_CHUNK_ROWS):
            sub = frame.iloc[start:start + COPY_CHUNK_ROWS]
            for attempt in range(1, attempts + 1):
                try:
                    buf = io.StringIO()
                    sub.to_csv(buf, index=False, header=True)
                    buf.seek(0)
                    with conn.cursor() as cur:
                        cur.copy_expert(copy_sql, buf)
                    conn.commit()
                    loaded += len(sub)
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    try: conn.close()
                    except Exception: pass
                    if attempt == attempts:
                        raise
                    time.sleep(attempt * 2)
                    conn = _connect()
    finally:
        try: conn.close()
        except Exception: pass
    return loaded


def full_reload_transactions(shard_dir: Path) -> None:
    """One-time / periodic full load: truncate, then COPY every txn_*.csv.gz
    shard on its own connection so a dropped link only retries one shard."""
    if not sync_enabled():
        log.warning("SUPABASE_DB_URL not set — skipping full transaction reload")
        return
    shards = sorted(Path(shard_dir).glob("txn_*.csv.gz"))
    if not shards:
        log.warning("No transaction shards found in %s", shard_dir)
        return

    # Drop ALL secondary indexes for the load. Maintaining 8 btree + 2 GIN
    # indexes per-row during a 3M-row COPY is the dominant cost (it slows to a
    # crawl as the table fills). We capture their definitions, drop them, load
    # into a PK-only table, then rebuild each once — far faster overall.
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'transactions' AND indexname <> 'transactions_pkey'"
            )
            saved_indexes = cur.fetchall()
            for name, _ in saved_indexes:
                cur.execute(f'DROP INDEX IF EXISTS "{name}"')
            cur.execute("TRUNCATE transactions")
    log.info("Supabase: dropped %d secondary indexes, truncated, loading %d shards…",
             len(saved_indexes), len(shards))

    total = 0
    for shard in shards:
        df = pd.read_csv(shard, compression="gzip", dtype=str)
        frame = _prepare_frame(df)
        frame = frame[frame["tran_id"] != ""]
        total += _load_frame_chunked(frame)
        log.info("Supabase: loaded %s (%d rows, %d total)", shard.name, len(frame), total)

    log.info("Supabase: rebuilding %d indexes + analyzing…", len(saved_indexes))
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, indexdef in saved_indexes:
                cur.execute(indexdef)
                log.info("Supabase: rebuilt index %s", name)
            cur.execute("ANALYZE transactions")
    log.info("Supabase: full reload complete — %d transactions", total)


# ── Dashboard aggregate blobs ────────────────────────────────────────────────
def get_dashboard_cache(key: str):
    """Read one dashboard_cache entry, or None.

    Needed so a producer can carry forward a key it does not itself build.
    activity_snapshot has two writers, and the one without district_history was
    overwriting the one with it on every refresh.
    """
    if not sync_enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select data from dashboard_cache where key = %s", (key,))
            row = cur.fetchone()
            if not row:
                return None
            d = row[0]
            return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return None


def require_dashboard_cache(key: str):
    """Read a cache entry or fail loudly.

    Stateful CI consumers use this instead of ``get_dashboard_cache``.  Once
    generated aggregates stop living in Git, treating a database outage as an
    empty/stale local fallback can create invalid provenance or a false
    completion signal.
    """
    if not sync_enabled():
        raise RuntimeError("SUPABASE_DB_URL is required to read dashboard state")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select data from dashboard_cache where key = %s", (key,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"dashboard_cache has no '{key}' entry")
    data = row[0]
    return json.loads(data) if isinstance(data, str) else data


def require_filer_comparison_details() -> list[dict]:
    """Return the small portion of filer_detail used by audit automation.

    Pulling every generated filer JSON would transfer roughly 240 MiB or store
    it in Git again. JSONB projection keeps the same fail-closed evidence while
    moving only scope and paired-comparison fields across the database boundary.
    """
    if not sync_enabled():
        raise RuntimeError("SUPABASE_DB_URL is required to read filer detail state")
    index = require_dashboard_cache("filer_index")
    current_slugs = {
        str(row.get("slug"))
        for row in index
        if isinstance(row, dict) and row.get("slug")
    }
    if not current_slugs:
        raise RuntimeError("dashboard_cache filer_index has no current slugs")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """select slug, name, filer_id,
                      detail->'filer_ids',
                      detail->'orestar_comparison',
                      detail->'closed'
               from filer_detail"""
        )
        rows = cur.fetchall()
    details = [
        {
            "slug": slug,
            "name": name,
            "filer_id": str(filer_id) if filer_id is not None else None,
            "filer_ids": filer_ids,
            "orestar_comparison": comparison,
            "closed": bool(closed),
        }
        for slug, name, filer_id, filer_ids, comparison, closed in rows
        if slug in current_slugs
    ]
    returned_slugs = {row["slug"] for row in details}
    missing = current_slugs - returned_slugs
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        raise RuntimeError(
            f"filer_detail is missing {len(missing)} current filer(s): {sample}"
        )
    return details


_PUBLICATION = ContextVar("dashboard_publication", default=None)
_CACHE_SQL = """INSERT INTO dashboard_cache (key, data, updated_at)
                VALUES (%s, %s::jsonb, now()) ON CONFLICT (key) DO UPDATE
                SET data=EXCLUDED.data, updated_at=now()"""
_DETAIL_SQL = """INSERT INTO filer_detail (slug, name, filer_id, detail, updated_at)
                 VALUES %s ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name,
                 filer_id=EXCLUDED.filer_id, detail=EXCLUDED.detail, updated_at=now()"""


def _publish_dashboard(staged):
    """One commit for all staged outputs; failed writes leave the old generation."""
    from psycopg2.extras import execute_values
    required = {"filer_index", "balance_snapshot_source", "balance_discrepancies"}
    if not required <= staged["caches"].keys() or staged["details"] is None:
        raise RuntimeError("Incomplete balance publication; refusing partial generation")
    index = json.loads(staged["caches"]["filer_index"].read_text())
    expected = {row["slug"] for row in index}
    seen = set()
    with staged["details"].open() as stream:
        for line in stream:
            slug = json.loads(line)["slug"]
            if slug in seen:
                raise RuntimeError("Duplicate committee in staged publication")
            seen.add(slug)
    if seen != expected or len(expected) != len(index):
        raise RuntimeError("Index and committee-detail scopes differ")
    receipt = {"generation": staged["id"], "detail_count": len(seen),
               "cache_sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest()
                                for key, path in staged["caches"].items()}}
    started = time.monotonic()
    log.info("Publication %s: opening transaction for %d details and %d caches",
             staged["id"], len(seen), len(staged["caches"]))
    # Explicit close matters: psycopg's transaction context alone does not close.
    with closing(_connect()) as conn, conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL idle_in_transaction_session_timeout = 120000")
            # A blocked publication should fail and be retried, not queue.
            cur.execute("SET LOCAL lock_timeout = 30000")
            def check_deadline():
                remaining_ms = int((900 - (time.monotonic() - started)) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError("Dashboard publication exceeded 15 minutes")
                cur.execute("SET LOCAL statement_timeout = %s", (min(120000, remaining_ms),))

            def write_batch(batch, done):
                check_deadline()
                execute_values(cur, _DETAIL_SQL, batch,
                               template="(%s, %s, %s, %s::jsonb, now())", page_size=50)
                log.info("Publication %s: uploaded %d/%d details (%.1fs)",
                         staged["id"], done, len(seen), time.monotonic() - started)
            batch, done = [], 0
            with staged["details"].open() as stream:
                for line in stream:
                    row = json.loads(line)
                    batch.append((row["slug"], row.get("name"), row.get("filer_id") or None,
                                  json.dumps(row["detail"], default=str)))
                    done += 1
                    if len(batch) == 50:
                        write_batch(batch, done)
                        batch = []
                if batch:
                    write_batch(batch, done)
            for key, path in staged["caches"].items():
                check_deadline()
                cur.execute(_CACHE_SQL, (key, path.read_text()))
                log.info("Publication %s: uploaded cache %s", staged["id"], key)
            check_deadline()
            cur.execute(_CACHE_SQL, ("balance_publication", json.dumps(receipt)))
    receipt_path = Path(os.environ.get("BALANCE_PUBLICATION_RECEIPT_PATH",
                                        "data/aggregated/balance_publication.json"))
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt))
    log.info("BALANCE_PUBLICATION_COMMITTED generation=%s details=%d elapsed=%.1fs",
             staged["id"], len(seen), time.monotonic() - started)


@contextmanager
def dashboard_publication():
    """Stage a full aggregation without holding a database transaction open."""
    if not sync_enabled() or _PUBLICATION.get() is not None:
        yield
        return
    with tempfile.TemporaryDirectory(prefix="balance-publication-") as directory:
        staged = {"id": str(uuid.uuid4()), "directory": Path(directory),
                  "caches": {}, "details": None}
        token = _PUBLICATION.set(staged)
        started = time.monotonic()
        log.info("Publication %s: starting local aggregation", staged["id"])
        try:
            yield
            log.info("Publication %s: aggregation complete (%.1fs); publishing",
                     staged["id"], time.monotonic() - started)
            _publish_dashboard(staged)
        except BaseException:
            log.exception("Publication %s failed; no success claim. Check generation receipt before retrying.",
                          staged["id"])
            raise
        finally:
            _PUBLICATION.reset(token)


def upsert_dashboard_cache(key: str, data) -> None:
    if not sync_enabled():
        return
    staged = _PUBLICATION.get()
    if staged is not None:
        path = staged["directory"] / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        path.write_text(json.dumps(data, default=str))
        staged["caches"][key] = path
        log.info("Publication %s: staged cache %s", staged["id"], key)
        return
    payload = json.dumps(data, default=str)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dashboard_cache (key, data, updated_at) "
            "VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (key, payload),
        )
        conn.commit()
    log.info("Supabase: cached dashboard aggregate '%s'", key)


def bulk_upsert_filer_detail(rows: list[dict]) -> None:
    """rows: list of {slug, name, filer_id, detail(dict)}."""
    if not sync_enabled() or not rows:
        return
    staged = _PUBLICATION.get()
    if staged is not None:
        if staged["details"] is not None:
            raise RuntimeError("Committee details staged more than once")
        path = staged["directory"] / "details.jsonl"
        with path.open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row, default=str) + "\n")
        staged["details"] = path
        staged["detail_count"] = len(rows)
        log.info("Publication %s: staged %d committee details", staged["id"], len(rows))
        return
    from psycopg2.extras import execute_values
    values = [
        (r["slug"], r.get("name"), r.get("filer_id") or None, json.dumps(r["detail"], default=str))
        for r in rows
    ]
    with _connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO filer_detail (slug, name, filer_id, detail, updated_at) "
            "VALUES %s "
            "ON CONFLICT (slug) DO UPDATE SET "
            "name = EXCLUDED.name, filer_id = EXCLUDED.filer_id, "
            "detail = EXCLUDED.detail, updated_at = now()",
            values,
            template="(%s, %s, %s, %s::jsonb, now())",
            page_size=500,
        )
        conn.commit()
    log.info("Supabase: upserted %d filer_detail rows", len(rows))


# ── Full-dataset CSV upload to Storage (the "Download all" button) ───────────
def upload_full_csv(csv_gz_path: Path) -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "exports")
    if not (url and key) or not Path(csv_gz_path).exists():
        return
    import requests
    object_path = "transactions.csv.gz"
    endpoint = f"{url}/storage/v1/object/{bucket}/{object_path}"
    with open(csv_gz_path, "rb") as f:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/gzip",
                "x-upsert": "true",
            },
            data=f,
        )
    if resp.status_code in (200, 201):
        log.info("Supabase: uploaded full CSV to %s/%s", bucket, object_path)
    else:
        log.warning("Supabase: full CSV upload failed (%s): %s", resp.status_code, resp.text[:200])
