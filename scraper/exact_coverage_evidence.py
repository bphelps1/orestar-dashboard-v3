"""Fail-closed selection of exact ORESTAR identity evidence.

Coverage results are durable observations, not permanent facts.  A result can
authorize an automatic action only when it belongs to the same paired app
snapshot, was collected after that ORESTAR summary, covers the full required
date range, and still matches the current per-filer transaction digest.

The selector is shared by missing-row remediation and balance-only omission of
rows ORESTAR no longer returns.  Keeping one implementation prevents the cash
calculation from accepting weaker evidence than the mutation guard.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from balance_snapshot import (
    COVERAGE_EVIDENCE_VERSION,
    evidence_is_current,
    exact_coverage_result_shape_is_valid,
    exact_evidence_identifier_is_valid,
    exact_filer_digest_version,
    transaction_filer_snapshots,
    transaction_snapshot_id,
)


FULL_HISTORY_START = "2006-01-01"
USABLE_HISTORY_KEY = "usable_history"

# Why a filer was refused certification.
#
# The certifier fails closed in eleven distinct places and, until now, reported
# a single number: "137 physical filers refused". That count cannot be acted
# on. A refusal caused by a paired capture that has aged out needs a new
# capture; one caused by rows that moved under the capture needs a re-diff; one
# caused by missing rows needs a backfill; and one caused by ambiguous
# canonical ownership needs a name fix. All four look identical in a count, so
# the backlog could only be worked by guessing.
#
# These are diagnostics, never inputs. Nothing reads a reason back to decide
# anything — the certification verdict is computed exactly as before, and a
# reason is only recorded alongside it.
BLOCK_NO_PAIRED_REQUIREMENT = "no_paired_requirement"
BLOCK_SCOPE_EXCLUDES_FILER = "scope_excludes_filer"
BLOCK_SCOPE_MEMBERS_DISAGREE = "scope_members_disagree"
BLOCK_AMBIGUOUS_SCOPE = "ambiguous_scope"
BLOCK_NO_DIFF_ROW = "no_diff_row"
BLOCK_DUPLICATE_DIFF_ROW = "duplicate_diff_row"
BLOCK_NO_ANCHORED_OBSERVATION = "no_anchored_observation"
BLOCK_NO_COMMON_RANGE = "no_common_range"
BLOCK_CONFLICTING_ACTIVE_RANGES = "conflicting_active_ranges"
BLOCK_INVALID_RANGE_DATES = "invalid_range_dates"
BLOCK_SHARD_READ_ERROR = "shard_read_error"
BLOCK_SHARDS_CHANGED = "shards_changed_during_certification"
BLOCK_DIGEST_MOVED = "digest_moved"
BLOCK_IDENTITY_SETS_MOVED = "identity_sets_moved"
BLOCK_MISSING_ROWS = "blocked_by_missing_rows"


def _record_block(
    blocked: set[str],
    reasons: dict[str, list[str]] | None,
    members: Iterable[str],
    code: str,
) -> None:
    """Mark ``members`` blocked and note why.

    A filer can be refused by more than one scope and for more than one reason
    within a scope — a digest that moved AND missing rows, say — so reasons
    accumulate in order rather than the first or last one winning. Collapsing
    them to a single code would reintroduce exactly the ambiguity this exists
    to remove: a filer recorded only as `blocked_by_missing_rows` would look
    like a backfill away from certifying when its digest has moved too.
    """
    for member in members:
        fid = str(member)
        blocked.add(fid)
        if reasons is None:
            continue
        codes = reasons.setdefault(fid, [])
        if code not in codes:
            codes.append(code)


def _collection_started(row: dict) -> datetime | None:
    """Return a precise UTC collection start, or ``None``."""
    value = row.get("collection_started_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _checked_at(row: dict) -> datetime | None:
    """Return a precise UTC completion time, or ``None``."""
    value = row.get("checked_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _result_signature(row: dict) -> tuple:
    """ORESTAR verdict fields used to reject tied conflicting observations."""
    return (
        row.get("complete"),
        tuple(row.get("missing") or []),
        tuple(row.get("surplus") or []),
        tuple(row.get("superseded") or []),
        row.get("orestar"),
        row.get("held"),
    )


def _observation_is_well_formed(row: Any, filer_id: str) -> bool:
    """Whether one exact observation has the complete structured schema."""
    if not isinstance(row, dict):
        return False
    started = _collection_started(row)
    completed = _checked_at(row)
    try:
        range_start = date.fromisoformat(str(row.get("range_start") or ""))
        range_end = date.fromisoformat(str(row.get("range_end") or ""))
    except ValueError:
        return False
    return (
        exact_coverage_result_shape_is_valid(row)
        and exact_evidence_identifier_is_valid(row.get("filer_id"))
        and row.get("filer_id") == filer_id
        and row.get("evidence_version") == COVERAGE_EVIDENCE_VERSION
        and exact_evidence_identifier_is_valid(
            row.get("transaction_snapshot_id")
        )
        and started is not None
        and completed is not None
        and started <= completed
        and range_start == date.fromisoformat(FULL_HISTORY_START)
        and range_end >= range_start
    )


def _usable_observation(row: Any, requirement: dict, filer_id: str) -> bool:
    return (
        _observation_is_well_formed(row, filer_id)
        and evidence_is_current(
            row,
            requirement["captured_at"],
            require_precise=True,
            require_collection_started=True,
            strictly_after=True,
            range_start=FULL_HISTORY_START,
            minimum_range_end=requirement["capture_day"],
        )
    )


def _looks_structured(row: dict) -> bool:
    """Whether a history row claims the precise evidence schema."""
    return any(key in row for key in (
        "evidence_version",
        "collection_started_at",
        "checked_at",
        "transaction_snapshot_id",
        "filer_transaction_digest",
        "filer_digest_version",
        "range_start",
        "range_end",
    ))


def _anchored_observation_lanes(
    row: dict,
    requirement: dict,
    filer_id: str,
) -> dict[tuple[str, str], dict] | None:
    """Newest verdict in every capture-anchored range/digest lane.

    The observation carrying the paired global fingerprint proves which local
    per-filer state existed at capture.  A later query after an unrelated shard
    change may supply the verdict, but only for that same digest and range.
    """
    # Never fall back around a malformed or stale top-level result.  It is the
    # latest successful write and could otherwise conceal conflicting evidence.
    if not _usable_observation(row, requirement, filer_id):
        return None
    history = row.get(USABLE_HISTORY_KEY, [])
    if not isinstance(history, list) or any(
        not isinstance(item, dict) for item in history
    ):
        return None
    # Any claimed owner must be this physical filer, including legacy records.
    if any(
        (claimed_owner := str(item.get("filer_id") or "").strip())
        and claimed_owner != filer_id
        for item in history
    ):
        return None
    # Legacy rows remain displayable but cannot authorize anything.  A record
    # claiming any structured provenance field must be completely valid; do
    # not step around a malformed record that could hide a newer verdict.
    if any(
        _looks_structured(item)
        and not _observation_is_well_formed(item, filer_id)
        for item in history
    ):
        return None

    usable = [
        item for item in [row, *history]
        if _usable_observation(item, requirement, filer_id)
    ]
    anchor_digests: dict[tuple[str, str, int], set[str]] = {}
    for item in usable:
        if item.get("transaction_snapshot_id") != requirement["transaction_snapshot_id"]:
            continue
        versioned_bounds = (
            str(item.get("range_start") or ""),
            str(item.get("range_end") or ""),
            exact_filer_digest_version(item),
        )
        anchor_digests.setdefault(versioned_bounds, set()).add(
            item.get("filer_transaction_digest")
        )
    # One paired snapshot has one state per physical filer, range and digest
    # algorithm. Different algorithms are expected to have different hashes;
    # they cannot supply capture anchors for one another.
    if any(len(digests) != 1 for digests in anchor_digests.values()):
        return None

    anchored_lanes = {
        (*versioned_bounds, next(iter(digests)))
        for versioned_bounds, digests in anchor_digests.items()
    }

    def observation_lane(item: dict) -> tuple:
        return (
            str(item.get("range_start") or ""),
            str(item.get("range_end") or ""),
            exact_filer_digest_version(item),
            item.get("filer_transaction_digest"),
        )

    newest_any_start = max(_collection_started(item) for item in usable)
    newest_any = [
        item for item in usable
        if _collection_started(item) == newest_any_start
    ]
    # Apply revocation across all supported versions before selecting a lane.
    # A later unanchored state/range/algorithm cannot hide behind an older
    # capture, even when that older observation uses the current algorithm.
    if any(observation_lane(item) not in anchored_lanes for item in newest_any):
        return None

    lanes: dict[tuple[str, str], dict] = {}
    for bounds in {key[:2] for key in anchor_digests}:
        observations = [
            item for item in usable
            if observation_lane(item)[:2] == bounds
            and observation_lane(item) in anchored_lanes
        ]
        # A newer verdict in either version supersedes the other version's
        # verdict for this range. Never let iteration order choose the result.
        newest_start = max(_collection_started(item) for item in observations)
        newest = [
            item for item in observations
            if _collection_started(item) == newest_start
        ]
        if len({_result_signature(item) for item in newest}) != 1:
            return None
        # Completion time breaks a same-query-start tie only after identical
        # verdicts are proved. It cannot rescue a pre-summary query.
        lanes[bounds] = max(newest, key=_checked_at)
    return lanes


def _requirement_signature(requirement: Any) -> tuple | None:
    """Normalize and validate one paired-snapshot scope requirement."""
    if not isinstance(requirement, dict):
        return None
    raw_members = requirement.get("scope_ids")
    if not isinstance(raw_members, (list, tuple)) or not raw_members:
        return None
    if any(not exact_evidence_identifier_is_valid(fid) for fid in raw_members):
        return None
    members = tuple(sorted(set(raw_members)))
    if len(members) != len(raw_members):
        return None
    fingerprint = requirement.get("transaction_snapshot_id")
    if not exact_evidence_identifier_is_valid(fingerprint):
        return None
    try:
        captured_at = float(requirement["captured_at"])
        capture_day = datetime.fromtimestamp(
            captured_at, tz=timezone.utc,
        ).date().isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if requirement.get("capture_day") != capture_day:
        return None
    return members, captured_at, capture_day, fingerprint


def rows_requesting_surplus(diff_rows: Iterable[dict]) -> set[str]:
    """Physical filer IDs with a current or historical surplus observation."""
    candidates: set[str] = set()
    for row in diff_rows:
        if not isinstance(row, dict):
            continue
        fid = row.get("filer_id")
        if not exact_evidence_identifier_is_valid(fid):
            continue
        history = row.get(USABLE_HISTORY_KEY, [])
        observations = [row]
        if isinstance(history, list):
            observations.extend(item for item in history if isinstance(item, dict))
        if any(isinstance(item.get("surplus"), list) and item["surplus"]
               for item in observations):
            candidates.add(fid)
    return candidates


def certify_exact_scope_rows(
    diff_rows: Iterable[dict],
    requirements: dict[str, dict],
    candidate_ids: Iterable[str],
    transaction_dir: Path,
    *,
    active_ranges: dict[str, str] | None = None,
    ambiguous_members: Iterable[str] = (),
    require_no_missing: bool = False,
    reasons: dict[str, list[str]] | None = None,
) -> tuple[dict[str, dict], set[str], str | None]:
    """Return exact rows safe to act on, blocked IDs, and a scan error.

    Certification is atomic at canonical-scope level.  Every physical member
    must share a valid anchored range, and every selected row must match a
    deterministic digest recomputed from the current transaction shards.

    Pass a dict as ``reasons`` to collect why each blocked filer was refused,
    as ``{filer_id: [code, ...]}`` drawn from the ``BLOCK_*`` constants above.
    It is an output collector and nothing more: the verdict does not depend on
    it, and omitting it leaves behaviour identical.
    """
    active_ranges = active_ranges or {}
    ambiguous = {str(fid) for fid in ambiguous_members if str(fid)}
    candidates = {str(fid) for fid in candidate_ids if str(fid)}
    source_rows = diff_rows.values() if isinstance(diff_rows, dict) else diff_rows
    rows_by_id: dict[str, dict] = {}
    duplicate_ids: set[str] = set()
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        fid = row.get("filer_id")
        if not exact_evidence_identifier_is_valid(fid):
            continue
        if fid in rows_by_id:
            duplicate_ids.add(fid)
        else:
            rows_by_id[fid] = row

    relevant_scopes: dict[tuple[str, ...], dict] = {}
    blocked: set[str] = set()
    for fid in candidates:
        requirement = requirements.get(fid)
        signature = _requirement_signature(requirement)
        if signature is None:
            _record_block(blocked, reasons, [fid], BLOCK_NO_PAIRED_REQUIREMENT)
            continue
        members = signature[0]
        if fid not in members:
            _record_block(
                blocked, reasons, [*members, fid], BLOCK_SCOPE_EXCLUDES_FILER,
            )
            continue
        # Every member must independently point to precisely the same paired
        # scope.  This rejects partial or overlapping canonical ownership.
        if any(_requirement_signature(requirements.get(member)) != signature
               for member in members):
            _record_block(
                blocked, reasons, members, BLOCK_SCOPE_MEMBERS_DISAGREE,
            )
            continue
        relevant_scopes[members] = requirement

    pending = []
    for members, requirement in relevant_scopes.items():
        if any(member in ambiguous for member in members):
            _record_block(blocked, reasons, members, BLOCK_AMBIGUOUS_SCOPE)
            continue
        member_lanes: dict[str, dict[tuple[str, str], dict]] = {}
        # Same three conditions as before, in the same order and with the same
        # early break — separated only so the refusal can name which one fired.
        # "no anchored observation" is the interesting one: it means the diff
        # measured this filer against a transaction snapshot the paired capture
        # never adopted, which is a re-capture, not a re-scrape.
        invalid = None
        for member in members:
            row = rows_by_id.get(member)
            if row is None:
                invalid = BLOCK_NO_DIFF_ROW
                break
            if member in duplicate_ids:
                invalid = BLOCK_DUPLICATE_DIFF_ROW
                break
            lanes = _anchored_observation_lanes(row, requirement, member)
            if not lanes:
                invalid = BLOCK_NO_ANCHORED_OBSERVATION
                break
            member_lanes[member] = lanes
        common_bounds = (
            set.intersection(*(set(lanes) for lanes in member_lanes.values()))
            if member_lanes else set()
        )
        if invalid:
            _record_block(blocked, reasons, members, invalid)
            continue
        if not common_bounds:
            _record_block(blocked, reasons, members, BLOCK_NO_COMMON_RANGE)
            continue

        active_ends = {
            active_ranges[member] for member in members if member in active_ranges
        }
        if len(active_ends) > 1:
            _record_block(
                blocked, reasons, members, BLOCK_CONFLICTING_ACTIVE_RANGES,
            )
            continue
        active_bounds = (
            (FULL_HISTORY_START, next(iter(active_ends))) if active_ends else None
        )
        if active_bounds in common_bounds:
            chosen_bounds = active_bounds
        else:
            # Prefer the lane whose least-recent member observation is newest;
            # greedy per-member choices could mix unrelated ranges.
            chosen_bounds = max(
                common_bounds,
                key=lambda bounds: (
                    min(
                        _collection_started(member_lanes[member][bounds])
                        for member in members
                    ),
                    bounds,
                ),
            )
        rows = [
            (member, member_lanes[member][chosen_bounds]) for member in members
        ]
        try:
            start = date.fromisoformat(chosen_bounds[0])
            end = date.fromisoformat(chosen_bounds[1])
        except ValueError:
            _record_block(blocked, reasons, members, BLOCK_INVALID_RANGE_DATES)
            continue
        pending.append((members, rows, start, end))

    # Group by range and declared algorithm, not by filer. Legacy observations
    # are verified with the unchanged v1 algorithm; no stored row is relabeled.
    grouped: dict[tuple[date, date], dict] = {}
    for members, rows, start, end in pending:
        group = grouped.setdefault((start, end), {"versions": {}, "scopes": []})
        for member, row in rows:
            version = exact_filer_digest_version(row)
            group["versions"].setdefault(version, set()).add(member)
        group["scopes"].append((members, rows))

    valid: dict[str, dict] = {}
    schema_error = None
    for (start, end), group in grouped.items():
        snapshot_before = transaction_snapshot_id(transaction_dir)
        try:
            current_by_version = {
                version: transaction_filer_snapshots(
                    transaction_dir, ids, start, end, digest_version=version,
                )
                for version, ids in group["versions"].items()
            }
        except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
            schema_error = str(exc)
            for members, _rows in group["scopes"]:
                _record_block(blocked, reasons, members, BLOCK_SHARD_READ_ERROR)
            continue
        if (not snapshot_before
                or transaction_snapshot_id(transaction_dir) != snapshot_before):
            schema_error = "transaction shards changed during certification"
            for members, _rows in group["scopes"]:
                _record_block(blocked, reasons, members, BLOCK_SHARDS_CHANGED)
            continue
        for members, rows in group["scopes"]:
            digest_matches = True
            identity_sets_match = True
            for member, row in rows:
                version = exact_filer_digest_version(row)
                snapshot = current_by_version[version].get(member, {})
                if (exact_filer_digest_version(snapshot) != version
                        or snapshot.get("filer_transaction_digest")
                        != row.get("filer_transaction_digest")):
                    digest_matches = False
                held_ids = snapshot.get("held_ids") or set()
                superseded_ids = snapshot.get("superseded_ids") or set()
                missing = set(row.get("missing") or [])
                surplus = set(row.get("surplus") or [])
                superseded = set(row.get("superseded") or [])
                # Rows ORESTAR re-priced under the same ID are rows we hold.
                changed = {
                    item.get("tran_id")
                    for item in row.get("amount_changed") or []
                    if isinstance(item, dict)
                }
                if (row.get("held") != len(held_ids)
                        or not changed.issubset(held_ids)
                        or not surplus.issubset(held_ids)
                        or not missing.isdisjoint(held_ids | superseded_ids)
                        or not superseded.issubset(superseded_ids)
                        or not superseded.isdisjoint(held_ids)):
                    identity_sets_match = False
                    break
            scope_has_no_missing = (
                not require_no_missing
                or all(not row.get("missing") for _member, row in rows)
            )
            if digest_matches and identity_sets_match and scope_has_no_missing:
                valid.update(rows)
            else:
                # Three unrelated failures shared one refusal here, and they
                # call for three different remedies: a moved digest means the
                # filer's rows changed under the capture (re-diff), moved
                # identity sets mean the stored surplus/missing/superseded no
                # longer describe the shards (re-diff, and a data question),
                # and missing rows mean a backfill must land first. Record
                # every one that fired, not just the first.
                if not digest_matches:
                    _record_block(
                        blocked, reasons, members, BLOCK_DIGEST_MOVED,
                    )
                if not identity_sets_match:
                    _record_block(
                        blocked, reasons, members, BLOCK_IDENTITY_SETS_MOVED,
                    )
                if not scope_has_no_missing:
                    _record_block(
                        blocked, reasons, members, BLOCK_MISSING_ROWS,
                    )
    return valid, blocked, schema_error
