#!/usr/bin/env python3
"""
Find filers with ORESTAR discrepancies that still need work.

An exact identity diff is the only evidence allowed to select a missing-ID
filer for mutation. Count surveys and dollar differences remain useful for
prioritizing which committees to diff, but never authorize a backfill. Selected
IDs are written to /tmp/auto_backfill_ids.txt.

Incomplete filers from previous failed runs are retried. Ordinary retries come
up naturally by exact missing-ID count; only a partially validated identity
tree is priority-boosted so its frozen snapshot can finish before another tree
starts. A historical "done" marker never overrides newer exact evidence.

Used by the backfill workflow in auto mode.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from balance_snapshot import (
    exact_evidence_identifier_is_valid,
)
from exact_coverage_evidence import (
    USABLE_HISTORY_KEY,
    certify_exact_scope_rows,
)
import supabase_sync

FILERS_DIR = Path("data/aggregated/filers")
INCOMPLETE_FILE = Path("data/incomplete_backfills.txt")
OUTPUT_FILE = Path(os.environ.get("AUTO_BACKFILL_OUTPUT", "/tmp/auto_backfill_ids.txt"))
MODE_FILE = Path(os.environ.get("AUTO_BACKFILL_MODE_OUTPUT",
                                "/tmp/auto_backfill_mode.txt"))
END_DATE_FILE = Path(os.environ.get("AUTO_BACKFILL_END_DATE_OUTPUT",
                                    "/tmp/auto_backfill_end_date.txt"))
RESUME_FILE = Path(os.environ.get("AUTO_BACKFILL_RESUME_OUTPUT",
                                  "/tmp/auto_backfill_resume.txt"))
STATUS_FILE = Path(os.environ.get("AUTO_BACKFILL_STATUS_OUTPUT",
                                  "/tmp/auto_backfill_status.txt"))
IDENTITY_BATCH_SIZE = 1

print(f"Working directory: {Path.cwd()}")
print(f"Local filer details available: {FILERS_DIR.exists()}")

# Incomplete filers from previous runs are retried.
# Format: "fid" or "fid:count". Filers with 3+ retries are skipped for now
# (they'll be retried after all other filers are processed).
MAX_RETRIES = 3
incomplete = {}  # fid -> retry count
if INCOMPLETE_FILE.exists():
    for line in INCOMPLETE_FILE.read_text().strip().split("\n"):
        if ":" in line:
            fid_str, cnt = line.split(":", 1)
            fid_str = fid_str.strip()
            incomplete[fid_str] = int(cnt)
        elif line.strip():
            incomplete[line.strip()] = 1
    retryable = {fid for fid, cnt in incomplete.items() if cnt < MAX_RETRIES}
    deferred = {fid for fid, cnt in incomplete.items() if cnt >= MAX_RETRIES}
    if retryable:
        print(f"Incomplete filers to retry: {len(retryable)}")
    if deferred:
        print(f"Deferred filers (>={MAX_RETRIES} retries, skipping for now): {len(deferred)} — {sorted(deferred)}")

# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------
#
# Rank by ROWS MISSING, not by dollar discrepancy.
#
# The dollar ranking sent the backfill after committees it could not help. Of
# the 50 largest deltas measured by the coverage survey, 32 had nothing to
# fetch at all: Committee for SAIF Keeping tops the list at $665,242 and holds
# all seven of the seven transactions ORESTAR has for it. Meanwhile Oregon
# Right to Life is $614,666 adrift and short only 42 rows, while Local 48 is
# short 13,626. Rows missing and dollars adrift turn out to be close to
# independent, so ranking by one to find the other never worked.
#
# The survey asks ORESTAR for its own record count -- one search, no export --
# and records the shortfall. Fetching what it found missing is the only part of
# the discrepancy a backfill can address; the rest is balance reconciliation
# for dormant and pre-ORESTAR committees, which is a different job.
SURVEY_FILE = Path("data/coverage_survey.json")
DIFF_FILE = Path("data/coverage_diff.json")
IDENTITY_PROGRESS_FILE = Path("data/identity_remediation_windows.json")
TRANSACTION_DIR = Path("data/transactions")
def _paired_requirements():
    """Return paired scope requirements, ambiguities, and dollar priorities."""
    scopes = {}
    ownership = {}
    balance_candidates = set()
    invalid_members = set()
    local_paths = list(FILERS_DIR.glob("*.json"))
    if supabase_sync.sync_enabled():
        # CI always uses the authoritative current database population. A
        # partial local aggregate directory must not silently shadow it.
        details = supabase_sync.require_filer_comparison_details()
    elif local_paths:
        details = []
        for path in local_paths:
            try:
                detail = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(detail, dict):
                details.append(detail)
    else:
        raise RuntimeError(
            "No filer comparison details are available locally or from Supabase"
        )
    for detail in details:
        comparison = detail.get("orestar_comparison") or {}
        if comparison.get("status") != "paired":
            continue
        detail_id_values = detail.get("filer_ids")
        captured_id_values = comparison.get("filer_ids")
        ids = sorted({
            text for fid in detail_id_values
            if (text := str(fid or "").strip())
        }) if isinstance(detail_id_values, list) else []
        captured_ids = sorted({
            text for fid in captured_id_values
            if (text := str(fid or "").strip())
        }) if isinstance(captured_id_values, list) else []
        if not ids or captured_ids != ids:
            malformed_members = set(ids) | set(captured_ids)
            invalid_members.update(malformed_members)
            try:
                malformed_delta = abs(float(
                    comparison.get("delta_at_capture") or 0
                ))
            except (TypeError, ValueError, OverflowError):
                malformed_delta = 0
            if (comparison.get("actionable") and not detail.get("closed")
                    and malformed_delta > 0.01):
                balance_candidates.update(malformed_members)
            continue
        captured_at = comparison.get("captured_at")
        transaction_id = comparison.get("app_transaction_snapshot_id")
        if (not ids or not captured_at
                or not exact_evidence_identifier_is_valid(transaction_id)):
            continue
        try:
            capture_day = datetime.fromtimestamp(
                float(captured_at), tz=timezone.utc
            ).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        requirement = {
            "captured_at": captured_at,
            "capture_day": capture_day,
            "transaction_snapshot_id": transaction_id,
            "scope_ids": ids,
        }
        key = tuple(ids)
        existing = scopes.get(key)
        if existing is not None:
            # Two detail files claiming the same physical scope are ambiguous
            # even when their captured values happen to match. There is no
            # unique canonical owner for an automatic mutation.
            requirement = {**requirement, "ambiguous": True}
        scopes[key] = requirement
        for fid in ids:
            ownership.setdefault(fid, set()).add(key)
        try:
            delta = abs(float(comparison.get("delta_at_capture") or 0))
        except (TypeError, ValueError, OverflowError):
            delta = 0
        if (comparison.get("actionable")
                and not detail.get("closed") and delta > 0.01):
            balance_candidates.update(ids)

    ambiguous_members = set(invalid_members)
    for fid, keys in ownership.items():
        if len(keys) > 1 or any(scopes[key].get("ambiguous") for key in keys):
            for key in keys:
                ambiguous_members.update(key)
    requirements = {
        fid: scopes[next(iter(keys))]
        for fid, keys in ownership.items()
        if len(keys) == 1 and fid not in ambiguous_members
    }
    return requirements, ambiguous_members, balance_candidates


def _automation_exact_rows(diff_rows, candidate_ids=(), active_ranges=None):
    """Return exact rows safe enough to authorize automatic remediation.

    An observation bearing the paired global fingerprint anchors the exact
    per-filer digest/range that existed at summary capture. A later query may
    supply the verdict after an unrelated filer changes the global hash, but
    only in that anchored digest/range lane. Current liveness comes from
    recomputing every member's deterministic per-filer digest.
    """
    requirements, ambiguous_members, balance_candidates = _paired_requirements()
    candidates = {str(fid) for fid in candidate_ids if str(fid)} | balance_candidates
    return certify_exact_scope_rows(
        diff_rows,
        requirements,
        candidates,
        TRANSACTION_DIR,
        active_ranges=active_ranges,
        ambiguous_members=ambiguous_members,
    )


def _active_identity_roots():
    """Return filer -> frozen end date for resumable top-level windows."""
    try:
        rows = json.loads(IDENTITY_PROGRESS_FILE.read_text()) \
            if IDENTITY_PROGRESS_FILE.exists() else []
    except Exception:
        rows = []
    roots = {}
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        if (not isinstance(key, list) or len(key) != 7 or key[0] != "ALL"
                or key[3:6] != ["None", "None", "None"]):
            continue
        fid, end = str(key[-1]), str(key[2])
        if end > roots.get(fid, ""):
            roots[fid] = end
    return roots


def _row_requests_exact_resolution(row):
    """Seed candidates from current or historical missing or changed rows."""
    if not isinstance(row, dict):
        return False
    if (row.get("complete") is None or bool(row.get("missing"))
            or bool(row.get("amount_changed")) or bool(row.get("superseded"))):
        return True
    history = row.get(USABLE_HISTORY_KEY) or []
    return isinstance(history, list) and any(
        isinstance(item, dict)
        and (bool(item.get("missing")) or bool(item.get("amount_changed"))
             or bool(item.get("superseded")))
        for item in history
    )


survey_rows = []
if SURVEY_FILE.exists():
    try:
        survey_rows = json.loads(SURVEY_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {SURVEY_FILE}: {exc}")
active_roots = _active_identity_roots()

filers = []
deferred_exact = []
exact_blocked = set()
exact_range_ends = {}
diff_rows = []
if DIFF_FILE.exists():
    try:
        diff_rows = json.loads(DIFF_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {DIFF_FILE}: {exc}")

candidate_ids = {
    str(row.get("filer_id") or "")
    for row in diff_rows
    if row.get("filer_id")
    and _row_requests_exact_resolution(row)
}
survey_short = {
    str(row.get("filer_id") or ""): int(row.get("missing") or 0)
    for row in survey_rows
    if row.get("filer_id") and int(row.get("missing") or 0) > 0
}
candidate_ids.update(survey_short)

automation_rows, exact_blocked, schema_error = _automation_exact_rows(
    diff_rows, candidate_ids, active_roots,
)
if exact_blocked:
    print(
        f"Exact authorization blocked for {len(exact_blocked)} physical filer(s); "
        "count and dollar evidence remain report-only"
    )
if schema_error:
    print(f"Transaction snapshot could not be certified: {schema_error}")
if survey_short:
    print(
        f"Count survey prioritizes {len(survey_short)} short filer(s), but does "
        "not authorize mutation without current exact identity evidence"
    )

work_counts = {}
for r in automation_rows.values():
    fid = str(r.get("filer_id") or "")
    missing = len(r.get("missing") or [])
    # Rows ORESTAR edited in place (same Tran ID, new amount) are repaired by
    # the same forced re-download: the merge keeps the newest copy of an ID.
    changed = len(r.get("amount_changed") or [])
    # "Superseded" rows are originals ORESTAR still returns although an
    # amendment we hold points at them. ORESTAR counts them, and the merge now
    # keeps them (process._drop_superseded), so a re-download restores them.
    restore = len(r.get("superseded") or [])
    exact_range_ends[fid] = str(r["range_end"])
    if missing + changed + restore <= 0:
        continue
    work_counts[fid] = (missing, changed, restore)
    work = missing + changed + restore
    # A partially completed forced tree is resumable even after the ordinary
    # retry limit. Its validated leaves must not be thrown away merely because
    # one runner made no progress.
    resumable = active_roots.get(fid) == exact_range_ends[fid]
    if incomplete.get(fid, 0) >= MAX_RETRIES and not resumable:
        deferred_exact.append((work, fid, r.get("name", "")))
        continue
    # Precise exact identity evidence overrides the historical done list. A
    # count can say "done" while an ORESTAR-absent row cancels the missing row.
    filers.append((work, fid, r.get("name", "")))

# "Deferred" means after the other exact-missing committees, not abandoned.
# Without this pass the workflow eventually emitted no IDs and reported all
# discrepancies addressed while known missing transaction IDs remained.
if not filers and deferred_exact:
    filers = deferred_exact
    print(f"Retrying {len(filers)} deferred exact-missing/changed filer(s); "
          "no non-deferred identity work remains")

if filers:
    # Finish an active frozen tree before starting another committee. This is
    # what turns a later `filer_ids=auto` dispatch into a true resume.
    filers.sort(
        key=lambda row: (
            active_roots.get(row[1]) == exact_range_ends.get(row[1]),
            row[0],
            row[1],
        ),
        reverse=True,
    )
    batch = filers[:IDENTITY_BATCH_SIZE]
    mode = "identity"
    print(f"{len(filers)} filer(s) have current exact missing or changed rows")
else:
    batch = []
    mode = "identity"

if batch:
    print(f"Selecting {len(batch)} filer(s) by exact missing or changed rows:")
    for score, fid, name in batch:
        retry = " (RETRY)" if fid in incomplete else ""
        missing, changed, restore = work_counts.get(fid, (score, 0, 0))
        shown = (f"{missing:,} exact IDs missing, {changed:,} amounts changed, "
                 f"{restore:,} live originals to restore")
        print(f"  {fid}: {shown} — {name}{retry}")
    OUTPUT_FILE.write_text(" ".join(fid for _, fid, _ in batch))
    STATUS_FILE.write_text("selected\n")
    MODE_FILE.write_text(mode + "\n")
    if (mode == "identity"
            and active_roots.get(batch[0][1]) == exact_range_ends[batch[0][1]]):
        END_DATE_FILE.write_text(active_roots[batch[0][1]] + "\n")
        RESUME_FILE.write_text("true\n")
        print(f"Resuming frozen identity tree through {active_roots[batch[0][1]]}")
    elif mode == "identity":
        # The exact diff and the forced fetch must cover the identical
        # inclusive window. Defaulting independently to "today" lets a chain
        # crossing midnight verify a different range from the one that
        # authorized it.
        END_DATE_FILE.write_text(exact_range_ends[batch[0][1]] + "\n")
        RESUME_FILE.write_text("false\n")
else:
    OUTPUT_FILE.unlink(missing_ok=True)
    END_DATE_FILE.unlink(missing_ok=True)
    RESUME_FILE.write_text("false\n")
    MODE_FILE.write_text("identity\n")
    if exact_blocked:
        STATUS_FILE.write_text("blocked\n")
        print(
            "Automatic remediation paused: candidate scopes lack current exact "
            "identity provenance. No data was mutated and this is not a "
            "completion signal."
        )
    else:
        STATUS_FILE.write_text("idle\n")
        print(
            "No automatic repair selected: current exact identity evidence "
            "contains no authorized missing rows or changed amounts."
        )
