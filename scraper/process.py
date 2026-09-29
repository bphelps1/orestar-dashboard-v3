"""
process.py — Clean, deduplicate, and aggregate ORESTAR Excel exports.

Pipeline:
  1. Read all Excel files from data/_raw/
  2. Deduplicate by ORESTAR transaction ID
  3. Normalize contributor/payee names
  4. Fuzzy-deduplicate names (rapidfuzz)
  5. Apply entity_map.json overrides
  6. Aggregate to JSON files for the dashboard
  7. Write/update data/transactions.csv.gz
  8. Delete raw Excel files
"""

import gzip
import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from rapidfuzz import fuzz, process as rfuzz_process
from donor_labels import build_donor_label_map, donor_label_key, normalize_donor_label

from balance_snapshot import (
    CALCULATION_VERSION,
    EMPTY_CASH_SCOPE_DIGEST,
    SOURCE_FILENAME,
    build_source as build_balance_snapshot_source,
    cash_scope_digests,
    evidence_is_current,
    exact_coverage_result_shape_is_valid,
    exact_evidence_identifier_is_valid,
    paired_comparison,
    transaction_snapshot_id,
)
from exact_coverage_evidence import (
    certify_exact_scope_rows,
    rows_requesting_surplus,
)
from orestar_amendment_chains import (
    CASH_BUCKETS,
    CASH_SIGN,
    CHAIN_ID_PREFIX,
    CHAIN_SUB_TYPE,
    CHAINS_FILENAME,
    evaluate_target,
    targets_for_filer,
)
from orestar_closures import (
    CLOSURE_ID_PREFIX,
    CLOSURE_SUB_TYPE,
    closure_reset,
    discontinued_on,
)
from orestar_parse import INDEPENDENT_FILER_TYPE
from orestar_certificates import (
    CERTIFICATES_FILENAME,
    GHOST_ID_PREFIX,
    GHOST_SUB_TYPE,
    certificate_restatements,
    certificates_by_filer,
)
import orestar_parse
import supabase_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT          = Path(__file__).parent.parent
RAW_DIR       = ROOT / "data" / "_raw"
DATA_DIR      = ROOT / "data"
# Account-summary fields that add up across committees. Used when one
# canonical name covers several ORESTAR committees, so the comparison totals
# describe the same set on both sides.
# The three in-kind varieties ORESTAR recognises. All are non-cash and all
# appear in BOTH its Total Contributions and Total Expenditures lines.
# ORESTAR states filing dates in Oregon local time; our capture instants are
# UTC epochs. Comparing the two without converting is an off-by-one-day error
# for most captures, because the balance jobs run between 00:00 and 06:00 UTC
# — which is the PREVIOUS afternoon or evening in Oregon. A contribution filed
# on the 14th Pacific therefore looks, to a naive UTC date comparison, as
# though it preceded a capture stamped 01:38 UTC on the 14th. It did not.
ORESTAR_TZ = ZoneInfo("America/Los_Angeles")


def _capture_local_date(comparison: dict | None):
    """Oregon-local calendar date of a paired capture instant, or None.

    ORESTAR's account summary states what had been FILED when it was read, so
    a like-for-like comparison has to bound our side the same way. `filed_date`
    carries only day precision, which is why this returns a date rather than
    the instant: rows filed ON this date cannot be placed before or after the
    capture, and are reported as an indeterminate band rather than guessed at.
    """
    try:
        captured_at = float((comparison or {}).get("captured_at"))
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(captured_at, ORESTAR_TZ).date()
    except (OverflowError, OSError, ValueError):
        return None


INKIND_SUBTYPES = frozenset({
    "In-Kind Contribution",
    "In-Kind/Forgiven Account Payable",
    "In-Kind/Forgiven Personal Expenditures",   # plural, as ORESTAR spells it
})

def _chain_breaks(yearly: dict) -> dict:
    """Where ORESTAR's own summaries fail to carry a balance forward.

    Returns {"total": signed sum, "boundaries": count, "detail": [...]}, or an
    empty dict when the chain is intact. Purely internal to ORESTAR's data, so
    unlike summary_vs_itemised it needs no evidence about our row coverage.
    """
    yrs = sorted(y for y in (yearly or {}) if str(y).isdigit())
    if len(yrs) < 2:
        return {}
    total = 0.0
    detail = []
    for i, y in enumerate(yrs[:-1]):
        nxt = yrs[i + 1]
        end = float((yearly.get(y) or {}).get("ending_cash_balance") or 0.0)
        beg = float((yearly.get(nxt) or {}).get("beginning_balance") or 0.0)
        jump = round(beg - end, 2)
        if abs(jump) > 0.01:
            total += jump
            detail.append({"from": str(y), "to": str(nxt), "amount": jump})
    if not detail:
        return {}
    return {"total": round(total, 2), "boundaries": len(detail), "detail": detail[:12]}


_ROW_COMPLETE: dict = {}
_ORESTAR_ABSENT: dict = {}


def _orestar_absent_for_filers(
    filer_ids,
    evidence: dict | None = None,
) -> set[str]:
    """Union certified ORESTAR-nonreturning IDs for a canonical aggregate."""
    source = _ORESTAR_ABSENT if evidence is None else evidence
    out: set[str] = set()
    for fid in filer_ids or []:
        out.update(source.get(str(fid)) or set())
    return out


def _aggregate_row_verdict(
    filer_ids,
    summary_ts,
    evidence: dict | None = None,
    *,
    transaction_id: str | None = None,
) -> bool | None:
    """Completeness for an aggregate, requiring current evidence for every filer."""
    source = _ROW_COMPLETE if evidence is None else evidence
    rows = [source.get(str(fid)) for fid in (filer_ids or [])]
    if not rows or any(row is None for row in rows):
        return None
    verdicts = [row[0] for row in rows]
    if False in verdicts:
        return False
    if not all(verdict is True for verdict in verdicts) or not summary_ts:
        return None
    evidence_rows = [row[1] for row in rows]
    current = []
    for item in evidence_rows:
        if isinstance(item, dict):
            if "complete" in item and transaction_id:
                # Exact identity evidence can support a paired-summary claim
                # only when it describes these exact local bytes and covers
                # the summary's inclusive date horizon. Historical date-only
                # rows remain visible but cannot certify this attribution.
                try:
                    summary_day = datetime.fromtimestamp(
                        float(summary_ts), tz=timezone.utc
                    ).date()
                except (TypeError, ValueError, OverflowError):
                    current.append(False)
                    continue
                current.append(evidence_is_current(
                    item,
                    summary_ts,
                    require_precise=True,
                    require_collection_started=True,
                    strictly_after=True,
                    transaction_snapshot_id=transaction_id,
                    range_start=date(2006, 1, 1),
                    minimum_range_end=summary_day,
                ))
            else:
                current.append(evidence_is_current(item, summary_ts))
        elif item is not None:
            # Compatibility for tests/callers passing a bare legacy date. A
            # date-only value means midnight UTC and is conservatively stale
            # for a summary read later that day.
            current.append(evidence_is_current({"checked": str(item)}, summary_ts))
        else:
            current.append(False)
    return True if all(current) else None


def _row_diff() -> tuple[dict, list[dict]]:
    """The row-level diff, when one has been run.

    Returns ({filer_id: (complete, evidence_row)}, raw evidence rows). Keeping
    the full timestamped observations avoids reducing same-day ordering to an
    unknowable date comparison. Raw surplus values are deliberately not
    projected here: they require paired-snapshot and current-shard
    certification before they may affect cash.

    diff_coverage.py compares tran_id SETS rather than counts, which is the
    only thing that can settle completeness here: ORESTAR's search returns
    superseded originals we drop on purpose, so a committee holding everything
    it should still reports fewer rows than ORESTAR, permanently. Counts have
    now been wrong in both directions — `held >= orestar` certified committees
    carrying surplus, `held == orestar` rejects committees where supersession
    worked. The diff distinguishes all three cases explicitly.

    `complete: null` means the diff could not be trusted (ORESTAR's results UI
    stops at 5,000 rows silently, so a short collection is refused rather than
    merged). That is carried through as None — unknown, not clean.
    """
    path = DATA_DIR / "coverage_diff.json"
    if not path.exists():
        return {}, []
    try:
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            return {}, []
        complete = {}
        for e in rows:
            if not isinstance(e, dict) or "filer_id" not in e:
                continue
            fid = str(e["filer_id"])
            # Presence matters. `complete: null` is an explicit refusal from
            # the identity diff and must override the count survey with
            # unknown; omitting it here silently falls back to the very
            # count-only evidence this file says is not authoritative.
            if "complete" in e:
                verdict = e["complete"] if type(e["complete"]) is bool else None
                if (e.get("evidence_version") is not None
                        and not exact_coverage_result_shape_is_valid(e)):
                    verdict = None
                # `complete` is an identity verdict. Holding every ID is not
                # holding every dollar when ORESTAR has since edited a row in
                # place, and "rows complete" is what lets the site say a gap
                # is ORESTAR's own summary disagreeing with its itemised rows.
                # A committee with drifted amounts cannot make that claim.
                if verdict is True and e.get("amount_changed"):
                    verdict = False
                complete[fid] = (verdict, e)
        return complete, rows
    except Exception:
        return {}, []


def _live_original_ids(rows: list | None = None) -> dict[str, str]:
    """Amended originals ORESTAR still counts: tran_id -> physical filer ID.

    Read from the current exact coverage result for each filer. A diff searches
    ORESTAR the way its public default does, which leaves out transactions
    ORESTAR expired, so an original it still returns is live there. Both lists
    qualify: ``superseded`` (live, and dropped by us) and ``live_originals``
    (live, whether or not we hold it). The second is what keeps the evidence
    once the row is restored, because a held row is no longer "superseded".

    Only well-formed exact results count. A legacy or malformed row, or a
    failed diff, contributes nothing, so without evidence the merge drops
    originals exactly as before. If ORESTAR later expires one, the next diff
    stops listing it and the merge drops it again.
    """
    if rows is None:
        path = DATA_DIR / "coverage_diff.json"
        try:
            rows = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            return {}
    live: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if (not isinstance(row, dict) or row.get("evidence_version") is None
                or type(row.get("complete")) is not bool
                or not exact_coverage_result_shape_is_valid(row)):
            continue
        fid = str(row.get("filer_id") or "").strip()
        for tran_id in [*(row.get("superseded") or []),
                        *(row.get("live_originals") or [])]:
            live[str(tran_id).strip()] = fid
    return live


def _paired_evidence_requirements(
    name_to_fids: dict[str, list[str]],
    comparisons: dict[str, dict],
) -> tuple[dict[str, dict], set[str]]:
    """Build unique physical-scope requirements for exact evidence.

    A physical filer claimed by multiple canonical aggregates, a duplicate
    aggregate, or a captured scope that differs from the current one is
    ambiguous.  The entire affected scope is refused rather than partially
    suppressing its cash rows.
    """
    scopes: dict[tuple[str, ...], dict] = {}
    ownership: dict[str, set[tuple[str, ...]]] = defaultdict(set)
    scope_owners: dict[tuple[str, ...], list[str]] = defaultdict(list)
    normalized_scopes: dict[str, list[str]] = {}
    invalid_members: set[str] = set()

    # Ownership is a property of the current canonical mapping, not of whether
    # a particular aggregate happened to receive a usable paired capture.  If
    # an unpaired/invalid aggregate overlaps a paired one, registering only the
    # paired side lets its certified omissions leak into both balances through
    # _orestar_absent_for_filers().  Inventory every current scope first so any
    # overlap or duplicate owner fails closed before evidence is considered.
    for name, raw_ids in name_to_fids.items():
        ids = sorted({str(fid).strip() for fid in raw_ids if str(fid).strip()})
        normalized_scopes[name] = ids
        if ids:
            key = tuple(ids)
            scope_owners[key].append(name)
            for fid in ids:
                ownership[fid].add(key)

    for name, ids in normalized_scopes.items():
        comparison = comparisons.get(name) or {}
        if comparison.get("status") != "paired":
            continue
        captured_values = comparison.get("filer_ids")
        captured_ids = sorted({
            str(fid).strip() for fid in captured_values if str(fid).strip()
        }) if isinstance(captured_values, list) else []
        if (not ids or captured_ids != ids
                or any(not exact_evidence_identifier_is_valid(fid) for fid in ids)):
            invalid_members.update(ids)
            invalid_members.update(captured_ids)
            continue
        fingerprint = comparison.get("app_transaction_snapshot_id")
        try:
            captured_at = float(comparison["captured_at"])
            capture_day = datetime.fromtimestamp(
                captured_at, tz=timezone.utc,
            ).date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_members.update(ids)
            continue
        if (not exact_evidence_identifier_is_valid(fingerprint)
                or comparison.get("orestar_data_changed_since_capture")):
            invalid_members.update(ids)
            continue

        key = tuple(ids)
        requirement = {
            "captured_at": captured_at,
            "capture_day": capture_day,
            "transaction_snapshot_id": fingerprint,
            "scope_ids": ids,
        }
        if key in scopes:
            requirement["ambiguous"] = True
        scopes[key] = requirement

    ambiguous_members = set(invalid_members)
    for key, owners in scope_owners.items():
        if len(owners) > 1:
            ambiguous_members.update(key)
    for fid, keys in ownership.items():
        if len(keys) > 1 or any(
            key in scopes and scopes[key].get("ambiguous") for key in keys
        ):
            for key in keys:
                ambiguous_members.update(key)

    requirements = {}
    for fid, keys in ownership.items():
        if len(keys) != 1 or fid in ambiguous_members:
            continue
        key = next(iter(keys))
        if key in scopes:
            requirements[fid] = scopes[key]
    return requirements, ambiguous_members


def _certified_orestar_absent(
    diff_rows,
    name_to_fids: dict[str, list[str]],
    comparisons: dict[str, dict],
    transaction_dir: Path,
    reasons: dict[str, list[str]] | None = None,
) -> tuple[dict[str, set[str]], set[str], str | None]:
    """Certify balance-only omissions without deleting stored rows.

    Pass a dict as ``reasons`` to collect why each refused filer was refused.
    It is an output collector, not a result: the return shape is unchanged so
    existing callers keep working, and certification itself is untouched. The
    point is only that the outcome stops being reported as a bare count, which
    cannot be worked — a capture that has aged out, rows that moved under the
    capture, and an un-backfilled shortfall all produce the same number and
    need different remedies.
    """
    requirements, ambiguous = _paired_evidence_requirements(
        name_to_fids, comparisons,
    )
    candidates = rows_requesting_surplus(diff_rows)
    certified, blocked, error = certify_exact_scope_rows(
        diff_rows,
        requirements,
        candidates,
        transaction_dir,
        ambiguous_members=ambiguous,
        require_no_missing=True,
        reasons=reasons,
    )
    absent = {
        fid: set(row.get("surplus") or [])
        for fid, row in certified.items()
        if row.get("surplus")
    }
    return absent, blocked, error


def _row_completeness() -> dict:
    """filer_id -> True when we hold every row ORESTAR reports for that filer.

    From the coverage survey, which asks ORESTAR how many records it holds and
    compares that against ours. It is the only evidence that can separate "our
    data is short" from "ORESTAR's summary disagrees with ORESTAR's own
    itemised transactions", and without it the second claim cannot honestly be
    made — a line difference looks identical either way.

    Missing file is not an error: every caller treats an absent filer as
    unknown and says nothing rather than guessing.
    """
    path = DATA_DIR / "coverage_survey.json"
    if not path.exists():
        return {}
    try:
        out = {}
        for e in json.loads(path.read_text()):
            # Judged on the RAW COUNTS, not on `missing`.
            #
            # `missing` is written as max(orestar - ours, 0) — clamped — so a
            # surplus on our side arrives as 0 and is indistinguishable from a
            # perfect match. Testing `missing == 0` therefore behaves exactly
            # like `<= 0` and cannot see a surplus at all. The raw `orestar`
            # and `held` counts are recorded alongside it and are unambiguous,
            # so they decide.
            #
            # A surplus disqualifies the itemised comparison exactly as a
            # shortfall does: the claim it supports is "our line sums ARE
            # ORESTAR's itemised sums", and holding rows ORESTAR does not have
            # breaks that as thoroughly as lacking rows it does.
            #
            # Plumbers & Steamfitters PAC is the live case — 11,766 held
            # against ORESTAR's 11,750, sixteen surplus rows worth $32,284.04,
            # every one of them absent from the current ORESTAR result while
            # still held by our append-only pipeline. Its re-survey recorded
            # missing=0, which would have
            # certified it complete and published a note blaming ORESTAR for a
            # difference that is ours.
            #
            # `missing` is ORESTAR's count minus ours, so a surplus on our side
            # comes through NEGATIVE and `<= 0` waved it through as complete.
            # A surplus disqualifies the itemised comparison exactly as a
            # shortfall does: the claim it supports is "our line sums ARE
            # ORESTAR's itemised sums", and holding rows ORESTAR does not have
            # breaks that just as thoroughly as lacking rows it does.
            #
            # Plumbers & Steamfitters PAC is the live case. A backfill left it
            # holding 11,766 rows against ORESTAR's 11,750 — 16 surplus, all in
            # 2026, inflating its contributions by $32,284.04. Under `<= 0` a
            # fresh survey would have certified it complete and published a
            # note blaming ORESTAR for a difference we introduced.
            _o, _h = e.get("orestar"), e.get("held")
            if _o is not None and _h is not None:
                _complete = (int(_o) == int(_h))
            else:                       # older survey rows without the counts
                _complete = (e.get("missing") or 0) == 0
            out[str(e["filer_id"])] = (_complete, e)
        return out
    except Exception:
        return {}


# Summaries scraped before the #90 parser fix report $0.00 in every loan field,
# so the figure can only be trusted from this instant on. Used by the non-exempt
# correction (#96) and the exempt one below.
_LOAN_FIELD_TRUSTWORTHY_FROM = 1787500800.0


def _summary_scrape_ts(summary: dict | None, fallback: float = 0.0) -> float:
    """Oldest contributing scrape timestamp for a committee-year.

    ``scrape_ts`` is the capture time for a physical filer. Combined multi-ID
    summaries additionally carry ``scrape_ts_min`` because parser-dependent
    fields are trustworthy only when every contributing page was scraped
    after the parser fix. A fresh current-page read must never promote untouched
    historical years through the filer-level fallback timestamp.
    """
    row = summary or {}
    try:
        if "scrape_ts_min" in row:
            value = row.get("scrape_ts_min")
        elif "scrape_ts" in row:
            value = row.get("scrape_ts")
        else:
            value = fallback
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _loan_fields_trustworthy(summary: dict | None, fallback: float = 0.0) -> bool:
    row = summary or {}
    # New-format combined rows carry the oldest component's parser version.
    # Never let one freshly parsed member bless a sibling whose optional loan
    # fields still came from the legacy synthetic-zero parser.
    version = row.get("summary_field_version_min", row.get("summary_field_version"))
    if version is not None:
        try:
            if int(version) < _SUMMARY_FIELD_VERSION:
                return False
        except (TypeError, ValueError):
            return False
    return _summary_scrape_ts(summary, fallback) >= _LOAN_FIELD_TRUSTWORTHY_FROM


def _summary_cash_fields_match_year(
    summary: dict | None,
    current_year_digest: str | None,
    comparison_available: bool,
) -> bool:
    """Whether one annual row was read against this exact transaction year.

    A current-page capture is profile-wide evidence only for the page it read.
    Historical annual rows keep independent provenance so a fresh 2026 page
    cannot bless a cached 2025 loan figure after a late 2025 backfill.  A new
    2026 filing likewise must not invalidate a proven 2006 treatment.
    """
    row = summary or {}
    return bool(
        comparison_available
        and exact_evidence_identifier_is_valid(current_year_digest)
        and row.get("app_year_transaction_digest") == current_year_digest
        and row.get("calculation_version") == CALCULATION_VERSION
        and exact_evidence_identifier_is_valid(row.get("scope_capture_id"))
    )

_SUMMABLE = ("ending_cash_balance", "beginning_balance", "contributions",
             "expenditures", "other_receipts", "other_disbursements",
             "balance_adjustments", "inkind_contributions",
             "inkind_expenditures", "loans_received",
             "loans_received_exempt", "loan_payments",
             "loan_payments_exempt", "accounts_receivable",
             "accounts_payable", "total_outstanding_loans",
             "outstanding_personal_expenditures", "balance_deficit")

_NULLABLE_SUMMARY_FIELDS = {
    "inkind_contributions", "inkind_expenditures", "loans_received",
    "loans_received_exempt", "loan_payments", "loan_payments_exempt",
    "accounts_receivable", "accounts_payable", "total_outstanding_loans",
    "outstanding_personal_expenditures", "balance_deficit",
}


def _sum_summary_field(rows: list[dict], key: str) -> float | None:
    """Sum a scope field without turning one component's unknown into zero."""
    if key in _NULLABLE_SUMMARY_FIELDS and any(row.get(key) is None for row in rows):
        return None
    return round(sum(float(row.get(key) or 0.0) for row in rows), 2)

_SUMMARY_ACTIVITY_FIELDS = (
    "contributions", "expenditures", "other_receipts",
    "other_disbursements", "beginning_balance", "ending_cash_balance",
    "balance_adjustments", "loans_received", "loans_received_exempt",
    "loan_payments", "loan_payments_exempt", "inkind_contributions",
    "inkind_expenditures", "accounts_receivable", "accounts_payable",
    "total_outstanding_loans", "outstanding_personal_expenditures",
    "balance_deficit",
)
_SUMMARY_FIELD_VERSION = 2


def _normalize_year_scrape_timestamps(yearly: dict) -> dict:
    """Migrate legacy per-filer timestamps onto their untouched year rows.

    The original cache stored one ``ts`` beside the ``years`` map. New captures
    timestamp each year independently so a cheap current-page refresh cannot
    freshen twenty historical pages. Copying the legacy value only when a row
    has no explicit timestamp preserves that distinction during rollout and,
    for multi-ID profiles, prevents a missing key from becoming an artificial
    ``scrape_ts_min = 0``.
    """
    for entry in (yearly or {}).values():
        if not isinstance(entry, dict):
            continue
        try:
            fallback = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            fallback = 0.0
        for row in (entry.get("years") or {}).values():
            if isinstance(row, dict):
                row.setdefault("scrape_ts", fallback)
    return yearly


def _combine_scope_yearly_summaries(yearlies: list[dict]) -> dict:
    """Combine only committee-years present for every physical filer.

    Treating an absent member as a zero is unsafe: a failed historical crawl
    can leave one sibling at 2020 while another reaches 2006, after which a
    sibling-only ORESTAR loan value would be compared with all members' app
    transactions. Missing coverage stays unknown instead.
    """
    combined: dict[str, dict] = {}
    if not yearlies:
        return combined
    for year in {key for years in yearlies for key in years}:
        rows = [years[year] for years in yearlies if year in years]
        if len(rows) != len(yearlies):
            continue
        base = dict(rows[0])
        for key in _SUMMABLE:
            base[key] = _sum_summary_field(rows, key)
        row_times = [float(row.get("scrape_ts") or 0) for row in rows]
        base["scrape_ts"] = max(row_times)
        base["scrape_ts_min"] = min(row_times)
        field_versions = []
        for row in rows:
            try:
                field_versions.append(int(row.get("summary_field_version") or 0))
            except (TypeError, ValueError):
                field_versions.append(0)
        base["summary_field_version"] = max(field_versions)
        base["summary_field_version_min"] = min(field_versions)
        # Per-year cash provenance is atomic across a canonical multi-ID scope.
        # A partial crawl may update one physical committee while preserving a
        # sibling's older row; never let the first row's copied metadata make
        # that mixed aggregate look paired.
        year_digests = {
            row.get("app_year_transaction_digest") for row in rows
            if exact_evidence_identifier_is_valid(
                row.get("app_year_transaction_digest")
            )
        }
        calculation_versions = {
            str(row.get("calculation_version") or "") for row in rows
        }
        scope_capture_ids = {
            row.get("scope_capture_id") for row in rows
            if exact_evidence_identifier_is_valid(row.get("scope_capture_id"))
        }
        provenance_is_common = bool(
            len(year_digests) == 1
            and len(calculation_versions) == 1
            and "" not in calculation_versions
            and len(scope_capture_ids) == 1
            and all(exact_evidence_identifier_is_valid(
                row.get("app_year_transaction_digest")
            ) and exact_evidence_identifier_is_valid(
                row.get("scope_capture_id")
            ) for row in rows)
        )
        for key in (
            "app_year_transaction_digest",
            "calculation_version",
            "scope_capture_id",
        ):
            base.pop(key, None)
        if provenance_is_common:
            base["app_year_transaction_digest"] = next(iter(year_digests))
            base["calculation_version"] = next(iter(calculation_versions))
            base["scope_capture_id"] = next(iter(scope_capture_ids))
        combined[year] = base
    return combined


def _summary_has_activity(row: dict | None) -> bool:
    return isinstance(row, dict) and any(
        abs(float(row.get(key) or 0.0)) > 0.0
        for key in _SUMMARY_ACTIVITY_FIELDS
    )


def _summary_is_confirmed_blank(row: dict | None) -> bool:
    """Whether a newly parsed row proves that nothing is held or moving."""
    if not isinstance(row, dict):
        return False
    try:
        field_version = int(row.get("summary_field_version") or 0)
    except (TypeError, ValueError):
        return False
    if field_version < _SUMMARY_FIELD_VERSION:
        # Legacy optional fields defaulted to zero when the label/value was
        # absent, so an all-zero legacy row cannot prove closure.
        return False
    if any(key not in row or row.get(key) is None for key in _SUMMARY_ACTIVITY_FIELDS):
        return False
    return not _summary_has_activity(row)

AGG_DIR       = DATA_DIR / "aggregated"
ENTITY_MAP    = Path(__file__).parent / "entity_map.json"
REVIEW_QUEUE  = DATA_DIR / "review_queue.json"
TRANS_DIR     = DATA_DIR / "transactions"   # per-year gzip files: txn_YYYY.csv.gz
COMMITTEES    = DATA_DIR / "committees.csv"

AGG_DIR.mkdir(parents=True, exist_ok=True)
TRANS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Column mapping: ORESTAR Excel headers → internal names
# (ORESTAR may vary header capitalisation between exports)
# ---------------------------------------------------------------------------

COL_MAP = {
    # Actual ORESTAR XLS column names (lowercased) → internal name
    "tran id":                  "tran_id",
    "transaction id":           "tran_id",
    "filed date":               "filed_date",
    "tran date":                "tran_date",   # transaction date (kept for reference)
    "date":                     "filed_date",
    "amount":                   "amount",
    "tran amount":              "amount",
    "sub type":                 "sub_type",
    "subtype":                  "sub_type",
    "contributor/payee":        "contributor_payee",
    "contributor":              "contributor_payee",
    "payee":                    "contributor_payee",
    "contributor/payee name":   "contributor_payee",
    "contributor type":         "contributor_type",
    "filer":                    "filer",
    "committee":                "filer",
    "filer name":               "filer",
    "committee name":           "filer",
    "office":                   "office",
    "office sought":            "office",
    "party":                    "party",
    "purpose":                  "purpose",
    "purp desc":                "purpose",     # ORESTAR actual column name
    "purpose codes":            "purpose_codes",
    "occptn txt":               "occupation",
    "emp name":                 "employer",
    "city":                     "city",
    "state":                    "state",
    "zip":                      "zip",
    "book type":                "book_type",
}

CONTRIBUTOR_TYPE_LABELS = {
    "I": "Individual",
    "B": "Business / Corporation",
    "L": "Labor Organization",
    "P": "Political Action Committee",
    "F": "Political Party",
    "C": "Candidate & Family",
    "U": "Unregistered Committee",
    "O": "Other",
}


def make_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:60]


# Fuzzy-dedup only the N most-frequent names; rarer names are self-canonical
# unless entity_map.json explicitly maps them.  At 0.76 µs/comparison,
# 10 000² ≈ 100 M comparisons ≈ 76 s — safe inside any workflow timeout.
MAX_DEDUP_NAMES = 10_000


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

ABBREV_MAP = {
    r"\bllc\b":  "LLC",
    r"\binc\.?": "Inc",
    r"\bcorp\.?": "Corp",
    r"\bpac\b":  "PAC",
    r"\bltd\.?": "Ltd",
    r"\bco\.?\b": "Co",
    r"\bassn\.?": "Assn",
    r"\bassoc\.?": "Assoc",
    r"\bcommittee\b": "Committee",
    r"\bfor\b":  "for",
    r"\band\b":  "and",
    r"\bof\b":   "of",
    r"\bthe\b":  "the",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/extra spaces, expand standard abbreviations."""
    if not isinstance(name, str):
        return ""
    n = name.strip().lower()
    # Remove punctuation except hyphens and apostrophes
    n = re.sub(r"[^\w\s\-']", " ", n)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
    return n


def canonical_name(raw: str) -> str:
    """Title-case a normalized name, restoring known acronyms."""
    n = normalize_name(raw)
    # Title case first
    n = n.title()
    # Re-apply acronym/abbreviation corrections
    for pattern, replacement in ABBREV_MAP.items():
        n = re.sub(pattern, replacement, n, flags=re.IGNORECASE)
    return n.strip()


# ---------------------------------------------------------------------------
# Entity map and fuzzy deduplication
# ---------------------------------------------------------------------------

def load_entity_map() -> dict[str, str]:
    if ENTITY_MAP.exists():
        with open(ENTITY_MAP) as f:
            return json.load(f)
    return {}


def save_entity_map(em: dict[str, str]) -> None:
    with open(ENTITY_MAP, "w") as f:
        json.dump(em, f, indent=2, sort_keys=True)


def apply_entity_map(name: str, entity_map: dict[str, str]) -> str:
    return entity_map.get(name, name)


def fuzzy_deduplicate(
    names: list[str],
    entity_map: dict[str, str],
    auto_threshold: float = 95.0,
    review_threshold: float = 85.0,
) -> tuple[dict[str, str], list[dict]]:
    """
    Cluster names by fuzzy similarity.

    Returns:
        canonical_map:  {raw_name → canonical_name}
        review_items:   list of {"a": ..., "b": ..., "score": ...} for 85–95% matches
    """
    # Apply existing entity_map first (case-insensitive: try title-cased key, then exact key)
    def _em_lookup(n: str) -> str:
        return entity_map.get(canonical_name(n), entity_map.get(n, n))

    resolved = {n: _em_lookup(n) for n in names}

    # Count frequency for picking canonical form
    freq: dict[str, int] = defaultdict(int)
    for n in names:
        freq[resolved[n]] += 1

    # Build clusters using union-find
    parent: dict[str, str] = {n: n for n in set(resolved.values())}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Canonical = higher frequency
        if freq[ra] >= freq[rb]:
            parent[rb] = ra
        else:
            parent[ra] = rb

    all_resolved = list(set(resolved.values()))
    review_items = []

    def _scorer(a, b, **kw):
        return max(fuzz.token_sort_ratio(a, b), fuzz.token_set_ratio(a, b))

    # Limit the O(n²) comparison pool to the most-frequent names so the step
    # completes within workflow timeouts.  Rare names (one-time contributors)
    # remain self-canonical; entity_map.json handles any manual merges needed
    # beyond this set.
    if len(all_resolved) > MAX_DEDUP_NAMES:
        log.warning(
            "Limiting fuzzy-dedup to the %d most-frequent names (of %d total); "
            "use entity_map.json for merges outside this set.",
            MAX_DEDUP_NAMES, len(all_resolved),
        )
        candidates = sorted(
            all_resolved, key=lambda n: freq.get(n, 0), reverse=True
        )[:MAX_DEDUP_NAMES]
    else:
        candidates = all_resolved

    log.info("Fuzzy-deduplicating %d names…", len(candidates))
    for i, name_a in enumerate(candidates):
        if i % 500 == 0 and i > 0:
            log.debug("  … %d / %d", i, len(candidates))
        # rapidfuzz process.extract returns top matches
        matches = rfuzz_process.extract(
            name_a,
            candidates,
            scorer=_scorer,
            score_cutoff=review_threshold,
            limit=10,
        )
        for name_b, score, _ in matches:
            if name_a == name_b:
                continue
            if score >= auto_threshold:
                union(name_a, name_b)
            else:
                # 85–95: flag for human review (only once per pair)
                pair = tuple(sorted([name_a, name_b]))
                review_items.append({"a": pair[0], "b": pair[1], "score": round(score, 1)})

    # Build canonical map for ALL resolved names (not just comparison candidates)
    cluster_freq: dict[str, dict[str, int]] = defaultdict(dict)
    for n in all_resolved:
        root = find(n)
        cluster_freq[root][n] = freq.get(n, 0)

    canonical_map: dict[str, str] = {}
    for raw, resolved_name in resolved.items():
        root = find(resolved_name)
        # Canonical = member with highest frequency
        canon = max(cluster_freq[root], key=lambda x: cluster_freq[root][x])
        canonical_map[raw] = canon

    # Deduplicate review items
    seen_pairs: set[tuple] = set()
    unique_review = []
    for item in review_items:
        pair = (item["a"], item["b"])
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_review.append(item)

    return canonical_map, unique_review


# ---------------------------------------------------------------------------
# Excel loading
# ---------------------------------------------------------------------------

XLS_MAGIC  = b"\xd0\xcf\x11\xe0"   # OLE2 Compound Document (old .xls)
XLSX_MAGIC = b"PK\x03\x04"         # ZIP archive (new .xlsx)


def _detect_engine(path: Path) -> str | None:
    """
    Peek at the first 4 bytes to determine the Excel engine to use.
    Returns 'xlrd' (old .xls), 'openpyxl' (new .xlsx), or None if HTML/unknown.
    """
    header = path.read_bytes()[:8]
    if header[:4] == XLS_MAGIC:
        return "xlrd"
    if header[:4] == XLSX_MAGIC:
        return "openpyxl"
    if header[:5].lower() in (b"<!doc", b"<html"):
        return None  # HTML error page — skip
    # Unknown: let openpyxl try and fail with a useful error
    return "openpyxl"


def load_excel_files(raw_dir: Path) -> pd.DataFrame:
    """Read all Excel files in raw_dir, returning a combined DataFrame."""
    files = sorted(raw_dir.glob("*.xls*"))
    if not files:
        log.warning("No Excel files found in %s", raw_dir)
        return pd.DataFrame()

    log.info("Loading %d Excel files…", len(files))
    frames = []
    for f in files:
        try:
            engine = _detect_engine(f)
            if engine is None:
                log.warning("Skipping %s — appears to be an HTML page, not Excel", f.name)
                continue
            df = pd.read_excel(f, engine=engine, dtype=str)
            # Skip cap files (4999 rows) — these are truncated parent windows
            # whose data is covered by their sub-window split files
            if len(df) >= 4999 and f.name.startswith("filer"):
                log.debug("Skipping cap file %s (%d rows — covered by split sub-windows)", f.name, len(df))
                continue
            # Normalize column names
            df.columns = [c.strip().lower() for c in df.columns]
            # Rename to internal names
            df = df.rename(columns={k: v for k, v in COL_MAP.items() if k in df.columns})
            df["_source_file"] = f.name
            # Derive tran_type from filename prefix: C_2026-...→"C", OR_2026-...→"OR"
            # ORESTAR does not include a transaction type column in the export.
            _valid_types = {"C", "E", "O", "OA", "OD", "OR"}
            _prefix = f.stem.split("_")[0].upper()
            if _prefix in _valid_types:
                df["tran_type"] = _prefix
            else:
                # Filer-targeted downloads have mixed types — infer from sub_type
                _SUB_TYPE_TO_TRAN_TYPE = {
                    "Cash Contribution": "C", "In-Kind Contribution": "C",
                    "In-Kind/Forgiven Personal Expenditures": "C",
                    "In-Kind/Forgiven Account Payable": "C",
                    "Loan Received (Non-Exempt)": "C", "Pledge of Cash": "C",
                    "Pledge of In-Kind": "C", "Pledge of Loan": "C",
                    "Cash Expenditure": "E", "Account Payable": "E",
                    "Expenditure Made by an Agent": "E",
                    "Personal Expenditure for Reimbursement": "E",
                    "Loan Payment (Non-Exempt)": "E",
                    "Miscellaneous Other Receipt": "OR", "Refunds and Rebates": "OR",
                    "Lost or Returned Check": "OR", "Interest/Investment Income": "OR",
                    "Items Sold at Fair Market Value": "OR",
                    "Loan Received (Exempt)": "OR",
                    "Account Payable Rescinded": "O", "Cash Balance Adjustment": "O",
                    "Loan Forgiven (Non-Exempt)": "O",
                    "Personal Expenditure Balance Adjustment": "O",
                    "Uncollectible Pledge of Cash": "O", "Uncollectible Pledge of In-Kind": "O",
                    "Miscellaneous Account Receivable": "OA",
                    "Unexpended Agent Balance": "OA",
                    "Miscellaneous Other Disbursement": "OD",
                    "Return or Refund of Contribution": "OD",
                    "Nonpartisan Activity": "OD", "Loan Payment (Exempt)": "OD",
                }
                st_col = "sub_type" if "sub_type" in df.columns else "sub type"
                if st_col in df.columns:
                    df["tran_type"] = df[st_col].map(_SUB_TYPE_TO_TRAN_TYPE).fillna("")
                    inferred = (df["tran_type"] != "").sum()
                    log.info("Inferred tran_type from sub_type for %d/%d rows in %s",
                             inferred, len(df), f.name)
                else:
                    df["tran_type"] = ""
            frames.append(df)
        except Exception as exc:
            log.warning("Failed to read %s: %s", f.name, exc)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    log.info("Loaded %d rows from %d files", len(combined), len(frames))
    return combined


# ---------------------------------------------------------------------------
# Per-year transaction file helpers
# ---------------------------------------------------------------------------

def _txn_path(year) -> Path:
    return TRANS_DIR / f"txn_{year}.csv.gz"


def _load_all_transactions() -> pd.DataFrame:
    """Load and concatenate all per-year transaction files."""
    files = sorted(TRANS_DIR.glob("txn_*.csv.gz"))
    if not files:
        return pd.DataFrame()
    return pd.concat(
        [pd.read_csv(f, compression="gzip", dtype=str) for f in files],
        ignore_index=True,
    )


def _save_transactions(df: pd.DataFrame) -> None:
    """Split df by year and write/overwrite per-year gzip CSV files.

    Uses mtime=0 for deterministic gzip output so that files whose content
    has not changed produce identical bytes on each run — keeping git diffs
    clean across years where no new data arrived.
    """
    import io
    TRANS_DIR.mkdir(parents=True, exist_ok=True)
    date_col = df["filed_date"].astype(str)
    years = date_col.apply(lambda d: d[:4] if len(d) >= 4 and d[:4].isdigit() else "0000")
    n_written = n_unchanged = 0
    for yr, grp in df.groupby(years, sort=False):
        # Serialise to CSV bytes in memory
        buf = io.BytesIO()
        grp.to_csv(buf, index=False)
        csv_bytes = buf.getvalue()

        # Compress deterministically (mtime=0 → no embedded timestamp)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(csv_bytes)
        new_gz = gz_buf.getvalue()

        dest = _txn_path(yr)
        if dest.exists() and dest.read_bytes() == new_gz:
            n_unchanged += 1
            continue  # identical content — skip write, no git diff

        dest.write_bytes(new_gz)
        n_written += 1

    log.info(
        "Wrote %d transactions: %d year file(s) updated, %d unchanged",
        len(df), n_written, n_unchanged,
    )


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

def _live_originals_for(rows: pd.DataFrame, evidence: dict[str, str]) -> list[dict]:
    """The live amended originals among one committee's rows, for display.

    An ID qualifies only if coverage evidence lists it AND an Amended row here
    names it as its original; otherwise it is an ordinary row and says nothing
    about amendments. Amounts and types come from our copy of each row.
    """
    needed = {"tran_id", "original id", "tran status"}
    if not evidence or rows is None or rows.empty or not needed <= set(rows.columns):
        return []
    ids = rows["tran_id"].astype(str).str.strip()
    originals = rows[ids.isin(evidence)]
    if originals.empty:
        return []
    amended = rows[rows["tran status"].astype(str).str.strip() == "Amended"]
    amended_orig = amended["original id"].astype(str).str.strip()

    def _date(value):
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")

    def _amount(value):
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    out = []
    for _, row in originals.iterrows():
        tran_id = str(row["tran_id"]).strip()
        amends = amended[amended_orig == tran_id]
        if amends.empty:
            continue
        when = _date(row.get("tran_date"))
        out.append({
            "original_id": tran_id,
            "filer_id": str(row.get("filer id") or "").strip() or evidence.get(tran_id),
            "tran_date": when,
            "year": int(when[:4]) if when else None,
            "sub_type": str(row.get("sub_type") or "").strip() or None,
            "amount": _amount(row.get("amount")),
            "amended": [
                {
                    "tran_id": str(a.get("tran_id")).strip(),
                    "sub_type": str(a.get("sub_type") or "").strip() or None,
                    "amount": _amount(a.get("amount")),
                }
                for _, a in amends.sort_values("tran_id").iterrows()
            ],
        })
    out.sort(key=lambda item: (item["tran_date"] or "", item["original_id"]))
    return out


def _deletions_by_filer_id() -> dict[str, list[dict]]:
    """Deleted transactions we had counted, as display entries per filer ID.

    Read from the kept deletion record (DELETIONS_PATH), because the rows
    themselves are gone from the shards. Only deletions that removed a row of
    ours are listed; the others never touched our numbers.
    """
    try:
        entries = json.loads(DELETIONS_PATH.read_text()) if DELETIONS_PATH.exists() else []
    except (OSError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}

    def _date(value):
        parsed = pd.to_datetime(value, errors="coerce", format="mixed")
        return None if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")

    def _amount(value):
        try:
            return round(float(str(value).replace("$", "").replace(",", "")), 2)
        except (TypeError, ValueError):
            return None

    out: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        removed = (entry or {}).get("removed") or []
        if not removed:
            continue
        deletion = entry.get("deletion") or {}
        counted = removed[-1]            # removed in chain order: the newest counted
        filer = _clean_id(deletion.get("filer id") or counted.get("filer id"))
        when = _date(counted.get("tran_date") or deletion.get("tran_date"))
        out[filer].append({
            "deletion_id": str(entry.get("deletion_id")),
            "deleted_on": _date(deletion.get("filed_date")),
            "removed_ids": [str(r.get("tran_id")) for r in removed],
            "filer_id": filer,
            "tran_date": when,
            "year": int(when[:4]) if when else None,
            "tran_type": counted.get("tran_type") or deletion.get("tran_type"),
            "sub_type": counted.get("sub_type") or deletion.get("sub_type"),
            "contributor_payee": counted.get("contributor_payee")
                                 or deletion.get("contributor_payee"),
            "amount": _amount(counted.get("amount")),
        })
    for rows in out.values():
        rows.sort(key=lambda r: (r["tran_date"] or "", r["deletion_id"]))
    return dict(out)


AMOUNT_UPDATES_PATH = DATA_DIR / "amount_updates.json"
AMOUNT_UPDATES_KEEP = 200


def _amount_updates(existing: pd.DataFrame, incoming: pd.DataFrame) -> dict:
    """Rows a download re-priced: same Tran ID, a different amount.

    ORESTAR edits lumped "Miscellaneous ... $100 and under" rows in place
    (see fetch.MISC_PREFIX), so a re-read is the only way their current
    amounts reach us. The merge already keeps the newest copy of each ID; this
    measures what that changed, which is the whole point of the re-read.
    """
    empty = {"rows": 0, "net": 0.0, "miscellaneous_rows": 0, "sample": []}
    if (existing is None or incoming is None or existing.empty or incoming.empty
            or "tran_id" not in existing.columns or "tran_id" not in incoming.columns
            or "amount" not in existing.columns or "amount" not in incoming.columns):
        return empty
    old = existing[["tran_id", "amount"]].copy()
    new = incoming[[c for c in ("tran_id", "amount", "contributor_payee", "filer id")
                    if c in incoming.columns]].copy()
    for frame in (old, new):
        frame["tran_id"] = frame["tran_id"].astype(str).str.strip()
        frame["amount"] = pd.to_numeric(
            frame["amount"].astype(str).str.replace(r"[$,]", "", regex=True),
            errors="coerce",
        )
    old = old.drop_duplicates("tran_id", keep="last")
    new = new.drop_duplicates("tran_id", keep="last")
    both = old.merge(new, on="tran_id", suffixes=("_old", "_new"))
    changed = both[(both["amount_new"] - both["amount_old"]).abs() > 0.005]
    if changed.empty:
        return empty
    payees = changed.get("contributor_payee", pd.Series("", index=changed.index))
    return {
        "rows": int(len(changed)),
        "net": round(float((changed["amount_new"] - changed["amount_old"]).sum()), 2),
        "miscellaneous_rows": int(payees.fillna("").astype(str)
                                  .str.startswith("Miscellaneous").sum()),
        "sample": [
            {"tran_id": r["tran_id"], "filer_id": str(r.get("filer id") or ""),
             "old": round(float(r["amount_old"]), 2), "new": round(float(r["amount_new"]), 2)}
            for _, r in changed.head(25).iterrows()
        ],
    }


def _record_amount_updates(existing: pd.DataFrame, incoming: pd.DataFrame) -> dict:
    """Measure re-priced rows, log them, and append to the published record."""
    result = _amount_updates(existing, incoming)
    log.info("Amount updates: %d existing rows re-priced by this merge (%d lumped "
             "\"Miscellaneous\" rows), net %s",
             result["rows"], result["miscellaneous_rows"], f"${result['net']:+,.2f}")
    if result["rows"]:
        try:
            history = json.loads(AMOUNT_UPDATES_PATH.read_text()) \
                if AMOUNT_UPDATES_PATH.exists() else []
            if not isinstance(history, list):
                history = []
        except (OSError, ValueError):
            history = []
        history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        **result})
        AMOUNT_UPDATES_PATH.write_text(json.dumps(history[-AMOUNT_UPDATES_KEEP:], indent=1))
    return result


# Transactions ORESTAR deleted. Deleting one files a separate record: status
# "Deleted", a Tran ID of its own, the filing date of the deletion, and an
# "Original Id" naming the transaction it deletes. The original becomes expired
# and ORESTAR stops counting it. The standard search hides deletion records, and
# a filed-date window is read once, so for years a deletion never reached us:
# we held 0 of them and kept counting every deleted row (ChamberPAC's $10,000
# contribution 5697866, deleted by 5768551 on 2026-08-18).
#
# The fetcher now asks for them. The merge removes each deleted transaction the
# way it removes an amended original, but nothing is lost: every removed row is
# kept here in full next to the deletion record that removed it. The file rides
# in the transactions state profile, with the shards it explains.
DELETIONS_PATH = DATA_DIR / "orestar_deletions.json"
DELETED_STATUS = "Deleted"


def _clean_id(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "nat"):
        return ""
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def _clean_ids(series: pd.Series) -> pd.Series:
    """_clean_id for a whole column at once (the merge holds ~3M rows)."""
    text = series.astype(str).str.strip().str.replace(r"^(\d+)\.0$", r"\1", regex=True)
    return text.mask(text.str.lower().isin(("nan", "none", "nat", "<na>")), "")


def _row_record(row) -> dict:
    """A row as plain strings, every column kept, for the deletion record."""
    out = {}
    for key, value in row.items():
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            continue
        out[str(key)] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return out


def _apply_deletions(df: pd.DataFrame, keep_live=frozenset()):
    """Remove deletion records and every version of a transaction they delete.

    Returns (df, removed_ids, entries). A transaction counts only through its
    newest version, so a chain whose newest version is a deletion counts
    nothing: the original and any amendments all go. A later amendment would
    outrank the deletion and stay (only the deletion record is dropped then);
    ORESTAR has shown none, but guessing would be worse than following the
    order. An original that coverage evidence shows ORESTAR still returning
    (``keep_live``) is kept, as for amendments.
    """
    orig_col = "original id" if "original id" in df.columns else None
    status_col = "tran status" if "tran status" in df.columns else None
    if not (orig_col and status_col and "tran_id" in df.columns) or df.empty:
        return df, set(), []
    status = df[status_col].astype(str).str.strip()
    is_deletion = status == DELETED_STATUS
    if not is_deletion.any():
        return df, set(), []

    keep_live = {_clean_id(v) for v in keep_live or ()}
    ids = _clean_ids(df["tran_id"])
    origs = _clean_ids(df[orig_col])
    targets = set(origs[is_deletion]) - {""}
    in_chain = is_deletion | origs.isin(targets) | ids.isin(targets)
    chain = df[in_chain].copy()
    chain["_id"] = ids[in_chain]
    # Chain key: the original's ID. Every version names it, and so does the
    # original itself; an original stored without one is keyed by its own ID.
    chain["_key"] = origs[in_chain].where(origs[in_chain] != "", chain["_id"])
    chain["_deletion"] = is_deletion[in_chain]
    chain["_filed"] = (pd.to_datetime(chain["filed_date"], errors="coerce", format="mixed")
                       if "filed_date" in chain.columns else pd.NaT)
    chain["_num"] = pd.to_numeric(chain["_id"], errors="coerce")
    chain = chain.sort_values(["_filed", "_num"], na_position="first")

    remove: set[str] = set()
    entries: list[dict] = []
    outranked = 0
    columns = [c for c in df.columns]
    for key, versions in chain.groupby("_key", sort=False):
        deletions = versions[versions["_deletion"]]
        if deletions.empty:
            continue
        remove |= set(deletions["_id"])
        newest_is_deletion = bool(versions.iloc[-1]["_deletion"])
        if not newest_is_deletion:
            outranked += 1
        removed_rows, kept_live = [], []
        if newest_is_deletion:
            for _, version in versions[~versions["_deletion"]].iterrows():
                if version["_id"] in keep_live:
                    kept_live.append(version["_id"])
                    continue
                remove.add(version["_id"])
                removed_rows.append(_row_record(version[columns]))
        for _, deletion in deletions.iterrows():
            entries.append({
                "deletion_id": deletion["_id"],
                "original_id": key,
                "deletion": _row_record(deletion[columns]),
                "removed": removed_rows if newest_is_deletion else [],
                "kept_live": kept_live,
                "outranked_by_later_version": not newest_is_deletion,
            })

    before = len(df)
    df = df[~ids.isin(remove)]
    removed_versions = sum(len(e["removed"]) for e in entries)
    log.info("Deleted transactions: %d deletion record(s) from ORESTAR; removed %d "
             "deleted version(s) we held (%d → %d rows)%s",
             int(is_deletion.sum()), removed_versions, before, len(df),
             f"; {outranked} outranked by a later amendment" if outranked else "")
    kept = sorted({i for e in entries for i in e["kept_live"]})
    if kept:
        log.info("Kept %d deleted original(s) ORESTAR still counts as live: %s",
                 len(kept), ", ".join(kept))
    return df, remove - set(ids[is_deletion]), entries


def _record_deletions(entries: list[dict]) -> None:
    """Merge this run's deletion records into the kept record, by deletion ID.

    A deletion can arrive again (overlapping windows, a re-download) after its
    rows are gone; the rows removed the first time are kept.
    """
    if not entries:
        return
    try:
        held = json.loads(DELETIONS_PATH.read_text()) if DELETIONS_PATH.exists() else []
        if not isinstance(held, list):
            held = []
    except (OSError, ValueError):
        held = []
    by_id = {str(e.get("deletion_id")): e for e in held if isinstance(e, dict)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in entries:
        prior = by_id.get(entry["deletion_id"])
        if prior is None:
            by_id[entry["deletion_id"]] = {**entry, "first_seen": now}
            continue
        seen = {r.get("tran_id") for r in prior.get("removed") or []}
        prior["removed"] = (prior.get("removed") or []) + [
            r for r in entry["removed"] if r.get("tran_id") not in seen]
        prior["kept_live"] = sorted(set(prior.get("kept_live") or [])
                                    | set(entry["kept_live"]))
        prior["deletion"] = entry["deletion"]
        prior["outranked_by_later_version"] = entry["outranked_by_later_version"]
    DELETIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DELETIONS_PATH.write_text(json.dumps(
        sorted(by_id.values(), key=lambda e: (len(e["deletion_id"]), e["deletion_id"])),
        indent=1))



def _drop_superseded(df, keep_live=frozenset()):
    """Keep only the version of each transaction that ORESTAR still counts.

    Rule 0 removes deleted transactions (``_apply_deletions``). Then two rules
    for amendments, and we had only the first:

      1. An original replaced by an amendment is dropped. ORESTAR's Account
         Summary counts the amendment, not the original, and the two arrive in
         different fetch windows because they were filed on different dates.

      2. Where a transaction was amended MORE THAN ONCE, only the newest
         amendment survives. Every amendment points back at the original rather
         than at the amendment before it, so rule 1 alone left a twice-amended
         transaction with two live rows and a three-times-amended one with
         three. Julie for County Commissioner holds three versions of the same
         $167.41 contribution — same date, same amount, three filed dates — and
         we counted all three. Dataset-wide: 160 stale rows, 68 committees,
         $462,613, all of it inflating our side, which is the direction that
         makes a committee look like it holds MORE than ORESTAR reports.

    Lived in two places before this, copied, with the second labelled "same
    logic as step 4b". They were the same, which is why fixing rule 2 in one
    would have left the other wrong. Returns (df, removed_tran_ids) so callers
    that sync to Postgres can delete what they dropped.

    Rule 1 has one exception, ``keep_live``: originals ORESTAR never expired.
    Normally amending a transaction expires the original, ORESTAR's default
    search stops returning it and its summary stops counting it. Sometimes
    ORESTAR leaves the original live, and then its summary counts BOTH
    versions. Oregon Firearms Federation PAC's $100 contribution 2041931 was
    amended eleven minutes later as 2041946 to fix the contributor's details;
    ORESTAR's history links the two, yet both are live, both show a $200
    aggregate for the donor, and ORESTAR's 2015 contributions include both.
    Elect Dave Hoppe's $600 cash expenditure 1672602, re-entered as the
    personal expenditure 1927730, is the same. The balance follows ORESTAR, so
    those originals are kept. Only exact coverage evidence (ORESTAR's default
    search returning the ID) can put an ID in ``keep_live``; see
    ``_live_original_ids``.
    """
    keep_live = {str(v).strip() for v in keep_live or ()}
    removed: set[str] = set()
    orig_col = "original id" if "original id" in df.columns else None
    status_col = "tran status" if "tran status" in df.columns else None
    if not (orig_col and status_col and "tran_id" in df.columns):
        return df, removed

    # Rule 0 — deleted transactions (see DELETIONS_PATH). Runs first: a deleted
    # chain must not leave its newest amendment for rule 2 to keep.
    df, removed, _deletions = _apply_deletions(df, keep_live)
    _record_deletions(_deletions)

    # Rule 1 — originals replaced by an amendment.
    amended = df[df[status_col] == "Amended"]
    if not amended.empty:
        superseded = set(
            amended[orig_col].dropna().astype(str).str.strip()
        ) & set(df["tran_id"].astype(str).str.strip())
        live = superseded & keep_live
        if live:
            log.info("Kept %d amended original(s) ORESTAR still counts as live: %s",
                     len(live), ", ".join(sorted(live, key=lambda v: (len(v), v))))
        superseded -= live
        if superseded:
            before = len(df)
            df = df[~df["tran_id"].astype(str).str.strip().isin(superseded)]
            removed |= superseded
            log.info("Removed %d superseded originals (replaced by amendments): "
                     "%d → %d rows", before - len(df), before, len(df))

    # Rule 2 — older amendments in a chain.
    amended = df[df[status_col] == "Amended"]
    if not amended.empty:
        key = ["filer_id", orig_col] if "filer_id" in df.columns else [orig_col]
        # Newest wins: filed date first, tran_id to break same-day ties.
        order = [c for c in ("filed_date", "tran_id") if c in amended.columns]
        ranked = amended.dropna(subset=[orig_col])
        if order:
            ranked = ranked.sort_values(order)
        stale = set(
            ranked.loc[ranked.duplicated(subset=key, keep="last"),
                       "tran_id"].astype(str).str.strip()
        )
        if stale:
            before = len(df)
            df = df[~df["tran_id"].astype(str).str.strip().isin(stale)]
            removed |= stale
            log.info("Removed %d superseded amendments (kept the newest of each "
                     "chain): %d → %d rows", before - len(df), before, len(df))

    return df, removed


def process() -> None:
    entity_map = load_entity_map()

    # ── 1. Load raw Excel files ──────────────────────────────────────────────
    new_df = load_excel_files(RAW_DIR)

    if new_df.empty:
        log.info("No new data to process.")
        df = _load_all_transactions()
        if df.empty:
            log.info("No existing transactions data. Nothing to do.")
            return
        log.info("Re-aggregating from existing transaction files (%d rows)", len(df))

        # Ensure amount is numeric for re-aggregation path
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)

        df, _ = _drop_superseded(df, keep_live=_live_original_ids())
    else:
        # ── 2. Type coercion ─────────────────────────────────────────────────
        for col in ["tran_id", "contributor_payee", "filer", "contributor_type",
                    "tran_type", "sub_type", "office", "party", "purpose"]:
            if col not in new_df.columns:
                new_df[col] = ""
            new_df[col] = new_df[col].fillna("").astype(str).str.strip()

        if "amount" in new_df.columns:
            new_df["amount"] = (
                pd.to_numeric(new_df["amount"].str.replace(r"[$,]", "", regex=True), errors="coerce")
                .fillna(0.0)
            )
        else:
            new_df["amount"] = 0.0

        if "filed_date" in new_df.columns:
            new_df["filed_date"] = pd.to_datetime(new_df["filed_date"], errors="coerce").dt.date
        else:
            new_df["filed_date"] = pd.NaT

        if "book_type" not in new_df.columns:
            new_df["book_type"] = ""
        new_df["book_type"] = (
            new_df["book_type"].fillna("").astype(str).str.strip()
            .replace({"Candidate's Immediate Family": "Candidate & Immediate Family", "": "Other"})
        )

        # ── 3. Load existing transactions and merge ───────────────────────────
        existing = _load_all_transactions()
        if not existing.empty:
            log.info("Loading existing transactions (%d rows)…", len(existing))
            if "book type" in existing.columns and "book_type" not in existing.columns:
                existing = existing.rename(columns={"book type": "book_type"})
            existing["amount"] = pd.to_numeric(existing["amount"], errors="coerce").fillna(0.0)
            existing["filed_date"] = pd.to_datetime(existing["filed_date"], errors="coerce").dt.date
            _record_amount_updates(existing, new_df)
            df = pd.concat([existing, new_df], ignore_index=True)
        else:
            df = new_df

        # ── 4. Deduplicate by tran_id ─────────────────────────────────────────
        if "tran_id" in df.columns and df["tran_id"].ne("").any():
            before = len(df)
            df = df.drop_duplicates(subset=["tran_id"], keep="last")
            log.info("Deduplicated by tran_id: %d → %d rows", before, len(df))

        # ── 4b. Remove originals superseded by amendments ─────────────────────
        # When a transaction is amended, ORESTAR creates a new row with a new
        # tran_id and status "Amended", whose "original id" points to the
        # original row.  Both rows may appear in our data because they were
        # filed on different dates (hence fetched in different weekly windows).
        # ORESTAR's Account Summary counts only the latest version, so we must
        # drop the originals to avoid double-counting.
        df, superseded_ids = _drop_superseded(df, keep_live=_live_original_ids())

        # ── 5. Name normalization + fuzzy dedup ───────────────────────────────
        all_names = (
            df["contributor_payee"].dropna().unique().tolist()
            + df["filer"].dropna().unique().tolist()
        )
        all_names = [n for n in all_names if n]

        canonical_map, review_items = fuzzy_deduplicate(
            all_names,
            entity_map,
            auto_threshold=95.0,
            review_threshold=80.0,
        )

        df["contributor_payee_canonical"] = df["contributor_payee"].map(
            lambda x: canonical_map.get(x, canonical_map.get(canonical_name(x), x))
        )
        df["filer_canonical"] = df["filer"].map(
            lambda x: canonical_map.get(x, canonical_map.get(canonical_name(x), x))
        )

        # ── 6. Map contributor type codes to labels ───────────────────────────
        df["contributor_type"] = df["contributor_type"].fillna("").astype(str)
        df["contributor_type_label"] = df["contributor_type"].map(
            lambda x: CONTRIBUTOR_TYPE_LABELS.get(x.strip().upper(), x)
        )

        # ── 7. Save review queue ──────────────────────────────────────────────
        if review_items:
            log.info("Writing %d review items to %s", len(review_items), REVIEW_QUEUE)
            with open(REVIEW_QUEUE, "w") as f:
                json.dump(review_items, f, indent=2)

        # ── 8. Write updated per-year transaction files ───────────────────────
        df["filed_date"] = df["filed_date"].astype(str)
        _save_transactions(df)

        # ── 8b. Sync the changed window to Postgres ───────────────────────────
        # Upsert only the freshly-fetched rows (their canonical values were
        # computed on the merged df above), and drop any superseded originals.
        _new_ids = set(new_df["tran_id"].astype(str).str.strip())
        _changed = df[df["tran_id"].astype(str).str.strip().isin(_new_ids)]
        supabase_sync.upsert_transactions(_changed)
        supabase_sync.delete_transactions(superseded_ids)
        # Assign donor_id to the freshly-synced rows (committee-id or exact
        # alias lookup; unknown names become provisional donors until the
        # weekly full resolution).
        if supabase_sync.sync_enabled():
            import resolve_donors
            resolve_donors.assign_incremental()

    # ── 9. Aggregate JSON files ───────────────────────────────────────────────
    aggregate(df)

    # ── 10. Delete raw Excel files ────────────────────────────────────────────
    #
    # Filer-targeted files used to be kept here so a backfill could resume from
    # them. They were committed to git to survive between runners, and git keeps
    # every version of every one forever: 10,205 files and an 80 GB repository,
    # against a 100 GB hard limit that would have stopped every push at once.
    #
    # Nothing needs them now. The rows are in the year shards and in Postgres,
    # and the fetcher decides what to skip from ORESTAR's own record counts plus
    # what the database holds — both of which outlive any file on disk.
    deleted = 0
    for f in RAW_DIR.glob("*.xlsx"):
        f.unlink()
        deleted += 1
    if deleted:
        log.info("Deleted %d raw Excel files (merged and synced)", deleted)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def to_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _donor_grouping_labels(df: pd.DataFrame) -> pd.Series:
    """Normalize all donor labels before any totals or top-N cutoffs.

    Backfilled rows can have an empty canonical value even when the canonical
    column exists. Fall back row by row without rewriting transaction fields.
    """
    labels = df.get("contributor_payee_canonical", pd.Series("", index=df.index))
    labels = labels.map(normalize_donor_label)
    missing = labels.eq("")
    if "contributor_payee" in df.columns:
        labels.loc[missing] = df.loc[missing, "contributor_payee"].map(normalize_donor_label)
    display = build_donor_label_map(labels.unique())
    return labels.map(donor_label_key).map(display)


@supabase_sync.dashboard_publication()
def aggregate(df: pd.DataFrame) -> None:
    log.info("Aggregating data for dashboard JSON files…")

    # Ensure amount is numeric
    df = df.copy()

    # Normalize book_type (legacy CSV may still have raw "book type" column name)
    if "book type" in df.columns and "book_type" not in df.columns:
        df = df.rename(columns={"book type": "book_type"})
    if "book_type" not in df.columns:
        df["book_type"] = "Other"
    else:
        df["book_type"] = (
            df["book_type"].fillna("").astype(str).str.strip()
            .replace({"Candidate's Immediate Family": "Candidate & Immediate Family", "": "Other"})
        )
    # Out-of-state flag: non-blank state that isn't OR → True; blank or OR → False
    _st = df["state"].fillna("").astype(str).str.strip().str.upper() if "state" in df.columns else pd.Series("", index=df.index)
    df["is_out_of_state"] = _st.ne("") & _st.ne("OR")

    df["amount"] = df["amount"].apply(to_float)
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df = df.dropna(subset=["filed_date"])

    # Use tran_date for year/month groupings (matches ORESTAR Account Summary);
    # fall back to filed_date for records where tran_date is missing.
    if "tran_date" in df.columns:
        _tran_date = pd.to_datetime(df["tran_date"], errors="coerce")
        _eff_date  = _tran_date.where(_tran_date.notna(), df["filed_date"])
        # Undated in ORESTAR's own record, so ORESTAR cannot place it in a
        # reporting period — and does not.
        #
        # The filed_date fallback above keeps these rows in the dataset, which
        # is right: the money is real and belongs in contributor totals. But it
        # also lands them in a YEAR, and ORESTAR's account summaries are
        # period-based, so a transaction with no transaction date appears in
        # none of them. Four rows dataset-wide carry no tran_date, and all four
        # committees were flagged for exactly their own row, to the cent:
        #
        #   6286 Friends of Sherrie Sprenger   $100.00
        #   5192 Northwest Sportfishing Ind.    $95.00
        #   2069 Oregonians for Affordable H.   $50.00
        #    145 Oregon Faculties PAC            $5.00
        #
        # Each has ONE year off — 2007, the filing year — entirely in
        # contributions. 2006 reconciles for all four, so ORESTAR is not
        # filing them under a different period; it is not counting them at all.
        # Marked here and held out of the cash balance only, so the rows keep
        # counting everywhere else.
        df["_undated"] = _tran_date.isna()
    else:
        _eff_date = df["filed_date"]
        df["_undated"] = False
    df["year"]  = _eff_date.dt.year.astype(int)
    df["month"] = _eff_date.dt.to_period("M").astype(str)

    # ── Filer-ID-based name normalization ───────────────────────────────────
    # All transactions for a given filer ID adopt the committee name from the
    # most recent transaction with that ID, so renames are reflected everywhere.
    # Uses the raw "filer" column (always populated) to build the map, since
    # backfilled rows may have blank filer_canonical values. Trim boundary
    # whitespace after choosing each ID's latest name, so historical raw names
    # cannot split a canonical scope. Preserve substantive spelling and case.
    if "filer id" in df.columns:
        _fid = df["filer id"].fillna("").astype(str).str.strip()
        _has_id = _fid.ne("")
        _id_map = (
            df.loc[_has_id]
            .assign(_fid_tmp=_fid[_has_id])
            .sort_values("filed_date")
            .groupby("_fid_tmp")["filer"]
            .last()
            .str.strip()
            .to_dict()
        )
        _mapped = _fid.map(_id_map)
        _fc = "filer_canonical" if "filer_canonical" in df.columns else "filer"
        df["filer_canonical"] = _mapped.where(_mapped.notna(), df.get(_fc, ""))
        log.info(
            "Filer-ID normalization applied: %d filer IDs → %d unique canonical names",
            len(_id_map), df["filer_canonical"].nunique(),
        )

    # Use canonical names if available, fall back to raw
    contrib_col = "contributor_payee_canonical" if "contributor_payee_canonical" in df.columns else "contributor_payee"
    filer_col   = "filer_canonical" if "filer_canonical" in df.columns else "filer"
    donor_col = "_donor_label"
    df[donor_col] = _donor_grouping_labels(df)

    ttype = df["tran_type"].str.strip().str.upper()
    contributions   = df[ttype == "C"]
    expenditures    = df[ttype == "E"]
    other_receipts  = df[ttype == "OR"]                    # Other Receipt only (matches ORESTAR "Other Receipts" line)
    other_disburse  = df[ttype == "OD"]                    # Other Disbursement only (matches ORESTAR "Other Disbursements" line)
    # Note: types O (Account Payable Rescinded, Loan Forgiven) and OA (Unexpended
    # Agent Balance, Misc Account Receivable) are non-cash accounting entries
    # that ORESTAR does NOT include in its Other Receipts or COH calculation.
    #
    # "Cash Balance Adjustment" is the exception, and excluding it was wrong.
    # ORESTAR's account summary carries a Balance Adjustments line that lands
    # between the two subtotals and moves the ending balance directly, and
    # these rows are its transaction-level source: summed per filer-year they
    # reproduce that line exactly in 1,092 of 1,629 cases (67%), and no other
    # sub_type or combination comes close.
    #
    # Leaving it out cost a permanent, carried-forward error. Oregon Realtors'
    # 2015 adjustment is -$27,432.32 and its 2015 discrepancy was +$27,432 to
    # the dollar; from 2017 on, its yearly contributions and expenditures match
    # ORESTAR exactly while the balance stays off by a constant. 205 committees
    # are explained by this alone.
    #
    # Taken from transactions rather than from the scraped summary line on
    # purpose: cash on hand is calculated, and ORESTAR stays the check.
    balance_adjust  = df[(ttype == "O") & (df["sub_type"].str.strip() == "Cash Balance Adjustment")]

    # Separate cash contributions from in-kind.
    # IMPORTANT: Use exact "In-Kind Contribution" match only. Other sub_types containing
    # "In-Kind" (e.g. "In-Kind/Forgiven Account Payable", "In-Kind/Forgiven Personal
    # Expenditures", "Pledge of In-Kind") should NOT be mirrored to the expenditure side,
    # as they are not standard in-kind contributions in ORESTAR's methodology.
    # Using str.contains("In-Kind") was a bug that inflated expenditures by $5.2M across
    # 1,483 filers without a matching contribution offset.
    # All three in-kind varieties, not just the plain one. This drives both the
    # separate in-kind metric and, by inversion, the cash-only contribution
    # figures — forgiven payables and forgiven personal expenditures are no
    # more cash than a donated banner is.
    inkind_mask     = contributions["sub_type"].isin(INKIND_SUBTYPES)
    cash_contribs   = contributions[~inkind_mask]
    inkind_contribs = contributions[inkind_mask]

    # ORESTAR-matching: Cash Contributions = Cash Contribution + In-Kind Contribution
    _orestar_contrib_mask = contributions["sub_type"].isin({"Cash Contribution"} | set(INKIND_SUBTYPES))
    _orestar_contribs = contributions[_orestar_contrib_mask]
    # ORESTAR-matching: Cash Expenditures = Cash Expenditure + In-Kind mirrored
    _cash_expend_mask = expenditures["sub_type"] == "Cash Expenditure"
    _cash_expend_global = expenditures[_cash_expend_mask]
    _inkind_global_amount = round(inkind_contribs["amount"].sum(), 2)

    # ── summary.json ─────────────────────────────────────────────────────────
    summary = {
        # Contributions are CASH ONLY app-wide; in-kind is reported separately
        # as total_inkind and never folded in. ORESTAR's own "Cash
        # Contributions" line does include in-kind, so these figures will
        # differ from ORESTAR's published summary by exactly total_inkind —
        # the ORESTAR reconciliation below uses its own _orestar_* frames and
        # is unaffected.
        "total_contributions":  round(cash_contribs["amount"].sum(), 2),
        "total_inkind":         round(inkind_contribs["amount"].sum(), 2),
        "total_expenditures":   round(_cash_expend_global["amount"].sum(), 2),
        "total_other_receipts":  round(other_receipts["amount"].sum(), 2),
        "total_other_disburse":  round(other_disburse["amount"].sum(), 2),
        "total_transactions":   int(len(df)),
        "num_contributions":    int(len(cash_contribs)),
        "num_inkind":           int(len(inkind_contribs)),
        "num_expenditures":     int(len(expenditures)),
        "date_range_start":     _eff_date.min().strftime("%Y-%m-%d") if len(df) else "",
        "date_range_end":       _eff_date.max().strftime("%Y-%m-%d") if len(df) else "",
        "last_updated":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json("summary.json", summary)

    # ── top_donors.json ───────────────────────────────────────────────────────
    top_donors_all = (
        cash_contribs.groupby(donor_col)["amount"]
        .sum()
        .nlargest(1000)
        .reset_index()
        .rename(columns={donor_col: "name", "amount": "total"})
    )
    top_donors_all["total"] = top_donors_all["total"].round(2)

    by_year_donors: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].unique()):
        yr_df = cash_contribs[cash_contribs["year"] == yr]
        top = (
            yr_df.groupby(donor_col)["amount"]
            .sum()
            .nlargest(1000)
            .reset_index()
            .rename(columns={donor_col: "name", "amount": "total"})
        )
        top["total"] = top["total"].round(2)
        by_year_donors[str(yr)] = top.to_dict(orient="records")

    _write_json("top_donors.json", {
        "all_time": top_donors_all.to_dict(orient="records"),
        "by_year":  by_year_donors,
    })

    # ── top_recipients.json ───────────────────────────────────────────────────
    top_recipients_all = (
        cash_contribs.groupby(filer_col)["amount"]
        .sum()
        .nlargest(100)
        .reset_index()
        .rename(columns={filer_col: "name", "amount": "total"})
    )
    top_recipients_all["total"] = top_recipients_all["total"].round(2)

    by_year_recipients: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].unique()):
        yr_df = cash_contribs[cash_contribs["year"] == yr]
        top = (
            yr_df.groupby(filer_col)["amount"]
            .sum()
            .nlargest(100)
            .reset_index()
            .rename(columns={filer_col: "name", "amount": "total"})
        )
        top["total"] = top["total"].round(2)
        by_year_recipients[str(yr)] = top.to_dict(orient="records")

    _write_json("top_recipients.json", {
        "all_time": top_recipients_all.to_dict(orient="records"),
        "by_year":  by_year_recipients,
    })

    # ── timeline.json ─────────────────────────────────────────────────────────
    cash_monthly = (
        cash_contribs.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "contributions"})
    )
    inkind_monthly = (
        inkind_contribs.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "inkind"})
    )
    expend_monthly = (
        expenditures.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "expenditures"})
    )
    count_monthly = (
        df.groupby("month").size().reset_index(name="count")
    )
    timeline = (
        cash_monthly
        .merge(inkind_monthly, on="month", how="outer")
        .merge(expend_monthly, on="month", how="outer")
        .merge(count_monthly,  on="month", how="outer")
        .fillna(0)
        .sort_values("month")
    )
    timeline["contributions"] = timeline["contributions"].round(2)
    timeline["inkind"]        = timeline["inkind"].round(2)
    timeline["expenditures"]  = timeline["expenditures"].round(2)
    timeline["count"]         = timeline["count"].astype(int)

    # ── by_contributor_type.json ──────────────────────────────────────────────
    def _type_rows(frame):
        """Build [{type, total, top_donors}] list interleaving in-state then out-of-state per type."""
        if frame.empty or "book_type" not in frame.columns:
            return []
        oos = frame["is_out_of_state"] if "is_out_of_state" in frame.columns else pd.Series(False, index=frame.index)
        in_s  = frame[~oos].groupby("book_type")["amount"].sum()
        out_s = frame[ oos].groupby("book_type")["amount"].sum()
        all_types = sorted(
            set(in_s.index) | set(out_s.index),
            key=lambda t: -(in_s.get(t, 0) + out_s.get(t, 0)),
        )
        rows = []
        for t in all_types:
            iv = round(float(in_s.get(t, 0)), 2)
            ov = round(float(out_s.get(t, 0)), 2)
            if iv:
                sub_in = frame[~oos & (frame["book_type"] == t)]
                top_in = (
                    sub_in.groupby(donor_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={donor_col: "name", "amount": "total"})
                )
                top_in["total"] = top_in["total"].round(2)
                rows.append({"type": t, "total": iv, "top_donors": top_in.to_dict(orient="records")})
            if ov:
                sub_oos = frame[oos & (frame["book_type"] == t)]
                top_oos = (
                    sub_oos.groupby(donor_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={donor_col: "name", "amount": "total"})
                )
                top_oos["total"] = top_oos["total"].round(2)
                rows.append({"type": t + " (out of state)", "total": ov, "top_donors": top_oos.to_dict(orient="records")})
        return rows

    by_type_by_year: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].dropna().unique()):
        yr_rows = _type_rows(cash_contribs[cash_contribs["year"] == yr])
        by_type_by_year[str(int(yr))] = yr_rows
    by_type_by_month: dict[str, list] = {}
    if "month" in cash_contribs.columns:
        for mo in sorted(cash_contribs["month"].dropna().unique()):
            by_type_by_month[str(mo)] = _type_rows(cash_contribs[cash_contribs["month"] == mo])
    _write_json("by_contributor_type.json", {
        "all_time": _type_rows(cash_contribs),
        "by_year":  by_type_by_year,
        "by_month": by_type_by_month,
    })

    # ── recent_transactions.json ──────────────────────────────────────────────
    recent = df.copy()
    recent["filed_date"] = recent["filed_date"].dt.strftime("%Y-%m-%d")
    recent["amount"] = recent["amount"].round(2)

    keep_cols = [c for c in [
        "tran_id", "filed_date", "tran_type", "amount",
        contrib_col, filer_col, "book_type", "purpose"
    ] if c in recent.columns]
    recent = recent[keep_cols].rename(columns={
        contrib_col: "contributor_payee",
        filer_col:   "filer",
    })
    recent = recent.sort_values("filed_date", ascending=False).head(10000)
    recent = recent.fillna("")
    _write_json("recent_transactions.json", recent.to_dict(orient="records"))

    # ── per-filer index + detail files ───────────────────────────────────────
    global_coh_data = aggregate_filers(df, contributions, inkind_contribs, expenditures,
                                       other_receipts, other_disburse, balance_adjust,
                                       filer_col, contrib_col, donor_col)

    # Add the exact, per-filer-derived cash trajectory to the visible global
    # component timeline. Synthetic January rows are possible when a committee
    # has an opening anchor but no transaction that month; their display totals
    # and count remain zero while the cash position starts at the right point.
    global_cash_rows = pd.DataFrame([
        {"month": month, "cash_balance_net": amount}
        for month, amount in global_coh_data["global_cash_timeline"].items()
    ])
    if not global_cash_rows.empty:
        timeline = timeline.merge(global_cash_rows, on="month", how="outer")
    else:
        timeline["cash_balance_net"] = 0.0
    timeline = timeline.fillna(0).sort_values("month")
    timeline["contributions"] = timeline["contributions"].round(2)
    timeline["inkind"] = timeline["inkind"].round(2)
    timeline["expenditures"] = timeline["expenditures"].round(2)
    timeline["cash_balance_net"] = timeline["cash_balance_net"].round(2)
    timeline["count"] = timeline["count"].astype(int)
    _write_json("timeline.json", timeline.to_dict(orient="records"))

    # Update summary.json with correct global cash-on-hand (sum of per-filer COH)
    summary["global_cash_on_hand"] = global_coh_data["global_cash_on_hand"]
    summary["global_beginning_balances"] = global_coh_data["global_beginning_balances"]
    summary["global_cash_excludes_independent_filers"] = global_coh_data[
        "global_cash_excludes_independent_filers"]
    _write_json("summary.json", summary)

    log.info("Aggregation complete. JSON files written to %s", AGG_DIR)


def _latest_year_summaries(yearly: dict, filer_ids: list[str]) -> dict:
    """Newest year's account summary per filer, keyed as the balance
    comparison expects.

    The yearly file names fields as ORESTAR labels them (contributions,
    expenditures, other_receipts); the balance comparison inherited
    orestar_-prefixed names from the scraper it replaces. Mapped here so the
    consumers stay untouched.
    """
    out: dict[str, dict] = {}
    for fid in filer_ids:
        years = (yearly.get(str(fid)) or {}).get("years", {})
        if not years:
            continue
        yr = max(years)                      # string years sort correctly
        y = years[yr]

        def _optional_amount(key: str) -> float | None:
            value = y.get(key)
            return None if value is None else float(value)

        out[str(fid)] = {
            "year": int(yr),
            # When this summary was captured, carried down from the FILER level.
            #
            # The yearly file stores ts once per filer — {"years": {...}, "ts": …}
            # — not inside each year, so lifting only the year dict dropped it and
            # `orestar_info.get("ts", 0)` found nothing. 7,297 of 7,299 account
            # summaries reached the site with no timestamp at all.
            #
            # That is not cosmetic: without it there is no way to tell "ORESTAR's
            # figure is older than our transactions" from "our figures are wrong",
            # which is exactly the question the 2026 balance divergence turns on.
            # The popover already tries to show "Scraped: …" and has been printing
            # the epoch.
            "ts": float(
                y.get("scrape_ts")
                or (yearly.get(str(fid)) or {}).get("ts")
                or 0
            ),
            "beginning_balance": float(y.get("beginning_balance") or 0),
            "ending_cash_balance": float(y.get("ending_cash_balance") or 0),
            "orestar_contributions": float(y.get("contributions") or 0),
            "orestar_expenditures": float(y.get("expenditures") or 0),
            "orestar_other_receipts": float(y.get("other_receipts") or 0),
            "orestar_other_disbursements": float(y.get("other_disbursements") or 0),
            "balance_adjustments": float(y.get("balance_adjustments") or 0),
            "inkind_contributions": _optional_amount("inkind_contributions"),
            "inkind_expenditures": _optional_amount("inkind_expenditures"),
            "loans_received": _optional_amount("loans_received"),
            "loans_received_exempt": _optional_amount("loans_received_exempt"),
            "loan_payments": _optional_amount("loan_payments"),
            "loan_payments_exempt": _optional_amount("loan_payments_exempt"),
            "accounts_receivable": _optional_amount("accounts_receivable"),
            "accounts_payable": _optional_amount("accounts_payable"),
            "total_outstanding_loans": _optional_amount("total_outstanding_loans"),
            "outstanding_personal_expenditures": _optional_amount(
                "outstanding_personal_expenditures"
            ),
            "balance_deficit": _optional_amount("balance_deficit"),
            "scrape_ts": float(
                y.get("scrape_ts")
                or (yearly.get(str(fid)) or {}).get("ts")
                or 0
            ),
        }
    return out


def scrape_account_summaries(
    filer_ids: list[str],
    *,
    cache_path: Path = DATA_DIR / "orestar_cash_balances.json",
    max_workers: int = 10,
    # A full refresh is ~7,245 live page fetches, about 58 minutes. At a 1-day
    # TTL the daily job re-scraped all of them on any day the weekly scrapers
    # had not just run, which does not fit in its 60-minute budget — the runs
    # on 22, 24, 25 and 27 July all died here, mid-scrape.
    #
    # Nothing needs it daily. These figures are the CHECK that cash-on-hand is
    # compared against, never an input to it, and the weekly filer-metadata and
    # earliest-balances jobs already own refreshing them.
    max_age_days: int = 7,
) -> dict[str, dict]:
    """Fetch full Account Summary from ORESTAR publicAccountSummary for each filer ID.

    Extracts all financial line items including Beginning Balance (Previous Year),
    which serves as the anchor for calculating per-year cash balances.

    Results are cached in *cache_path*.  Only filer IDs missing from the cache
    (or whose cached entry is older than *max_age_days*) are re-fetched.

    Returns ``{filer_id_str: {year, beginning_balance, ending_cash_balance, ...}}``
    for every ID that could be resolved.
    """
    import concurrent.futures
    import urllib.request

    # Load cache
    cache: dict = {}
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)

    now_ts = datetime.now().timestamp()
    cutoff = now_ts - max_age_days * 86_400

    ids_to_fetch = [
        fid for fid in filer_ids
        if fid not in cache or cache[fid].get("ts", 0) < cutoff
    ]

    if not ids_to_fetch:
        log.info("ORESTAR account-summary cache is fresh (%d entries)", len(cache))
    else:
        log.info(
            "Fetching ORESTAR Account Summary for %d / %d filers …",
            len(ids_to_fetch), len(filer_ids),
        )

        # Regex to extract the year from the page heading
        _YEAR_RE = re.compile(
            r"Account Summary Information for the year\s+(\d{4})",
        )

        def _parse_dollar(html: str, label: str) -> float | None:
            """Extract a dollar amount following a label in ORESTAR HTML.

            The old docstring here called ($X,XXX.XX) the POSITIVE format. It
            is ORESTAR's negative format — accounting parentheses — and reading
            it as positive is what flipped the sign on every committee in the
            red. See scraper/orestar_parse.py.
            """
            return orestar_parse.parse_dollar(html, label)

        def _fetch_one(fid: str) -> tuple[str, dict | None]:
            url = (
                "https://secure.sos.state.or.us/orestar/"
                f"publicAccountSummary.do?filerId={fid}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ORESTAR-dashboard/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except Exception:
                return fid, None

            # Extract year
            year_m = _YEAR_RE.search(html)
            if not year_m:
                return fid, None
            year = int(year_m.group(1))

            # Extract all financial line items
            ending = _parse_dollar(html, "Ending Cash Balance")
            if ending is None:
                return fid, None  # Page didn't load properly

            # Extract Financial Status section separately to avoid
            # ambiguous label matches (e.g. "Cash Balance" appears as both
            # a section header in Cash Balance and a line item in Financial Status)
            fs_html = ""
            fs_start = html.find("<h5>Financial Status</h5>")
            if fs_start == -1:
                fs_start = html.find("Financial Status-->")
            if fs_start >= 0:
                fs_html = html[fs_start:]

            result = {
                "year": year,
                # Contributions section
                "beginning_balance": _parse_dollar(html, "Beginning Balance (Previous Year)") or 0.0,
                "ending_cash_balance": ending,
                "orestar_contributions": _parse_dollar(html, "Cash Contributions") or 0.0,
                "loans_received": _parse_dollar(html, "Loans Received (non-exempt)") or 0.0,
                "inkind_contributions": _parse_dollar(html, "In-Kind Contributions") or 0.0,
                # Expenditures section
                "orestar_expenditures": _parse_dollar(html, "Cash Expenditures") or 0.0,
                "loan_payments": _parse_dollar(html, "Loan Payments (non-exempt)") or 0.0,
                "inkind_expenditures": _parse_dollar(html, "In-Kind Expenditures") or 0.0,
                # Cash Balance section
                "orestar_other_receipts": _parse_dollar(html, "Other Receipts") or 0.0,
                "loans_received_exempt": _parse_dollar(html, "Loans Received (exempt)") or 0.0,
                "orestar_other_disbursements": _parse_dollar(html, "Other Disbursements") or 0.0,
                "loan_payments_exempt": _parse_dollar(html, "Loan Payments (exempt)") or 0.0,
                "balance_adjustments": _parse_dollar(html, "Balance Adjustments") or 0.0,
                # Financial Status section (parsed from FS-only HTML to avoid ambiguous matches)
                "cash_balance_fs": _parse_dollar(fs_html, "Cash Balance") or 0.0,
                "accounts_receivable": _parse_dollar(fs_html, "Accounts Receivable") or 0.0,
                "total_outstanding_loans": _parse_dollar(fs_html, "Total Outstanding Loans") or 0.0,
                "outstanding_personal_expenditures": _parse_dollar(fs_html, "Outstanding Personal Expenditures") or 0.0,
                "accounts_payable": _parse_dollar(fs_html, "Accounts Payable") or 0.0,
                "balance_deficit": _parse_dollar(fs_html, "Balance Deficit") or 0.0,
            }
            return fid, result

        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, fid): fid for fid in ids_to_fetch}
            for future in concurrent.futures.as_completed(futures):
                fid, val = future.result()
                done += 1
                if done % 500 == 0:
                    log.info("  … %d / %d fetched", done, len(ids_to_fetch))
                if val is not None:
                    cache[fid] = {**val, "ts": now_ts}

        log.info("ORESTAR account-summary fetch complete (%d cached total)", len(cache))

        # Persist cache
        with open(cache_path, "w") as f:
            json.dump(cache, f, separators=(",", ":"))

    # Return full summary dicts (including scrape timestamp for frontend display)
    result = {}
    for fid, entry in cache.items():
        if "ending_cash_balance" in entry:
            result[fid] = dict(entry)  # keep all fields including "ts"
        elif "balance" in entry:
            # Legacy cache entry (old format) — treat as ending balance only
            result[fid] = {"ending_cash_balance": entry["balance"], "year": 0, "beginning_balance": 0.0, "ts": entry.get("ts", 0)}
    return result


def _trailing_blank_closure(years: dict) -> tuple[bool, int | None, float | None]:
    """Return closure evidence for one physical ORESTAR filer.

    Closure cannot be inferred after year-wise aggregation: in a multi-ID
    canonical profile, one filer's trailing blank can be newer than another
    filer's still-live final statement and falsely close the entire profile.
    Each physical filer must independently have a real statement followed by
    at least one blank statement.
    """
    if not isinstance(years, dict):
        return False, None, None

    ordered = sorted(
        (str(year) for year in years if str(year).isdigit()), key=int
    )
    if not ordered:
        return False, None, None

    # Only a continuous suffix of fully parsed rows can establish closure. An
    # F5-truncated row or a legacy synthetic-zero row is unknown, not blank.
    suffix_start = len(ordered)
    for index in range(len(ordered) - 1, -1, -1):
        if not _summary_is_confirmed_blank(years[ordered[index]]):
            break
        suffix_start = index
    if suffix_start == len(ordered):
        return False, None, None

    live = [year for year in ordered[:suffix_start]
            if _summary_has_activity(years[year])]
    if not live:
        return False, None, None
    blank_from = int(ordered[suffix_start])
    final_balance = float(years[live[-1]].get("ending_cash_balance") or 0.0)
    return True, blank_from, final_balance


def aggregate_filers(
    df: pd.DataFrame,
    contributions: pd.DataFrame,   # all type C contributions (including in-kind)
    inkind_contribs: pd.DataFrame,
    expenditures: pd.DataFrame,    # E type
    other_receipts: pd.DataFrame,  # OR/O/OA type (e.g. Return/Refund, Misc Receipt)
    other_disburse: pd.DataFrame,  # OD type (e.g. Misc Other Disbursement)
    balance_adjust: pd.DataFrame,  # type O "Cash Balance Adjustment" only
    filer_col: str,
    contrib_col: str,
    donor_col: str | None = None,
) -> None:
    """Generate filer_index.json and per-filer detail files under data/aggregated/filers/."""
    if donor_col is None:
        # Direct callers can pass contribution frames created before donor
        # normalization. Label both frames without mutating their source data.
        donor_col = "_donor_label"
        df = df.assign(**{donor_col: _donor_grouping_labels(df)})
        contributions = contributions.assign(
            **{donor_col: _donor_grouping_labels(contributions)}
        )

    filers_dir = AGG_DIR / "filers"
    filers_dir.mkdir(parents=True, exist_ok=True)

    # Fingerprint the exact app-side transaction snapshot once.  A freshly
    # scraped ORESTAR summary is allowed to become an authoritative comparison
    # only when its saved app balance came from these exact bytes.
    _transaction_snapshot_id = transaction_snapshot_id(TRANS_DIR)
    _balance_snapshot_scopes: list[dict] = []

    def _filer_type_rows(frame):
        if frame.empty or "book_type" not in frame.columns:
            return []
        oos = frame["is_out_of_state"] if "is_out_of_state" in frame.columns else pd.Series(False, index=frame.index)
        in_s  = frame[~oos].groupby("book_type")["amount"].sum()
        out_s = frame[ oos].groupby("book_type")["amount"].sum()
        all_types = sorted(
            set(in_s.index) | set(out_s.index),
            key=lambda t: -(in_s.get(t, 0) + out_s.get(t, 0)),
        )
        rows = []
        for t in all_types:
            iv = round(float(in_s.get(t, 0)), 2)
            ov = round(float(out_s.get(t, 0)), 2)
            if iv:
                sub_in = frame[~oos & (frame["book_type"] == t)]
                top_in = (
                    sub_in.groupby(donor_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={donor_col: "name", "amount": "total"})
                )
                top_in["total"] = top_in["total"].round(2)
                rows.append({"type": t, "total": iv, "top_donors": top_in.to_dict(orient="records")})
            if ov:
                sub_oos = frame[oos & (frame["book_type"] == t)]
                top_oos = (
                    sub_oos.groupby(donor_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={donor_col: "name", "amount": "total"})
                )
                top_oos["total"] = top_oos["total"].round(2)
                rows.append({"type": t + " (out of state)", "total": ov, "top_donors": top_oos.to_dict(orient="records")})
        return rows

    # ── Build slug registry with collision handling ──────────────────────────
    all_filer_names = sorted([n for n in df[filer_col].dropna().unique() if n != ""])
    slug_registry: dict[str, str] = {}  # slug → name (first claimant)
    filer_slugs: dict[str, str] = {}    # name → slug

    for name in all_filer_names:
        base = make_slug(name)
        slug = base
        counter = 2
        while slug in slug_registry and slug_registry[slug] != name:
            slug = f"{base}_{counter}"
            counter += 1
        slug_registry[slug] = name
        filer_slugs[name] = slug

    # ── Pre-group for performance ─────────────────────────────────────────────
    contrib_groups  = contributions.groupby(filer_col)
    inkind_groups   = inkind_contribs.groupby(filer_col)
    expend_groups   = expenditures.groupby(filer_col)
    or_groups       = other_receipts.groupby(filer_col)
    od_groups       = other_disburse.groupby(filer_col)
    ba_groups       = balance_adjust.groupby(filer_col)
    all_groups      = df.groupby(filer_col)

    def get_group(groups, name):
        return groups.get_group(name) if name in groups.groups else pd.DataFrame()

    def monthly_sum(frame, col_name):
        if frame.empty or "month" not in frame.columns:
            return pd.Series(dtype=float, name=col_name)
        return frame.groupby("month")["amount"].sum().rename(col_name)

    # ── Scrape ORESTAR Account Summary ───────────────────────────────────────
    # Build canonical-name → filer-id mapping so we can look up ORESTAR data.
    _filer_id_col = "filer id" if "filer id" in df.columns else None
    _name_to_fids: dict[str, list[str]] = {}
    _name_to_fid: dict[str, str] = {}
    _orestar_data: dict[str, dict] = {}  # canonical name → full summary dict
    _comparison_by_name: dict[str, dict] = {}
    _current_scope_digests: dict[str, str] = {}
    _current_year_digests: dict[str, dict[str, str]] = {}
    # Loaded here, ahead of the first use. The balance comparison below now
    # derives from this file, so reading it later left _yearly_summaries
    # unbound and killed the whole run:
    #   UnboundLocalError: cannot access local variable '_yearly_summaries'
    _yearly_path = DATA_DIR / "orestar_yearly_summaries.json"
    _yearly_summaries: dict[str, dict] = {}  # filer_id → {years: {yr: {...}}, ts}
    if _yearly_path.exists():
        with open(_yearly_path) as f:
            _yearly_summaries = _normalize_year_scrape_timestamps(json.load(f))
        log.info("Loaded yearly ORESTAR summaries for %d filers", len(_yearly_summaries))

    if _filer_id_col:
        # EVERY filer id a canonical name covers, not just one.
        #
        # 265 canonical names span more than one ORESTAR committee — 554
        # committees, 106,337 rows, $133M — usually because the committees
        # differ only in punctuation ("Yes! Keep Our Groceries Tax Free!" vs
        # "...Tax-Free!"). Aggregation groups by canonical NAME, so those rows
        # are summed together; ORESTAR reports per committee.
        #
        # Keeping one id via drop_duplicates compared our merged totals against
        # a single committee's summary, which reads as a huge overage that
        # looks exactly like ORESTAR under-reporting. That one name was 78% of
        # 2018's entire apparent gap: ours $8,632,365 against ORESTAR's
        # $6,362,423, purely because the other committee's $4.6M was folded in
        # on our side only.
        _name_to_fids = (
            df[[filer_col, _filer_id_col]]
            .dropna(subset=[_filer_id_col])
            .assign(_fid=lambda x: x[_filer_id_col].astype(str).str.strip())
            .query("_fid != ''")
            .groupby(filer_col)["_fid"]
            .agg(lambda v: sorted(set(v)))
            .to_dict()
        )
        # Kept for callers that only need a representative id.
        _name_to_fid = {n: ids[-1] for n, ids in _name_to_fids.items()}
        # A global shard fingerprint proves which complete checkout produced an
        # aggregate, but it becomes stale for every committee when any one
        # committee changes.  Bind summary-derived cash treatment to the exact
        # transaction rows in its canonical scope instead.  This catches
        # amount/status/date edits as well as added rows, without making an
        # unrelated daily filing invalidate all weekly summary captures.
        for name in _name_to_fids:
            _scope_digest, _year_digests = cash_scope_digests(
                get_group(all_groups, name).to_dict(orient="records")
            )
            _current_scope_digests[name] = _scope_digest
            _current_year_digests[name] = _year_digests
        # Loaded once here rather than per filer: 7,263 committees would
        # otherwise re-read and re-parse the same file.
        global _ROW_COMPLETE, _ORESTAR_ABSENT
        _ROW_COMPLETE = _row_completeness()
        # The row diff wins wherever it exists. It is expensive — it pages
        # entire result sets where the survey spends one search — so it covers
        # only committees someone chose to run it on, and the count survey
        # still answers for the rest.
        _diff_complete, _diff_rows = _row_diff()
        if _diff_complete:
            _ROW_COMPLETE.update(_diff_complete)
            _diff_usable = sum(v[0] is not None for v in _diff_complete.values())
            log.info("row-level diff available for %d committees (%d usable, %d "
                     "explicitly unknown; authoritative over the count survey)",
                     len(_diff_complete), _diff_usable,
                     len(_diff_complete) - _diff_usable)
        log.info("row-completeness known for %d committees (from the coverage survey)",
                 len(_ROW_COMPLETE))
        unique_fids = sorted({f for ids in _name_to_fids.values() for f in ids})

        # Cash omission uses the same exact-evidence gate as automatic missing-
        # row remediation.  A legacy/top-level surplus is only an audit clue;
        # every member of the current canonical scope needs a common precise
        # lane anchored to its paired account-summary snapshot, and its
        # deterministic per-filer digest must still match the shards on disk.
        _comparison_by_name = {
            name: paired_comparison(
                fids,
                _yearly_summaries,
                current_transaction_id=_transaction_snapshot_id,
                current_scope_digest=_current_scope_digests.get(name),
            )
            for name, fids in _name_to_fids.items()
        }
        _surplus_candidates = rows_requesting_surplus(_diff_rows)
        _block_reasons: dict[str, list[str]] = {}
        _ORESTAR_ABSENT, _blocked_absent, _evidence_error = (
            _certified_orestar_absent(
                _diff_rows, _name_to_fids, _comparison_by_name, TRANS_DIR,
                reasons=_block_reasons,
            )
        )
        if _surplus_candidates:
            log.info(
                "Exact ORESTAR-absence evidence certified for %d physical "
                "filers (%d rows); %d physical filers refused",
                len(_ORESTAR_ABSENT),
                sum(len(ids) for ids in _ORESTAR_ABSENT.values()),
                len(_blocked_absent),
            )
            # The refusal count above is not actionable on its own. Break it
            # down by cause, and write the per-filer detail out so a refusal
            # can be traced to one committee rather than inferred from
            # aggregates. Each filer can carry several reasons, so the
            # percentages here deliberately do not sum to the total.
            _reason_tally = Counter(
                code
                for codes in _block_reasons.values()
                for code in codes
            )
            for _code, _n in _reason_tally.most_common():
                log.info(
                    "  refused: %-36s %4d filers", _code, _n,
                )
            _unattributed = sorted(
                fid for fid in _blocked_absent if not _block_reasons.get(fid)
            )
            if _unattributed:
                # Should be unreachable: every blocking site records a reason.
                # If this ever fires, a new refusal path was added without
                # attribution and the breakdown above is silently incomplete.
                log.warning(
                    "  refused with NO recorded reason: %d filers (%s)",
                    len(_unattributed), ", ".join(_unattributed[:10]),
                )
            _refusal_path = AGG_DIR / "certification_refusals.json"
            _refusal_path.write_text(json.dumps({
                "generated": datetime.now(timezone.utc).isoformat(
                    timespec="seconds",
                ),
                "certified_filers": len(_ORESTAR_ABSENT),
                "certified_rows": sum(
                    len(ids) for ids in _ORESTAR_ABSENT.values()
                ),
                "refused_filers": len(_blocked_absent),
                "by_reason": dict(_reason_tally.most_common()),
                "filers": {
                    fid: codes
                    for fid, codes in sorted(_block_reasons.items())
                },
            }, indent=2))
            log.info("Wrote %s", _refusal_path.name)
        if _evidence_error:
            log.warning(
                "ORESTAR-absence evidence could not be certified: %s",
                _evidence_error,
            )
        # The full-scope digest above must stay independent of certified
        # absence: it is the immutable row fingerprint used to certify the
        # exact diff, so making it depend on the verdict would create a cycle.
        # Annual provenance is different. A certified nonreturning row is
        # omitted from cash while remaining in the shards, so its year digest
        # must include that exclusion state. The no-exclusion digests computed
        # above are already correct; recompute only scopes whose certified
        # state changes at least one held row.
        for name, fids in _name_to_fids.items():
            _absent_ids = _orestar_absent_for_filers(fids)
            if not _absent_ids:
                continue
            _unused_scope_digest, _year_digests = cash_scope_digests(
                get_group(all_groups, name).to_dict(orient="records"),
                cash_excluded_transaction_ids=_absent_ids,
            )
            _current_year_digests[name] = _year_digests
        # Derived from the yearly summaries rather than scraped again.
        #
        # Two caches were reading the SAME ORESTAR page. scrape_account_summaries
        # took the current year into orestar_cash_balances.json;
        # fetch_earliest_balances takes every year into
        # orestar_yearly_summaries.json. The yearly file is a strict superset —
        # 7,518 filers against 7,383, and nothing in the other file is absent
        # from it.
        #
        # Keeping both meant they drifted. The cash-balances cache was last
        # written 2026-03-26..04-04 while the yearly file is current, so every
        # balance comparison — the whole discrepancy tab — was measuring today's
        # transactions against ORESTAR figures four months old. 359 of 3,666
        # same-year balances disagreed purely from that lag.
        #
        # It also carried the in-kind bug fixed in #56: it asks for "In-Kind
        # Contributions", a label the page never prints, so its in-kind fields
        # are zero for every filer.
        #
        # Dropping it removes ~7,245 page fetches (about 58 minutes) from the
        # daily refresh, which is the step that used to blow the job's budget.
        _fid_to_summary = _latest_year_summaries(_yearly_summaries, unique_fids)
        # Map canonical name → ORESTAR account summary, SUMMED over every
        # committee the name covers, so both sides of the comparison describe
        # the same set of committees. Single-committee names are unaffected.
        _partial_merge = 0
        for canon_name, fids in _name_to_fids.items():
            parts = [_fid_to_summary[f] for f in fids if f in _fid_to_summary]
            if not parts:
                continue
            # A name's transactions are summed across every committee it covers,
            # so the ORESTAR side must cover the same committees or the two are
            # not comparable. Falling back to whichever summaries happen to
            # exist reports the missing committee's entire volume as a
            # discrepancy: "Oregon People's Rebate" spans filers 20684 and
            # 22517, only 22517 has a summary, and the comparison showed a
            # $251,794 gap that was purely 20684's own contributions. Our 42
            # rows for 22517 matched ORESTAR to the cent.
            #
            # 36 of 265 multi-committee names are in this state. Skipping is
            # the honest outcome: no comparison beats a false one, and these
            # committees simply go unchecked until their summaries are scraped.
            if len(parts) < len(fids):
                _partial_merge += 1
                log.debug("Skipping ORESTAR comparison for %s — %d of %d committees "
                          "have summaries", canon_name, len(parts), len(fids))
                continue
            if len(parts) == 1:
                _orestar_data[canon_name] = parts[0]
                continue
            merged = dict(parts[0])
            for k in _SUMMABLE:
                merged[k] = _sum_summary_field(parts, k)
            # Freshness for an aggregate is bounded by its newest component.
            # Using parts[0]'s timestamp can let older row-diff evidence certify
            # a canonical name even though another committee's summary was
            # captured later.
            merged["ts"] = max(float(p.get("ts") or 0) for p in parts)
            merged["merged_filer_ids"] = fids     # so the join is auditable
            _orestar_data[canon_name] = merged
        if _partial_merge:
            log.warning("%d canonical names skipped: they span several committees but only "
                        "some have ORESTAR summaries — scrape the rest to compare them",
                        _partial_merge)
        log.info("ORESTAR account summaries mapped for %d / %d filers", len(_orestar_data), len(all_filer_names))

    # ── Load earliest-year beginning balances (from Playwright scraper) ──────
    _earliest_balances_path = DATA_DIR / "earliest_balances.json"
    _earliest_balances: dict[str, dict] = {}  # filer_id → {earliest_year, beginning_balance, ts}
    if _earliest_balances_path.exists():
        with open(_earliest_balances_path) as f:
            _earliest_balances = json.load(f)
        log.info("Loaded %d earliest-year beginning balances", len(_earliest_balances))
    else:
        log.warning("No earliest_balances.json found — beginning balances will default to $0")

    # Load per-year ORESTAR summaries

    # Build canonical-name → earliest balance mapping
    _name_to_earliest: dict[str, dict] = {}
    _name_to_yearly: dict[str, dict] = {}  # canonical name → {yr: summary}
    if _filer_id_col:
        for canon_name, fids in _name_to_fids.items():
            # Opening balance: sum across the committees the name covers, since
            # the rolling calculation sums their transactions.
            # Same completeness rule as the account summaries above: an anchor
            # summed over some of a name's committees, against transactions
            # summed over all of them, understates the opening balance by the
            # missing committees' share and carries that error forward for ever.
            begins = [_earliest_balances[f] for f in fids if f in _earliest_balances]
            if begins and len(begins) == len(fids):
                if len(begins) == 1:
                    _name_to_earliest[canon_name] = begins[0]
                else:
                    _name_to_earliest[canon_name] = {
                        "earliest_year": min(b.get("earliest_year", 9999) for b in begins),
                        "beginning_balance": round(
                            sum(float(b.get("beginning_balance") or 0) for b in begins), 2),
                        # Only trustworthy if EVERY part reached its first statement.
                        "reached_earliest": all(b.get("reached_earliest") for b in begins),
                    }
            # Yearly summaries: add year by year across the same committees.
            yearlies = [_yearly_summaries[f].get("years", {}) for f in fids
                        if f in _yearly_summaries]
            if not yearlies or len(yearlies) < len(fids):
                continue          # incomplete coverage — see the note above
            if len(yearlies) == 1:
                _name_to_yearly[canon_name] = yearlies[0]
                continue
            _name_to_yearly[canon_name] = _combine_scope_yearly_summaries(yearlies)

    # Certificates of Limited Contributions and Expenditures — see
    # orestar_certificates.py. A certificate year is one ORESTAR's itemized
    # totals cannot see, and its restated opening balance is the only record of
    # what moved. Absent file: no ghost rows, balances exactly as before.
    _certificates: dict[str, dict[int, dict]] = {}
    _certificates_path = DATA_DIR / CERTIFICATES_FILENAME
    if _certificates_path.exists():
        try:
            _certificates = certificates_by_filer(json.loads(_certificates_path.read_text()))
        except (OSError, ValueError) as exc:
            # A certificate file that cannot be read yields no ghost rows —
            # the balances then match the pre-certificate calculation, which
            # is a visible, recoverable state, unlike half-applied rows.
            log.warning("Ignoring unreadable %s: %s", _certificates_path.name, exc)
        log.info("Loaded certificates for %d filers", len(_certificates))
    # A physical filer claimed by more than one canonical name would receive
    # its restatement once per name, counting ORESTAR's money twice. Those are
    # skipped and reported rather than guessed at.
    _fid_owners: Counter = Counter(
        fid for fids in _name_to_fids.values() for fid in set(fids)
    )
    _ghost_skipped_ambiguous: set[str] = set()
    _ghost_committees = 0
    _ghost_row_count = 0
    _ghost_amount = 0.0
    # Early-era amendment chains (see orestar_amendment_chains.py): versions
    # ORESTAR's summary still counts though its search hides them. Collected
    # per filer-year by fetch_amendment_chains.py; absent file, no change.
    _amendment_chains: dict = {}
    _chains_path = DATA_DIR / CHAINS_FILENAME
    if _chains_path.exists():
        try:
            _amendment_chains = json.loads(_chains_path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable %s: %s", _chains_path.name, exc)
    _chain_decisions: Counter = Counter()
    _chain_rows_added = 0
    _closure_resets_applied = 0
    _closure_amount = 0.0
    _independent_filer_scopes = 0
    # Independent Expenditure Filers kept out of statewide cash (see below).
    _independent_cash_excluded = 0.0
    # Amended originals ORESTAR still counts (see _drop_superseded). The merge
    # already kept them in the mirror; here they are listed per committee so
    # the site can name every one by transaction ID.
    _live_originals_evidence = _live_original_ids()
    _live_original_committees = 0
    _live_original_rows = 0
    # Transactions ORESTAR deleted that we had counted. The merge removed them
    # (see _apply_deletions); here each committee lists them by ID.
    _deletions_by_filer = _deletions_by_filer_id()
    _deletion_committees = 0
    _deletion_rows = 0

    # Load filer metadata (party, office, committee type) from scraper cache
    _filer_metadata_path = DATA_DIR / "filer_metadata.json"
    _filer_metadata: dict[str, dict] = {}  # filer_id → {committee_type, office, party, ...}
    if _filer_metadata_path.exists():
        with open(_filer_metadata_path) as f:
            _filer_metadata = json.load(f)
        log.info("Loaded %d filer metadata entries", len(_filer_metadata))
    else:
        log.warning("No filer_metadata.json found — party/office data will be unavailable")

    # Load leadership roles (filer_id → role info)
    _leadership_path = DATA_DIR / "leadership_roles.json"
    _leadership: dict[str, dict] = {}  # filer_id → {role_title, chamber, party, ...}
    if _leadership_path.exists():
        with open(_leadership_path) as f:
            _leadership = json.load(f)
        log.info("Loaded %d leadership roles", len(_leadership))
    else:
        log.info("No leadership_roles.json found — leadership data will be unavailable")

    # ── Per-filer detail files ────────────────────────────────────────────────
    # Remove stale files whose slugs are no longer in the current filer set.
    current_slugs = set(filer_slugs.values())
    for stale in filers_dir.glob("*.json"):
        if stale.stem not in current_slugs:
            stale.unlink()
            log.debug("Removed stale filer file: %s", stale.name)

    index_rows = []
    _filer_detail_rows: list[dict] = []  # accumulated for one bulk upsert to Supabase
    _global_beginning_balances: dict[str, float] = {}  # year → sum of per-filer beginning balances
    # Browser-global cash trajectory. Unlike the visible global component
    # timeline, this is built from each filer's fully corrected cash path and
    # includes that filer's opening anchor in the month it becomes effective.
    _global_cash_timeline: dict[str, float] = defaultdict(float)
    log.info("Generating per-filer detail files for %d filers…", len(all_filer_names))

    for name in all_filer_names:
        slug = filer_slugs[name]
        _scope_fids = _name_to_fids.get(name, [])
        _comparison = dict(
            _comparison_by_name.get(name)
            or paired_comparison(
                _scope_fids,
                _yearly_summaries,
                current_transaction_id=_transaction_snapshot_id,
                current_scope_digest=_current_scope_digests.get(name),
            )
        )
        # Parser freshness only says a loan field was read correctly.  It does
        # not prove that a weekly account summary still describes today's daily
        # transaction rows.  Any operation that changes cash using annual
        # summary fields must therefore share this stronger, scope-local gate.
        _summary_pair_available = bool(
            _comparison.get("status") == "paired"
            and not _comparison.get("orestar_data_changed_since_capture")
        )
        _summary_scope_current = bool(
            _summary_pair_available
            and _comparison.get("scope_digest_matches_capture") is True
        )
        _current_year_digests_here = _current_year_digests.get(name, {})

        def _digest_for_year(value) -> str | None:
            try:
                year = str(int(value))
            except (TypeError, ValueError):
                return None
            return _current_year_digests_here.get(
                year, EMPTY_CASH_SCOPE_DIGEST,
            )
        filer_contrib = get_group(contrib_groups, name)
        filer_inkind  = get_group(inkind_groups,  name)
        filer_expend  = get_group(expend_groups,  name)
        filer_or      = get_group(or_groups,      name)
        filer_od      = get_group(od_groups,      name)
        filer_ba      = get_group(ba_groups,      name)
        filer_all     = get_group(all_groups,     name)

        # ── ORESTAR-matching line item definitions (empirically verified) ──────
        # ORESTAR "Cash Contributions" = Cash Contribution + In-Kind Contribution
        #   (In-kind is on BOTH sides — contribution AND expenditure — netting to zero)
        # ORESTAR "Cash Expenditures" = Cash Expenditure + In-Kind (mirrored)
        #   EXCLUDES: Account Payable, PER, Agent, Loan Payment
        # ORESTAR "Other Receipts" = type OR only
        # ORESTAR "Other Disbursements" = type OD only
        #
        # COH effective formula (in-kind cancels):
        #   Ending = Begin + CashContribution + LoansReceived + OtherReceipts
        #            - CashExpenditure - LoanPayments - OtherDisbursements

        # Stat card / ORESTAR-matching totals
        # These two frames exist ONLY to reconcile against ORESTAR's account
        # summary, so they must mirror ORESTAR's own arithmetic exactly:
        #
        #   Total Contributions = Cash Contributions
        #                       + Loans Received (non-exempt)
        #                       + In-Kind
        #   Total Expenditures  = Cash Expenditures
        #                       + Loan Payments (non-exempt)
        #                       + In-Kind
        #
        # Both were short. Contributions omitted non-exempt loans, so any
        # committee with a loan looked to be missing exactly the loan amount —
        # Oregonians Are Ready showed "missing $1,000,000" in 2024 while our
        # rows summed to ORESTAR's $1,114,500 to the cent. Expenditures counted
        # only cash, omitting both loan payments and in-kind.
        #
        # Measured over 25,451 committee-years, matching ORESTAR exactly:
        #   contributions  78.6% -> 86.5%
        #   expenditures   55.7% -> 90.3%
        #
        # This is a DIAGNOSTIC, not the balance. Cash on hand was never
        # affected: _COH_C_TYPES/_COH_E_TYPES already carried non-exempt loans
        # and correctly leave in-kind out of a cash figure. What was wrong was
        # the instrument used to decide where money is missing — and it sent
        # this investigation after $15M of gaps that were never real.
        # ORESTAR recognises THREE in-kind varieties, not one. Its Total
        # Contributions and Total Expenditures lines both count all of them,
        # and each lands on both sides — which is why the omission produced
        # symmetric gaps, equal on contributions and expenditures.
        #
        # Friends of Sam Carpenter 2018 is the clean example: short exactly
        # $140,060.27 on each side, matching that year's
        # In-Kind/Forgiven Account Payable ($75,646.13) plus
        # In-Kind/Forgiven Personal Expenditures ($64,414.14).
        #
        # Note the plural on the second. An earlier check of mine used the
        # singular, matched nothing, and made this look worth one percentage
        # point rather than six.
        _CONTRIB_TYPES = ({"Cash Contribution", "Loan Received (Non-Exempt)"}
                          | set(INKIND_SUBTYPES))
        _EXPEND_TYPES  = {"Cash Expenditure", "Loan Payment (Non-Exempt)"}
        _orestar_contrib = filer_contrib[filer_contrib["sub_type"].isin(_CONTRIB_TYPES)] if not filer_contrib.empty else filer_contrib
        # In-kind sits under tran_type C in the source data, so ORESTAR's
        # expenditure total is rebuilt from the expenditure rows plus the
        # in-kind frame.
        # Cash expenditures plus non-exempt loan payments — and deliberately
        # NOT in-kind, which is added downstream where our_e is assembled:
        #     our_e = _yearly_cash_exp + _yearly_inkind
        #
        # In-kind genuinely belongs in this comparison; ORESTAR's Total
        # Expenditures includes it, confirmed against freshly parsed summaries
        # (Yes on 117 2024: cash+loan 3,957,175.42 + in-kind 5,467,744.61 =
        # 9,424,920.03, ORESTAR's figure to the cent). It was already being
        # added there. Folding it in here as well counted it TWICE, storing
        # 14,892,664.64 for that committee-year and inflating 9,155
        # committee-years by $179,850,067 in total.
        _cash_expend_only = (filer_expend[filer_expend["sub_type"].isin(_EXPEND_TYPES)]
                             if not filer_expend.empty else filer_expend)
        _inkind_amount = round(float(filer_inkind["amount"].sum()) if not filer_inkind.empty else 0.0, 2)

        # Cash only — in-kind is a separate, distinct metric (total_inkind).
        # _orestar_contrib / _yearly_orestar_c keep the in-kind-inclusive
        # definition for the ORESTAR reconciliation further down.
        _cash_contrib_only = (filer_contrib[filer_contrib["sub_type"] != "In-Kind Contribution"]
                              if not filer_contrib.empty else filer_contrib)
        total_in    = round(float(_cash_contrib_only["amount"].sum()) if not _cash_contrib_only.empty else 0.0, 2)
        total_inkind = _inkind_amount
        total_out   = round(float(_cash_expend_only["amount"].sum()) if not _cash_expend_only.empty else 0.0, 2)
        total_or    = round(float(filer_or["amount"].sum()) if not filer_or.empty else 0.0, 2)
        total_od    = round(float(filer_od["amount"].sum()) if not filer_od.empty else 0.0, 2)
        tran_count  = int(len(filer_all))

        # COH-affecting frames (in-kind nets to zero, so exclude it)
        _COH_C_TYPES = {"Cash Contribution", "Loan Received (Non-Exempt)"}
        _COH_E_TYPES = {"Cash Expenditure", "Loan Payment (Non-Exempt)"}
        _c_for_coh = filer_contrib[filer_contrib["sub_type"].isin(_COH_C_TYPES)] if not filer_contrib.empty else filer_contrib
        _e_for_coh = filer_expend[filer_expend["sub_type"].isin(_COH_E_TYPES)] if not filer_expend.empty else filer_expend
        # See the _undated note above: ORESTAR's period-based summaries exclude
        # a transaction it cannot date, so the balance excludes it too. Applied
        # to the cash frames only — the rows stay in contributor totals, donor
        # aggregates and transaction listings, where they are perfectly valid.
        # Rows ORESTAR no longer returns, held out of the balance but not
        # deleted. Exact absence does not prove whether ORESTAR withdrew a
        # filing, superseded it, or changed some other source-side state.
        #
        # ORESTAR can stop returning a filing, and nothing in this pipeline ever
        # removes it. Our store
        # only drifts upward, invisibly, until something forces it into view —
        # Plumbers & Steamfitters PAC carried 16 such rows worth $32,284.04 and
        # surveyed as "missing: 0", because a count cannot see them.
        #
        # The paired, current exact search proves ORESTAR did not count these at
        # the comparison snapshot, so the balance must not either. The
        # rows themselves stay: they are real history, they belong in
        # contributor totals, donor aggregates and transaction listings, and
        # deleting on the strength of a scraper's output is a far riskier
        # operation than declining to add a number up. Same treatment the
        # undated rows get above, and for the same reason.
        #
        # Only from fully certified diff_coverage.py evidence, never from a
        # count. Identity is the only evidence precise enough to name a row as
        # nonreturning.
        # Aggregation is by canonical name, and one name can span several filer
        # IDs. Union the row-level evidence across the same set of committees;
        # consulting one representative ID can leave another committee's known
        # nonreturning rows in the combined balance. Certification is atomic:
        # no member contributes IDs unless every member shared one valid lane.
        _orestar_absent_here = _orestar_absent_for_filers(_scope_fids)

        def _dated(_f):
            if _f is None or _f.empty:
                return _f
            if "_undated" in _f.columns:
                _f = _f[~_f["_undated"].fillna(False)]
            if (_orestar_absent_here and not _f.empty
                    and "tran_id" in _f.columns):
                _f = _f[
                    ~_f["tran_id"].astype(str).isin(_orestar_absent_here)
                ]
            return _f
        _c_for_coh = _dated(_c_for_coh)
        _e_for_coh = _dated(_e_for_coh)
        # Disbursements and balance adjustments go through the same filter.
        # Today every known nonreturning row is a type-C contribution, so these
        # are guards rather than corrections — but a filter covering three of
        # five cash frames is the #99 bug waiting to happen again, where a
        # correction reached some consumers and not the rest.
        _od_for_coh = _dated(filer_od)
        _ba_for_coh = _dated(filer_ba)
        # Exempt loans, where ORESTAR's own summary says it did not count one.
        #
        # #96 made ORESTAR's reported figure decide for NON-exempt loans, which
        # are type C. Exempt loans are type OR and passed through here
        # untouched — which is how Committee for SAIF Keeping came to show
        # $665,242.33 of cash on hand, 32% of every flagged dollar on the
        # dashboard, for a closed committee whose 2006 statement reads $173.13
        # in, $45.00 spent, $128.13 out.
        #
        # This is the #96 rule applied to the other loan field, and it replaces
        # the narrower "ORESTAR recorded no inflow at all" test that shipped
        # first. That test was built on a measurement that was simply wrong:
        # `Loans Received (exempt)` was reported as non-zero on "three records
        # in the entire dataset", so the field looked useless as a signal. It
        # is non-zero on 39, and — the denominator that actually matters —
        # ORESTAR populates it for 39 of the 54 committee-years where we hold
        # an exempt loan, matching our figure TO THE CENT every time. It never
        # once reports a different amount. Either ORESTAR counted the loan and
        # says so, or it did not count it and reports zero.
        #
        # The split is almost perfectly chronological, which is why the narrow
        # test caught SAIF Keeping and missed the rest:
        #
        #     2006-2012     2 reported, 14 omitted
        #     2014-2026    37 reported,  1 omitted
        #
        # ORESTAR evidently changed practice around 2013. That is NOT encoded
        # as a year cutoff here. A constant would be arbitrary, would fix
        # nothing the reported figure does not already identify, and would
        # mishandle the lone 2018 omission. ORESTAR's own number draws the line
        # exactly where ORESTAR drew it.
        #
        # Measured from a pre-#106 baseline across every committee-year holding
        # an exempt loan, not only the flagged ones: 14 fixed ($853,037), 0
        # broken, 39 left alone because the two figures already agree.
        #
        # Two guards, and the first is why this can be trusted where the
        # earlier attempt could not:
        #
        #   Self-consistency. ORESTAR's lines must add up to ORESTAR's own
        #   stated ending balance. This holds for 25,058 of 25,059
        #   committee-years; the single exception is Yes! Keep Our Groceries
        #   Tax Free 2018, whose summary is $128,000 short of itself and whose
        #   balance only reconciles with the $465,000 exempt loan left IN.
        #   Without this guard that committee breaks by $465,000.
        #
        #   Evidence the record parsed. A summary that FAILED to parse is all
        #   zeros, and all-zero is trivially self-consistent, so require a
        #   non-zero figure elsewhere. One blank scrape and this is all that
        #   stands between a parse failure and deleted transactions.
        #
        # Filtering the frame rather than adjusting a total afterwards is
        # deliberate: the balance, the as-of comparison, the reconciliation and
        # the timeline all derive from _or_for_coh, and #99 was the lesson in
        # what happens when a correction reaches some of those and not others.
        # A row-level filter is exact here precisely because ORESTAR never
        # reports a partial amount — it is all or nothing.
        _or_for_coh = _dated(filer_or)  # All type OR, minus anything undated
        _exempt_dropped: dict[str, float] = {}
        _summary_treatment_pending_years: set[str] = set()
        if (not _or_for_coh.empty and "year" in _or_for_coh.columns
                and "sub_type" in _or_for_coh.columns):
            _uncounted_years: set[int] = set()
            _exempt_years = {
                str(int(year)) for year in _or_for_coh.loc[
                    _or_for_coh["sub_type"] == "Loan Received (Exempt)",
                    "year",
                ].dropna()
            }
            for _yr_s in sorted(_exempt_years):
                _ty = (_name_to_yearly.get(name, {}) or {}).get(_yr_s)
                if (not _summary_cash_fields_match_year(
                        _ty,
                        _digest_for_year(_yr_s),
                        _summary_pair_available,
                    ) or not _loan_fields_trustworthy(_ty)):
                    _summary_treatment_pending_years.add(_yr_s)
                    continue
                if any(_ty.get(_key) is None for _key in (
                    "loans_received_exempt", "loan_payments_exempt",
                )):
                    # Missing is not a reported zero. Older parsers collapsed
                    # the two and could consequently delete a real exempt-loan
                    # transaction from cash-on-hand.
                    _summary_treatment_pending_years.add(_yr_s)
                    continue
                def _amt_of(_k, _row=_ty):
                    return float(_row.get(_k) or 0.0)
                # ORESTAR says it counted an exempt loan that year: leave ours.
                if abs(_amt_of("loans_received_exempt")) > 0.01:
                    continue
                _implied = (_amt_of("beginning_balance") + _amt_of("contributions")
                            + _amt_of("other_receipts") + _amt_of("loans_received_exempt")
                            + _amt_of("balance_adjustments") - _amt_of("expenditures")
                            - _amt_of("other_disbursements") - _amt_of("loan_payments_exempt"))
                if abs(_implied - _amt_of("ending_cash_balance")) > 0.01:
                    continue                      # ORESTAR contradicts itself: not authoritative
                _parsed = (abs(_amt_of("beginning_balance")) + abs(_amt_of("ending_cash_balance"))
                           + abs(_amt_of("expenditures")) + abs(_amt_of("other_disbursements")))
                if _parsed <= 0.0:
                    _summary_treatment_pending_years.add(_yr_s)
                    continue                      # cannot tell a parsed record from a blank one
                _uncounted_years.add(int(_yr_s))
            if _uncounted_years:
                _mask = (
                    (_or_for_coh["sub_type"] == "Loan Received (Exempt)")
                    & (_or_for_coh["year"].isin(_uncounted_years))
                )
                if bool(_mask.any()):
                    for _y, _a in (
                        _or_for_coh[_mask].groupby("year")["amount"].sum().items()
                    ):
                        _exempt_dropped[str(int(_y))] = round(float(_a), 2)
                    _or_for_coh = _or_for_coh[~_mask]
                    log.info(
                        "%s: excluding %s of exempt loans from cash (%s) — "
                        "ORESTAR's own summary reports none for those years",
                        name,
                        f"${sum(_exempt_dropped.values()):,.2f}",
                        ", ".join(sorted(_exempt_dropped)),
                    )

        # Calculate net transactions per year for COH computation
        def _yearly_net(contrib_df, expend_df, or_df, od_df, ba_df):
            """Return {year_str: net_cash_flow} for each year with transactions.

            Balance adjustments carry their own sign already (they are usually
            negative), so they are added rather than subtracted — the same way
            ORESTAR's summary adds its Balance Adjustments line to reach the
            ending balance.
            """
            nets: dict[str, float] = {}
            all_frames = []
            for frame, sign in [(contrib_df, 1), (or_df, 1), (expend_df, -1), (od_df, -1),
                                (ba_df, 1)]:
                if not frame.empty and "year" in frame.columns:
                    yearly = frame.groupby("year")["amount"].sum()
                    for yr, amt in yearly.items():
                        yr_s = str(int(yr))
                        nets[yr_s] = nets.get(yr_s, 0.0) + sign * float(amt)
            return nets

        # Ghost rows for certificate years.
        #
        # During a Certificate of Limited Contributions and Expenditures a
        # committee does not itemize, so money it really handled never appears
        # as a transaction — ORESTAR records it only by opening the next year
        # at a different balance than it closed the last. Friends of Daniel
        # Bunn's certificates for 2013-2015 hid $8,250 that way, later spent
        # through ordinary itemized expenditures, which left our balance at
        # -$8,250 against ORESTAR's $0.00.
        #
        # Each restatement becomes a derived row in the balance-adjustment
        # frame, the one frame every cash consumer reads: yearly nets, the
        # filed-year and capture-bounded views, and the monthly timeline the
        # browser rebuilds cash from. Adjusting yearly_nets alone would fail
        # the timeline reconciliation below and halt aggregation — the rows
        # are what keep every figure consistent by construction.
        #
        # They are never written to the transaction mirror: that store must
        # stay an exact copy of ORESTAR, and the coverage diff would read a
        # synthetic id as a surplus row. Display frames, donor aggregates and
        # transaction counts never see them either.
        _ghost_specs: list[dict] = []
        # Our own net cash movement per physical filer and year, BEFORE any
        # ghost row, so a restatement leaving a certificate year is measured
        # from what our rows already carry (see certificate_restatements for
        # the double count that makes this necessary). Built only for scopes
        # that hold a certificate — about 300 of 7,300.
        _filer_year_nets: dict[str, dict[int, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        if any(_certificates.get(str(_f)) for _f in _name_to_fids.get(name, [])):
            for _frame, _sign in ((_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                                  (_od_for_coh, -1), (_ba_for_coh, 1)):
                if (_frame is None or _frame.empty or "filer id" not in _frame.columns
                        or "year" not in _frame.columns):
                    continue
                for (_ffid, _fyr), _famt in (
                    _frame.groupby(["filer id", "year"])["amount"].sum().items()
                ):
                    _key = str(_ffid).strip()
                    if _key.endswith(".0") and _key[:-2].isdigit():
                        _key = _key[:-2]
                    _filer_year_nets[_key][int(_fyr)] += _sign * float(_famt)
        for _fid in _name_to_fids.get(name, []):
            _fid_certs = _certificates.get(str(_fid))
            if not _fid_certs:
                continue
            if _fid_owners.get(_fid, 0) > 1:
                _ghost_skipped_ambiguous.add(str(_fid))
                continue
            _fid_years = (_yearly_summaries.get(str(_fid)) or {}).get("years") or {}
            for _spec in certificate_restatements(
                _fid_years, _fid_certs, dict(_filer_year_nets.get(str(_fid)) or {}),
            ):
                _ghost_specs.append({**_spec, "filer_id": str(_fid)})
        if _ghost_specs:
            _ghost_rows = []
            for _spec in _ghost_specs:
                _when = pd.Timestamp(_spec["date"])
                _ghost_rows.append({
                    "tran_id": (f"{GHOST_ID_PREFIX}{_spec['filer_id']}-"
                                f"{_spec['boundary'][0]}-{_spec['boundary'][1]}"),
                    "original_id": None,
                    "tran_date": _when,
                    # Stamped with its own date so the filed-year and
                    # capture-bounded views include it: the row stands for
                    # ORESTAR's own restatement, which its summary already
                    # reflects, not for a late filing.
                    "filed_date": _when,
                    "tran_type": "O",
                    "sub_type": GHOST_SUB_TYPE,
                    "contributor_payee": "ORESTAR certificate-period restatement",
                    "amount": float(_spec["amount"]),
                    "year": int(_when.year),
                    "month": _when.to_period("M").strftime("%Y-%m"),
                    "filer id": _spec["filer_id"],
                    filer_col: name,
                    "_undated": False,
                })
            _ba_for_coh = pd.concat(
                [_ba_for_coh, pd.DataFrame(_ghost_rows)], ignore_index=True,
            )
            _ghost_committees += 1
            _ghost_row_count += len(_ghost_specs)
            _ghost_amount += sum(abs(float(s["amount"])) for s in _ghost_specs)
        # Early-era amendment chains. evaluate_target applies ORESTAR's 2007
        # counting only where it reproduces ORESTAR's own summary line to the
        # cent from ORESTAR's own rows, and names every version it moves.
        # The rows join the balance-adjustment frame for the same reason the
        # ghost rows do: every cash consumer reads it, so the timeline, the
        # yearly nets and the capture-bounded views stay in agreement.
        _chains_here: list[dict] = []
        _chain_by_year: dict[str, float] = defaultdict(float)
        _chain_rows = []
        for _fid in _name_to_fids.get(name, []):
            _entries = targets_for_filer(_amendment_chains, str(_fid))
            if not _entries:
                continue
            if _fid_owners.get(_fid, 0) > 1:
                _chain_decisions["ambiguous_scope"] += len(_entries)
                continue
            for _entry in _entries:
                _bucket = _entry["tran_type"]
                _frame = {"C": _c_for_coh, "E": _e_for_coh,
                          "OR": _or_for_coh, "OD": _od_for_coh}[_bucket]
                _held: dict[str, float] = {}
                if (_frame is not None and not _frame.empty
                        and {"filer id", "year", "tran_id", "sub_type"} <= set(_frame.columns)):
                    # Only the sub types that feed this summary line: the
                    # other-receipts frame also carries exempt loans, which
                    # ORESTAR prints on a line of their own.
                    _mine = _frame[
                        (_frame["filer id"].astype(str).str.replace(r"\.0$", "", regex=True)
                         == str(_fid))
                        & (_frame["year"] == int(_entry["year"]))
                        & (_frame["sub_type"].isin(CASH_BUCKETS[_bucket]))
                    ]
                    _held = {
                        str(_t).strip(): round(float(_a), 2)
                        for _t, _a in zip(_mine["tran_id"], _mine["amount"])
                    }
                _summary = (((_yearly_summaries.get(str(_fid)) or {}).get("years") or {})
                            .get(str(_entry["year"])) or {})
                _decision = evaluate_target(_entry, _summary, _held, _bucket)
                _chain_decisions[_decision.get("reason") or "applied"] += 1
                if not _decision["applied"]:
                    continue
                _versions = {
                    _v["tran_id"]: _v for _c in _decision["chains"] for _v in _c["versions"]
                }
                _cash_sign = CASH_SIGN[_bucket]
                for _adj in _decision["adjustments"]:
                    _v = _versions[_adj["tran_id"]]
                    _when = pd.to_datetime(_v.get("tran_date"), format="%m/%d/%Y")
                    _signed = _cash_sign * float(_adj["amount"]) * (
                        1.0 if _adj["effect"] == "add" else -1.0)
                    _chain_rows.append({
                        "tran_id": f"{CHAIN_ID_PREFIX}{_fid}-{_adj['tran_id']}-{_adj['effect']}",
                        "original_id": None,
                        "tran_date": _when,
                        "filed_date": _when,
                        "tran_type": "O",
                        "sub_type": CHAIN_SUB_TYPE,
                        "contributor_payee": _v.get("payee") or "ORESTAR amendment chain",
                        "amount": round(_signed, 2),
                        "year": int(_when.year),
                        "month": _when.to_period("M").strftime("%Y-%m"),
                        "filer id": str(_fid),
                        filer_col: name,
                        "_undated": False,
                    })
                    _chain_by_year[str(int(_when.year))] += round(_signed, 2)
                _chains_here.append({
                    "filer_id": str(_fid),
                    "year": int(_entry["year"]),
                    "bucket": _bucket,
                    "orestar_line": _decision["orestar_line"],
                    "held_total": _decision["held_total"],
                    "cash_effect": round(_cash_sign * _decision["moved"], 2),
                    "adjustments": _decision["adjustments"],
                    "chains": _decision["chains"],
                })
        if _chain_rows:
            _ba_for_coh = pd.concat(
                [_ba_for_coh, pd.DataFrame(_chain_rows)], ignore_index=True,
            )
            _chain_rows_added += len(_chain_rows)

        # Discontinued committees (see orestar_closures.py): ORESTAR's
        # statements after a Discontinuation are blank and open at $0.00, so
        # the last balance vanishes without a transaction. The row is measured
        # against ORESTAR's last real close and our own rows in the blank
        # years, so neither those rows nor an earlier data gap are absorbed.
        _closures_here: list[dict] = []
        _closure_by_year: dict[str, float] = defaultdict(float)
        for _fid in _name_to_fids.get(name, []):
            _disc_date = discontinued_on(_filer_metadata.get(str(_fid)))
            if not _disc_date or _fid_owners.get(_fid, 0) > 1:
                continue
            _nets: dict[int, float] = defaultdict(float)
            for _frame, _sign in ((_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                                  (_od_for_coh, -1), (_ba_for_coh, 1)):
                if (_frame is None or _frame.empty
                        or not {"filer id", "year", "amount"} <= set(_frame.columns)):
                    continue
                _own = _frame[
                    _frame["filer id"].astype(str).str.replace(r"\.0$", "", regex=True)
                    == str(_fid)
                ]
                if "sub_type" in _own.columns:
                    _own = _own[~_own["sub_type"].isin(
                        {GHOST_SUB_TYPE, CHAIN_SUB_TYPE, CLOSURE_SUB_TYPE})]
                for _yr, _amt in _own.groupby("year")["amount"].sum().items():
                    _nets[int(_yr)] += _sign * float(_amt)
            _spec = closure_reset(
                (_yearly_summaries.get(str(_fid)) or {}).get("years") or {},
                (_certificates.get(str(_fid)) or {}).keys(),
                dict(_nets),
                _disc_date,
            )
            if _spec is None:
                continue
            _when = pd.Timestamp(_spec["date"])
            _ba_for_coh = pd.concat([_ba_for_coh, pd.DataFrame([{
                "tran_id": f"{CLOSURE_ID_PREFIX}{_fid}-{_spec['year']}",
                "original_id": None,
                "tran_date": _when,
                "filed_date": _when,
                "tran_type": "O",
                "sub_type": CLOSURE_SUB_TYPE,
                "contributor_payee": "ORESTAR closure restatement",
                "amount": float(_spec["amount"]),
                "year": int(_when.year),
                "month": _when.to_period("M").strftime("%Y-%m"),
                "filer id": str(_fid),
                filer_col: name,
                "_undated": False,
            }])], ignore_index=True)
            _closures_here.append({**_spec, "filer_id": str(_fid)})
            _closure_by_year[str(_spec["year"])] += float(_spec["amount"])
            _closure_resets_applied += 1
            _closure_amount += abs(float(_spec["amount"]))

        # Independent Expenditure Filers: ORESTAR's own page script hides the
        # balance section for filer type "IF", so ORESTAR publishes no cash
        # balance for them. A scope made only of such filers is shown and
        # compared without one. The type comes from ORESTAR's page, never
        # guessed from a filer's transactions.
        _filer_types: list[str | None] = []
        for _fid in _name_to_fids.get(name, []):
            _fyears = (_yearly_summaries.get(str(_fid)) or {}).get("years") or {}
            _typed = [(_y, _r.get("filer_type")) for _y, _r in _fyears.items()
                      if isinstance(_r, dict) and _r.get("filer_type")]
            _filer_types.append(max(_typed)[1] if _typed else None)
        _independent_filer = bool(_filer_types) and all(
            _t == INDEPENDENT_FILER_TYPE for _t in _filer_types)
        if _independent_filer:
            _independent_filer_scopes += 1

        _ghost_by_year: dict[str, float] = defaultdict(float)
        for _spec in _ghost_specs:
            _ghost_by_year[str(_spec["year"])] += float(_spec["amount"])
        _certificate_years_here: dict[str, list[dict]] = {}
        for _fid in _name_to_fids.get(name, []):
            for _cy, _cert in sorted((_certificates.get(str(_fid)) or {}).items()):
                _certificate_years_here.setdefault(str(_cy), []).append(
                    {"filer_id": str(_fid), **_cert}
                )

        _live_originals_here = _live_originals_for(filer_all, _live_originals_evidence)
        _live_originals_by_year: dict[str, list[str]] = defaultdict(list)
        for _lo in _live_originals_here:
            if _lo.get("year") is not None:
                _live_originals_by_year[str(_lo["year"])].append(_lo["original_id"])
        if _live_originals_here:
            _live_original_committees += 1
            _live_original_rows += len(_live_originals_here)
        _deletions_here = [
            _entry
            for _fid in _name_to_fids.get(name, [])
            for _entry in _deletions_by_filer.get(str(_fid), [])
        ]
        if _deletions_here:
            _deletion_committees += 1
            _deletion_rows += len(_deletions_here)

        yearly_nets = _yearly_net(_c_for_coh, _e_for_coh, _or_for_coh, _od_for_coh, _ba_for_coh)

        # The same nets, attributed by FILING year instead of transaction year.
        #
        # ORESTAR's annual statement covers what was FILED in that period, and
        # for most years filing follows activity closely enough that the two
        # bases agree. 2006 is the exception and a large one: ORESTAR launched,
        # committees entered years of accumulated activity, and essentially all
        # of it carries a 2006 transaction date with a 2007 filing date. Every
        # one of Citizens for Mannix's 2006 loans -- $309,000 across five rows
        # -- was filed in January 2007.
        #
        # Measured over the 73 committees that diverge in 2006: attributing by
        # transaction date reconciles NONE of them and leaves $2,872,708;
        # attributing by filing date reconciles 22 exactly and leaves $200,041.
        #
        # This is NOT used as the primary basis, because the same measurement
        # says filing date is worse in most other years -- 2025 goes from $18K
        # to $147K, 2026 from $1.0M to $1.3M. It is computed alongside so a
        # divergence that DISAPPEARS under the filing basis can be recognised
        # for what it is: a period-boundary effect, not missing data.
        # Candidate rules for which rows ORESTAR's 2006 statement counts.
        #
        # Computed HERE, from the same frames and the same sign convention as
        # yearly_nets, because every attempt to reimplement this net in ad-hoc
        # SQL has produced a different number — once by $457,723 on a single
        # committee, and once making a diverging committee appear to reconcile.
        # A candidate basis is only meaningful if it differs from the live
        # figure in the basis alone.
        #
        # 2006 is the only year in question: our figures match ORESTAR from
        # 2007 on, often to within tens of dollars across a hundred committees.
        def _net_2006(row_filter):
            total = 0.0
            for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                              (_od_for_coh, -1), (_ba_for_coh, 1)]:
                if _f.empty or "year" not in _f.columns:
                    continue
                _g = _f[_f["year"] == 2006]
                if _g.empty:
                    continue
                _g = row_filter(_g)
                if _g is None or _g.empty:
                    continue
                total += _sign * float(_g["amount"].sum())
            return round(total, 2)

        def _filed_by(frame, cutoff):
            if "filed_date" not in frame.columns:
                return None
            _fd = pd.to_datetime(frame["filed_date"], errors="coerce")
            return frame[_fd.notna() & (_fd <= pd.Timestamp(cutoff))]

        basis_2006 = {
            "tran_2006": _net_2006(lambda g: g),
            "tran_2006_filed_by_2006": _net_2006(lambda g: _filed_by(g, "2006-12-31")),
            "tran_2006_filed_by_2007_01_31": _net_2006(lambda g: _filed_by(g, "2007-01-31")),
            "tran_2006_filed_by_2007_06_30": _net_2006(lambda g: _filed_by(g, "2007-06-30")),
            "tran_2006_filed_by_2007_12_31": _net_2006(lambda g: _filed_by(g, "2007-12-31")),
        }

        _filed_frames = []
        for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                          (_od_for_coh, -1), (_ba_for_coh, 1)]:
            if not _f.empty and "filed_date" in _f.columns:
                _g = _f.copy()
                _g["_fy"] = pd.to_datetime(_g["filed_date"], errors="coerce").dt.year
                _filed_frames.append((_g.dropna(subset=["_fy"]), _sign))
        yearly_nets_filed: dict[str, float] = {}
        for _g, _sign in _filed_frames:
            for _yr, _amt in _g.groupby("_fy")["amount"].sum().items():
                _k = str(int(_yr))
                yearly_nets_filed[_k] = yearly_nets_filed.get(_k, 0.0) + _sign * float(_amt)

        # The same nets, bounded to what had been FILED when ORESTAR's summary
        # was read.
        #
        # This is NOT another accounting basis. It is the same basis evaluated
        # at the moment ORESTAR's number describes — the only way to compare
        # like for like against a figure that was frozen days ago while filings
        # kept arriving. Measured over the 270 committees diverging in 2026,
        # bounding this way accounts for 89% of the gap and matches 93 of them
        # to the cent; the naive comparison attributes the same money to
        # missing data.
        #
        # `filed_date` carries only day precision, so rows filed ON the capture
        # date genuinely cannot be placed. They are summed separately as an
        # indeterminate band rather than assigned to either side: a residual
        # smaller than its band is not evidence of anything. Guessing here is
        # what produced the "ORESTAR contradicts its own itemised list" reading
        # of committees whose rows had simply been filed that afternoon.
        _capture_day = _capture_local_date(_comparison_by_name.get(name))
        yearly_nets_at_capture: dict[str, float] = {}
        yearly_capture_band: dict[str, float] = {}
        if _capture_day is not None:
            for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                              (_od_for_coh, -1), (_ba_for_coh, 1)]:
                if _f.empty or "filed_date" not in _f.columns or "year" not in _f.columns:
                    continue
                _g = _f.copy()
                _fd = pd.to_datetime(_g["filed_date"], errors="coerce").dt.date
                # A row we cannot date on the filing side cannot be bounded by
                # a filing cutoff either; it stays out of both figures rather
                # than defaulting into the "before" side.
                _known = _fd.notna()
                for _frame, _target in (
                    (_g[_known & (_fd < _capture_day)], yearly_nets_at_capture),
                    (_g[_known & (_fd == _capture_day)], yearly_capture_band),
                ):
                    if _frame.empty:
                        continue
                    for _yr, _amt in _frame.groupby("year")["amount"].sum().items():
                        _k = str(int(_yr))
                        _target[_k] = _target.get(_k, 0.0) + _sign * float(_amt)

        # Determine beginning balances and cash-on-hand
        # Strategy: use the earliest-year beginning balance scraped directly from
        # ORESTAR (via Playwright), then roll forward through yearly transaction nets.
        # This avoids error-prone back-calculation from the current year.
        orestar_info = _orestar_data.get(name)

        # Count loan principal as cash only where ORESTAR counts it.
        #
        # A loan received is not automatically the committee's money. Ted
        # Wheeler's 2006 statement records $233,000 of loans and reports
        # Loans Received $0.00 in the cash section, Total Outstanding Loans
        # $230,000 under Financial Status, and an ending cash balance of
        # $1,819.65 — against $107.90 of cash expenditures that year. Had that
        # $233,000 been cash, it would still have been sitting there. It never
        # entered the account; the candidate's spending was recorded as a loan
        # to the committee.
        #
        # We added it anyway, so his balance ran $233,000 high — and kept
        # running high forever, because ORESTAR carries its own (loan-free)
        # ending forward every year. Measured today: our $234,530.14 against
        # ORESTAR's $1,530.14, a gap equal to the 2006 loans to the cent.
        #
        # The rule is NOT "ignore loans". In most years ORESTAR does count them
        # and the money genuinely arrives: Wheeler's 2010, 2011, 2020 and 2021
        # loans all appear in ORESTAR's own figures and match our rows exactly.
        # Excluding loans wholesale was measured across all 46,761
        # committee-years and breaks 2,581 of them, taking the residual from
        # $3.55M to $24.36M.
        #
        # So ORESTAR's own reported figure decides, per committee-year. It
        # agrees with our transaction rows 738 times and disagrees 123, almost
        # all of them in 2006 (65, $2,228,906).
        #
        # Guarded on parser freshness and, independently, on an exact paired
        # scope digest. Before the #90 parser fix this field read $0.00 on every
        # record; after it, a correctly parsed weekly figure can still be stale
        # once daily transaction collection advances. In either case our raw
        # current transaction figure stands.
        _sum_ts = float((orestar_info or {}).get("ts") or 0)
        _our_loans_by_year = {}
        if not _c_for_coh.empty and "year" in _c_for_coh.columns:
            _lr = _c_for_coh[
                _c_for_coh["sub_type"] == "Loan Received (Non-Exempt)"
            ]
            if not _lr.empty:
                _our_loans_by_year = _lr.groupby("year")["amount"].sum().to_dict()
        _their_years = _name_to_yearly.get(name, {})
        for _yr, _ours_value in _our_loans_by_year.items():
            _yr_s = str(int(_yr))
            _ty = _their_years.get(_yr_s)
            if (not _summary_cash_fields_match_year(
                    _ty,
                    _digest_for_year(_yr_s),
                    _summary_pair_available,
                ) or not _loan_fields_trustworthy(_ty, _sum_ts)):
                _summary_treatment_pending_years.add(_yr_s)
                continue
            _theirs = _ty.get("loans_received")
            if _theirs is None:
                _summary_treatment_pending_years.add(_yr_s)
                continue
            try:
                _ours = float(_ours_value)
            except (TypeError, ValueError):
                continue
            _theirs = float(_theirs)
            # ORESTAR may report a loan in a year for which we hold no loan
            # row. That is evidence of a missing transaction, not permission to
            # synthesize undated cash. Normalize only principal represented by
            # held rows; exact remediation can add a genuinely missing row.
            if abs(_ours) <= 1e-9:
                continue
            _adj = round(_theirs - _ours, 2)
            if abs(_adj) > 0.0:
                yearly_nets[_yr_s] = round(yearly_nets[_yr_s] + _adj, 2)

        beginning_balances: dict[str, float] = {}
        cash_on_hand_source = "calculated"   # always — see the check below
        has_orestar_summary = False
        orestar_live_difference = None
        orestar_year = 0

        sorted_years = sorted(yearly_nets.keys())

        # Look up earliest beginning balance from the Playwright-scraped cache.
        # Only use it if the scraped year is at or before the first year with
        # transactions. If the scraped "earliest year" is AFTER our first
        # transaction year, the scraper returned the current-year balance
        # (not the actual earliest), so the beginning balance should be $0.
        # The anchor is the "Beginning Balance (Previous Year)" from the FIRST
        # account statement ORESTAR holds for this committee. Read from the
        # right page it needs no special-casing by year: a committee that
        # predates ORESTAR shows the real money it walked in with, and one
        # formed later shows $0, because there was nothing before.
        #
        # So the only question is whether the scraper actually got to that
        # page. `reached_earliest` answers it. Without that flag the two
        # outcomes were indistinguishable, and a timed-out click banked
        # whatever middle year it stopped on as the committee's opening
        # balance — filer 142 recorded $220,614.68 from 2024 for a committee
        # filing since 2006.
        earliest_info = _name_to_earliest.get(name)
        first_txn_year = int(sorted_years[0]) if sorted_years else 9999
        first_year_begin = 0.0
        _first_balance_year: int | None = None
        # Whether our pre-statement transactions agree with the opening balance
        # ORESTAR states. None when the question does not arise.
        opening_reconciles = None
        opening_our_prior_net = None
        _balance_excluded_years: set[int] = set()
        if earliest_info:
            scraped_year = earliest_info.get("earliest_year", 9999)
            # Older cache entries predate the flag; fall back to the year test,
            # which catches the same failure whenever transactions run earlier.
            complete = earliest_info.get("reached_earliest", scraped_year <= first_txn_year)
            if complete and scraped_year <= first_txn_year:
                first_year_begin = earliest_info["beginning_balance"]
                _first_balance_year = int(scraped_year)
            elif (complete and scraped_year > first_txn_year
                  and earliest_info.get("beginning_balance") is not None):
                # ORESTAR's first statement postdates our first transaction.
                #
                # Both figures describe the same money and they disagree, so
                # one of them has to be chosen rather than quietly averaged by
                # accident. ORESTAR's is the bank position it certified at the
                # time; ours is whatever pre-statement rows happen to be in the
                # transaction record, and those are systematically partial —
                # ORESTAR's transaction search returns back-dated entries filed
                # later, but its account summaries only begin in 2006, so the
                # early rows are contributions whose matching expenditures were
                # never filed as transactions.
                #
                # Measured on all five committees in this position, our
                # pre-statement net exceeds ORESTAR's stated opening in four:
                # Citizens for Mannix holds $150,000 of 2003-04 contributions
                # and NO expenditures against a stated opening of $203.84. We
                # were carrying $149,796 of money that had already been spent.
                #
                # So: take ORESTAR's figure as the anchor and drop the
                # pre-statement rows it already accounts for, rather than
                # trusting a record we can prove is incomplete.
                _pre = round(sum(yearly_nets.get(str(y), 0.0)
                                 for y in range(first_txn_year, scraped_year)), 2)
                first_year_begin = earliest_info["beginning_balance"]
                _first_balance_year = int(scraped_year)
                opening_reconciles = abs(_pre - first_year_begin) <= 0.01
                opening_our_prior_net = _pre
                for _y in range(first_txn_year, scraped_year):
                    yearly_nets.pop(str(_y), None)
                    _balance_excluded_years.add(_y)
                    # The opening supersedes these years' cash, so missing
                    # annual loan fields cannot affect the current balance.
                    _summary_treatment_pending_years.discard(str(_y))
                log.info(
                    "%s: anchoring on ORESTAR's %d opening $%.2f; our %d-%d rows "
                    "net $%.2f (%s) and are superseded by it",
                    name, scraped_year, first_year_begin, first_txn_year,
                    scraped_year - 1, _pre,
                    "reconciles" if opening_reconciles else "does NOT reconcile",
                )
            elif earliest_info.get("beginning_balance"):
                log.warning(
                    "Ignoring $%.2f opening balance for %s: paging stopped at %d but "
                    "transactions start %d — not the first statement",
                    earliest_info["beginning_balance"], name, scraped_year, first_txn_year,
                )

        # The late-anchor branch can remove years from yearly_nets. Rebuild the
        # iteration list after that mutation; retaining the old list attached a
        # 2026 official opening balance to 2025 merely because a superseded row
        # was still visible there.
        sorted_years = sorted(yearly_nets.keys())
        if (_first_balance_year is not None
                and (not sorted_years
                     or _first_balance_year < int(sorted_years[0]))):
            beginning_balances[str(_first_balance_year)] = round(
                first_year_begin, 2
            )

        # Roll forward: beginning balance for each year, then cash on hand
        running = first_year_begin
        for yr_s in sorted_years:
            beginning_balances[yr_s] = round(running, 2)
            running += yearly_nets.get(yr_s, 0.0)
        cash_on_hand = round(running, 2)

        # --- signature of the per-year deltas -------------------------------
        # Computed after the yearly loop below fills yearly_discrepancies;
        # declared here so the record above always has a value.
        discrepancy_signature = None
        discrepancy_first_bad_year = None
        # Compare against ORESTAR-reported ending balance for validation.
        #
        # This is a CHECK, never an input: cash_on_hand above is calculated
        # from transactions and nothing here changes it. The flag used to be
        # set to "orestar" at this point, which read as though the figure came
        # from ORESTAR — it never did.
        is_closed = False
        closed_since = None
        closed_final_balance = None
        if orestar_info and orestar_info.get("year", 0) > 0:
            orestar_year = orestar_info["year"]
            orestar_ending = orestar_info.get("ending_cash_balance", 0.0)
            has_orestar_summary = True

            # Anchor on the last year ORESTAR reports ACTIVITY in, not merely
            # the last year it issued a statement.
            #
            # ORESTAR keeps producing an annual Account Summary after a
            # committee stops operating, and those trailing statements are
            # entirely zero — every line, including the ending balance. The
            # cash-balances file stores the LATEST year, so for a wound-down
            # committee the check was comparing our full-history balance
            # against a blank form.
            #
            # Citizens for Mannix is the clearest case. Its real history is all
            # there in ORESTAR:
            #
            #     2006  ending $568.75    contributions $186,050
            #     2007  ending $1,560.47  contributions $205,940
            #     2008  ending $106.66    contributions $645,535
            #     2009  ending $2,600.66  contributions $56,941
            #     2010  ending $0.00      contributions $12,451   <- wound down
            #     2011-2015                                        <- empty stubs
            #
            # We anchored on 2015 and recorded the committee as $299,796 adrift.
            # 253 committees carry $1.66M of "discrepancy" for this reason, and
            # a re-scrape confirmed the zeros are real rather than a scraping
            # gap — the statements genuinely are blank, so the fix belongs here
            # rather than in the scraper.
            #
            # A year that ends at zero after real activity is a perfectly good
            # anchor: that IS the committee's closing balance. Only years with
            # no activity at all are skipped.
            _all_years = _name_to_yearly.get(name, {})
            if _all_years:
                _live = sorted((int(y) for y, v in _all_years.items()
                                if str(y).isdigit() and _summary_has_activity(v)))
                if _live and _live[-1] < orestar_year:
                    _anchor = _live[-1]
                    log.debug(
                        "%s: ORESTAR's %d statement is blank — anchoring on %d instead",
                        name, orestar_year, _anchor,
                    )
                    orestar_year = _anchor
                    orestar_ending = float(
                        _all_years[str(_anchor)].get("ending_cash_balance") or 0.0
                    )

            # Closed: ORESTAR's most recent statements are entirely blank —
            # no activity AND no cash balance.
            #
            # This is the strongest signal the record offers that a committee
            # is finished. It is deliberately NOT the same as dormant: a
            # dormant committee has ORESTAR carrying a real balance forward
            # year after year (Oregon Strong, $889,626.78), which is a
            # committee holding money and filing nothing. A blank statement
            # says the opposite — nothing held, nothing moved.
            #
            # Requiring the cash balance to be zero is what separates them,
            # and it is why "no contributions or expenditures this year" is not
            # sufficient on its own.
            # Closure is a physical-filer fact. The year-wise aggregate above
            # is lossy for multi-ID profiles: one component's newer blank page
            # can sit after another component's still-live last statement and
            # make the union look closed. Require independent trailing-blank
            # evidence for every component before suppressing the aggregate
            # balance difference.
            _closures = [
                _trailing_blank_closure(
                    (_yearly_summaries.get(str(_fid)) or {}).get("years", {})
                )
                for _fid in _scope_fids
            ]
            if (_summary_scope_current
                    and _closures and len(_closures) == len(_scope_fids)
                    and all(item[0] for item in _closures)):
                is_closed = True
                closed_since = max(int(item[1]) for item in _closures)
                closed_final_balance = round(
                    sum(float(item[2] or 0.0) for item in _closures), 2
                )
            # A dormant committee still gets a statement: ORESTAR carries its
            # balance forward every year whether or not anything moves. We have
            # no transactions for those years, so looking the year up in our
            # own tables returned 0 and the committee appeared to be off by its
            # entire balance. Oregon Strong sat at $889,626.78 on both sides
            # and was recorded as $889,626.78 adrift; 1,447 committees — 41% of
            # everything flagged — were wrong for exactly this reason.
            #
            # When we have no data for ORESTAR's year, our balance at the end of
            # it is simply the balance we last rolled forward to: cash_on_hand.
            if str(orestar_year) in beginning_balances:
                our_anchor_ending = round(
                    beginning_balances[str(orestar_year)]
                    + yearly_nets.get(str(orestar_year), 0.0), 2
                )
            else:
                our_anchor_ending = cash_on_hand
            orestar_live_difference = round(our_anchor_ending - orestar_ending, 2)

        # The only authoritative all-time comparison is the app balance saved
        # alongside the ORESTAR page.  Reconstructing a past app state from
        # filed_date is impossible: late historical backfills and same-day
        # filings both carry dates unrelated to when we collected them.
        if _comparison.get("status") == "paired":
            _comparison["current_app_cash_on_hand"] = cash_on_hand
            _comparison["app_balance_change_since_capture"] = round(
                cash_on_hand - float(_comparison["app_cash_on_hand"]), 2
            )
            _comparison["current_tran_count"] = tran_count
            _comparison["tran_count_change_since_capture"] = (
                tran_count - int(_comparison.get("app_tran_count") or 0)
            )
            # The global transaction fingerprint changes when any committee
            # receives a row.  For user-facing freshness, scope-level movement
            # is the useful signal; otherwise one filing would label all 7,000
            # paired committees as changed.
            _comparison["app_data_changed_since_capture"] = bool(
                _comparison.get("scope_digest_matches_capture") is not True
                or abs(_comparison["app_balance_change_since_capture"]) > 0.01
                or _comparison["tran_count_change_since_capture"] != 0
            )
            _comparison["annual_summary_refresh_needed"] = bool(
                _summary_treatment_pending_years
            )
            _comparison["annual_summary_refresh_years"] = sorted(
                _summary_treatment_pending_years
            )
            _comparison["actionable"] = bool(
                not _comparison["app_data_changed_since_capture"]
                and not _comparison.get("orestar_data_changed_since_capture")
                and not _summary_treatment_pending_years
                and not is_closed
                and not _independent_filer
            )
            if _independent_filer:
                _comparison["actionability_reason"] = "no_published_balance"
            elif is_closed:
                _comparison["actionability_reason"] = "closed_trailing_summary"
            elif _comparison.get("orestar_data_changed_since_capture"):
                _comparison["actionability_reason"] = "newer_summary_was_not_paired"
            elif _comparison["app_data_changed_since_capture"]:
                _comparison["actionability_reason"] = "app_state_changed_since_capture"
            elif _summary_treatment_pending_years:
                _comparison["actionability_reason"] = "annual_summary_years_unpaired"

        # Per-year comparison context: retain raw rolled balances and line
        # items, but mark only same-year activity/movement as current when its
        # annual page shares the exact year digest. Rolled begin/end positions
        # also depend on every prior year and remain non-diagnostic here.
        orestar_yearly = _name_to_yearly.get(name, {})
        yearly_discrepancies: dict[str, dict] = {}

        # Pre-compute per-year sums from our filtered transaction frames
        def _yearly_sums(frame):
            if frame.empty or "year" not in frame.columns:
                return {}
            return frame.groupby("year")["amount"].sum().to_dict()

        # For ORESTAR comparison: Cash Contribution + In-Kind, Cash
        # Expenditure + In-Kind. These are reconciliation consumers, so use the
        # same dated/certified-absence filters as the balance rather than
        # publishing line items that contradict the corrected cash figure.
        _yearly_orestar_c = _yearly_sums(_dated(_orestar_contrib))
        _yearly_cash_exp = _yearly_sums(_dated(_cash_expend_only))
        _yearly_inkind = _yearly_sums(_dated(filer_inkind))
        # Loan receipts per year, so a committee-year can be checked against
        # ORESTAR's own reported loans line.
        _loans_recv = (
            _c_for_coh[
                _c_for_coh["sub_type"] == "Loan Received (Non-Exempt)"
            ] if not _c_for_coh.empty else _c_for_coh
        )
        _yearly_loans = _yearly_sums(_loans_recv)

        # Per-year loan scaling, shared by the balance, the timeline and this
        # comparison so all three treat loan principal identically.
        _coh_loan_scale: dict[int, float] = {}
        _their_yrs_cmp = _name_to_yearly.get(name, {})
        for _y, _amt in (_yearly_loans or {}).items():
            _ty = _their_yrs_cmp.get(str(int(_y)))
            if (not _summary_cash_fields_match_year(
                    _ty,
                    _digest_for_year(_y),
                    _summary_pair_available,
                ) or _ty.get("loans_received") is None
                    or not _loan_fields_trustworthy(_ty, _sum_ts)):
                continue
            _amt = float(_amt)
            if abs(_amt) <= 1e-9:
                continue
            _coh_loan_scale[int(_y)] = float(_ty["loans_received"]) / _amt

        def _loan_scale_for_year(_y):
            try:
                return _coh_loan_scale.get(int(_y), 1.0)
            except (TypeError, ValueError):
                return 1.0

        _yearly_or = _yearly_sums(_or_for_coh)
        _yearly_od = _yearly_sums(_od_for_coh)

        if orestar_yearly:
            for yr_s in sorted_years:
                yr_orestar = orestar_yearly.get(yr_s, {})
                if not yr_orestar:
                    continue

                yr_int = int(yr_s) if yr_s.isdigit() else None
                _year_pair_current = _summary_cash_fields_match_year(
                    yr_orestar,
                    _digest_for_year(yr_s),
                    _summary_pair_available,
                )
                our_begin = beginning_balances.get(yr_s, 0.0)
                # Contributions on the same basis as the balance and the stat
                # card: loan principal counted the way ORESTAR counts it.
                #
                # _orestar_contrib includes loans received, so this line was
                # comparing our raw loan figure against ORESTAR's reported one
                # while every other line — and the balance itself — already used
                # theirs. Wheeler's 2006 read ours $235,500 against ORESTAR
                # $2,500, the difference being exactly his ten loan rows, on a
                # year where the ending balance matched to the cent.
                #
                # It also left the page disagreeing with itself: since #99 the
                # timeline and the Contributions card carry the adjusted figure,
                # so the row below the card contradicted it.
                our_c  = round(float(_yearly_orestar_c.get(yr_int, 0))
                               - float(_yearly_loans.get(yr_int, 0))
                               + float(_yearly_loans.get(yr_int, 0)) * _loan_scale_for_year(yr_int), 2)
                our_e  = round(float(_yearly_cash_exp.get(yr_int, 0)) + float(_yearly_inkind.get(yr_int, 0)), 2)
                our_or = round(float(_yearly_or.get(yr_int, 0)), 2)
                our_od = round(float(_yearly_od.get(yr_int, 0)), 2)
                # Transaction basis, for every year including 2006.
                #
                # 2006 was briefly switched to the FILING basis, because the
                # committees that diverge there reconcile under it: Citizens for
                # Mannix's whole 2006 loan book — $309,000 across five rows —
                # carries 2006 transaction dates and 2007-01 filing dates, and
                # ORESTAR's 2006 statement reports only the $159,000 filed on
                # the 29th.
                #
                # That switch was reverted. It was measured only on the 74
                # committee-years ALREADY diverging, where it looked like a drop
                # from $3.18M to $205K. Applied to everyone it took 2006 from 73
                # affected committees to 513 — 426 of them newly wrong by under
                # $5,000 each — because ORESTAR's 2006 statement is not uniformly
                # filing-based. Committees that filed contemporaneously agree on
                # both bases; late filers follow the filing; and committees with
                # 2005 activity filed in 2006 are broken BY the filing basis.
                #
                # Trading $690K of concentrated, explainable error for small
                # errors across 440 additional committees is the worse deal, and
                # it is still not what ORESTAR does. The rule ORESTAR actually
                # applied in its first year is not yet known; see
                # audit_2006_basis.py, which measures it per committee instead
                # of assuming it.
                our_net = yearly_nets.get(yr_s, 0.0)
                our_end = round(our_begin + our_net, 2)

                orestar_end = yr_orestar.get("ending_cash_balance")
                orestar_c   = yr_orestar.get("contributions")
                orestar_e   = yr_orestar.get("expenditures")
                orestar_or  = yr_orestar.get("other_receipts")
                orestar_od  = yr_orestar.get("other_disbursements")
                orestar_beg = yr_orestar.get("beginning_balance")

                delta_c   = round(our_c  - (orestar_c or 0), 2) if orestar_c is not None else None
                delta_e   = round(our_e  - (orestar_e or 0), 2) if orestar_e is not None else None
                delta_or  = round(our_or - (orestar_or or 0), 2) if orestar_or is not None else None
                delta_od  = round(our_od - (orestar_od or 0), 2) if orestar_od is not None else None
                delta_end = round(our_end - (orestar_end or 0), 2) if orestar_end is not None else None
                delta_beg = round(our_begin - (orestar_beg or 0), 2) if orestar_beg is not None else None

                # How much ORESTAR says the balance MOVED this year, against
                # how much our transactions say it moved.
                #
                # delta_end below cannot answer "is this year's data right?",
                # because our_end is built on OUR rolled-forward beginning
                # balance: one bad year in 2008 makes every year after it look
                # wrong, and the real culprit is indistinguishable from its
                # eighteen innocent successors.
                #
                # ORESTAR states a beginning AND an ending balance for every
                # year, so the difference between them is that year's movement
                # as the audited record has it — with no dependence on anything
                # earlier. Comparing our own net against it isolates the single
                # year, which is what makes it possible to say WHERE our
                # transaction history diverges rather than merely that it does.
                #
                # Both sides include balance adjustments: yearly_nets is built
                # with filer_ba, and ORESTAR's ending reflects adjustments too,
                # so the two definitions match.
                orestar_movement = (
                    round((orestar_end or 0) - (orestar_beg or 0), 2)
                    if orestar_end is not None and orestar_beg is not None else None
                )
                delta_movement = (
                    round(round(our_net, 2) - orestar_movement, 2)
                    if orestar_movement is not None else None
                )

                # Does this divergence disappear if the year is read the way
                # ORESTAR's statement is assembled — by what was FILED in it?
                #
                # If so it is a period-boundary effect, not missing data: the
                # transactions exist on both sides, they simply fall on
                # different sides of a year line. Marking that is what stops
                # 2006 being re-investigated as a data gap, which has now
                # happened three times, each with a different wrong
                # explanation attached (pre-ORESTAR boundary, loan treatment,
                # a parse bug).
                our_net_filed = round(yearly_nets_filed.get(yr_s, 0.0), 2)
                delta_movement_filed = (
                    round(our_net_filed - orestar_movement, 2)
                    if orestar_movement is not None else None
                )
                # A past per-year app state cannot be reconstructed from
                # filed_date.  Keep the legacy fields for schema compatibility,
                # but leave them unknown until per-year values are captured as
                # part of the paired snapshot too.
                our_net_asof = None
                delta_movement_asof = None
                snapshot_lag = False

                # What our side summed when ORESTAR's summary was read, and how
                # much of the answer the same-day filings could still move.
                # Recorded, never subtracted: `delta_movement` above stays the
                # headline and nothing is removed from the discrepancy total.
                # The point is to make a timing gap legible as timing, not to
                # excuse it — the fix for a real one is a fresher capture.
                our_net_at_capture = (
                    round(yearly_nets_at_capture.get(yr_s, 0.0), 2)
                    if _capture_day is not None else None
                )
                delta_movement_at_capture = (
                    round(our_net_at_capture - orestar_movement, 2)
                    if our_net_at_capture is not None
                    and orestar_movement is not None else None
                )
                capture_band = (
                    round(abs(yearly_capture_band.get(yr_s, 0.0)), 2)
                    if _capture_day is not None else None
                )
                # True only when bounding to the capture leaves a residual that
                # the indeterminate band cannot account for. That is the
                # honest reading of "this gap is not explained by when we
                # looked" — and it is deliberately the claim that needs
                # evidence, rather than its opposite.
                survives_capture_bound = (
                    None if delta_movement_at_capture is None
                    else abs(delta_movement_at_capture) > (capture_band or 0.0) + 0.005
                )

                # Deliberately NOT flagging "this would reconcile on another
                # basis" as an explanation.
                #
                # That is a suppression mechanism wearing a classifier's clothes:
                # it leaves the calculation disagreeing with ORESTAR and hides
                # the difference behind a tolerance. Where another basis is
                # demonstrably the right one — 2006 — the fix is to compute on
                # it, which is what happens above. Where it is not, the
                # difference is real and belongs in the total.
                # ORESTAR contradicting its own summary.
                #
                # Our cash-balance formula IS ORESTAR's, read off the page:
                #   Beginning + Total Contributions + Other Receipts
                #   + Loans Received (exempt) - Total Expenditures
                #   - Other Disbursements - Loan Payments (exempt)
                #   + Balance Adjustments = Ending Cash Balance
                # with Total Contributions including non-exempt loans and
                # in-kind, and in-kind cancelling against the expenditure side.
                # That is why 2007-2025 reconcile to within tens of dollars
                # across thousands of committee-years.
                #
                # Where it does not reconcile, the inconsistency is ORESTAR's.
                # Ted Wheeler's 2006 summary reports Loans Received $0.00 while
                # the SAME page reports Total Outstanding Loans $230,000.00,
                # against $233,000 of loan transactions ORESTAR itself lists.
                # Its own numbers disagree with each other, so no calculation
                # can match all of them at once — mirroring the loans line
                # reconciles 66 committee-years and breaks 44 whose movement
                # follows OUR figure instead.
                #
                # Recorded rather than resolved, and surfaced in the tooltip, so
                # a reader sees that the difference is in the source record.
                our_loans_received = round(float(_yearly_loans.get(yr_int, 0)), 2)
                orestar_loans_received = (float(yr_orestar.get("loans_received"))
                                          if yr_orestar.get("loans_received") is not None else None)
                loan_gap = (round(our_loans_received - orestar_loans_received, 2)
                            if orestar_loans_received is not None else None)
                orestar_omits_loans = (
                    bool(
                        loan_gap is not None and abs(loan_gap) > 1.0
                        and delta_movement is not None
                        and abs(abs(loan_gap) - abs(delta_movement))
                        <= max(1.0, abs(loan_gap) * 0.05)
                    )
                    if _year_pair_current else None
                )

                attribution_artifact = False

                # EVERY year ORESTAR gives us a figure for, not only the ones
                # that disagree.
                #
                # Storing only mismatches made the table impossible to read as
                # evidence: Friends of Ted Wheeler showed a single 2006 row out
                # of 21 comparable years, which looks like one broken year
                # rather than twenty reconciled ones and one outstanding. The
                # years that agree are the reassurance — they are what says the
                # calculation is sound where it is not being questioned.
                # Same-year provenance certifies annual activity, not the
                # rolled opening/ending position, which also depends on every
                # prior year and on the separately scraped opening anchor.
                deltas = [
                    d for d in (
                        delta_c, delta_e, delta_or, delta_od, delta_movement,
                    ) if d is not None
                ]
                reconciles = (
                    not any(abs(d) > 0.01 for d in deltas)
                    if _year_pair_current else None
                )
                # Completeness is PERISHABLE, and this treated it as permanent.
                #
                # The survey asks ORESTAR how many records it holds on a given
                # day. An active committee files constantly, so "complete on
                # 13 August" says nothing about a summary captured on 26
                # August. Friends of Christine Drazan was certified complete at
                # 4,062 rows, and by the time its summary was re-scraped
                # ORESTAR held 4,294 for 2026 against our 4,249 — 45 rows
                # short. The note nevertheless told readers that ORESTAR's
                # summary disagreed with its own transactions, over $243,465,
                # when the truth was simply that we were behind.
                #
                # 213 of the 220 committees carrying that note, worth $491,534,
                # had a certificate older than the summary being compared. So
                # the certificate only counts when it is at least as recent as
                # the summary. This is the same failure #113 fixed for stale
                # SNAPSHOTS, in the other direction: there we compared against
                # an old summary, here we vouch with an old survey.
                #
                # It costs real coverage — Ferrioli's 2006 gap is verified and
                # loses its note because nobody re-surveyed it — and that is
                # the right trade. A silent row is recoverable; a confident
                # wrong attribution is not. Re-running the survey restores
                # every note it withdraws, with evidence behind it.
                # The summary and transaction totals above cover every filer ID
                # behind this canonical name, so completeness must cover that
                # same set. One representative committee cannot certify an
                # aggregate containing several committees.
                _year_summary_ts = float(yr_orestar.get("scrape_ts") or _sum_ts)
                _rows_complete_here = (
                    _aggregate_row_verdict(
                        _name_to_fids.get(name, []),
                        _year_summary_ts,
                        transaction_id=_transaction_snapshot_id,
                    )
                    if _year_pair_current else None
                )
                # A line-item claim also needs this annual page paired to the
                # exact rows currently assigned to its year. Filing dates and
                # a fresh page for some other year cannot prove that match.
                if True:
                    yearly_discrepancies[yr_s] = {
                        # Raw source/app values remain useful audit context, but
                        # no agreement, mismatch, or causal diagnosis is current
                        # unless this annual page shares the exact year digest.
                        "comparison_current": _year_pair_current,
                        # True when every line item agrees, so the UI can show
                        # a reconciled year as confirmation rather than styling
                        # it as a small problem.
                        "reconciles": reconciles,
                        # Certificate years: ORESTAR's own year totals omit the
                        # money that moved unitemized, so a ghost row dated in
                        # this year shows up here as a difference ORESTAR itself
                        # only records in a later opening balance. Carried so
                        # the UI can label it as a restatement, not an error.
                        "certificate_year": yr_s in _certificate_years_here,
                        "certificate_restatement": (
                            round(_ghost_by_year[yr_s], 2)
                            if yr_s in _ghost_by_year else None
                        ),
                        # ORESTAR's drop to $0.00 after a discontinuation.
                        "closure_reset": (
                            round(_closure_by_year[yr_s], 2)
                            if yr_s in _closure_by_year else None
                        ),
                        # Cash moved onto ORESTAR's line by early-era
                        # amendment-chain versions it still counts.
                        "amendment_chain_adjustment": (
                            round(_chain_by_year[yr_s], 2)
                            if yr_s in _chain_by_year else None
                        ),
                        # Amended originals ORESTAR still counts in this year,
                        # by transaction ID, so the row can name them.
                        "live_originals": (
                            sorted(_live_originals_by_year[yr_s],
                                   key=lambda v: (len(v), v))
                            if yr_s in _live_originals_by_year else None
                        ),
                        "our_begin": our_begin,
                        "our_contributions": our_c,
                        "our_expenditures": our_e,
                        "our_other_receipts": our_or,
                        "our_other_disbursements": our_od,
                        "our_net": round(our_net, 2),
                        "our_end": our_end,
                        "orestar_begin": round(orestar_beg, 2) if orestar_beg is not None else None,
                        "orestar_contributions": round(orestar_c, 2) if orestar_c is not None else None,
                        "orestar_expenditures": round(orestar_e, 2) if orestar_e is not None else None,
                        "orestar_other_receipts": round(orestar_or, 2) if orestar_or is not None else None,
                        "orestar_other_disbursements": round(orestar_od, 2) if orestar_od is not None else None,
                        "orestar_end": round(orestar_end, 2) if orestar_end is not None else None,
                        "delta_contributions": delta_c,
                        "delta_expenditures": delta_e,
                        "delta_other_receipts": delta_or,
                        "delta_other_disbursements": delta_od,
                        "delta_begin": delta_beg,
                        # The drift-free pair: what ORESTAR says this year moved,
                        # and how far our transactions are from that. Unlike
                        # `discrepancy`, these do not inherit earlier years' error.
                        "orestar_movement": orestar_movement,
                        "delta_movement": delta_movement,
                        # Same year read on ORESTAR's own basis. When this
                        # reconciles and delta_movement does not, the year is a
                        # filing-period boundary artifact rather than a gap.
                        "our_net_filed": our_net_filed,
                        "delta_movement_filed": delta_movement_filed,
                        "attribution_artifact": attribution_artifact,
                        # Our side as of the summary's capture. When this
                        # reconciles and the live comparison does not, the
                        # difference is the reporting window.
                        "our_loans_received": our_loans_received,
                        "orestar_loans_received": orestar_loans_received,
                        "orestar_omits_loans": orestar_omits_loans,
                        "our_net_asof": our_net_asof,
                        "delta_movement_asof": delta_movement_asof,
                        # Capture-bounded view. See the note where these are
                        # computed: diagnostic only, nothing is suppressed.
                        "our_net_at_capture": our_net_at_capture,
                        "delta_movement_at_capture": delta_movement_at_capture,
                        "capture_indeterminate_band": capture_band,
                        "survives_capture_bound": survives_capture_bound,
                        "capture_local_date": (
                            _capture_day.isoformat() if _capture_day else None
                        ),
                        "snapshot_lag": snapshot_lag,
                        # ORESTAR's summary against ORESTAR's OWN itemised
                        # transactions, where we know our row set equals its.
                        #
                        # Friends of Ted Ferrioli 2006 is the case this exists
                        # for. ORESTAR's summary reports $213,961.90 of
                        # expenditures and $204,049.26 of contributions; the
                        # 161 transactions ORESTAR itemises for that year total
                        # $173,367.64 and $203,155.00. Verified against ORESTAR
                        # directly, not inferred: a date-bounded record count
                        # (161 = 161, where the survey's all-years figure could
                        # not have settled it), a narrow search finding exactly
                        # ONE $40,000 row, a fresh read of the summary, and a
                        # full sum paged off ORESTAR's own results screens.
                        # Net -40,000 + 300 = -39,700, which then propagates as
                        # an opening-balance gap for twelve further years.
                        #
                        # We follow the itemised record, which our figures
                        # match to the cent. Matching the summary instead would
                        # mean synthesising a second $40,000 row — disagreeing
                        # with the record we currently match exactly, and
                        # corrupting every donor and payee total on the site to
                        # repair one balance.
                        #
                        # Gated on known completeness. Where the survey has not
                        # measured a committee this stays None and the site
                        # says nothing, because a line difference looks the
                        # same whether ORESTAR over-reports or we under-hold.
                        "rows_complete": _rows_complete_here,
                        "summary_vs_itemised": (
                            None if not _rows_complete_here or orestar_c is None
                                    or orestar_e is None else
                            round((orestar_c - our_c) + ((orestar_or or 0) - our_or)
                                  - (orestar_e - our_e)
                                  - ((orestar_od or 0) - our_od), 2)),
                        "discrepancy": delta_end,
                    }

        # Accumulate global beginning balances (sum across all filers per year).
        # Independent Expenditure Filers hold no published balance, so they
        # contribute none to the statewide figure (see the cash timeline below).
        for yr_s, bal in (beginning_balances.items() if not _independent_filer else ()):
            _global_beginning_balances[yr_s] = round(
                _global_beginning_balances.get(yr_s, 0.0) + bal, 2
            )

        # Timeline — matches ORESTAR methodology (empirically verified).
        # contributions = Cash Contribution + In-Kind (matches ORESTAR "Cash Contributions")
        # expenditures  = Cash Expenditure + In-Kind mirrored (matches ORESTAR "Cash Expenditures")
        # loans_received / loan_payments = separate for COH calculation
        # inkind shown separately for transparency
        # COH = begin + contributions + loans_received + other_receipts
        #       - expenditures - loan_payments - other_disbursements
        #   (in-kind is in both contributions and expenditures, nets to zero)
        # Keep the raw component series for history/totals, while deriving a
        # separate cash series from the certified/dated frames.  A row ORESTAR
        # no longer returns is still part of the public filing history; exact
        # absence only says it must not move the current ORESTAR cash balance.
        _raw_loans_in = (
            filer_contrib[
                filer_contrib["sub_type"] == "Loan Received (Non-Exempt)"
            ] if not filer_contrib.empty else filer_contrib
        )
        _raw_loans_out = (
            filer_expend[
                filer_expend["sub_type"] == "Loan Payment (Non-Exempt)"
            ] if not filer_expend.empty else filer_expend
        )
        _loans_in = (
            _c_for_coh[
                _c_for_coh["sub_type"] == "Loan Received (Non-Exempt)"
            ] if not _c_for_coh.empty else _c_for_coh
        )
        _loans_out = (
            _e_for_coh[
                _e_for_coh["sub_type"] == "Loan Payment (Non-Exempt)"
            ] if not _e_for_coh.empty else _e_for_coh
        )

        # The named component fields remain historical/display values.  Cash
        # consumers use cash_balance_net below instead of reconstructing cash
        # from these raw series.
        c_monthly = monthly_sum(_orestar_contrib, "contributions")
        i_monthly = monthly_sum(filer_inkind, "inkind")
        raw_li_monthly = monthly_sum(_raw_loans_in, "raw_loans_received")
        li_monthly = monthly_sum(_loans_in, "loans_received")
        # Expenditures = Cash Expenditure + In-Kind (mirrored).
        ce_monthly = monthly_sum(_cash_expend_only, "cash_exp")
        ik_monthly = monthly_sum(filer_inkind, "inkind_exp")
        raw_lo_monthly = monthly_sum(_raw_loans_out, "raw_loan_payments")
        lo_monthly = monthly_sum(_loans_out, "loan_payments")

        # These two series are deliberately separate from the display fields:
        # they contain only rows allowed to affect the stored cash balance.
        coh_c_monthly = monthly_sum(
            _c_for_coh, "cash_balance_contributions",
        )
        coh_e_monthly = monthly_sum(
            _e_for_coh, "cash_balance_expenditures",
        )
        # Other-receipt/disbursement/adjustment display fields follow the same
        # history-preserving rule as C/E. Their filtered counterparts feed only
        # cash_balance_net, so an exact-absent row never silently disappears.
        or_monthly = monthly_sum(filer_or, "other_receipts")
        od_monthly = monthly_sum(filer_od, "other_disbursements")
        ba_monthly = monthly_sum(filer_ba, "balance_adjustments")
        coh_or_monthly = monthly_sum(
            _or_for_coh, "cash_balance_other_receipts",
        )
        coh_od_monthly = monthly_sum(
            _od_for_coh, "cash_balance_other_disbursements",
        )
        coh_ba_monthly = monthly_sum(
            _ba_for_coh, "cash_balance_adjustments",
        )
        count_monthly_filer = filer_all.groupby("month").size().rename("count")
        tl_df = pd.concat([
            c_monthly, i_monthly, raw_li_monthly, li_monthly,
            ce_monthly, ik_monthly, raw_lo_monthly, lo_monthly,
            coh_c_monthly, coh_e_monthly,
            or_monthly, od_monthly, ba_monthly,
            coh_or_monthly, coh_od_monthly, coh_ba_monthly,
            count_monthly_filer,
        ], axis=1).fillna(0).sort_index()
        # The timeline must carry the SAME loan figures the balance was built
        # from, because the browser recomputes cash on hand from these rows:
        #
        #     netFlow = totalIn + totalLoansIn + totalOR
        #             - totalOut - totalLoansOut - totalOD
        #
        # so a correction applied only to yearly_nets is silently undone on
        # render. Ted Wheeler's stored balance was $1,530.14, matching ORESTAR
        # to the cent, while the page displayed $726,880 with a red discrepancy
        # warning — the client re-adding the loan principal the server had just
        # removed.
        #
        # Scaled proportionally within the year, so a month keeps its share of
        # whatever ORESTAR reports for that year. Where ORESTAR reports nothing
        # (2006 for 65 committees) every month goes to zero, which is the point.
        _loan_scale: dict[int, float] = {}
        _nonexempt_loan_cash_treatment: dict[str, dict[str, float]] = {}
        _their_years_tl = _name_to_yearly.get(name, {})
        _ours_tl = {}
        if not _c_for_coh.empty and "year" in _c_for_coh.columns:
            _lr_tl = _c_for_coh[
                _c_for_coh["sub_type"] == "Loan Received (Non-Exempt)"
            ]
            if not _lr_tl.empty:
                _ours_tl = _lr_tl.groupby("year")["amount"].sum().to_dict()
        for _y, _ours_amt in _ours_tl.items():
            _ty = _their_years_tl.get(str(int(_y)))
            if (not _summary_cash_fields_match_year(
                    _ty,
                    _digest_for_year(_y),
                    _summary_pair_available,
                ) or _ty.get("loans_received") is None
                    or not _loan_fields_trustworthy(_ty, _sum_ts)):
                continue
            _ours_amt = float(_ours_amt)
            if abs(_ours_amt) <= 1e-9:
                continue
            _reported_amt = float(_ty["loans_received"])
            _loan_scale[int(_y)] = _reported_amt / _ours_amt
            if round(_reported_amt - _ours_amt, 2):
                _nonexempt_loan_cash_treatment[str(int(_y))] = {
                    "transaction_total": round(_ours_amt, 2),
                    "orestar_counted": round(_reported_amt, 2),
                }

        # Allocate paired annual loan totals to their held transaction months
        # in whole cents. Rounding each proportional share independently
        # can miss the annual figure by several cents; putting that remainder
        # into one month can make a tiny positive receipt negative. Hamilton
        # (largest-remainder) apportionment is exact, deterministic and cannot
        # reverse a monthly sign when the source and annual total agree in sign.
        _loan_allocated: dict[str, float] = {}
        _loan_rounding_adjustments: dict[str, float] = {}
        _cent = Decimal("0.01")

        def _money_cents(value) -> int:
            return int(
                Decimal(str(value)).quantize(
                    _cent, rounding=ROUND_HALF_UP,
                ) * 100
            )

        for _year_int, _scale in _loan_scale.items():
            _year_key = str(_year_int)
            _summary_row = _their_years_tl.get(_year_key) or {}
            _target_cents = _money_cents(_summary_row["loans_received"])
            _weights = []
            for _month, _source_row in tl_df.iterrows():
                if not str(_month).startswith(f"{_year_key}-"):
                    continue
                _weight_cents = _money_cents(
                    _source_row.get("loans_received", 0)
                )
                if _weight_cents:
                    _weights.append((str(_month), _weight_cents))
            _denominator = sum(weight for _, weight in _weights)
            if not _weights or not _denominator:
                # No dated transaction basis means there is nowhere honest to
                # put a summary-only amount. The discrepancy remains visible
                # until exact remediation supplies the missing transaction.
                continue

            _shares = []
            _base_total = 0
            for _month, _weight_cents in _weights:
                _quota = Fraction(
                    _target_cents * _weight_cents, _denominator,
                )
                _base_cents = _quota.numerator // _quota.denominator
                _remainder = _quota - _base_cents
                _shares.append([
                    _month, _weight_cents, _base_cents, _remainder,
                ])
                _base_total += _base_cents
            _cents_left = _target_cents - _base_total
            if not 0 <= _cents_left <= len(_shares):
                raise ValueError(
                    f"{name}: invalid loan apportionment for {_year_key}"
                )
            _ranked = sorted(
                range(len(_shares)),
                key=lambda index: (-_shares[index][3], _shares[index][0]),
            )
            for _index in _ranked[:_cents_left]:
                _shares[_index][2] += 1

            for _month, _weight_cents, _allocated_cents, _remainder in _shares:
                if ((_weight_cents > 0 and _allocated_cents < 0)
                        or (_weight_cents < 0 and _allocated_cents > 0)):
                    raise ValueError(
                        f"{name}: loan apportionment reverses sign in {_month}"
                    )
                _allocated = _allocated_cents / 100
                _loan_allocated[_month] = _allocated
                _naively_rounded = round(
                    (_weight_cents / 100) * _scale, 2
                )
                _adjustment = round(_allocated - _naively_rounded, 2)
                if _adjustment:
                    _loan_rounding_adjustments[_month] = _adjustment

        def _scaled_loans(month_key, raw):
            _month_key = str(month_key)
            if _month_key in _loan_allocated:
                return _loan_allocated[_month_key]
            try:
                _y = int(_month_key[:4])
            except (TypeError, ValueError):
                return raw
            return raw * _loan_scale.get(_y, 1.0)

        def _timeline_cash_net(month_key, row):
            try:
                _year = int(str(month_key)[:4])
            except (TypeError, ValueError):
                _year = None
            # When an ORESTAR opening statement supersedes incomplete earlier
            # transaction history, those rows stay visible but cannot also be
            # rolled into the balance anchored by that statement.
            if _year in _balance_excluded_years:
                return 0.0
            return (
                float(row.get("cash_balance_contributions", 0))
                - float(row.get("loans_received", 0))
                + _scaled_loans(
                    month_key, float(row.get("loans_received", 0)),
                )
                + float(row.get("cash_balance_other_receipts", 0))
                + float(row.get("cash_balance_adjustments", 0))
                - float(row.get("cash_balance_expenditures", 0))
                - float(row.get("cash_balance_other_disbursements", 0))
            )

        timeline = [
            {
                "month": m,
                # Preserve every held historical component for charts and
                # totals, including loan rows that ORESTAR no longer returns.
                # Loan normalization and certified absence affect only the
                # separate cash_balance_net path below.
                "contributions":       round(
                    float(row.get("contributions", 0)), 2),
                "inkind":              round(float(row.get("inkind",              0)), 2),
                "loans_received":      round(
                    float(row.get("raw_loans_received", 0)), 2),
                "expenditures":        round(
                    float(row.get("cash_exp", 0))
                    + float(row.get("inkind_exp", 0)), 2),
                "loan_payments":       round(
                    float(row.get("raw_loan_payments", 0)), 2),
                "other_receipts":      round(float(row.get("other_receipts",      0)), 2),
                "other_disbursements": round(float(row.get("other_disbursements", 0)), 2),
                "balance_adjustments": round(float(row.get("balance_adjustments", 0)), 2),
                # Authoritative per-month cash movement.  Browser consumers
                # use this when present and fall back to the legacy component
                # equation for older generated data.
                "cash_balance_net": round(_timeline_cash_net(m, row), 2),
                **({"cash_balance_rounding_adjustment":
                    _loan_rounding_adjustments[str(m)]}
                   if str(m) in _loan_rounding_adjustments else {}),
                "count":               int(row.get("count", 0)),
            }
            for m, row in tl_df.iterrows()
        ]

        # Every year's emitted path must reproduce the exact cash calculation.
        # Check by year as well as all-time so opposite errors cannot cancel.
        _timeline_rows_by_year: dict[str, list[dict]] = defaultdict(list)
        for _timeline_row in timeline:
            _month = str(_timeline_row.get("month") or "")
            if len(_month) >= 4 and _month[:4].isdigit():
                _timeline_rows_by_year[_month[:4]].append(_timeline_row)
        for _year, _expected_net in yearly_nets.items():
            _year_rows = _timeline_rows_by_year.get(str(_year), [])
            _emitted_net = round(sum(
                float(item.get("cash_balance_net", 0)) for item in _year_rows
            ), 2)
            if _emitted_net != round(float(_expected_net), 2):
                raise ValueError(
                    f"{name}: timeline cash net for {_year} differs from "
                    f"yearly net by "
                    f"{round(float(_expected_net) - _emitted_net, 2):.2f}"
                )

        if round(sum(
            float(item.get("cash_balance_net", 0)) for item in timeline
        ), 2) != round(sum(float(value) for value in yearly_nets.values()), 2):
            raise ValueError(f"{name}: timeline cash path does not reconcile")

        # Global cash cannot be reconstructed from the raw global component
        # timeline: each committee has its own opening anchor and its own
        # evidence-gated exclusions. Aggregate the already-correct per-filer
        # movement instead, injecting each opening balance at its first
        # effective year so date-filtered roll-forwards remain meaningful.
        #
        # Independent Expenditure Filers are left out. ORESTAR publishes no
        # cash balance for them, and their spending comes from money that never
        # passed through a reported committee account, so adding it as negative
        # cash pulled the statewide figure down by money no committee held.
        # Their spending still counts in the statewide expenditure totals.
        if _independent_filer:
            _independent_cash_excluded += float(cash_on_hand)
        else:
            for _timeline_row in timeline:
                _global_cash_timeline[str(_timeline_row["month"])] += float(
                    _timeline_row.get("cash_balance_net", 0)
                )
            if (abs(float(first_year_begin)) > 0.0
                    and _first_balance_year is not None):
                _global_cash_timeline[
                    f"{_first_balance_year}-01"
                ] += float(first_year_begin)

        # Donor tables use the same cash-only scope as the global leaderboard.
        # Keep the broader contribution frame for account calculations above.
        _filer_cash_donors = filer_contrib[
            ~filer_contrib["sub_type"].isin(INKIND_SUBTYPES)
        ] if not filer_contrib.empty else filer_contrib

        # Top donors (who gave TO this filer) — all-time and by year
        if not _filer_cash_donors.empty and donor_col in _filer_cash_donors.columns:
            td = (
                _filer_cash_donors.groupby(donor_col)["amount"]
                .sum().nlargest(1000).reset_index()
                .rename(columns={donor_col: "name", "amount": "total"})
            )
            td["total"] = td["total"].round(2)
            top_donors_list = td.to_dict(orient="records")

            top_donors_by_year: dict[str, list] = {}
            if "year" in _filer_cash_donors.columns:
                for yr in sorted(_filer_cash_donors["year"].dropna().unique()):
                    yr_df = _filer_cash_donors[_filer_cash_donors["year"] == yr]
                    td_yr = (
                        yr_df.groupby(donor_col)["amount"]
                        .sum().nlargest(1000).reset_index()
                        .rename(columns={donor_col: "name", "amount": "total"})
                    )
                    td_yr["total"] = td_yr["total"].round(2)
                    top_donors_by_year[str(int(yr))] = td_yr.to_dict(orient="records")
        else:
            top_donors_list = []
            top_donors_by_year = {}

        # Top payees (what this filer paid out) — all-time and by year
        if not filer_expend.empty and contrib_col in filer_expend.columns:
            tp = (
                filer_expend.groupby(contrib_col)["amount"]
                .sum().nlargest(200).reset_index()
                .rename(columns={contrib_col: "name", "amount": "total"})
            )
            tp["total"] = tp["total"].round(2)
            top_payees_list = tp.to_dict(orient="records")
            top_payees_by_year: dict[str, list] = {}
            if "year" in filer_expend.columns:
                for yr in sorted(filer_expend["year"].dropna().unique()):
                    yr_df = filer_expend[filer_expend["year"] == yr]
                    tp_yr = (
                        yr_df.groupby(contrib_col)["amount"]
                        .sum().nlargest(200).reset_index()
                        .rename(columns={contrib_col: "name", "amount": "total"})
                    )
                    tp_yr["total"] = tp_yr["total"].round(2)
                    top_payees_by_year[str(int(yr))] = tp_yr.to_dict(orient="records")
        else:
            top_payees_list = []
            top_payees_by_year = {}

        # By contributor type — all-time (with top_donors), by year, and by month
        by_type_list = _filer_type_rows(_filer_cash_donors)
        by_type_by_year_filer: dict[str, list] = {}
        by_type_by_month_filer: dict[str, list] = {}
        if not _filer_cash_donors.empty:
            if "year" in _filer_cash_donors.columns:
                for yr in sorted(_filer_cash_donors["year"].dropna().unique()):
                    yr_rows = _filer_type_rows(_filer_cash_donors[_filer_cash_donors["year"] == yr])
                    by_type_by_year_filer[str(int(yr))] = yr_rows
            if "month" in _filer_cash_donors.columns:
                for mo in sorted(_filer_cash_donors["month"].dropna().unique()):
                    by_type_by_month_filer[str(mo)] = _filer_type_rows(_filer_cash_donors[_filer_cash_donors["month"] == mo])

        # Build ORESTAR account summary block for frontend display
        _acct_summary = {}
        if orestar_info and orestar_info.get("year", 0) > 0:
            _acct_summary = {
                "year": orestar_info["year"],
                "beginning_balance": orestar_info.get("beginning_balance", 0.0),
                "ending_cash_balance": orestar_info.get("ending_cash_balance", 0.0),
                "orestar_contributions": orestar_info.get("orestar_contributions", 0.0),
                "orestar_other_receipts": orestar_info.get("orestar_other_receipts", 0.0),
                "orestar_expenditures": orestar_info.get("orestar_expenditures", 0.0),
                "orestar_other_disbursements": orestar_info.get("orestar_other_disbursements", 0.0),
                "balance_adjustments": orestar_info.get("balance_adjustments", 0.0),
                "loans_received": orestar_info.get("loans_received", 0.0),
                "loans_received_exempt": orestar_info.get("loans_received_exempt", 0.0),
                "loan_payments": orestar_info.get("loan_payments", 0.0),
                "loan_payments_exempt": orestar_info.get("loan_payments_exempt", 0.0),
                "inkind_contributions": orestar_info.get("inkind_contributions", 0.0),
                "inkind_expenditures": orestar_info.get("inkind_expenditures", 0.0),
                "cash_balance_fs": orestar_info.get("cash_balance_fs", 0.0),
                "accounts_receivable": orestar_info.get("accounts_receivable", 0.0),
                "total_outstanding_loans": orestar_info.get("total_outstanding_loans", 0.0),
                "outstanding_personal_expenditures": orestar_info.get("outstanding_personal_expenditures", 0.0),
                "accounts_payable": orestar_info.get("accounts_payable", 0.0),
                "balance_deficit": orestar_info.get("balance_deficit", 0.0),
                "scrape_ts": orestar_info.get("ts", 0),
            }

        # The legacy signature compared rolled ending balances and called a
        # changing delta "missing_transactions". A same-year digest can prove
        # that year's movement and line items, but not its opening position:
        # a late row in 2025 changes 2026's rolled beginning without changing
        # the 2026 digest. Keep the raw begin/end fields for inspection, but do
        # not revive that causal label until those cumulative positions are
        # themselves captured.

        _orestar_absent_summary = {
            "count": len(_orestar_absent_here),
            "amount": round(float(
                filer_all.loc[
                    filer_all["tran_id"].astype(str).isin(_orestar_absent_here),
                    "amount",
                ].sum()
            ) if (_orestar_absent_here and not filer_all.empty
                  and "tran_id" in filer_all.columns) else 0.0, 2),
        } if _orestar_absent_here else {}

        detail = {
            "name": name, "slug": slug,
            "filer_ids": _scope_fids,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "total_or": total_or, "total_od": total_od,
            "cash_on_hand": cash_on_hand, "tran_count": tran_count,
            "cash_on_hand_source": cash_on_hand_source,
            "has_orestar_summary": has_orestar_summary,
            "has_orestar_check": _comparison.get("status") == "paired",
            "orestar_discrepancy": (
                _comparison.get("delta_at_capture")
                if _comparison.get("actionable") else None
            ),
            "orestar_live_difference": orestar_live_difference,
            "orestar_comparison": _comparison,
            # Surfaced on the site as a "Closed" label, so a reader can
            # tell a finished committee from one that is merely quiet.
            "closed": is_closed,
            "closed_since": closed_since,
            "closed_final_balance": closed_final_balance,
            "orestar_year": orestar_year,
            "orestar_account_summary": _acct_summary,
            "orestar_yearly": _name_to_yearly.get(name, {}),
            # ORESTAR's own year-to-year balances, where they do not chain.
            #
            # Year N's ending balance should be year N+1's beginning balance.
            # For 444 of 6,511 committees with more than one summary year it is
            # not, and the jumps have no transactions behind them: the silent
            # years were checked against ORESTAR directly and it reports ZERO
            # records for them, matching what we hold.
            #
            # Hood River County Democrats is the clearest case. Its 2009
            # summary ends at $336.93 and its 2010 summary begins at $475.85 —
            # $138.92 out of nowhere — and seven more such jumps follow. They
            # sum to $6,192.22, which is its entire discrepancy to the cent.
            # Across the bucket this holds for 171 of 178 committees.
            #
            # This is the same defect the summary_vs_itemised note describes,
            # only across years instead of within one, and it needs no
            # completeness evidence: it is ORESTAR's summary against ORESTAR's
            # summary. Nothing here can be fixed on our side — we roll the
            # balance forward from transactions and that arithmetic is sound.
            # Recording it is what lets the site say so.
            "orestar_chain_breaks": _chain_breaks(_name_to_yearly.get(name, {})),
            # Rows a current, paired exact search proves ORESTAR no longer
            # returns. They remain in transaction history but are omitted from
            # cash-only frames. Absence alone does not establish disposition.
            "orestar_absent": _orestar_absent_summary,
            # Deprecated schema alias for already-deployed clients. New code
            # and copy use the disposition-neutral key above.
            "orestar_withdrawn": _orestar_absent_summary,
            # Exempt loan principal held out of cash because ORESTAR's own
            # statement records no receipts at all in those years. Carried per
            # year so the site can say WHY a figure omits a transaction the
            # committee plainly filed, rather than silently differing from a
            # number the reader can look up.
            "exempt_loans_excluded": _exempt_dropped,
            # Certificates of Limited Contributions and Expenditures held by
            # this scope's filers, and the derived ghost rows that carry
            # ORESTAR's restatements into the balance. Listed in full so every
            # dollar they move can be traced to the two ORESTAR figures it
            # came from.
            "orestar_certificates": _certificate_years_here,
            "certificate_restatements": [
                {k: v for k, v in _spec.items()} for _spec in _ghost_specs
            ],
            "certificate_restatement_total": round(
                sum(float(_spec["amount"]) for _spec in _ghost_specs), 2
            ),
            # Originals ORESTAR never expired after they were amended, and so
            # still counts alongside the amendment. Kept in the balance to
            # match ORESTAR; each entry names the original and every amendment
            # pointing at it, so a reader can look both up on ORESTAR.
            "orestar_live_originals": _live_originals_here,
            # Transactions ORESTAR deleted after we had fetched them. Removed
            # from the balance to match ORESTAR; each names the deleted ID and
            # the deletion record, both of which ORESTAR's history page shows.
            "orestar_deletions": _deletions_here,
            # Early-era amendment chains ORESTAR still counts (see
            # orestar_amendment_chains.py): each applied filer-year with the
            # versions added or taken away and every version of their chains,
            # so each dollar can be traced to ORESTAR's history pages.
            "orestar_amendment_chains": _chains_here,
            # Discontinued committees whose last ORESTAR balance vanished at
            # the first blank statement (see orestar_closures.py).
            "orestar_closure_resets": _closures_here,
            # ORESTAR publishes no cash balance for Independent Expenditure
            # Filers; the site shows none and compares none for them.
            "filer_kind": "independent_expenditure" if _independent_filer else None,
            "balance_published": not _independent_filer,
            # Annual non-exempt loan totals used for the cash lane when a
            # trustworthy ORESTAR statement differs from the eligible held
            # transaction total. Raw loan rows remain visible in the timeline;
            # this record lets the UI explain the resulting cash adjustment.
            "nonexempt_loan_cash_treatment":
                _nonexempt_loan_cash_treatment,
            # Exact marker for clients reading the two treatment maps above.
            # Legacy profile JSON lacks it and therefore cannot present a
            # summary-derived adjustment as current.
            "annual_summary_treatment_basis": "paired_year_digest_v1",
            "yearly_discrepancies": yearly_discrepancies,
            # Candidate 2006 bases, on the pipeline's own definition, so the
            # rule ORESTAR actually applied can be identified by measurement
            # rather than guessed at a fifth time.
            "basis_2006": basis_2006,
            # Deprecated causal labels. They remain in the schema as null until
            # rolled opening/ending positions are captured too; year-local
            # provenance alone cannot distinguish an opening error from a late
            # transaction in any preceding year.
            "discrepancy_signature": discrepancy_signature,
            "discrepancy_first_bad_year": discrepancy_first_bad_year,
            # Surfaced so a reader can see WHY the opening balance was
            # taken from ORESTAR rather than from our own rows.
            "opening_reconciles": opening_reconciles,
            "opening_our_prior_net": opening_our_prior_net,
            "beginning_balances": beginning_balances,
            "timeline": timeline,
            "top_donors": top_donors_list,
            "top_donors_by_year": top_donors_by_year,
            "top_payees": top_payees_list,
            "top_payees_by_year": top_payees_by_year,
            "by_contributor_type": by_type_list,
            "by_contributor_type_by_year": by_type_by_year_filer,
            "by_contributor_type_by_month": by_type_by_month_filer,
        }

        out_path = filers_dir / f"{slug}.json"
        with open(out_path, "w") as f:
            json.dump(detail, f, separators=(",", ":"), default=str)

        _fid_for_index = ""
        if _filer_id_col:
            _fid_for_index = _name_to_fid.get(name, "")
        # Attach scraped metadata (party, office, committee type) if available
        _meta = _filer_metadata.get(_fid_for_index, {}) if _fid_for_index else {}
        _party = _meta.get("party", "")
        _office_raw = _meta.get("office", "")
        _committee_type = _meta.get("committee_type", "")
        _pac_type = _meta.get("pac_type", "")
        _nature = _meta.get("nature", "")
        _candidate_name = _meta.get("candidate_name", "")
        _election = _meta.get("election", "")
        # Normalize office to just the title (strip district number)
        # e.g. "State Representative, 25th District" → "State Representative"
        _office = _office_raw.split(",")[0].strip() if _office_raw else ""

        # Attach leadership role if available
        _leader = _leadership.get(_fid_for_index, {}) if _fid_for_index else {}
        _leadership_role = _leader.get("role_title", "")
        # Assign leadership tier:
        #   1 = Speaker of House / President of Senate (top)
        #   2 = Majority Leaders / Ways & Means Co-Chairs
        #   3 = Chairs, Pro Tems, other leadership
        _leadership_tier = 0
        if _leadership_role:
            _lr = _leadership_role.lower()
            if "speaker of the house" in _lr or "senate president" == _lr.strip() or (
                "president" in _lr and "pro" not in _lr
            ):
                _leadership_tier = 1
            elif ("majority leader" in _lr and "deputy" not in _lr and "assistant" not in _lr) \
                    or "ways and means co-chair" in _lr:
                _leadership_tier = 2
            else:
                _leadership_tier = 3

        _filer_detail_rows.append(
            {"slug": slug, "name": name, "filer_id": _fid_for_index, "detail": detail}
        )

        index_rows.append({
            "slug": slug, "name": name,
            "filer_id": _fid_for_index,
            "filer_ids": _scope_fids,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "cash_on_hand": cash_on_hand,
            # False for Independent Expenditure Filers: ORESTAR publishes no
            # balance for them and statewide cash leaves them out.
            "balance_published": not _independent_filer,
            "party": _party,
            "office": _office,
            "office_district": _office_raw,
            "committee_type": _committee_type,
            "pac_type": _pac_type,
            "nature": _nature,
            "candidate_name": _candidate_name,
            "election": _election,
            "leadership_role": _leadership_role,
            "leadership_tier": _leadership_tier,
        })

        if _transaction_snapshot_id and _scope_fids:
            _balance_snapshot_scopes.append({
                "filer_ids": _scope_fids,
                "name": name,
                "slug": slug,
                "cash_on_hand": cash_on_hand,
                "tran_count": tran_count,
                "app_scope_transaction_digest": _current_scope_digests[name],
                "app_year_transaction_digests": _current_year_digests[name],
            })

    # Sort index by total_in descending
    index_rows.sort(key=lambda r: r["total_in"], reverse=True)
    _write_json("filer_index.json", index_rows)
    _write_json(SOURCE_FILENAME, build_balance_snapshot_source(
        _transaction_snapshot_id,
        _balance_snapshot_scopes,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ))
    # Mirror per-filer detail blobs into Postgres (filer_detail table). Once
    # generated files are no longer committed, a failed write must fail the
    # run instead of leaving the live app silently stale.
    supabase_sync.bulk_upsert_filer_detail(_filer_detail_rows)
    log.info(
        "Wrote filer_index.json (%d filers) and %d filer detail files",
        len(index_rows), len(index_rows),
    )

    # ── balance_discrepancies.json — paired comparisons only ────────────────
    #
    # A live app balance and an older ORESTAR summary are not comparable.  The
    # old code tried to rewind the app with filed_date; that is day-granular,
    # unrelated to collection time for backfills, and used a different cash
    # formula from the loan-adjusted live balance.  It created discrepancies
    # for committees that actually matched.  Only immutable paired captures
    # can drive this list or downstream remediation now.
    log.info(
        "Certificate restatements: %d ghost rows across %d committees "
        "(|$%s| moved); %d filers skipped as claimed by more than one committee",
        _ghost_row_count, _ghost_committees, f"{_ghost_amount:,.2f}",
        len(_ghost_skipped_ambiguous),
    )
    log.info(
        "Closure resets: %d applied (|$%s|); independent expenditure filer "
        "scopes without a published balance: %d",
        _closure_resets_applied, f"{_closure_amount:,.2f}", _independent_filer_scopes,
    )
    log.info(
        "Amendment chains: %d derived rows; decisions %s",
        _chain_rows_added, dict(sorted(_chain_decisions.items())),
    )
    log.info(
        "Live amended originals: %d kept across %d committees (ORESTAR still "
        "counts them); %d IDs in coverage evidence",
        _live_original_rows, _live_original_committees, len(_live_originals_evidence),
    )
    log.info(
        "Deleted transactions: %d removed across %d committees (ORESTAR deleted "
        "them after we fetched them)",
        _deletion_rows, _deletion_committees,
    )
    _disc_rows = []
    _unpaired_rows = []
    _refresh_rows = []
    _nonactionable_rows = []
    _summary_count = 0
    _paired_count = 0
    _comparable_count = 0
    _population_count = 0
    _unchecked_count = 0
    _no_published_balance = 0
    for _row in _filer_detail_rows:
        _d = _row["detail"]
        _acct = _d.get("orestar_account_summary") or {}
        _audit_fids = [str(fid) for fid in (_d.get("filer_ids") or []) if str(fid)]
        if not _audit_fids:
            # Without a physical filer ID there is no ORESTAR account-summary
            # scope to capture. Keep this outside the measurable population.
            continue
        if _d.get("balance_published") is False:
            # ORESTAR publishes no balance to compare against (Independent
            # Expenditure Filers). Counted, never flagged.
            _no_published_balance += 1
            continue
        _population_count += 1
        if _acct.get("ending_cash_balance") is None:
            _unchecked_count += 1
            _available = []
            _latest_partial = []
            for _fid in _audit_fids:
                _years = (_yearly_summaries.get(_fid) or {}).get("years") or {}
                if not _years:
                    continue
                _latest_year = max(_years)
                _latest_row = _years[_latest_year]
                if _latest_row.get("ending_cash_balance") is None:
                    continue
                _available.append(_fid)
                _latest_partial.append((_latest_year, _latest_row))
            _unpaired_rows.append({
                "slug": _row["slug"],
                "name": _row["name"],
                "filer_id": _row.get("filer_id"),
                "filer_ids": _audit_fids,
                "status": "unchecked",
                "reason": "summary_scope_incomplete",
                "available_filer_ids": _available,
                "missing_filer_ids": [
                    fid for fid in _audit_fids if fid not in set(_available)
                ],
                "current_calculated": _d.get("cash_on_hand", 0.0),
                "latest_orestar": (
                    round(sum(float(row.get("ending_cash_balance") or 0.0)
                              for _, row in _latest_partial), 2)
                    if _latest_partial else None
                ),
                "current_tran_count": _d.get("tran_count", 0),
                "dormant": False,
                "closed": False,
                "scrape_ts": (
                    max(_summary_scrape_ts(row) for _, row in _latest_partial)
                    if _latest_partial else None
                ),
            })
            continue
        _summary_count += 1
        _cmp = _d.get("orestar_comparison") or {}
        if _cmp.get("status") != "paired":
            _unpaired_rows.append({
                "slug": _row["slug"],
                "name": _row["name"],
                "filer_id": _row.get("filer_id"),
                "filer_ids": _d.get("filer_ids") or [],
                "status": _cmp.get("status") or "unpaired",
                "reason": _cmp.get("reason") or "capture_missing",
                "current_calculated": _d.get("cash_on_hand", 0.0),
                "latest_orestar": _acct.get("ending_cash_balance"),
                "current_tran_count": _d.get("tran_count", 0),
                "dormant": str(_acct.get("year")) not in (
                    _d.get("beginning_balances") or {}
                ),
                "closed": bool(_d.get("closed")),
                "scrape_ts": _acct.get("scrape_ts"),
            })
            continue

        _paired_count += 1
        _delta = round(float(_cmp.get("delta_at_capture") or 0.0), 2)
        _audit_row = {
            "slug": _row["slug"],
            "name": _row["name"],
            "filer_id": _row.get("filer_id"),
            "filer_ids": _d.get("filer_ids") or [],
            "calculated": _cmp.get("app_cash_on_hand", 0.0),
            "orestar": _cmp.get("orestar_cash_on_hand", 0.0),
            "delta": _delta,
            "current_calculated": _d.get("cash_on_hand", 0.0),
            "latest_orestar": _acct.get("ending_cash_balance"),
            "app_balance_change_since_capture": round(
                float(_cmp.get("app_balance_change_since_capture") or 0.0), 2
            ),
            "scrape_ts": _cmp.get("captured_at"),
            "capture_started_at": _cmp.get("capture_started_at"),
            "app_snapshot_created_at": _cmp.get("app_snapshot_created_at"),
            "orestar_year": _acct.get("year"),
            "tran_count": _cmp.get("app_tran_count", 0),
            "current_tran_count": _d.get("tran_count", 0),
            "dormant": str(_acct.get("year")) not in (_d.get("beginning_balances") or {}),
            "closed": bool(_d.get("closed")),
            "closed_since": _d.get("closed_since"),
            "comparison_status": "paired",
            "app_data_changed_since_capture": bool(
                _cmp.get("app_data_changed_since_capture")
            ),
            "orestar_data_changed_since_capture": bool(
                _cmp.get("orestar_data_changed_since_capture")
            ),
            "annual_summary_refresh_years": _cmp.get(
                "annual_summary_refresh_years", []
            ),
        }
        if (_cmp.get("app_data_changed_since_capture")
                or _cmp.get("orestar_data_changed_since_capture")
                or _cmp.get("annual_summary_refresh_needed")):
            _audit_row["reason"] = (
                "newer_summary_was_not_paired"
                if _cmp.get("orestar_data_changed_since_capture")
                else (
                    "app_state_changed_since_capture"
                    if _cmp.get("app_data_changed_since_capture")
                    else "annual_summary_years_unpaired"
                )
            )
            _refresh_rows.append(_audit_row)
            continue
        if _d.get("closed"):
            if abs(_delta) > 0.01:
                _audit_row["reason"] = "closed_trailing_summary"
                _nonactionable_rows.append(_audit_row)
            continue

        _comparable_count += 1
        if abs(_delta) <= 0.01:
            continue
        _change = round(float(_cmp.get("app_balance_change_since_capture") or 0.0), 2)
        _newer_app_data = bool(_cmp.get("app_data_changed_since_capture"))
        _disc_rows.append({
            "slug": _row["slug"],
            "name": _row["name"],
            "filer_id": _row.get("filer_id"),
            "filer_ids": _d.get("filer_ids") or [],
            # Both values below were frozen together.  Current values are kept
            # separately so the admin can see movement without judging on it.
            "calculated": _cmp.get("app_cash_on_hand", 0.0),
            "orestar": _cmp.get("orestar_cash_on_hand", 0.0),
            "delta": _delta,
            "asof_delta": _delta,  # compatibility for existing admin clients
            "current_calculated": _d.get("cash_on_hand", 0.0),
            "latest_orestar": _acct.get("ending_cash_balance"),
            "live_delta": _d.get("orestar_live_difference"),
            "post_summary_activity": _change,  # compatibility; app-state change
            "app_balance_change_since_capture": _change,
            "comparison_basis": _cmp.get("basis"),
            "comparison_status": "paired",
            "transaction_snapshot_id": _cmp.get("app_transaction_snapshot_id"),
            "orestar_year": _acct.get("year"),
            "scrape_ts": _cmp.get("captured_at"),
            "capture_started_at": _cmp.get("capture_started_at"),
            "app_snapshot_created_at": _cmp.get("app_snapshot_created_at"),
            "tran_count": _cmp.get("app_tran_count", 0),
            "current_tran_count": _d.get("tran_count", 0),
            "dormant": str(_acct.get("year")) not in (_d.get("beginning_balances") or {}),
            "closed": bool(_d.get("closed")),
            "closed_since": _d.get("closed_since"),
            "newer_app_data": _newer_app_data,
            "snapshot_stale": _newer_app_data,  # compatibility for old clients
        })

    _disc_rows.sort(key=lambda r: -abs(r["delta"]))
    _newer_app_rows = [
        row for row in _refresh_rows
        if row.get("app_data_changed_since_capture")
    ]
    _newer_count = len(_newer_app_rows)
    _newer_amount = round(sum(abs(r["delta"]) for r in _newer_app_rows), 2)
    _write_json("balance_discrepancies.json", {
        # The admin must never treat the pre-pairing live-vs-old-summary blob
        # already in Supabase as current evidence during rollout.  Both fields
        # are required by the client before any row enters the actionable
        # bucket; an old/malformed payload is displayed as unpaired instead.
        "schema_version": 2,
        "basis": "paired_capture_window_v1",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "population": _population_count,
        "checked": _summary_count,
        "unchecked": _unchecked_count,
        "paired": _paired_count,
        "comparable": _comparable_count,
        "unpaired": len(_unpaired_rows),
        "refresh_needed": len(_refresh_rows),
        "nonactionable": len(_nonactionable_rows),
        "no_published_balance": _no_published_balance,
        "flagged": len(_disc_rows),
        "newer_app_data": _newer_count,
        "newer_app_data_amount": _newer_amount,
        "snapshot_stale": _newer_count,          # compatibility
        "snapshot_stale_amount": _newer_amount,
        "rows": _disc_rows,
        "unpaired_rows": _unpaired_rows,
        "refresh_rows": _refresh_rows,
        "nonactionable_rows": _nonactionable_rows,
    })

    # ── by_party_type.json — Donor type composition by party by year ────────
    # Cross-references filer_index party data with per-filer donor type
    # breakdowns to produce a partisan comparison dataset.
    _party_type_by_year: dict[str, dict[str, dict[str, dict]]] = {}
    _party_filer_count = {"Democrat": 0, "Republican": 0}

    for row in index_rows:
        _party = row.get("party", "")
        _pac_type = row.get("pac_type", "")

        # Include candidate committees with party
        if _party in ("Democrat", "Republican"):
            pass  # use _party as-is
        # Include caucus PACs — party determined from ORESTAR's "Nature of
        # Committee" field (e.g., "Supporting House Democratic Candidates").
        # ORESTAR PAC pages have no explicit party field; the nature text
        # is the authoritative source scraped directly from the SOO page.
        elif _pac_type == "Caucus":
            _nature = (row.get("nature", "") or "").lower()
            if "democrat" in _nature:
                _party = "Democrat"
            elif "republican" in _nature:
                _party = "Republican"
            else:
                continue  # Can't determine party
        else:
            continue
        _slug = row["slug"]
        _detail_path = filers_dir / f"{_slug}.json"
        if not _detail_path.exists():
            continue
        _party_filer_count[_party] += 1
        with open(_detail_path) as f:
            _detail = json.load(f)
        _by_type_year = _detail.get("by_contributor_type_by_year", {})
        for _yr, _type_rows in _by_type_year.items():
            if _yr not in _party_type_by_year:
                _party_type_by_year[_yr] = {}
            if _party not in _party_type_by_year[_yr]:
                _party_type_by_year[_yr][_party] = {}
            for _tr in _type_rows:
                _t = _tr["type"]
                if _t not in _party_type_by_year[_yr][_party]:
                    _party_type_by_year[_yr][_party][_t] = {
                        "total": 0.0, "donors": {}
                    }
                _party_type_by_year[_yr][_party][_t]["total"] += _tr.get("total", 0)
                for _d in _tr.get("top_donors", []):
                    _donors = _party_type_by_year[_yr][_party][_t]["donors"]
                    _donors[_d["name"]] = _donors.get(_d["name"], 0) + _d["total"]

    # Flatten to output format
    _party_output: dict = {"by_year": {}, "meta": {
        "democrat_committees": _party_filer_count["Democrat"],
        "republican_committees": _party_filer_count["Republican"],
    }}
    for _yr in sorted(_party_type_by_year.keys()):
        _party_output["by_year"][_yr] = {}
        for _p in ("Democrat", "Republican"):
            _types = _party_type_by_year[_yr].get(_p, {})
            _party_output["by_year"][_yr][_p] = [
                {
                    "type": _t,
                    "total": round(_data["total"], 2),
                    "top_donors": sorted(
                        [{"name": _n, "total": round(_v, 2)}
                         for _n, _v in _data["donors"].items()],
                        key=lambda x: -x["total"],
                    )[:5],
                }
                for _t, _data in sorted(
                    _types.items(), key=lambda x: -x[1]["total"]
                )
            ]

    _write_json("by_party_type.json", _party_output)
    log.info(
        "Wrote by_party_type.json (D: %d committees, R: %d committees)",
        _party_filer_count["Democrat"], _party_filer_count["Republican"],
    )

    # ── activity_snapshot.json — Campaign Pulse module data ────────────────
    from generate_activity_snapshot import generate as _gen_snapshot
    _snapshot = _gen_snapshot(agg_dir=AGG_DIR, filers_dir=filers_dir)

    # Carry forward what this generator does not build.
    #
    # legislative_map has TWO producers. generate_activity_snapshot builds it
    # from the filing roster; refresh_legislative_map rebuilds the same key and
    # adds district_history, the per-district prior-cycle results the race map
    # renders under "Previous cycles".
    #
    # Rewriting the whole key here silently deleted that history on every daily
    # refresh. racemap.js reads it, refresh_legislative_map writes it, and
    # nothing errored — the panel simply showed nothing for all but the few
    # hours between a candidate-filings run and the next refresh. The feature
    # has never worked in the normal course of a day.
    #
    # Preserving a key the current generator cannot produce is the fix: the
    # daily refresh has no candidate-filing or election-results data in hand,
    # so it is in no position to replace it.
    if supabase_sync.sync_enabled():
        _prev_snap = supabase_sync.require_dashboard_cache("activity_snapshot")
        _prev_hist = ((_prev_snap.get("legislative_map") or {}).get("district_history"))
        if _prev_hist and "district_history" not in (_snapshot.get("legislative_map") or {}):
            _snapshot.setdefault("legislative_map", {})["district_history"] = _prev_hist
            log.info("Preserved district_history for %d chamber(s) from the previous snapshot",
                     len(_prev_hist))
    else:
        _prev_snap = supabase_sync.get_dashboard_cache("activity_snapshot") or {}
        _prev_hist = ((_prev_snap.get("legislative_map") or {}).get("district_history"))
        if _prev_hist and "district_history" not in (_snapshot.get("legislative_map") or {}):
            _snapshot.setdefault("legislative_map", {})["district_history"] = _prev_hist
            log.info("Preserved district_history for %d chamber(s) from the previous snapshot",
                     len(_prev_hist))

    _write_json("activity_snapshot.json", _snapshot)
    log.info(
        "Wrote activity_snapshot.json (%d candidates)",
        _snapshot["meta"]["total_candidates"],
    )

    # ── Build donor → filer mapping for clickable donor links ──────────────
    # For each filer, build a lookup by normalized name and by filer ID.
    # Then check top donor names against filer names for linking.
    _norm_cache: dict[str, str] = {}  # normalized → canonical filer name

    def _normalize_name(n: str) -> str:
        """Normalize a name for matching: lowercase, strip punctuation/whitespace."""
        import re
        n = n.lower().strip()
        n = re.sub(r"[''`]s\b", "s", n)  # possessives
        n = re.sub(r"[^a-z0-9\s]", "", n)  # strip punctuation
        n = re.sub(r"\s+", " ", n).strip()
        # Common suffixes
        for suffix in [" pac", " committee", " cmte", " comm", " political action committee",
                       " for oregon", " for or"]:
            if n.endswith(suffix):
                n = n[:-len(suffix)].strip()
        return n

    filer_by_norm: dict[str, list] = {}  # normalized name → list of {slug, name, filer_id}
    filer_by_fid: dict[str, dict] = {}   # filer_id → {slug, name}

    for row in index_rows:
        norm = _normalize_name(row["name"])
        entry = {"slug": row["slug"], "name": row["name"], "filer_id": row.get("filer_id", "")}
        filer_by_norm.setdefault(norm, []).append(entry)
        if entry["filer_id"]:
            filer_by_fid[entry["filer_id"]] = entry

    # Collect all unique donor names from top_donors.json data
    # (We'll read it back since it was already written)
    donor_filer_map = {}  # donor_name_lower → {slug, name, confidence}
    top_donors_path = AGG_DIR / "top_donors.json"
    if top_donors_path.exists():
        import json as _json
        with open(top_donors_path) as _f:
            _td = _json.load(_f)
        all_donor_names = set()
        for d in _td.get("all_time", []):
            all_donor_names.add(d["name"])
        for yr_list in _td.get("by_year", {}).values():
            for d in yr_list:
                all_donor_names.add(d["name"])

        # Regex for extracting parenthetical filer IDs from donor names
        # e.g. "Oregon Hospital Political Action Committee (161)"
        _PAREN_FID_RE = re.compile(r"\((\d+)\)\s*$")

        for donor_name in all_donor_names:
            dn_lower = donor_name.lower().strip()
            dn_norm = _normalize_name(donor_name)

            # 1. Exact name match (case-insensitive)
            exact_matches = [r for r in index_rows if r["name"].lower().strip() == dn_lower]
            if len(exact_matches) == 1:
                donor_filer_map[dn_lower] = {
                    "slug": exact_matches[0]["slug"],
                    "name": exact_matches[0]["name"],
                    "confidence": "high",
                }
                continue

            # 2. Normalized name match
            norm_matches = filer_by_norm.get(dn_norm, [])
            if len(norm_matches) == 1:
                donor_filer_map[dn_lower] = {
                    "slug": norm_matches[0]["slug"],
                    "name": norm_matches[0]["name"],
                    "confidence": "high",
                }
                continue

            # 3. Parenthetical filer ID match — e.g. "Name Here (12345)"
            # Check if the donor name ends with a number in parentheses
            # and that number corresponds to a filer ID.
            paren_m = _PAREN_FID_RE.search(donor_name)
            if paren_m:
                paren_fid = paren_m.group(1)
                if paren_fid in filer_by_fid:
                    filer_entry = filer_by_fid[paren_fid]
                    # Sanity check: some name similarity between donor and filer
                    donor_base = donor_name[:paren_m.start()].strip().lower()
                    filer_name_lower = filer_entry["name"].lower()
                    # Check if any significant word (3+ chars) from donor base
                    # appears in the filer name, or vice versa
                    donor_words = {w for w in donor_base.split() if len(w) >= 3}
                    filer_words = {w for w in filer_name_lower.split() if len(w) >= 3}
                    overlap = donor_words & filer_words
                    if overlap or donor_base in filer_name_lower or filer_name_lower in donor_base:
                        donor_filer_map[dn_lower] = {
                            "slug": filer_entry["slug"],
                            "name": filer_entry["name"],
                            "confidence": "high",
                        }
                        continue

            # 4. Multiple matches → ambiguous
            if len(norm_matches) > 1 or len(exact_matches) > 1:
                matches = exact_matches if len(exact_matches) > 1 else norm_matches
                donor_filer_map[dn_lower] = {
                    "candidates": [{"slug": m["slug"], "name": m["name"]} for m in matches],
                    "confidence": "ambiguous",
                }

    _write_json("donor_filer_map.json", donor_filer_map)
    log.info("Wrote donor_filer_map.json (%d linked, %d ambiguous)",
             sum(1 for v in donor_filer_map.values() if v.get("confidence") == "high"),
             sum(1 for v in donor_filer_map.values() if v.get("confidence") == "ambiguous"))

    # Return global COH data so aggregate() can update summary.json
    return {
        "global_cash_on_hand": round(sum(
            r["cash_on_hand"] for r in index_rows if r.get("balance_published", True)
        ), 2),
        "global_beginning_balances": _global_beginning_balances,
        # What statewide cash leaves out, so the site can say so.
        "global_cash_excludes_independent_filers": {
            "filers": _independent_filer_scopes,
            "cash_excluded": round(_independent_cash_excluded, 2),
        },
        "global_cash_timeline": {
            month: round(amount, 2)
            for month, amount in sorted(_global_cash_timeline.items())
        },
    }


def _write_json(filename: str, data) -> None:
    path = AGG_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    log.info("Wrote %s (%d bytes)", filename, path.stat().st_size)
    # Mirror the aggregate blob into Postgres (dashboard_cache) so the frontend
    # reads it from Supabase instead of this file. No-ops without credentials;
    # with credentials, failures propagate because Postgres is the only live
    # copy after generated output leaves Git.
    supabase_sync.upsert_dashboard_cache(filename.removesuffix(".json"), data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_combined_csv() -> Path:
    """Concatenate all per-year shards into one gzip CSV for the full download.
    Streams shard-by-shard to bound memory."""
    out = DATA_DIR / "transactions_full.csv.gz"
    shards = sorted(TRANS_DIR.glob("txn_*.csv.gz"))
    with gzip.open(out, "wt", newline="") as w:
        for i, shard in enumerate(shards):
            with gzip.open(shard, "rt") as r:
                header = r.readline()
                if i == 0:
                    w.write(header)
                shutil.copyfileobj(r, w)
    log.info("Built combined full CSV %s (%d shards)", out.name, len(shards))
    return out


if __name__ == "__main__":
    import os
    import sys

    # Every Supabase write in this file is a silent no-op when SUPABASE_DB_URL
    # is unset. That is right for local runs, but in CI it meant three
    # scheduled workflows spent an hour scraping, wrote correct JSON to the
    # repo, exited 0 — and left the live site reading days-old data, with
    # nothing in the log to say so. Say it loudly instead of returning quietly.
    if not supabase_sync.sync_enabled():
        _where = "CI" if os.environ.get("GITHUB_ACTIONS") else "this machine"
        print("=" * 72, file=sys.stderr)
        print(f"WARNING: SUPABASE_DB_URL is not set on {_where}.", file=sys.stderr)
        print("         Files on disk will be updated; the LIVE SITE WILL NOT.",
              file=sys.stderr)
        print("=" * 72, file=sys.stderr)
    if "--supabase-full-load" in sys.argv:
        # One-time / periodic: load every transaction shard into Postgres and
        # publish the combined full-dataset CSV to Storage. Run once after the
        # migrations are applied, then rely on the daily incremental sync.
        log.info("Supabase full load: reloading all transaction shards…")
        supabase_sync.full_reload_transactions(TRANS_DIR)
        supabase_sync.upload_full_csv(_build_combined_csv())
    elif "--merge-only" in sys.argv:
        # Just merge raw Excel into txn_YYYY.csv.gz — skip aggregation.
        # Used by filer-targeted backfill to save time; the daily refresh
        # will run the full aggregation later.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
        log.info("Merge-only mode: loading raw Excel files and updating transaction files")
        new_df = load_excel_files(RAW_DIR)
        if new_df.empty:
            log.info("No new data to merge.")
        else:
            existing = _load_all_transactions()
            _record_amount_updates(existing, new_df)
            df = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
            df["tran_id"] = df["tran_id"].astype(str).str.strip()
            before = len(df)
            df = df.drop_duplicates(subset=["tran_id"], keep="last")
            log.info("Deduplicated by tran_id: %d → %d rows", before, len(df))
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
            # Normalize filed_date to YYYY-MM-DD (ORESTAR Excel uses MM/DD/YYYY)
            if "filed_date" in df.columns:
                _fd = pd.to_datetime(df["filed_date"], format="mixed", dayfirst=False, errors="coerce")
                df["filed_date"] = _fd.dt.strftime("%Y-%m-%d").fillna(df["filed_date"])
                df, _superseded = _drop_superseded(df, keep_live=_live_original_ids())
                _save_transactions(df)
            else:
                log.warning("No filed_date column — cannot split by year")

            # Push the freshly-merged rows to Postgres NOW, not on tomorrow's
            # daily refresh.
            #
            # This used to write year shards and stop, which made the database
            # lag the shards by up to a day. That was tolerable when raw Excel
            # files were kept on disk as the resume record — but those files are
            # the reason the repository reached 80 GB, and the fetcher's
            # "do we already hold this window?" check reads POSTGRES. Leaving
            # the database behind would make that check answer "no" for rows we
            # had just downloaded, and the next chain run would fetch them all
            # over again.
            _new_ids = set(new_df["tran_id"].astype(str).str.strip())
            _changed = df[df["tran_id"].astype(str).str.strip().isin(_new_ids)]
            supabase_sync.upsert_transactions(_changed)
            if _superseded:
                supabase_sync.delete_transactions(_superseded)
            log.info("Synced %d merged rows to Postgres", len(_changed))

            # Raw files are now disposable. Every one of them has been merged
            # into the year shards and pushed to Postgres, and the fetcher
            # decides what to skip from ORESTAR's record counts plus what the
            # database holds — not from what happens to be left on disk.
            #
            # Keeping them cost 10,205 committed files and 80 GB of repository,
            # because git retains every version of every one forever.
            cleaned = 0
            for f in RAW_DIR.glob("*.xls*"):
                f.unlink()
                cleaned += 1
            log.info("Merge complete. Cleaned %d raw files (all merged and synced).", cleaned)
    else:
        process()
