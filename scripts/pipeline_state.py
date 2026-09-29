#!/usr/bin/env python3
"""Persist scraper checkpoints in Supabase Storage instead of Git.

State is split into profiles so a chained account-summary scrape does not
download the 143 MiB transaction ledger every batch. Blobs are immutable and
content-addressed. The authoritative manifest lives in
``dashboard_cache('pipeline_state_manifest')`` and is advanced under a
Postgres advisory session lock with a generation compare-and-swap. Storage
``latest.json`` and ``previous.json`` objects are recovery mirrors only.

Environment:
  SUPABASE_DB_URL
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_STORAGE_BUCKET       defaults to ``exports``
  PIPELINE_STATE_PREFIX         defaults to ``pipeline-state/v1``
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests


SCHEMA_VERSION = 1
DEFAULT_BUCKET = "exports"
DEFAULT_PREFIX = "pipeline-state/v1"
PROFILE_NAMES = ("transactions", "summaries", "auxiliary")
BASE_FILE = ".pipeline-state-base.json"
TRANSACTION_GLOB = "txn_*.csv.gz"
MANIFEST_CACHE_KEY = "pipeline_state_manifest"
# Signed int64 spelling of the ASCII bytes ``PIPELINE``. Every cooperative
# publisher for this Supabase project must take the same session-scoped
# lock before advancing the authoritative pointer.
PUBLICATION_LOCK_ID = int.from_bytes(b"PIPELINE", "big", signed=True)

# Per-run counters/markers are intentionally absent: a successor recomputes
# them rather than inheriting an earlier run's completion signal.
PROFILE_FILES = {
    "transactions": (
        "data/fetched_windows.json",
        # Progress of the history-wide lumped-row re-read (fetch.py misc-reread).
        "data/fetched_windows_misc.json",
        "data/fetched_windows_tran.json",
        # Every deletion record the merge applied, with each row it removed kept
        # in full. Travels with the shards: they no longer hold those rows.
        "data/orestar_deletions.json",
        "data/record_counts.json",
        "data/truncated_windows.json",
    ),
    "summaries": (
        "data/account_summary_sweep_state.json",
        "data/earliest_balances.json",
        "data/orestar_cash_balances.json",
        # Certificates of Limited Contributions and Expenditures: which years
        # ORESTAR's itemized totals cannot see. Travels with the annual
        # summaries because the ghost rows are derived from the two together.
        "data/orestar_certificates.json",
        # Early-era amendment chains: versions ORESTAR's search hides but its
        # summaries still count. Checked against the annual summaries above.
        "data/orestar_amendment_chains.json",
        "data/orestar_yearly_summaries.json",
    ),
    "auxiliary": (
        # Rows each merge re-priced (lumped rows ORESTAR edited in place).
        "data/amount_updates.json",
        "data/backfilled_filers.txt",
        "data/candidate_filings.json",
        "data/coverage_diff.json",
        "data/coverage_survey.json",
        "data/filer_metadata.json",
        "data/identity_remediation_failures.json",
        "data/identity_remediation_windows.json",
        "data/incomplete_backfills.txt",
        # This is the filer-id mapping consumed by process.py. It is not the
        # same shape as the monthly leadership_roles table, whose newly scraped
        # name-based rows may have no filer_id and cannot replace this input.
        "data/leadership_roles.json",
    ),
}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class StateError(RuntimeError):
    """A state snapshot could not be safely read or published."""


def _load_dotenv(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _prefix() -> str:
    prefix = os.environ.get("PIPELINE_STATE_PREFIX", DEFAULT_PREFIX).strip("/")
    if not prefix or any(part in ("", ".", "..") for part in prefix.split("/")):
        raise StateError("PIPELINE_STATE_PREFIX is unsafe")
    return prefix


def _config(root: Path) -> tuple[str, str, str]:
    _load_dotenv(root)
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", DEFAULT_BUCKET)
    if not url or not key or not bucket:
        raise StateError(
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and a storage bucket are required"
        )
    return url, key, bucket


def _database_config(root: Path) -> dict[str, str]:
    """Return psycopg2 kwargs without requiring URL-escaped passwords.

    Supabase connection strings in this repository historically contain raw
    URL-special characters. Match ``scraper.supabase_sync``'s parsing rather
    than passing the URI back through a URL parser.
    """
    _load_dotenv(root)
    dsn = os.environ.get("SUPABASE_DB_URL", "")
    if not dsn:
        raise StateError("SUPABASE_DB_URL is required for authoritative pipeline state")
    body = dsn.split("://", 1)[1] if "://" in dsn else dsn
    userpass, separator, hostpart = body.rpartition("@")
    user, password_separator, password = userpass.partition(":")
    hostport, path_separator, dbname = hostpart.partition("/")
    host, port_separator, port = hostport.rpartition(":")
    if not all(
        (
            separator,
            password_separator,
            path_separator,
            port_separator,
            user,
            password,
            host,
            port,
        )
    ):
        raise StateError("SUPABASE_DB_URL is malformed")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "dbname": dbname.split("?", 1)[0] or "postgres",
        "sslmode": "require",
        "connect_timeout": "30",
        "keepalives": "1",
        "keepalives_idle": "30",
        "keepalives_interval": "10",
        "keepalives_count": "5",
    }


def _connect_database(root: Path):
    config = _database_config(root)
    try:
        import psycopg2
    except ImportError as exc:
        raise StateError("psycopg2 is required for authoritative pipeline state") from exc

    last_error: Exception | None = None
    for attempt in range(1, 7):
        try:
            return psycopg2.connect(**config)
        except psycopg2.OperationalError as exc:
            last_error = exc
            message = str(exc)
            transient = any(
                marker in message
                for marker in (
                    "ECHECKOUTTIMEOUT",
                    "authentication did not complete",
                    "too many clients",
                    "timeout expired",
                    "SSL connection has been closed",
                    "server closed the connection",
                    "Connection refused",
                )
            )
            if not transient or attempt == 6:
                break
            time.sleep(min(60, 5 * 2 ** (attempt - 1)))
        except Exception as exc:
            last_error = exc
            break
    raise StateError(
        f"Could not connect to authoritative pipeline state: {last_error}"
    ) from last_error


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _transaction_snapshot_id(root: Path) -> str | None:
    """Match balance_snapshot.transaction_snapshot_id without importing scraper code."""
    paths = sorted((root / "data" / "transactions").glob(TRANSACTION_GLOB))
    if not paths:
        return None
    digest = hashlib.sha256(b"orestar-transaction-snapshot-v1\0")
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _valid_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _profile_paths(root: Path, profile: str) -> list[Path]:
    return [root / rel for rel in PROFILE_FILES[profile] if (root / rel).is_file()]


def _tar_info(path: Path, root: Path) -> tarfile.TarInfo:
    info = tarfile.TarInfo(path.relative_to(root).as_posix())
    stat = path.stat()
    info.size = stat.st_size
    info.mode = stat.st_mode & 0o777
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _build_profile_archive(root: Path, profile: str, output: Path) -> dict:
    """Build a deterministic small archive and return member checksums."""
    members = {}
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for path in _profile_paths(root, profile):
                    relative = path.relative_to(root).as_posix()
                    members[relative] = {
                        "sha256": _sha256_path(path),
                        "size": path.stat().st_size,
                    }
                    with path.open("rb") as source:
                        archive.addfile(_tar_info(path, root), source)
    return members


def _allowed_member(profile: str, member_name: str) -> bool:
    pure = PurePosixPath(member_name)
    return (
        not pure.is_absolute()
        and ".." not in pure.parts
        and member_name in PROFILE_FILES[profile]
    )


def _transaction_entries(root: Path) -> list[dict]:
    entries = []
    for path in sorted((root / "data" / "transactions").glob(TRANSACTION_GLOB)):
        digest = _sha256_path(path)
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "object": f"{_prefix()}/objects/{digest}",
                "sha256": digest,
                "size": path.stat().st_size,
            }
        )
    return entries


def _validate_object(entry: object) -> dict:
    if not isinstance(entry, dict):
        raise StateError("Manifest contains a malformed object")
    name, digest, size = entry.get("object"), entry.get("sha256"), entry.get("size")
    valid_digest = (
        isinstance(digest, str)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )
    if (
        not valid_digest
        or name != f"{_prefix()}/objects/{digest}"
        or not isinstance(size, int)
        or size < 0
    ):
        raise StateError("Manifest contains an unsafe object reference")
    return entry


def _validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION:
        raise StateError("Unsupported or malformed pipeline-state manifest")
    try:
        uuid.UUID(value.get("generation", ""))
    except (AttributeError, TypeError, ValueError):
        raise StateError("Pipeline-state manifest has no generation")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_NAMES):
        raise StateError("Pipeline-state manifest has the wrong profile set")
    for profile in PROFILE_NAMES:
        entry = profiles[profile]
        if not isinstance(entry, dict):
            raise StateError(f"Malformed {profile} profile")
        archive = _validate_object(entry.get("archive"))
        members = archive.get("members")
        if not isinstance(members, dict):
            raise StateError(f"The {profile} profile has no member inventory")
        for relative, details in members.items():
            if not _allowed_member(profile, relative):
                raise StateError(f"Unsafe {profile} member: {relative}")
            if (
                not isinstance(details, dict)
                or not isinstance(details.get("sha256"), str)
                or len(details["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in details["sha256"])
                or not isinstance(details.get("size"), int)
                or details["size"] < 0
            ):
                raise StateError(f"Malformed checksum for {relative}")
        if profile == "transactions":
            if not _valid_snapshot_id(entry.get("transaction_snapshot_id")):
                raise StateError("Transaction profile has no exact snapshot identifier")
            shards = entry.get("shards")
            if not isinstance(shards, list) or not shards:
                raise StateError("Transaction profile has no ledger shards")
            seen = set()
            for shard in shards:
                _validate_object(shard)
                relative = shard.get("path")
                pure = PurePosixPath(relative) if isinstance(relative, str) else None
                if (
                    pure is None
                    or len(pure.parts) != 3
                    or pure.parts[:2] != ("data", "transactions")
                    or not pure.name.startswith("txn_")
                    or not pure.name.endswith(".csv.gz")
                    or relative in seen
                ):
                    raise StateError("Manifest contains an unsafe transaction path")
                seen.add(relative)
        elif "shards" in entry:
            raise StateError(f"Unexpected shards in {profile} profile")
    return value


def _headers(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "apikey": key, **extra}


def _object_url(url: str, bucket: str, name: str, *, download: bool) -> str:
    route = "object/authenticated" if download else "object"
    return f"{url}/storage/v1/{route}/{quote(bucket, safe='')}/{quote(name, safe='/')}"


def _request(method: str, url: str, *, attempts: int = 4, **kwargs) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, timeout=(30, 300), **kwargs)
            if response.status_code not in RETRYABLE_STATUS:
                return response
            last_error = StateError(
                f"Storage returned HTTP {response.status_code}: {response.text[:200]}"
            )
        except requests.RequestException as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(15, 2 ** (attempt - 1)))
    raise StateError(f"Storage request failed after {attempts} attempts: {last_error}")


def _download(config: tuple[str, str, str], name: str) -> bytes | None:
    url, key, bucket = config
    response = _request(
        "GET", _object_url(url, bucket, name, download=True), headers=_headers(key)
    )
    missing = response.status_code == 404
    if response.status_code == 400:
        try:
            error = response.json()
            missing = (
                str(error.get("statusCode")) == "404"
                or error.get("code") in {"NoSuchKey", "not_found"}
            )
        except (ValueError, AttributeError):
            pass
    if missing:
        return None
    if response.status_code != 200:
        raise StateError(
            f"Could not download {name}: HTTP {response.status_code} {response.text[:200]}"
        )
    return response.content


def _upload(
    config: tuple[str, str, str],
    name: str,
    payload: bytes | Path,
    content_type: str,
    *,
    upsert: bool,
) -> None:
    url, key, bucket = config
    headers = _headers(
        key, **{"Content-Type": content_type, "x-upsert": str(upsert).lower()}
    )
    last_error: Exception | None = None
    for attempt in range(1, 5):
        try:
            if isinstance(payload, Path):
                with payload.open("rb") as source:
                    response = requests.post(
                        _object_url(url, bucket, name, download=False),
                        headers=headers,
                        data=source,
                        timeout=(30, 300),
                    )
            else:
                response = requests.post(
                    _object_url(url, bucket, name, download=False),
                    headers=headers,
                    data=payload,
                    timeout=(30, 300),
                )
            if response.status_code in (200, 201):
                return
            text = response.text[:500]
            duplicate = response.status_code in (400, 409) and any(
                marker in text.lower()
                for marker in ("already exists", "duplicate", "resourcealreadyexists")
            )
            if not upsert and duplicate:
                return
            last_error = StateError(
                f"Upload of {name} returned HTTP {response.status_code}: {text}"
            )
            if response.status_code not in RETRYABLE_STATUS:
                break
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 4:
            time.sleep(min(15, 2 ** (attempt - 1)))
    raise StateError(str(last_error))


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_storage_manifest(
    config: tuple[str, str, str], name: str = "latest.json"
) -> dict | None:
    """Read a non-authoritative recovery mirror from Storage."""
    payload = _download(config, f"{_prefix()}/{name}")
    if payload is None:
        return None
    try:
        return _validate_manifest(json.loads(payload))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateError(f"Remote {name} is not valid JSON: {exc}") from exc


def _manifest_from_database(value: object) -> dict:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return _validate_manifest(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateError(f"Authoritative pipeline state is not valid JSON: {exc}") from exc


def _select_database_manifest(cursor) -> dict | None:
    cursor.execute(
        "select data from dashboard_cache where key = %s",
        (MANIFEST_CACHE_KEY,),
    )
    row = cursor.fetchone()
    return _manifest_from_database(row[0]) if row else None


def _read_manifest(root: Path) -> dict | None:
    """Read the authoritative manifest directly from Postgres."""
    connection = _connect_database(root)
    try:
        with connection.cursor() as cursor:
            return _select_database_manifest(cursor)
    except StateError:
        raise
    except Exception as exc:
        raise StateError(f"Could not read authoritative pipeline state: {exc}") from exc
    finally:
        connection.close()


def _read_base(root: Path) -> dict | None:
    try:
        value = json.loads((root / BASE_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != SCHEMA_VERSION
        or not isinstance(value.get("generation"), str)
    ):
        return None
    profiles = value.get("hydrated_profiles")
    if (
        not isinstance(profiles, list)
        or any(not isinstance(profile, str) for profile in profiles)
        or len(profiles) != len(set(profiles))
        or set(profiles) - set(PROFILE_NAMES)
    ):
        return None
    return value


def _write_base(root: Path, manifest: dict, hydrated_profiles) -> None:
    hydrated = set(hydrated_profiles)
    if hydrated - set(PROFILE_NAMES):
        raise StateError("Refusing to record unknown hydrated profiles")
    payload = (
        json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "generation": manifest["generation"],
                "transaction_snapshot_id": manifest["profiles"]["transactions"][
                    "transaction_snapshot_id"
                ],
                "hydrated_profiles": [
                    profile for profile in PROFILE_NAMES if profile in hydrated
                ],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    destination = root / BASE_FILE
    temporary = root / f"{BASE_FILE}.{uuid.uuid4()}.tmp"
    try:
        temporary.write_text(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _create_manifest(
    root: Path, selected: tuple[str, ...], previous: dict | None
) -> tuple[dict, dict[str, Path]]:
    if previous is None and set(selected) != set(PROFILE_NAMES):
        raise StateError("The first snapshot must include every state profile")
    profiles = dict(previous["profiles"]) if previous else {}
    archives: dict[str, Path] = {}
    try:
        for profile in selected:
            handle = tempfile.NamedTemporaryFile(
                prefix=f"pipeline-{profile}-", suffix=".tar.gz", delete=False
            )
            handle.close()
            archive_path = Path(handle.name)
            archives[profile] = archive_path
            members = _build_profile_archive(root, profile, archive_path)
            digest = _sha256_path(archive_path)
            entry = {
                "archive": {
                    "object": f"{_prefix()}/objects/{digest}",
                    "sha256": digest,
                    "size": archive_path.stat().st_size,
                    "members": members,
                }
            }
            if profile == "transactions":
                shards = _transaction_entries(root)
                if not shards:
                    raise StateError("Refusing to publish a transaction profile with no shards")
                entry["shards"] = shards
                entry["transaction_snapshot_id"] = _transaction_snapshot_id(root)
            profiles[profile] = entry
        manifest = {
            "schema": SCHEMA_VERSION,
            "generation": str(uuid.uuid4()),
            "parent_generation": previous.get("generation") if previous else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_repository": os.environ.get("GITHUB_REPOSITORY", "local-bootstrap"),
            "source_revision": os.environ.get("GITHUB_SHA", "local-bootstrap"),
            "profiles": profiles,
        }
        return _validate_manifest(manifest), archives
    except Exception:
        for path in archives.values():
            path.unlink(missing_ok=True)
        raise


def _commit_manifest(
    root: Path,
    config: tuple[str, str, str],
    manifest: dict,
    expected_generation: str | None,
) -> dict | None:
    """Serialize a generation CAS and its best-effort Storage mirrors.

    A session advisory lock survives the database commit. That lets the
    authoritative pointer commit before any network mirror I/O while still
    preventing an older publisher from overwriting a newer publisher's fixed
    Storage paths. Closing the connection releases the lock even if explicit
    unlock fails.
    """
    if manifest.get("parent_generation") != expected_generation:
        raise StateError("New manifest does not descend from the expected generation")
    connection = _connect_database(root)
    locked = False
    committed = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (PUBLICATION_LOCK_ID,))
            locked = True
            current = _select_database_manifest(cursor)
            actual_generation = current.get("generation") if current else None
            if actual_generation != expected_generation:
                raise StateError(
                    "Remote state advanced during publication; pull and retry"
                )

            encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            if expected_generation is None:
                cursor.execute(
                    "insert into dashboard_cache (key, data, updated_at) "
                    "values (%s, %s::jsonb, now()) "
                    "on conflict (key) do nothing returning key",
                    (MANIFEST_CACHE_KEY, encoded),
                )
            else:
                cursor.execute(
                    "update dashboard_cache "
                    "set data = %s::jsonb, updated_at = now() "
                    "where key = %s and data->>'generation' = %s returning key",
                    (encoded, MANIFEST_CACHE_KEY, expected_generation),
                )
            if cursor.fetchone() is None:
                raise StateError(
                    "Authoritative manifest compare-and-swap failed; pull and retry"
                )
        connection.commit()
        committed = True

        try:
            if current is not None:
                _upload(
                    config,
                    f"{_prefix()}/previous.json",
                    _manifest_bytes(current),
                    "application/json",
                    upsert=True,
                )
            _upload(
                config,
                f"{_prefix()}/latest.json",
                _manifest_bytes(manifest),
                "application/json",
                upsert=True,
            )
        except Exception as exc:
            # The database row already committed and is authoritative. Keeping
            # the session lock through this attempt preserves mirror ordering;
            # a later successful publisher will repair both fixed paths.
            print(f"WARNING: Storage manifest mirror deferred: {exc}", file=sys.stderr)
        return current
    except StateError:
        if not committed:
            connection.rollback()
        raise
    except Exception as exc:
        if not committed:
            connection.rollback()
            raise StateError(
                f"Could not commit authoritative pipeline state: {exc}"
            ) from exc
        print(f"WARNING: post-commit pipeline-state error: {exc}", file=sys.stderr)
        return current
    finally:
        if locked:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select pg_advisory_unlock(%s)", (PUBLICATION_LOCK_ID,)
                    )
                connection.commit()
            except Exception as exc:
                print(
                    f"WARNING: explicit pipeline-state unlock failed; "
                    f"closing the session: {exc}",
                    file=sys.stderr,
                )
        connection.close()


def publish(
    root: Path,
    selected: tuple[str, ...],
    *,
    bootstrap: bool,
    dry_run: bool,
    garbage_collect: bool,
) -> dict:
    if garbage_collect:
        raise StateError(
            "Pipeline-state garbage collection is disabled during the Storage cutover"
        )
    config = None if dry_run else _config(root)
    previous = None if dry_run else _read_manifest(root)
    base = _read_base(root)
    if bootstrap:
        if previous is not None:
            raise StateError("Refusing bootstrap because a snapshot already exists")
    else:
        if previous is None:
            raise StateError("No snapshot exists; use the explicit bootstrap command once")
        if base is None or base["generation"] != previous["generation"]:
            raise StateError(
                "Local state was not pulled from the current generation; pull again before push"
            )
        unhydrated = set(selected) - set(base["hydrated_profiles"])
        if unhydrated:
            raise StateError(
                "Refusing to publish profile(s) not hydrated from this generation: "
                + ", ".join(sorted(unhydrated))
            )
    manifest, archives = _create_manifest(root, selected, previous)
    try:
        changed_size = sum(manifest["profiles"][name]["archive"]["size"] for name in selected)
        if "transactions" in selected:
            changed_size += sum(
                row["size"] for row in manifest["profiles"]["transactions"]["shards"]
            )
        print(
            f"Prepared profiles {', '.join(selected)} "
            f"({changed_size / 1024 / 1024:.1f} MiB logical publication)."
        )
        if dry_run:
            return manifest

        local_shards = {
            path.relative_to(root).as_posix(): path
            for path in (root / "data" / "transactions").glob(TRANSACTION_GLOB)
        }
        for profile in selected:
            if profile == "transactions":
                for shard in manifest["profiles"][profile]["shards"]:
                    _upload(
                        config,
                        shard["object"],
                        local_shards[shard["path"]],
                        "application/gzip",
                        upsert=False,
                    )
            archive = manifest["profiles"][profile]["archive"]
            _upload(
                config,
                archive["object"],
                archives[profile],
                "application/gzip",
                upsert=False,
            )

        expected = previous.get("generation") if previous else None
        _commit_manifest(root, config, manifest, expected)
        hydrated = set(selected)
        if base is not None:
            hydrated.update(base["hydrated_profiles"])
        _write_base(root, manifest, hydrated)
        print(
            f"Published generation {manifest['generation']} from "
            f"{manifest['source_repository']} @ {manifest['source_revision']}."
        )
        return manifest
    finally:
        for path in archives.values():
            path.unlink(missing_ok=True)


def _extract_archive(staging: Path, profile: str, payload: bytes, entry: dict) -> None:
    if _sha256_bytes(payload) != entry["sha256"]:
        raise StateError(f"Downloaded {profile} archive failed its checksum")
    expected, seen = entry["members"], set()
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not _allowed_member(profile, member.name):
                    raise StateError(f"Refusing unexpected {profile} member: {member.name}")
                details = expected.get(member.name)
                if details is None or member.name in seen:
                    raise StateError(f"Unexpected {profile} member: {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise StateError(f"Could not read {profile} member: {member.name}")
                target = staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                if digest.hexdigest() != details["sha256"] or size != details["size"]:
                    raise StateError(f"State member failed verification: {member.name}")
                seen.add(member.name)
    except (tarfile.TarError, OSError) as exc:
        raise StateError(f"Could not extract {profile} archive: {exc}") from exc
    if seen != set(expected):
        raise StateError(f"The {profile} archive omitted expected files")


def _quarantine_omitted_members(
    root: Path,
    profile: str,
    present: set[str],
    quarantine: Path,
    moved: list[tuple[Path, Path]],
) -> list[tuple[Path, Path]]:
    """Remove stale profile members from service without destroying local data."""
    for relative in PROFILE_FILES[profile]:
        if relative in present:
            continue
        target = root / relative
        if not target.exists() and not target.is_symlink():
            continue
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise StateError(f"Refusing to replace non-file state path: {relative}")
        destination = quarantine / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, destination)
        moved.append((destination, target))
    return moved


def _restore_install(
    installed: list[tuple[Path, Path]],
    backups: list[tuple[Path, Path]],
    quarantined: list[tuple[Path, Path]],
) -> list[str]:
    """Best-effort rollback of an interrupted verified-state installation."""
    failures = []
    for target, staged in reversed(installed):
        try:
            if not target.exists() and not target.is_symlink():
                raise OSError(f"installed path disappeared: {target}")
            staged.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, staged)
        except OSError as exc:
            failures.append(f"preserve {target}: {exc}")
    for source, target in reversed(backups + quarantined):
        try:
            if target.exists() or target.is_symlink():
                raise OSError(f"active path unexpectedly exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError as exc:
            failures.append(f"restore {target}: {exc}")
    return failures


def pull(root: Path, selected: tuple[str, ...]) -> dict:
    config = _config(root)
    manifest = _read_manifest(root)
    if manifest is None:
        raise StateError("No pipeline-state snapshot exists; bootstrap it before running jobs")
    prior_base = _read_base(root)
    hydrated = set(selected)
    if prior_base is not None and prior_base["generation"] == manifest["generation"]:
        hydrated.update(prior_base["hydrated_profiles"])
    staging = Path(tempfile.mkdtemp(prefix="pipeline-state-pull-"))
    retain_staging = False
    try:
        for profile in selected:
            profile_entry = manifest["profiles"][profile]
            archive_entry = profile_entry["archive"]
            payload = _download(config, archive_entry["object"])
            if payload is None:
                raise StateError(f"Missing {profile} archive: {archive_entry['object']}")
            _extract_archive(staging, profile, payload, archive_entry)
            if profile == "transactions":
                directory = staging / "data" / "transactions"
                directory.mkdir(parents=True, exist_ok=True)
                for shard in profile_entry["shards"]:
                    payload = _download(config, shard["object"])
                    if payload is None:
                        raise StateError(f"Missing transaction object: {shard['object']}")
                    if _sha256_bytes(payload) != shard["sha256"] or len(payload) != shard["size"]:
                        raise StateError(f"Transaction shard failed verification: {shard['path']}")
                    (directory / PurePosixPath(shard["path"]).name).write_bytes(payload)

        quarantine = root / ".pipeline-state-quarantine" / str(uuid.uuid4())
        replaced_root = quarantine / ".replaced"
        installed: list[tuple[Path, Path]] = []
        backups: list[tuple[Path, Path]] = []
        quarantined: list[tuple[Path, Path]] = []
        base_target = root / BASE_FILE
        base_invalidated = False
        try:
            # The base file is the local publication authorization. Move it
            # out of service before changing any hydrated file so an abrupt
            # interruption can never leave a mixed checkout authorized to
            # publish. A successful install writes a fresh base last.
            if base_target.exists() or base_target.is_symlink():
                if (
                    base_target.exists()
                    and base_target.is_dir()
                    and not base_target.is_symlink()
                ):
                    raise StateError(f"Refusing to replace directory: {BASE_FILE}")
                base_backup = replaced_root / BASE_FILE
                base_backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(base_target, base_backup)
                backups.append((base_backup, base_target))
            base_invalidated = True

            # Each old destination is moved aside before its verified
            # replacement is installed. Tracking the staged source lets a
            # rollback preserve the downloaded bytes while restoring the old
            # checkout exactly.
            replacements: list[tuple[Path, Path, str]] = []
            for profile in selected:
                members = manifest["profiles"][profile]["archive"]["members"]
                replacements.extend(
                    (staging / relative, root / relative, relative)
                    for relative in members
                )
                if profile == "transactions":
                    replacements.extend(
                        (path, root / "data" / "transactions" / path.name,
                         f"data/transactions/{path.name}")
                        for path in (staging / "data" / "transactions").glob(
                            TRANSACTION_GLOB
                        )
                    )

            for source, target, relative in replacements:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    if target.exists() and target.is_dir() and not target.is_symlink():
                        raise StateError(
                            f"Refusing to replace non-file state path: {relative}"
                        )
                    backup = replaced_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, backup)
                    backups.append((backup, target))
                os.replace(source, target)
                installed.append((target, source))

            for profile in selected:
                members = manifest["profiles"][profile]["archive"]["members"]
                _quarantine_omitted_members(
                    root, profile, set(members), quarantine, quarantined
                )
                if profile == "transactions":
                    target_dir = root / "data" / "transactions"
                    wanted = {
                        PurePosixPath(row["path"]).name
                        for row in manifest["profiles"][profile]["shards"]
                    }
                    for old in target_dir.glob(TRANSACTION_GLOB):
                        if old.name in wanted:
                            continue
                        destination = quarantine / old.relative_to(root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(old, destination)
                        quarantined.append((destination, old))
            _write_base(root, manifest, hydrated)
        except Exception as exc:
            retain_staging = True
            restore_failures = []
            if base_invalidated and (
                base_target.exists() or base_target.is_symlink()
            ):
                try:
                    failed_base = staging / f"{BASE_FILE}.failed"
                    os.replace(base_target, failed_base)
                except OSError as restore_exc:
                    restore_failures.append(
                        f"preserve failed {base_target}: {restore_exc}"
                    )
            restore_failures.extend(
                _restore_install(installed, backups, quarantined)
            )
            if restore_failures:
                raise StateError(
                    "Pipeline-state install failed and quarantine recovery was "
                    "incomplete; verified files and retained copies are under "
                    f"{staging} and "
                    f"{quarantine}: {'; '.join(restore_failures)}"
                ) from exc
            shutil.rmtree(quarantine, ignore_errors=True)
            print(
                f"WARNING: Verified pipeline state retained at {staging} after "
                "the local install was rolled back.",
                file=sys.stderr,
            )
            if isinstance(exc, StateError):
                raise
            raise StateError(f"Could not install verified pipeline state: {exc}") from exc

        # Superseded versions of manifest-present files are rollback data, not
        # durable history. Keep the quarantine only when omitted stale files
        # were actually moved out of service.
        shutil.rmtree(replaced_root, ignore_errors=True)
        if not quarantined:
            shutil.rmtree(quarantine, ignore_errors=True)
        if quarantined:
            print(
                f"Quarantined {len(quarantined)} stale checkpoint file(s) under "
                f"{quarantine}; remove them after validating the new generation."
            )
        print(f"Hydrated generation {manifest['generation']}: {', '.join(selected)}.")
        return manifest
    finally:
        if not retain_staging:
            shutil.rmtree(staging, ignore_errors=True)


def status(root: Path) -> dict:
    manifest = _read_manifest(root)
    if manifest is None:
        raise StateError("No pipeline-state snapshot exists")
    shards = manifest["profiles"]["transactions"]["shards"]
    print(f"Generation: {manifest['generation']}")
    print(f"Created:    {manifest['created_at']}")
    print(f"Source:     {manifest['source_repository']} @ {manifest['source_revision']}")
    print(
        f"Ledger:     {len(shards)} shards, "
        f"{sum(row['size'] for row in shards) / 1024 / 1024:.1f} MiB"
    )
    print(
        "Snapshot:   "
        f"{manifest['profiles']['transactions']['transaction_snapshot_id']}"
    )
    for profile in PROFILE_NAMES:
        archive = manifest["profiles"][profile]["archive"]
        print(f"{profile.title():12}{len(archive['members'])} checkpoint files")
    return manifest


def _profiles(values: list[str] | None) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(values or PROFILE_NAMES))
    unknown = set(selected) - set(PROFILE_NAMES)
    if unknown:
        raise StateError(f"Unknown state profile(s): {', '.join(sorted(unknown))}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pull", "push", "bootstrap", "status"))
    # Do not combine choices= with nargs="*": Python 3.11 can validate the
    # empty list itself as a choice, making the profile-less `status` command
    # fail before main() runs. _profiles() performs the same validation.
    parser.add_argument("profiles", nargs="*")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true", help="build only; do not upload")
    parser.add_argument("--no-gc", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        selected = _profiles(args.profiles)
        if args.command == "pull":
            if args.dry_run:
                parser.error("--dry-run is only valid with push/bootstrap")
            pull(root, selected)
        elif args.command in ("push", "bootstrap"):
            publish(
                root,
                selected,
                bootstrap=args.command == "bootstrap",
                dry_run=args.dry_run,
                # Immutable objects remain retained during cutover. Cleanup
                # needs a separate reachability design and is never automatic.
                garbage_collect=False,
            )
        else:
            if args.profiles or args.dry_run:
                parser.error("status does not accept profiles or --dry-run")
            status(root)
    except StateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
