#!/usr/bin/env python3
"""
diff_coverage.py — which ROWS do we hold that ORESTAR does not, and vice versa?

survey_coverage.py compares COUNTS. That is cheap — one search per committee —
and it is the right first pass, but it cannot answer the question it appears to
answer, because the two sides are not counting the same thing.

ORESTAR's search returns superseded originals. This pipeline drops them: when
an amendment exists, the original it replaced is removed, which is correct and
is what `_drop_superseded` is for. So a committee that holds EVERY row it
should still reports fewer than ORESTAR, permanently. Oregon Firearms
Federation PAC holds 3,905 against ORESTAR's 3,906 and is not missing anything
at all — a backfill downloaded the full 3,906 and the merge correctly dropped
one superseded row straight back out.

That single fact broke a whole afternoon of analysis. "21 committees short 429
rows" was mostly this. Worse, the completeness test built on those counts has
now been wrong in both directions: `held >= orestar` wrongly certified
committees holding SURPLUS rows, and `held == orestar` wrongly rejects
committees where supersession simply worked. There is no correct count-based
test, because a count cannot distinguish "a row we should not have" from "a row
we correctly removed".

Comparing the IDENTITIES settles it. ORESTAR's results page prints a Tran ID
per row; diffing that set against ours yields two exact answers instead of one
ambiguous number:

    surplus  — ids we hold that ORESTAR does not return.
               Withdrawn or superseded filings. Nothing removes these today,
               so our store drifts upward invisibly. Plumbers & Steamfitters
               PAC held 16, worth $32,284.04, and surveyed as "missing: 0".

    missing  — ids ORESTAR returns that we do not hold.
               Genuinely absent; the backfill can recover these.

The cost is real and is the reason this is a separate tool rather than the
default. The count survey is one search per committee; this exports every
provably complete leaf and may need many disjoint searches to get below the
export cap. Reserve it for committees where the answer changes a decision.

ORESTAR caps the results UI at 100 pages — 5,000 rows — and its Excel export at
4,999 rows. Windows are therefore split until each reports at most the export
cap, then downloaded once. Every leaf is checked against its reported count,
and every group of children is reconciled against its parent. A short or
overlapping collection is treated as FAILED rather than merged: a partial
collection is worse than none, because it looks like an answer.

Usage:
    python scraper/diff_coverage.py --filer-ids 221 19050
    python scraper/diff_coverage.py --flagged --limit 20
    python scraper/diff_coverage.py --report
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import fetch as F
import survey_coverage as SC
import supabase_sync
from balance_snapshot import (
    COVERAGE_EVIDENCE_VERSION,
    FILER_DIGEST_SCHEMA_VERSION,
    SOURCE_FILENAME,
    evidence_is_current,
    exact_coverage_result_shape_is_valid,
    exact_evidence_identifier_is_valid,
    exact_filer_digest_version,
    parse_transaction_amount,
    transaction_filer_snapshots,
    transaction_snapshot_id,
    utc_timestamp,
)
from atomic_balance_evidence import (
    AtomicEvidenceError,
    requirements_from_ready_plan,
)
from exact_coverage_evidence import certify_exact_scope_rows
from search_budget import SearchBudget, SearchBudgetError, estimate_scope_searches

DATA_DIR = Path(__file__).parent.parent / "data"
DIFF_PATH = DATA_DIR / "coverage_diff.json"
TRANSACTION_DIR = DATA_DIR / "transactions"
SNAPSHOT_SOURCE_PATH = DATA_DIR / "aggregated" / SOURCE_FILENAME
FILERS_DIR = DATA_DIR / "aggregated" / "filers"
IDENTITY_PROGRESS_PATH = DATA_DIR / "identity_remediation_windows.json"

# Keep a small lineage behind the current usable result. Slots are reserved
# for the active paired-summary anchor and its newest same-state verdict (or
# every conflicting anchor needed to remain fail-closed); the remaining newest
# observations let operators audit changes without unbounded generated JSON.
# Current + history together retain at most this many usable observations.
USABLE_OBSERVATION_LIMIT = 8
USABLE_HISTORY_KEY = "usable_history"
USABLE_RESULT_FIELDS = (
    "filer_id", "name", "orestar", "held", "complete", "surplus", "missing",
    "superseded", "evidence_version", "checked", "collection_started_at",
    "checked_at", "transaction_snapshot_id", "filer_transaction_digest",
    "range_start", "range_end", "filer_digest_version",
    "exact_search_count", "amount_changed", "amount_checked", "live_originals",
)

# Two re-checks of committees whose withdrawn rows are moving a balance for
# every one committee measured for the first time. Any finite ratio prevents
# starvation; this one says re-checking is twice as urgent as new coverage
# without ever letting new coverage stop.
RECHECK_PER_NEW = 2

# A blocked results page is a runner-wide condition, not fifty independent
# committee failures. Two consecutive unusable committees are enough to stop
# the slice, commit what was learned, and leave a real cooldown before the next
# scheduled attempt.
MAX_CONSECUTIVE_FAILURES = 2

# These failures can plausibly clear after the shared ORESTAR/F5 cooldown.
# Proved partition mismatches and exhausted time budgets are structural for the
# current query/run and must not spend automatic verification retries.
RETRYABLE_GATE_FAILURES = frozenset({"unusable_window", "session_expired"})

# ORESTAR shows 50 rows per page and stops offering "Next" after 100 of them.
# Not documented anywhere; measured by paging filer 221 and getting exactly
# 5,000 of 5,266 rows with the button quietly disabled.
PAGE_ROWS = 50
UI_ROW_CAP = 5_000

# The failed runs made roughly 600 paginated requests through two very large
# committees before F5 stopped rendering counts for about eighteen minutes.
# Pace both searches and Next clicks below that observed burst rate. Throughput
# is secondary here: a fast partial answer is deliberately not an answer.
ORESTAR_REQUEST_DELAY = max(3.0, F.REQUEST_DELAY)

# Local dates are only a routing hint.  Keeping seeded windows below the UI cap
# leaves room for rows that ORESTAR has and our database does not, while every
# seeded window still covers a contiguous piece of the original date range.
SEED_TARGET_ROWS = 4_000

log = logging.getLogger("diff_coverage")

Window = tuple[str, date, date, str | None, str | None, str | None]


class CollectionDeadlineExceeded(RuntimeError):
    """The current committee exhausted the run budget before it was provable."""


class PartitionMismatchError(RuntimeError):
    """Disjoint child searches did not reproduce their parent's true count."""


class AtomicSnapshotDrift(AtomicEvidenceError):
    """The immutable local transaction window changed during an atomic run."""


def _current_transaction_snapshot_id() -> str | None:
    """Return the immutable fingerprint of the shards this diff will read.

    A targeted merge intentionally updates shards before the slower aggregate
    source is rebuilt. That state is valid for a post-fetch identity gate, so
    a stale source is diagnostic here rather than fatal. The automatic
    *selector* separately requires source/current equality before a balance
    discrepancy can authorize a new remediation.
    """
    local_id = transaction_snapshot_id(TRANSACTION_DIR)
    if not local_id:
        log.error("No local transaction shards are available to fingerprint.")
        return None
    if supabase_sync.sync_enabled():
        # The clean repository no longer carries generated aggregates. The
        # database cache is what the live app reads, and a failure to read it
        # must fail closed rather than silently weakening provenance.
        source = supabase_sync.require_dashboard_cache("balance_snapshot_source")
    else:
        try:
            source = json.loads(SNAPSHOT_SOURCE_PATH.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Cannot read aggregate transaction snapshot source %s: %s",
                        SNAPSHOT_SOURCE_PATH, exc)
            return local_id
    source_id = source.get("transaction_snapshot_id")
    if source_id != local_id:
        log.warning(
            "Aggregate snapshot source trails the local shards: source %s, local %s",
            source_id or "(missing)", local_id,
        )
    return local_id


def _evidence_fields(
    transaction_id: str,
    filer_digest: str,
    start: date,
    end: date,
    *,
    collection_started_at: str,
    checked_at: str | None = None,
) -> dict:
    """The provenance shared by usable and unusable exact-diff attempts."""
    instant = checked_at or utc_timestamp()
    return {
        "evidence_version": COVERAGE_EVIDENCE_VERSION,
        "checked": instant[:10],              # legacy display compatibility
        "collection_started_at": collection_started_at,
        "checked_at": instant,
        "transaction_snapshot_id": transaction_id,
        "filer_transaction_digest": filer_digest,
        "filer_digest_version": FILER_DIGEST_SCHEMA_VERSION,
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
    }


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise CollectionDeadlineExceeded("coverage-diff time budget reached")


def _remaining_timeout_ms(deadline: float | None, ceiling_ms: int) -> int:
    """A blocking-call timeout that cannot extend past ``deadline``."""
    if deadline is None:
        return ceiling_ms
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise CollectionDeadlineExceeded("coverage-diff time budget reached")
    return min(ceiling_ms, remaining)


# ---------------------------------------------------------------------------
# Local side
# ---------------------------------------------------------------------------

def _local_held_ids(
    filer_ids,
    start: date,
    end: date,
) -> dict[str, tuple[set[str], set[str]]]:
    """Compatibility view over the shared per-filer snapshot reader."""
    snapshots = transaction_filer_snapshots(TRANSACTION_DIR, filer_ids, start, end)
    return {
        fid: (row["held_ids"], row["superseded_ids"])
        for fid, row in snapshots.items()
    }


# ---------------------------------------------------------------------------
# ORESTAR side
# ---------------------------------------------------------------------------

def _parse_rows(text: str) -> dict[str, dict]:
    """Tran IDs off a results page.

    The table is tab separated:
        Tran ID | Tran Date | Status | Filer | Contributor/Payee | Sub Type | Amount

    Keyed on the leading integer and a trailing "$" amount so headers, footers
    and the site's navigation chrome cannot be mistaken for data.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = [c.strip() for c in line.split("\t")]
        if len(parts) < 7:
            continue
        tid, amount = parts[0], parts[-1]
        if not tid.isdigit():
            continue
        # ORESTAR renders negatives in accounting parentheses: "($68.50)", not
        # "-$68.50". Requiring a leading "$" therefore drops every negative row
        # silently — one Cash Balance Adjustment was enough to make filer 3865
        # collect 3,905 of 3,906 and be refused as unusable. A parser that
        # skips rows it does not recognise must be able to say so; this one
        # could not, and only the collected-vs-reported guard caught it.
        neg = amount.startswith("(") and amount.endswith(")")
        if neg:
            amount = amount[1:-1]
        if not amount.startswith("$"):
            continue
        try:
            amt = float(amount[1:].replace(",", ""))
        except ValueError:
            continue
        if neg:
            amt = -amt
        out[tid] = {"date": parts[1], "status": parts[2],
                    "payee": parts[4], "sub_type": parts[5], "amount": amt}
    return out


def _wait_for_new_rows(
    page,
    seen: set[str],
    timeout_seconds: int = 20,
    deadline: float | None = None,
) -> dict[str, dict]:
    """Wait until the results table contains at least one previously unseen ID.

    A fixed sleep after clicking Next is not a page-change guarantee. Under
    load the old, non-empty page can remain visible for several seconds; reading
    it again silently loses a page while still looking like a successful parse.
    """
    _check_deadline(deadline)
    poll_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        poll_deadline = min(poll_deadline, deadline)
    while time.monotonic() < poll_deadline:
        current = _parse_rows(page.inner_text("body"))
        if current and (not seen or set(current) - seen):
            return current
        page.wait_for_timeout(250)
    _check_deadline(deadline)
    return {}


def _window_label(window: Window) -> str:
    tran_type, start, end, amt_from, amt_to, payee_prefix = window
    bits = [f"{tran_type} {start}→{end}"]
    if amt_from is not None or amt_to is not None:
        bits.append(f"${amt_from or '0'}-{amt_to or 'max'}")
    if payee_prefix:
        bits.append(f"payee~{payee_prefix}*")
    return " ".join(bits)


def _parse_export_rows(content: bytes) -> dict[str, dict] | None:
    """Read transaction IDs, and their amounts, from an ORESTAR Excel export.

    The amount is kept because ORESTAR edits some rows in place: a lumped
    "Miscellaneous Cash Contributions $100 and under" row keeps its Tran ID,
    status and filed date while its amount grows or shrinks (Eli for Portland
    5675002 went from $340 to $830). An identity diff sees nothing wrong there,
    so the amount is the only evidence the row changed. A row whose amount
    cannot be read carries no "amount" key and is simply not compared.
    """
    if content[:4] == F._XLS_MAGIC:
        engine = "xlrd"
    elif content[:4] == F._XLSX_MAGIC:
        engine = "openpyxl"
    else:
        return None
    try:
        import pandas as pd

        frame = pd.read_excel(io.BytesIO(content), engine=engine, dtype=str)
    except Exception as exc:                               # noqa: BLE001
        log.warning("Could not parse ORESTAR Excel export (%s)", exc)
        return None
    columns = {str(column).strip().lower(): column for column in frame.columns}
    id_column = columns.get("tran id") or columns.get("transaction id")
    if id_column is None:
        log.warning("ORESTAR Excel export has no transaction-ID column")
        return None
    # Same header spellings process.py's COL_MAP accepts for the stored amount.
    amount_column = columns.get("amount") or columns.get("tran amount")
    amounts = (frame[amount_column] if amount_column is not None
               else [None] * len(frame))
    rows: dict[str, dict] = {}
    for raw_id, raw_amount in zip(frame[id_column], amounts):
        if raw_id is None or (isinstance(raw_id, float) and raw_id != raw_id):
            continue
        tran_id = str(raw_id).strip()
        if tran_id.endswith(".0") and tran_id[:-2].isdigit():
            tran_id = tran_id[:-2]
        if not tran_id.isdigit():
            continue
        amount = parse_transaction_amount(raw_amount)
        rows[tran_id] = {} if amount is None else {"amount": amount}
    return rows


def _amount_drift(
    theirs: dict[str, dict],
    held_amounts: dict[str, float | None],
) -> tuple[list[dict], int]:
    """Rows both sides hold under one Tran ID but with different amounts.

    Returns (changed, checked). ``checked`` counts only the shared rows whose
    amount was readable on both sides, so "no drift" is never claimed for rows
    that were not actually compared.
    """
    changed: list[dict] = []
    checked = 0
    if not isinstance(theirs, dict):
        return changed, checked          # bare IDs carry no amounts to compare
    for tran_id, row in theirs.items():
        if tran_id not in held_amounts:
            continue
        held = held_amounts[tran_id]
        orestar = parse_transaction_amount((row or {}).get("amount"))
        if held is None or orestar is None:
            continue
        checked += 1
        if abs(orestar - held) > 0.005:
            changed.append({"tran_id": tran_id, "held": held, "orestar": orestar})
    changed.sort(key=lambda item: (len(item["tran_id"]), item["tran_id"]))
    return changed, checked


def _export_rows(
    page,
    context,
    filer_id: str,
    label: str,
    deadline: float | None = None,
) -> dict[str, dict] | None:
    """Download the current result set once instead of paging it 50 rows at a time."""
    try:
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")
    except Exception as exc:                               # noqa: BLE001
        log.warning("Filer %s %s: could not read export token (%s)",
                    filer_id, label, exc)
        return None
    if not csrf:
        log.warning("Filer %s %s: no export token on results page", filer_id, label)
        return None

    results_url = page.url
    session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
    session_path = f";{session_match.group(1)}" if session_match else ""
    export_url = f"{F.EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"
    content = b""
    try:
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        response = F.requests.get(
            export_url,
            cookies=cookies,
            headers={"User-Agent": F.USER_AGENT, "Referer": results_url},
            timeout=max(0.001, _remaining_timeout_ms(deadline, 120_000) / 1000),
        )
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            content = response.content
    except Exception as exc:                               # noqa: BLE001
        log.warning("Filer %s %s: direct export failed (%s)", filer_id, label, exc)

    rows = _parse_export_rows(content)
    if rows is None:
        # ORESTAR occasionally rejects the cookie replay while allowing the same
        # export in the browser session.  Use Playwright's temporary download as
        # the fallback; nothing is written into the repository.
        try:
            with page.expect_download(
                timeout=_remaining_timeout_ms(deadline, 60_000)
            ) as download_info:
                try:
                    page.goto(
                        export_url,
                        wait_until="commit",
                        timeout=_remaining_timeout_ms(deadline, 60_000),
                    )
                except PlaywrightError as exc:
                    # A Content-Disposition response begins the download and
                    # aborts the document navigation. Playwright reports that
                    # successful hand-off as "Download is starting" even
                    # though expect_download captured the file.
                    if "Download is starting" not in str(exc):
                        raise
            content = Path(download_info.value.path()).read_bytes()
        except Exception as exc:                           # noqa: BLE001
            log.warning("Filer %s %s: browser export failed (%s)",
                        filer_id, label, exc)
            return None
        rows = _parse_export_rows(content)
    return rows


def _read_current_results_count(
    page,
    deadline: float | None,
    timeout_seconds: int = 20,
) -> int | None:
    """Read the already-rendering results count without issuing a new search."""
    _check_deadline(deadline)
    poll_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        poll_deadline = min(poll_deadline, deadline)
    while time.monotonic() < poll_deadline:
        text = page.inner_text(
            "body", timeout=_remaining_timeout_ms(deadline, 30_000)
        )
        match = re.search(r"([\d,]+)\s+records found", text)
        if match:
            return int(match.group(1).replace(",", ""))
        if "no records found" in text.lower():
            return 0
        page.wait_for_timeout(250)
    _check_deadline(deadline)
    return None


def _goto_tran_id_sort(
    page,
    filer_id: str,
    label: str,
    direction: str,
    expected_count: int,
    deadline: float | None,
) -> bool:
    """Sort the current exact result set by its unique transaction ID."""
    selector = (
        f'a[href*="by=RSN"][href*="srtOrder={direction}"]'
    )
    try:
        _check_deadline(deadline)
        links = page.locator(selector)
        if links.count() == 0:
            log.warning(
                "Filer %s %s: no Tran ID %s sort link on results page",
                filer_id, label, direction,
            )
            return False
        href = links.first.get_attribute("href")
        if not href:
            log.warning(
                "Filer %s %s: empty Tran ID %s sort link",
                filer_id, label, direction,
            )
            return False
        # Sorting is another ORESTAR results request.  Pace it like a search;
        # the point of this fast path is to avoid the prefix burst, not replace
        # it with a smaller unpaced burst.
        page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
        _check_deadline(deadline)
        page.goto(
            urljoin(page.url, href),
            wait_until="domcontentloaded",
            timeout=_remaining_timeout_ms(deadline, 60_000),
        )
        _check_deadline(deadline)
    except CollectionDeadlineExceeded:
        raise
    except Exception as exc:                               # noqa: BLE001
        log.warning(
            "Filer %s %s: Tran ID %s sort failed (%s)",
            filer_id, label, direction, exc,
        )
        return False

    if "secure.sos.state.or.us/orestar" not in page.url:
        log.warning(
            "Filer %s %s: Tran ID %s sort left ORESTAR (%s)",
            filer_id, label, direction, page.url,
        )
        return False
    rendered_count = _read_current_results_count(page, deadline)
    if rendered_count != expected_count:
        log.warning(
            "Filer %s %s: Tran ID %s sort rendered %s rows, expected %d",
            filer_id,
            label,
            direction,
            "unknown" if rendered_count is None else rendered_count,
            expected_count,
        )
        return False
    log.info(
        "Filer %s %s: Tran ID %s sort preserved the %d-row parent",
        filer_id, label, direction, expected_count,
    )
    return True


def _export_tran_id_extremes(
    page,
    context,
    filer_id: str,
    label: str,
    expected_count: int,
    row_cap: int,
    deadline: float | None,
) -> dict[str, dict] | None:
    """Verified subset from the low and high ends of a capped parent.

    Tran ID is unique.  Therefore, when ``expected_count <= 2 * row_cap``,
    the first ``row_cap`` IDs in ascending order plus the first ``row_cap`` in
    descending order must cover the whole result set.  Each export is still
    validated independently, and the caller still accepts only an exact union.
    """
    if expected_count > 2 * row_cap:
        return None

    merged: dict[str, dict] = {}
    for direction in ("asc", "desc"):
        if not _goto_tran_id_sort(
            page, filer_id, label, direction, expected_count, deadline
        ):
            return merged or None
        rows = _export_rows(page, context, filer_id, label, deadline)
        if rows is None or len(rows) != row_cap:
            log.warning(
                "Filer %s %s: Tran ID %s export contained %s IDs, expected %d",
                filer_id,
                label,
                direction,
                "unknown" if rows is None else len(rows),
                row_cap,
            )
            return merged or None
        merged.update(rows)
    return merged


def _collect_window(
    page,
    filer_id: str,
    start: date,
    end: date,
    tran_type: str = "ALL",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
    deadline: float | None = None,
    context=None,
) -> dict | None:
    """Every row ORESTAR returns for one window, or None if it cannot be trusted.

    None means "do not use this window", and the caller must not treat it as an
    empty result. Half a result set looks exactly like a committee that has
    fewer rows than it does, and that is how a diff manufactures surplus.
    """
    window: Window = (tran_type, start, end, amt_from, amt_to, payee_prefix)
    label = _window_label(window)

    # Recursive windows issue searches too; pacing only Next clicks leaves a
    # burst at every split boundary.
    _check_deadline(deadline)
    page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
    _check_deadline(deadline)
    try:
        reported = SC.orestar_count(
            page, filer_id, start, end, tran_type, amt_from, amt_to, payee_prefix,
            deadline=deadline,
        )
    except SC.SearchDeadlineExceeded as exc:
        raise CollectionDeadlineExceeded(str(exc)) from exc
    _check_deadline(deadline)
    if reported is None:
        log.warning("Filer %s %s: no record count read", filer_id, label)
        return None
    if reported == 0:
        return {"reported": 0, "rows": {}}
    # An export holds at most 4,999 data rows; the UI itself holds 5,000.  Split
    # at the lower limit so a completed leaf can be fetched in one request.
    row_cap = min(UI_ROW_CAP, F.ORESTAR_ROW_CAP)
    if reported > row_cap:
        return {"reported": reported, "rows": None}      # caller must split

    if context is not None:
        _check_deadline(deadline)
        rows = _export_rows(page, context, filer_id, label, deadline)
        _check_deadline(deadline)
        if rows is None or len(rows) != reported:
            log.warning("Filer %s %s: export contained %s of %d IDs — window UNUSABLE",
                        filer_id, label, "unknown" if rows is None else len(rows),
                        reported)
            return None
        return {"reported": reported, "rows": rows}

    first_page = _wait_for_new_rows(page, set(), deadline=deadline)
    if not first_page:
        log.warning("Filer %s %s: first result page never rendered", filer_id, label)
        return None

    rows: dict[str, dict] = dict(first_page)
    max_pages = (reported + PAGE_ROWS - 1) // PAGE_ROWS
    pages_read = 1
    while len(rows) < reported and pages_read < max_pages:
        _check_deadline(deadline)
        nxt = [b for b in page.query_selector_all('input[value="Next"]') if b.is_enabled()]
        if not nxt:
            break
        try:
            nxt[0].click()
            # This is both polite pacing and the minimum wait before polling the
            # content. The poll below, rather than this delay, proves progress.
            page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
            _check_deadline(deadline)
        except Exception as e:                            # noqa: BLE001
            if isinstance(e, CollectionDeadlineExceeded):
                raise
            log.warning("Filer %s %s: paging stopped (%s)", filer_id, label, e)
            return None
        current = _wait_for_new_rows(page, set(rows), deadline=deadline)
        if not current:
            log.warning("Filer %s %s: Next produced no new rows after page %d",
                        filer_id, label, pages_read)
            return None
        rows.update(current)
        pages_read += 1

    if len(rows) != reported:
        log.warning("Filer %s %s: collected %d of %d — window UNUSABLE",
                    filer_id, label, len(rows), reported)
        return None
    return {"reported": reported, "rows": rows}


def _split(start: date, end: date) -> list[tuple[date, date]]:
    """Halve a window. Returns [] when it can no longer be divided."""
    if start >= end:
        return []
    # Integer day arithmetic deliberately allows mid == start. That is the
    # correct split for a two-day window: [day one], [day two].
    mid = start + timedelta(days=(end - start).days // 2)
    return [(start, mid), (mid + timedelta(days=1), end)]


def _date_seed_windows(
    filer_id: str,
    start: date,
    end: date,
    target_rows: int = SEED_TARGET_ROWS,
) -> list[tuple[date, date]]:
    """Build complete, contiguous date windows from the local row distribution.

    The local rows decide only where to put boundaries; they never decide which
    dates to search.  The returned windows cover ``start`` through ``end`` with
    no gaps, so ORESTAR-only dates remain in scope.  Every parent and the final
    root are reconciled against ORESTAR's own count before the result is usable.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """select tran_date, count(*) from transactions
                   where filer_id = %s and tran_date between %s and %s
                   group by tran_date order by tran_date""",
                (filer_id, start, end),
            )
            date_counts = cur.fetchall()
    finally:
        conn.close()

    normalized: list[tuple[date, int]] = []
    for raw_day, raw_count in date_counts:
        day = raw_day.date() if isinstance(raw_day, datetime) else raw_day
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        normalized.append((day, int(raw_count)))
    return _build_date_seed_windows(start, end, normalized, target_rows)


def _build_date_seed_windows(
    start: date,
    end: date,
    date_counts: list[tuple[date, int]],
    target_rows: int = SEED_TARGET_ROWS,
) -> list[tuple[date, date]]:
    """Pure partition builder used by `_date_seed_windows` and its tests."""
    if sum(count for _, count in date_counts) <= target_rows:
        return []

    windows: list[tuple[date, date]] = []
    window_start = start
    running = 0
    one_day = timedelta(days=1)
    for day, count in date_counts:
        if count >= target_rows:
            if window_start < day:
                windows.append((window_start, day - one_day))
            windows.append((day, day))
            window_start = day + one_day
            running = 0
            continue
        if running and running + count > target_rows:
            windows.append((window_start, day - one_day))
            window_start = day
            running = count
        else:
            running += count
    if window_start <= end:
        windows.append((window_start, end))
    return windows if len(windows) > 1 else []


def _date_windows_cover(
    start: date, end: date, windows: list[tuple[date, date]]
) -> bool:
    expected = start
    for window_start, window_end in windows:
        if window_start != expected or window_end < window_start:
            return False
        expected = window_end + timedelta(days=1)
    return expected == end + timedelta(days=1)


def _is_prefix_partition(parent: Window, children: list[Window]) -> bool:
    """Whether ``children`` only add contributor-prefix filters to ``parent``."""
    return bool(children) and parent[5] is None and all(
        child[:5] == parent[:5] and child[5] is not None for child in children
    )


def _order_children_by_local_cost(
    filer_id: str, children: list[Window]
) -> list[Window]:
    """Run cheap disjoint siblings first and leave the request-heavy one last.

    Local counts are only a scheduling hint.  They never remove a child or
    participate in reconciliation, and an unavailable count preserves the
    narrowing ladder's original order.
    """
    costs: list[tuple[int, int, Window]] = []
    for index, child in enumerate(children):
        cost = F._held_rows(filer_id, *child)
        if cost is None:
            return children
        costs.append((int(cost), index, child))
    ordered = [child for _cost, _index, child in sorted(costs)]
    if ordered != children:
        log.info(
            "Filer %s: ordered %d child windows cheapest-first; "
            "largest local window (%d rows) runs last",
            filer_id, len(children), max(cost for cost, _index, _child in costs),
        )
    return ordered


def _collect_tree(
    page,
    context,
    filer_id: str,
    window: Window,
    deadline: float | None,
    depth: int,
    seed_windows: list[tuple[date, date]] | None,
) -> dict | None:
    """Collect one window and prove every descendant reconciles to its parent."""
    _check_deadline(deadline)
    tran_type, start, end, amt_from, amt_to, payee_prefix = window
    result = _collect_window(
        page, filer_id, start, end, tran_type, amt_from, amt_to, payee_prefix,
        deadline, context,
    )
    if result is None or result["rows"] is not None:
        return result

    children: list[Window]
    if depth == 0:
        if seed_windows is None:
            try:
                _check_deadline(deadline)
                seed_windows = _date_seed_windows(filer_id, start, end)
                _check_deadline(deadline)
            except CollectionDeadlineExceeded:
                raise
            except Exception as exc:                       # noqa: BLE001
                log.warning("Filer %s: local date seeding failed (%s); using ladder",
                            filer_id, exc)
                seed_windows = []
        if seed_windows:
            if not _date_windows_cover(start, end, seed_windows):
                raise PartitionMismatchError(
                    f"date seeds do not cover {start}→{end}"
                )
            children = [("ALL", a, b, None, None, None) for a, b in seed_windows]
        else:
            children = F._narrow_filer(*window)
    else:
        children = F._narrow_filer(*window)

    if not children:
        raise PartitionMismatchError(
            f"{_window_label(window)} has {result['reported']} rows after every "
            "narrowing dimension is spent"
        )

    prefix_partition = _is_prefix_partition(window, children)
    if context is not None and not prefix_partition:
        children = _order_children_by_local_cost(filer_id, children)

    log.info("Filer %s %s: %d rows, over the %d cap — narrowing into %d",
             filer_id, _window_label(window), result["reported"], UI_ROW_CAP,
             len(children))

    parent_reported = result["reported"]
    parent_sample: dict[str, dict] = {}
    row_cap = min(UI_ROW_CAP, F.ORESTAR_ROW_CAP)
    if context is not None and prefix_partition:
        # Prefixes are the only available split for a single-day/single-amount
        # batch, but dozens of count+export pairs can trip F5 near the end of
        # the alphabet.  Before walking them, sort the exact parent by its
        # unique Tran ID in both directions.  Two capped exports cover any
        # parent up to twice the export cap; otherwise each is still genuine
        # overlap evidence for the prefix fallback.
        #
        # This is a proof, not an estimate: every member of the union came from
        # this exact parent or one of its stricter filters, and a subset of an
        # N-item set with N unique members is the full set.  Anything short,
        # oversized, duplicated across processed children, or otherwise
        # inconsistent is still refused.
        _check_deadline(deadline)
        parent_sample = (
            _export_tran_id_extremes(
                page,
                context,
                filer_id,
                _window_label(window),
                parent_reported,
                row_cap,
                deadline,
            )
            or {}
        )
        _check_deadline(deadline)
        if parent_sample:
            if len(parent_sample) > parent_reported:
                raise PartitionMismatchError(
                    f"{_window_label(window)} opposite Tran ID exports have "
                    f"{len(parent_sample)} unique IDs, parent reports "
                    f"{parent_reported}"
                )
            log.info(
                "Filer %s %s: retained %d IDs from opposite Tran ID exports",
                filer_id, _window_label(window), len(parent_sample),
            )
            if len(parent_sample) == parent_reported:
                log.info(
                    "Filer %s %s: reconciled %d IDs from opposite Tran ID "
                    "exports; prefix leaves not needed",
                    filer_id, _window_label(window), parent_reported,
                )
                return {"reported": parent_reported, "rows": parent_sample}

        if not parent_sample:
            sample = _export_rows(
                page, context, filer_id, _window_label(window), deadline
            )
            _check_deadline(deadline)
            if sample is not None and len(sample) == row_cap:
                parent_sample = sample
                log.info(
                    "Filer %s %s: retained %d capped parent IDs as overlap evidence",
                    filer_id, _window_label(window), len(parent_sample),
                )
            else:
                log.warning(
                    "Filer %s %s: capped parent export contained %s IDs, expected "
                    "%d; ignoring the sample and requiring every child",
                    filer_id,
                    _window_label(window),
                    "unknown" if sample is None else len(sample),
                    row_cap,
                )

        if parent_sample and len(parent_sample) < row_cap:
            log.warning(
                "Filer %s %s: overlap evidence has only %d IDs, expected at "
                "least %d; ignoring it and requiring every child",
                filer_id, _window_label(window), len(parent_sample), row_cap,
            )
            parent_sample = {}

    child_rows: dict[str, dict] = {}
    child_reported = 0
    for child_index, child in enumerate(children, start=1):
        _check_deadline(deadline)
        part = _collect_tree(
            page, context, filer_id, child, deadline, depth + 1, seed_windows=[]
        )
        if part is None:
            return None
        child_reported += part["reported"]
        child_rows.update(part["rows"])
        if len(child_rows) != child_reported:
            raise PartitionMismatchError(
                f"{_window_label(window)} processed children report "
                f"{child_reported} rows / {len(child_rows)} unique IDs"
            )

        if parent_sample:
            merged = dict(parent_sample)
            merged.update(child_rows)
            if len(merged) > parent_reported:
                raise PartitionMismatchError(
                    f"{_window_label(window)} overlap evidence has "
                    f"{len(merged)} unique IDs, parent reports {parent_reported}"
                )
            if len(merged) == parent_reported:
                log.info(
                    "Filer %s %s: reconciled %d IDs after %d/%d prefix leaves",
                    filer_id, _window_label(window), parent_reported,
                    child_index, len(children),
                )
                return {"reported": parent_reported, "rows": merged}

    merged = dict(parent_sample)
    merged.update(child_rows)
    if len(merged) != parent_reported or (
        not parent_sample and child_reported != parent_reported
    ):
        raise PartitionMismatchError(
            f"{_window_label(window)} children report {child_reported} rows / "
            f"{len(child_rows)} unique child IDs / {len(merged)} with overlap "
            f"evidence, parent reports {parent_reported}"
        )
    return {"reported": parent_reported, "rows": merged}


def orestar_ids(
    page,
    filer_id: str,
    start: date,
    end: date,
    depth: int = 0,
    deadline: float | None = None,
    seed_windows: list[tuple[date, date]] | None = None,
    context=None,
    raise_partition_error: bool = False,
) -> dict | None:
    """Every ORESTAR Tran ID, or None unless the complete tree reconciles."""
    root: Window = ("ALL", start, end, None, None, None)
    try:
        result = _collect_tree(
            page, context, filer_id, root, deadline, depth, seed_windows=seed_windows
        )
    except PartitionMismatchError as exc:
        log.error("Filer %s: partition UNUSABLE (%s)", filer_id, exc)
        if raise_partition_error:
            raise
        return None
    return None if result is None else result["rows"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _costs(filer_ids: list[str]) -> dict[str, int]:
    """Rows we hold per committee — a good proxy for what measuring one costs.

    Every window over the cap costs extra searches and exports across the
    narrowing tree, so cost is superlinear in size. Our own row count is close
    enough to rank by and free to obtain.
    """
    if not filer_ids:
        return {}
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""select filer_id, count(*) from transactions
                           where filer_id = any(%s) group by 1""", (list(filer_ids),))
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def _prioritise(targets: list[dict], entries: dict) -> list[dict]:
    """Order committees so the scarce request budget lands where it matters.

    A diff going stale is not uniformly harmful, and that decides the order.

    Where withdrawn rows are currently being SUBTRACTED from a balance, an
    out-of-date answer actively moves a number: if a filer re-files a
    transaction ORESTAR starts counting it again, and until we re-check we keep
    excluding it. Those want re-checking soonest.

    A committee never measured costs only the information we do not yet have.
    One measured clean has nothing being subtracted, so a stale answer there
    changes no figure at all, and it goes last.

    But a strict priority order STARVES. Re-checking a committee only moves it
    to the back of its own group rather than out of it, so the withdrawn group
    never empties — and once it grows past what a day of runs can process,
    committees never measured are never reached, permanently. The survey puts
    roughly 126 of 691 committees in that group, so that is the steady state,
    not an edge case.

    So the two urgent groups are INTERLEAVED at a fixed ratio instead: two
    re-checks for every one first measurement. Both make progress regardless of
    how large either grows, and the ratio decides how fast — not whether.

    Deliberately NOT an expiry rule. An earlier draft ignored the withdrawn
    list once it was older than the summary being compared, which would have
    re-included Plumbers & Steamfitters PAC's sixteen correctly withdrawn rows
    — re-flagging it for $32,284.04 — because of the calendar, with no evidence
    anything had changed. Age decides what to RE-MEASURE. Only a measurement
    changes an answer.
    """
    recheck, failed, fresh, lazy = [], [], [], []
    today = date.today().isoformat()
    for t in targets:
        e = entries.get(str(t["filer_id"]))
        if not e:
            fresh.append(t)
            continue
        if e.get("complete") is None:
            attempted = e.get("last_attempt") or e.get("checked") or ""
            # A chained successor is not a cooldown. Retrying the same refusal
            # again minutes later recreates the cascade under a new run ID.
            # Manual targeted runs without --recheck can still force a retry;
            # the rolling sweep waits until the next day.
            if attempted >= today:
                continue
            failed.append((attempted, t))
            continue
        # A chain is several slices of one sweep, not permission to remeasure
        # the same expensive committee in every slice. checked is the last
        # usable measurement; last_attempt also suppresses a same-day retry
        # after a failed recheck whose prior evidence was preserved.
        if (e.get("last_attempt") or e.get("checked") or "") >= today:
            continue
        if e.get("surplus"):
            recheck.append((e.get("checked") or "", t))
        else:
            lazy.append((e.get("checked") or "", t))
    # Order by COST inside each tier, cheapest first.
    #
    # The interleave above balances committee COUNTS, and that is not the same
    # as balancing work. Friends of Tina Kotek is 29,268 rows and needs fifteen
    # recursive splits; the median flagged committee is eighty rows and needs
    # one search. Two re-checks of the giants is not "two committees" of
    # budget, it is the entire budget — and because they carry withdrawn rows
    # they sort first every single day.
    #
    # Measured, not assumed: a 57-minute run spent every minute inside filers
    # 4792 and 19050, measured three committees, tripped the F5 block and
    # stopped the chain. At three per run the remaining 620 need roughly 200
    # days. The count-based interleave fixed starvation on the count axis and
    # left it untouched on the cost axis.
    #
    # Cheapest-first inverts that: 647 of 668 flagged committees fit in one
    # window with no splitting at all.
    _cost = _costs([str(t["filer_id"]) for t in targets])
    def _c(t):
        return _cost.get(str(t["filer_id"]), 0)
    recheck.sort(key=lambda x: (x[0], _c(x[1])))   # oldest evidence, then cheapest
    failed.sort(key=lambda x: (x[0], _c(x[1])))
    fresh.sort(key=_c)                             # pure coverage: cheapest first
    lazy.sort(key=lambda x: (x[0], _c(x[1])))

    # Recovery attempts and first measurements alternate. One deterministic
    # bad filer cannot starve new coverage, while transient F5 casualties are
    # still retried on the next eligible daily run after a real cooldown.
    work: list[dict] = []
    fi = ni = 0
    fq = [t for _, t in failed]
    while fi < len(fq) or ni < len(fresh):
        if fi < len(fq):
            work.append(fq[fi]); fi += 1
        if ni < len(fresh):
            work.append(fresh[ni]); ni += 1

    out: list[dict] = []
    ri = wi = 0
    rq = [t for _, t in recheck]
    while ri < len(rq) or wi < len(work):
        for _ in range(RECHECK_PER_NEW):      # two re-checks...
            if ri < len(rq):
                out.append(rq[ri]); ri += 1
        if wi < len(work):                    # ...then one recovery/new item
            out.append(work[wi]); wi += 1
    out = out + [t for _, t in lazy]

    # At most one over-cap committee per slice.
    #
    # A committee past UI_ROW_CAP cannot be answered by a single search: every
    # window is split and re-paged, so one of them can cost more requests than
    # a hundred ordinary committees and is the most likely thing to trip F5.
    # Letting one through per run keeps the giants genuinely re-checked —
    # they are where withdrawn rows actually live — without letting them own
    # the budget. The rest move to the back rather than being dropped.
    big = [t for t in out if _c(t) > UI_ROW_CAP]
    small = [t for t in out if _c(t) <= UI_ROW_CAP]
    return (big[:1] + small + big[1:]) if big else out


def _strict_utc_timestamp(value) -> float | None:
    """Parse a precise UTC instant without promoting legacy dates."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if (parsed.tzinfo is None
                or parsed.utcoffset() != timezone.utc.utcoffset(parsed)):
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _history_timestamp(row: dict) -> float:
    """Sortable query-start instant for a structured observation."""
    timestamp = _strict_utc_timestamp(row.get("collection_started_at"))
    return timestamp if timestamp is not None else float("-inf")


def _history_order_key(row: dict) -> tuple[int, float, float]:
    """Query start is authoritative; completion only breaks safe ties.

    Legacy date-only observations remain retained for UI/history, but never
    outrank a structured query merely because their display date is newer.
    """
    started = _strict_utc_timestamp(row.get("collection_started_at"))
    completed = _strict_utc_timestamp(row.get("checked_at"))
    if started is not None:
        return 1, started, completed if completed is not None else float("-inf")
    legacy = _strict_utc_timestamp(row.get("checked_at"))
    if legacy is None:
        try:
            legacy = datetime.fromisoformat(str(row.get("checked"))).replace(
                tzinfo=timezone.utc,
            ).timestamp()
        except (TypeError, ValueError, OverflowError):
            legacy = float("-inf")
    return 0, legacy, legacy


def _structured_usable_record_is_valid(row: dict, filer_id: str) -> bool:
    """Validate persisted successful evidence independently of a capture."""
    started = _strict_utc_timestamp(row.get("collection_started_at"))
    completed = _strict_utc_timestamp(row.get("checked_at"))
    try:
        range_start = date.fromisoformat(str(row.get("range_start") or ""))
        range_end = date.fromisoformat(str(row.get("range_end") or ""))
    except (TypeError, ValueError):
        return False
    return (
        exact_coverage_result_shape_is_valid(row)
        and str(row.get("filer_id") or "") == filer_id
        and row.get("evidence_version") == COVERAGE_EVIDENCE_VERSION
        and exact_evidence_identifier_is_valid(
            row.get("transaction_snapshot_id")
        )
        and started is not None
        and completed is not None
        and started <= completed
        and range_start <= range_end
    )


def _active_identity_range_ends() -> dict[str, str]:
    """Physical filer -> frozen range end for live merge-only repairs."""
    try:
        rows = json.loads(IDENTITY_PROGRESS_PATH.read_text()) \
            if IDENTITY_PROGRESS_PATH.exists() else []
    except (OSError, json.JSONDecodeError):
        rows = []
    roots: dict[str, str] = {}
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        if (not isinstance(key, list) or len(key) != 7 or key[0] != "ALL"
                or key[3:6] != ["None", "None", "None"]):
            continue
        fid = str(key[-1]).strip()
        try:
            end = date.fromisoformat(str(key[2])).isoformat()
        except (TypeError, ValueError):
            continue
        if fid and end > roots.get(fid, ""):
            roots[fid] = end
    return roots


def _usable_history_record(row: dict | None) -> dict | None:
    """Copy one usable result without recursive or attempt-only metadata."""
    if not isinstance(row, dict) or type(row.get("complete")) is not bool:
        return None
    record = {}
    for key in USABLE_RESULT_FIELDS:
        if key not in row:
            continue
        value = row[key]
        record[key] = list(value) if isinstance(value, list) else value
    return record


def _usable_record_identity(record: dict) -> str:
    """Deduplicate observations without letting a rename consume a slot."""
    stable = {key: value for key, value in record.items() if key != "name"}
    return json.dumps(stable, sort_keys=True, separators=(",", ":"))


def _entry_observations(entry: dict | None, filer_id: str) -> list[dict]:
    """Flatten current/history records and enforce their physical owner."""
    if not isinstance(entry, dict):
        return []
    raw = [entry]
    history = entry.get(USABLE_HISTORY_KEY) or []
    if isinstance(history, list):
        raw.extend(history)
    out = []
    for row in raw:
        record = _usable_history_record(row)
        if record is not None and str(record.get("filer_id") or "") == filer_id:
            out.append(record)
    return out


def _active_paired_requirements() -> dict[str, dict]:
    """Unique automatic scope owner -> paired capture provenance.

    A physical filer claimed by more than one generated detail is ambiguous
    and cannot drive automation, so it has no anchor to pin here. This keeps
    the history hard-bounded while preserving the one active fingerprint for
    every scope that the selector could actually authorize.
    """
    claims: dict[str, list[tuple[str, tuple[str, ...], str, float]]] = {}
    active_ends = _active_identity_range_ends()
    local_paths = list(FILERS_DIR.glob("*.json"))
    if supabase_sync.sync_enabled():
        details = [
            (f"{row.get('slug') or row.get('filer_id')}.json", row)
            for row in supabase_sync.require_filer_comparison_details()
        ]
    elif local_paths:
        details = []
        for path in local_paths:
            try:
                detail = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(detail, dict):
                details.append((path.name, detail))
    else:
        raise RuntimeError(
            "No filer comparison details are available locally or from Supabase"
        )
    for owner, detail in details:
        comparison = detail.get("orestar_comparison") or {}
        detail_id_values = detail.get("filer_ids")
        captured_id_values = comparison.get("filer_ids")
        ids = tuple(sorted({
            text for fid in detail_id_values
            if (text := str(fid or "").strip())
        })) if isinstance(detail_id_values, list) else ()
        captured_ids = tuple(sorted({
            text for fid in captured_id_values
            if (text := str(fid or "").strip())
        })) if isinstance(captured_id_values, list) else ()
        fingerprint = comparison.get("app_transaction_snapshot_id")
        captured_at = comparison.get("captured_at")
        try:
            captured_at = float(captured_at)
        except (TypeError, ValueError, OverflowError):
            continue
        if (comparison.get("status") != "paired" or not ids
                or captured_ids != ids
                or not exact_evidence_identifier_is_valid(fingerprint)):
            continue
        claim = (owner, ids, fingerprint, captured_at)
        for fid in ids:
            claims.setdefault(fid, []).append(claim)
    requirements = {
        fid: {
            "transaction_snapshot_id": filer_claims[0][2],
            "captured_at": filer_claims[0][3],
            "scope_ids": list(filer_claims[0][1]),
        }
        for fid, filer_claims in claims.items()
        if len(filer_claims) == 1
    }
    for fid, requirement in requirements.items():
        members = requirement["scope_ids"]
        if any(requirements.get(member, {}).get("scope_ids") != members
               for member in members):
            continue
        ends = {active_ends[member] for member in members if member in active_ends}
        # Every member receives the identical scope-level preference. One live
        # member is enough to keep its frozen range; conflicting roots are
        # explicitly marked so pruning cannot pretend there is one safe lane.
        requirement["active_range_end"] = next(iter(ends)) if len(ends) == 1 else None
        requirement["active_range_conflict"] = len(ends) > 1
    return requirements


def _precise_usable_history_record(
    row: dict,
    requirement: dict,
    *,
    filer_id: str | None = None,
) -> bool:
    """Whether a stored result can participate in an automation lineage."""
    if not exact_coverage_result_shape_is_valid(row):
        return False
    if (filer_id is not None and str(row.get("filer_id") or "") != filer_id):
        return False
    fingerprint = row.get("transaction_snapshot_id")
    if not exact_evidence_identifier_is_valid(fingerprint):
        return False
    try:
        capture_day = datetime.fromtimestamp(
            requirement["captured_at"], tz=timezone.utc,
        ).date()
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return evidence_is_current(
        row,
        requirement["captured_at"],
        require_precise=True,
        require_collection_started=True,
        strictly_after=True,
        range_start=date(2006, 1, 1),
        minimum_range_end=capture_day,
    )


def _bounded_usable_history(
    rows,
    *,
    active_requirement: dict | None = None,
    filer_id: str | None = None,
    anchor_bounds: tuple[str, str] | None = None,
) -> list[dict]:
    """Newest results plus the active anchor and its newest ORESTAR verdict."""
    unique = {}
    for row in rows:
        record = _usable_history_record(row)
        if record is None:
            continue
        identity = _usable_record_identity(record)
        unique.setdefault(identity, record)
    ordered = sorted(unique.values(), key=_history_order_key, reverse=True)
    pinned = []
    if active_requirement:
        fingerprint = active_requirement.get("transaction_snapshot_id")
        valid_anchors = [
            row for row in ordered
            if row.get("transaction_snapshot_id") == fingerprint
            and _precise_usable_history_record(
                row, active_requirement, filer_id=filer_id,
            )
            and (anchor_bounds is None or (
                row.get("range_start"), row.get("range_end")
            ) == anchor_bounds)
        ]
        if valid_anchors and anchor_bounds is None:
            # No common multi-ID range exists yet. Pin the newest anchor bound
            # for this member; ordinary recency slots retain other candidates
            # until a common lane forms on a later member write.
            selected_bounds = max(
                {
                    (row.get("range_start"), row.get("range_end"))
                    for row in valid_anchors
                },
                key=lambda bounds: max(
                    _history_order_key(row) for row in valid_anchors
                    if (row.get("range_start"), row.get("range_end")) == bounds
                ),
            )
            valid_anchors = [
                row for row in valid_anchors
                if (row.get("range_start"), row.get("range_end"))
                == selected_bounds
            ]
        # Preserve the referenced record for UI/history even during rollout
        # when no old record has the new collection-start field. It cannot
        # authorize anything until a valid anchor exists.
        if valid_anchors:
            # A global fingerprint, exact range and algorithm imply one
            # per-filer digest. If corrupt history claims more than one, retain
            # every conflicting lane so the selector continues to see and
            # reject the ambiguity instead of aging it out.
            anchor_lanes = {
                (
                    row.get("filer_transaction_digest"),
                    exact_filer_digest_version(row),
                    row.get("range_start"),
                    row.get("range_end"),
                )
                for row in valid_anchors
            }
            for lane in sorted(anchor_lanes):
                lane_anchors = [
                    row for row in valid_anchors
                    if (
                        row.get("filer_transaction_digest"),
                        exact_filer_digest_version(row),
                        row.get("range_start"),
                        row.get("range_end"),
                    ) == lane
                ]
                anchor = max(lane_anchors, key=_history_order_key)
                if all(anchor is not item for item in pinned):
                    pinned.append(anchor)
                lineage_candidates = [
                    row for row in ordered
                    if _precise_usable_history_record(
                        row, active_requirement, filer_id=filer_id,
                    )
                    and (
                        row.get("filer_transaction_digest"),
                        exact_filer_digest_version(row),
                        row.get("range_start"),
                        row.get("range_end"),
                    ) == lane
                ]
                newest_started = max(
                    _history_timestamp(row) for row in lineage_candidates
                )
                for lineage in lineage_candidates:
                    if (_history_timestamp(lineage) == newest_started
                            and all(lineage is not item for item in pinned)):
                        pinned.append(lineage)
        else:
            legacy_anchor = next((
                row for row in ordered
                if row.get("transaction_snapshot_id") == fingerprint
                and (anchor_bounds is None or (
                    row.get("range_start"), row.get("range_end")
                ) == anchor_bounds)
            ), None)
            if legacy_anchor is not None:
                pinned.append(legacy_anchor)
    if len(pinned) > USABLE_OBSERVATION_LIMIT:
        raise ValueError("too many tied active-lineage observations to bound safely")
    selected = pinned + [
        row for row in ordered if all(row is not item for item in pinned)
    ][:USABLE_OBSERVATION_LIMIT - len(pinned)]
    return sorted(selected, key=_history_order_key, reverse=True)


def _common_scope_anchor_bounds(
    entries: dict,
    result: dict,
    active_requirements: dict[str, dict],
) -> tuple[str, str] | None:
    """Newest exact range anchored for every member of one canonical scope."""
    fid = str(result["filer_id"])
    requirement = active_requirements.get(fid)
    if not requirement:
        return None
    members = [str(member) for member in requirement.get("scope_ids") or []]
    if not members:
        return None
    by_member = {}
    for member in members:
        member_requirement = active_requirements.get(member)
        if member_requirement != requirement:
            return None
        observations = _entry_observations(entries.get(member), member)
        if member == fid:
            incoming = _usable_history_record(result)
            if incoming is not None:
                observations.append(incoming)
        anchors = [
            row for row in observations
            if row.get("transaction_snapshot_id")
            == requirement["transaction_snapshot_id"]
            and _precise_usable_history_record(
                row, requirement, filer_id=member,
            )
        ]
        by_member[member] = anchors
    common = set.intersection(*(
        {(row.get("range_start"), row.get("range_end")) for row in anchors}
        for anchors in by_member.values()
    ))
    if not common:
        return None
    active_end = requirement.get("active_range_end")
    active_bounds = (date(2006, 1, 1).isoformat(), active_end) \
        if active_end else None
    if active_bounds in common:
        return active_bounds
    return max(
        common,
        key=lambda bounds: (
            min(max(
                _history_order_key(row)
                for row in by_member[member]
                if (row.get("range_start"), row.get("range_end")) == bounds
            ) for member in members),
            bounds,
        ),
    )


def _store_usable_result(
    entries: dict,
    result: dict,
    *,
    active_requirements: dict[str, dict] | None = None,
) -> dict:
    """Replace the current result without losing its paired-capture lineage."""
    fid = str(result["filer_id"])
    active_requirements = active_requirements or {}
    active_requirement = active_requirements.get(fid)
    prior = entries.get(fid) or {}
    history = prior.get(USABLE_HISTORY_KEY) or []
    if not isinstance(history, list):
        history = []
    prior_record = _usable_history_record(prior)
    current_record = _usable_history_record(result)
    if (current_record is None
            or not _structured_usable_record_is_valid(current_record, fid)):
        raise ValueError(f"usable result for filer {fid} has invalid provenance")
    candidates = [
        *history,
        *([prior_record] if prior_record else []),
        *([current_record] if current_record else []),
    ]
    candidates = [
        row for row in candidates if str(row.get("filer_id") or "") == fid
    ]
    if not candidates:
        raise ValueError(f"usable result for filer {fid} has no owned record")
    projected_current = max(candidates, key=_history_order_key)
    anchor_bounds = _common_scope_anchor_bounds(
        entries, result, active_requirements,
    )
    # While a multi-ID scope is being repaired one member may acquire the
    # active-range anchor before the rest. Keep that seed rather than evicting
    # it before the later member is re-diffed and the common lane can form.
    preferred_end = (active_requirement or {}).get("active_range_end")
    preferred_bounds = (date(2006, 1, 1).isoformat(), preferred_end) \
        if preferred_end else None
    if preferred_bounds is not None and any(
        row.get("transaction_snapshot_id")
        == active_requirement.get("transaction_snapshot_id")
        and (row.get("range_start"), row.get("range_end")) == preferred_bounds
        and _precise_usable_history_record(
            row, active_requirement, filer_id=fid,
        )
        for row in candidates
    ):
        anchor_bounds = preferred_bounds
    kept = _bounded_usable_history(
        candidates,
        active_requirement=active_requirement,
        filer_id=fid,
        anchor_bounds=anchor_bounds,
    )
    current_identity = _usable_record_identity(projected_current)
    kept = [
        row for row in kept
        if _usable_record_identity(row) != current_identity
    ]
    if len(kept) >= USABLE_OBSERVATION_LIMIT:
        # All bounded slots were needed for fail-closed pins and did not
        # include the projected current observation. Never exceed the bound or
        # discard an ambiguity merely to make room for a newer unrelated lane.
        raise ValueError(
            "active evidence pins leave no bounded slot for current result"
        )
    stored = dict(projected_current)
    if kept:
        stored[USABLE_HISTORY_KEY] = kept
    entries[fid] = stored
    return stored


def _load() -> dict:
    if not DIFF_PATH.exists():
        return {}
    try:
        return {e["filer_id"]: e for e in json.loads(DIFF_PATH.read_text())}
    except Exception:                                     # noqa: BLE001
        return {}


def _load_atomic_entries() -> dict[str, dict]:
    """Load auxiliary evidence without the legacy lossy fallback.

    Ordinary reporting historically treated malformed state as empty. An
    atomic writer cannot: saving after that fallback would erase every prior
    observation. Missing state is a valid empty start; corrupt or duplicate
    state is a hard refusal.
    """
    if not DIFF_PATH.exists():
        return {}
    try:
        rows = json.loads(DIFF_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicEvidenceError(
            f"Could not read existing exact evidence {DIFF_PATH}: {exc}"
        ) from exc
    if not isinstance(rows, list):
        raise AtomicEvidenceError("Existing exact evidence is not a list")
    entries: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise AtomicEvidenceError("Existing exact evidence has a non-object row")
        raw_fid = str(row.get("filer_id") or "")
        fid = raw_fid.strip()
        if not fid or fid != raw_fid or not fid.isdigit():
            raise AtomicEvidenceError(
                "Existing exact evidence has an invalid filer ID"
            )
        if fid in entries:
            raise AtomicEvidenceError(
                f"Existing exact evidence duplicates filer {fid}"
            )
        entries[fid] = row
    return entries


def _save(entries: dict) -> None:
    DIFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(entries.values(), key=lambda e: -(len(e.get("surplus") or [])))
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=DIFF_PATH.parent,
            prefix=f".{DIFF_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(rows, handle, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, DIFF_PATH)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def _target_name(entries: dict, target: dict) -> str:
    """Keep a known committee name when a targeted run supplies only its ID."""
    fid = str(target["filer_id"])
    return target.get("name") or (entries.get(fid) or {}).get("name", "")


def _record_failure(
    entries: dict,
    target: dict,
    reason: str,
    *,
    transaction_id: str | None = None,
    filer_digest: str | None = None,
    start: date | None = None,
    end: date | None = None,
    collection_started_at: str | None = None,
    attempted_at: str | None = None,
) -> dict:
    """Record an unusable attempt without destroying earlier usable evidence."""
    fid = str(target["filer_id"])
    prior = entries.get(fid) or {}
    entry = dict(prior)
    instant = attempted_at or utc_timestamp()
    attempt_fields = {
        "last_attempt": instant[:10],
        "last_attempt_collection_started_at": collection_started_at,
        "last_attempt_at": instant,
        "last_attempt_transaction_snapshot_id": transaction_id,
        "last_attempt_filer_transaction_digest": filer_digest,
        "last_attempt_filer_digest_version": FILER_DIGEST_SCHEMA_VERSION,
        "last_attempt_range_start": start.isoformat() if start else None,
        "last_attempt_range_end": end.isoformat() if end else None,
    }
    if prior.get("complete") is None:
        # A refusal is explicitly unknown, so its timestamp cannot certify
        # completeness even though it records when the attempt occurred. New
        # unknown rows carry the same exact provenance as usable measurements;
        # ``complete: null`` remains the fail-closed discriminator.
        entry.update({"filer_id": fid, "complete": None})
        if (transaction_id and filer_digest and start and end
                and collection_started_at):
            entry.update(_evidence_fields(
                transaction_id,
                filer_digest,
                start,
                end,
                collection_started_at=collection_started_at,
                checked_at=instant,
            ))
        else:
            # Compatibility for direct callers/tests that do not supply a
            # snapshot. Production refuses to start without one.
            entry["checked_at"] = instant
    else:
        entry.setdefault("filer_id", fid)
    entry["name"] = _target_name(entries, target)
    entry.update(attempt_fields)
    entry["last_failure"] = reason
    entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
    entries[fid] = entry
    return entry


def report() -> int:
    entries = _load()
    if not entries:
        log.error("No %s yet — run the diff first.", DIFF_PATH)
        return 1
    rows = list(entries.values())
    ok = [r for r in rows if r.get("complete") is not None]
    sur = [r for r in ok if r.get("surplus")]
    mis = [r for r in ok if r.get("missing")]
    sup = [r for r in ok if r.get("superseded")]
    drift = [r for r in ok if r.get("amount_changed")]
    live = [r for r in ok if r.get("live_originals")]
    unpriced = [r for r in ok if "amount_checked" not in r]
    clean = [r for r in ok if not r.get("surplus") and not r.get("missing")
             and not r.get("amount_changed")]
    print()
    print(f"  committees diffed          : {len(ok):,}")
    print(f"  exact match                : {len(clean):,}")
    print(f"  hold rows ORESTAR does not : {len(sur):,}   "
          f"{sum(len(r['surplus']) for r in sur):,} rows")
    print(f"  missing rows ORESTAR has   : {len(mis):,}   "
          f"{sum(len(r['missing']) for r in mis):,} rows")
    print(f"  superseded (correctly gone): {len(sup):,}   "
          f"{sum(len(r['superseded']) for r in sup):,} rows")
    print(f"  amounts changed on ORESTAR : {len(drift):,}   "
          f"{sum(len(r['amount_changed']) for r in drift):,} rows   "
          f"${sum(i['orestar'] - i['held'] for r in drift for i in r['amount_changed']):+,.2f}")
    print(f"  live amended originals     : {len(live):,}   "
          f"{sum(len(r['live_originals']) for r in live):,} rows "
          "(ORESTAR still counts them; kept)")
    if unpriced:
        print(f"  amounts never compared     : {len(unpriced):,}   "
              "(diffed before amounts were recorded)")
    # Progress against the set that actually matters.
    #
    # The rolling re-check has no natural finish — by design, since a committee
    # measured last week can change tomorrow. But "diff every filer with a
    # remaining discrepancy" DOES have one, and without this the only way to
    # know whether it had been reached was to count JSON entries by hand.
    try:
        flagged = {str(f["filer_id"]) for f in SC._flagged_committees()}
    except Exception:                                     # noqa: BLE001
        flagged = set()
    if flagged:
        usable_ids = {f for f, e in entries.items() if e.get("complete") is not None}
        seen = flagged & usable_ids
        todo = flagged - usable_ids
        stale = sorted((e.get("checked") or "") for f, e in entries.items()
                       if f in seen and e.get("checked"))
        print(f"\n  flagged committees         : {len(flagged):,}")
        print(f"    measured at least once   : {len(seen):,}  ({len(seen)/len(flagged)*100:.0f}%)")
        print(f"    never measured           : {len(todo):,}")
        if stale:
            print(f"    oldest measurement       : {stale[0]}")

    failed = [r for r in rows if r.get("complete") is None]
    if failed:
        print(f"  could not be diffed        : {len(failed):,}  "
              f"(windows incomplete — NOT counted as clean)")
    if sur:
        print("\n  largest surplus:")
        for r in sorted(sur, key=lambda r: -len(r["surplus"]))[:15]:
            print(f"    {r['filer_id']:<8}{r.get('name','')[:36]:<36}"
                  f"+{len(r['surplus']):<5}(ORESTAR {r['orestar']:,} held {r['held']:,})")
    if drift:
        print("\n  largest amount changes:")
        for r in sorted(drift, key=lambda r: -sum(
                abs(i["orestar"] - i["held"]) for i in r["amount_changed"]))[:15]:
            moved = sum(i["orestar"] - i["held"] for i in r["amount_changed"])
            print(f"    {r['filer_id']:<8}{r.get('name','')[:36]:<36}"
                  f"{len(r['amount_changed']):<5}rows  ${moved:+,.2f}")
    return 0


# ---------------------------------------------------------------------------

def _remediation_verification_failures(
    entries: dict,
    requested_ids,
    successful_ids: set[str],
    *,
    verification_started_at: float | None = None,
    transaction_id: str | None = None,
    filer_digests: dict[str, str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[str]:
    """Explain why a targeted missing-ID remediation cannot be certified."""
    failures = []
    for fid in map(str, requested_ids):
        row = entries.get(fid) or {}
        missing = row.get("missing") or []
        if fid not in successful_ids:
            failures.append(f"{fid}: no fresh usable diff")
        elif verification_started_at is not None and (
            not (filer_digests or {}).get(fid)
            or not evidence_is_current(
                row,
                verification_started_at,
                require_precise=True,
                require_collection_started=True,
                strictly_after=True,
                transaction_snapshot_id=transaction_id,
                filer_transaction_digest=(filer_digests or {}).get(fid),
                filer_digest_version=FILER_DIGEST_SCHEMA_VERSION,
                range_start=start,
                range_end=end,
            )
        ):
            failures.append(f"{fid}: fresh diff provenance does not match verification")
        elif missing:
            failures.append(f"{fid}: {len(missing)} missing IDs remain")
    return failures


def _retryable_gate_targets(
    requested_ids,
    entries: dict,
    successful_ids: set[str],
    failure_reasons: dict[str, str],
) -> list[str]:
    """Return only targets whose inconclusive gate can safely be retried.

    A multi-filer gate may prove some filers clean before another hits a
    transient refusal. Retry just the inconclusive filers. If any fresh usable
    result still has missing IDs, or any inconclusive result is structural,
    stop instead: a cooldown cannot fix either condition.
    """
    requested = list(map(str, requested_ids or []))
    if any(
        fid in successful_ids and (entries.get(fid) or {}).get("missing")
        for fid in requested
    ):
        return []
    inconclusive = [fid for fid in requested if fid not in successful_ids]
    recorded_reasons = [
        failure_reasons[fid] for fid in inconclusive if fid in failure_reasons
    ]
    if not inconclusive or not recorded_reasons or any(
        reason not in RETRYABLE_GATE_FAILURES for reason in recorded_reasons
    ):
        return []
    return inconclusive


def _load_atomic_scope_plan(
    path: Path,
    transaction_id: str,
    end: date,
) -> tuple[list[list[dict]], dict[str, dict]]:
    """Load whole scopes and their just-captured summary requirements."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AtomicEvidenceError(f"Could not read atomic scope plan {path}: {exc}") \
            from exc
    scope_ids, requirements, _active_ranges = requirements_from_ready_plan(
        payload,
        transaction_id,
        end_date=end.isoformat(),
    )
    return [
        [
            {
                "filer_id": filer_id,
                "name": str(raw_scope.get("name") or ""),
            }
            for filer_id in ids
        ]
        for ids, raw_scope in zip(scope_ids, payload["scopes"], strict=True)
    ], requirements


def _persist_failed_scope(
    entries: dict,
    targets: list[dict],
    reasons: dict[str, str],
    *,
    transaction_id: str,
    local_digests: dict[str, str],
    start: date,
    end: date,
    collection_starts: dict[str, str],
) -> dict:
    """Persist one failed scope without any newly usable partial member."""
    ids = {str(target.get("filer_id") or "") for target in targets}
    if ("" in ids or not set(collection_starts).issubset(ids)
            or any(reasons.get(fid) != "search_budget" for fid in ids - set(collection_starts))):
        raise ValueError("failed scope lacks per-member collection start times")
    if transaction_snapshot_id(TRANSACTION_DIR) != transaction_id:
        raise AtomicSnapshotDrift(
            "transaction snapshot changed before failed-scope commit"
        )
    staged = copy.deepcopy(entries)
    for target in targets:
        fid = str(target["filer_id"])
        _record_failure(
            staged,
            target,
            reasons.get(fid, "scope_incomplete"),
            transaction_id=transaction_id,
            filer_digest=local_digests.get(fid),
            start=start,
            end=end,
            collection_started_at=collection_starts.get(fid),
        )
    _save(staged)
    return staged


def _persist_usable_scope(
    entries: dict,
    results: list[dict],
    requirements: dict[str, dict],
    *,
    transaction_id: str,
    start: date,
    end: date,
) -> dict:
    """Certify and atomically save a complete canonical scope."""
    ids = {str(result.get("filer_id") or "") for result in results}
    if not ids or "" in ids or len(ids) != len(results):
        raise ValueError("atomic scope results have missing or duplicate filer IDs")
    staged = copy.deepcopy(entries)
    for result in results:
        _store_usable_result(
            staged,
            result,
            active_requirements=requirements,
        )
    certified, blocked, scan_error = certify_exact_scope_rows(
        list(staged.values()),
        requirements,
        ids,
        TRANSACTION_DIR,
        active_ranges={filer_id: end.isoformat() for filer_id in ids},
    )
    if scan_error:
        raise AtomicSnapshotDrift(f"exact evidence scan failed: {scan_error}")
    if set(certified) != ids or blocked.intersection(ids):
        detail = f"certified={sorted(certified)} blocked={sorted(blocked)}"
        raise ValueError(f"complete scope did not certify: {detail}")
    if transaction_snapshot_id(TRANSACTION_DIR) != transaction_id:
        raise AtomicSnapshotDrift("transaction snapshot changed before scope commit")
    if any(
        result.get("range_start") != start.isoformat()
        or result.get("range_end") != end.isoformat()
        or result.get("transaction_snapshot_id") != transaction_id
        for result in results
    ):
        raise ValueError("atomic scope result provenance changed before commit")
    _save(staged)
    return staged


def _run_atomic_scope_plan(args: argparse.Namespace) -> int:
    """Diff ready canonical scopes with all-or-none usable persistence.

    The ordinary CLI remains per-filer.  This mode is deliberately stricter:
    it honors the soft budget and the runner breaker only between scopes, uses
    the ready plan's fresh paired-capture requirements, and saves usable rows
    only after every physical member of a scope completed. The workflow search
    limit is a hard backstop: exhausting it rejects the entire admitted scope.
    """
    try:
        search_budget = SearchBudget.from_environment()
        if search_budget is not None:
            search_budget.require_capacity(1)
    except SearchBudgetError as exc:
        log.error("Atomic exact collection not started: %s", exc)
        return 1
    start = date(args.start_year, 1, 1)
    end = args.end_date
    if end is None:
        raise AtomicEvidenceError("--scope-plan requires a frozen --end-date")
    if end < start:
        raise AtomicEvidenceError("--end-date must not precede --start-year")

    # The ready plan is the authority for this same-job window. Do not reread
    # generated database details here: they intentionally trail the summary
    # capture until this workflow's final publish and aggregation.
    transaction_id = transaction_snapshot_id(TRANSACTION_DIR)
    if transaction_id is None:
        log.error("Refusing to write coverage evidence without an exact local snapshot.")
        return 1
    groups, active_requirements = _load_atomic_scope_plan(
        args.scope_plan, transaction_id, end
    )
    targets = [target for group in groups for target in group]
    if not targets:
        log.info("No ready scopes to diff.")
        print("RUN_RESULT attempted=0 usable=0 unusable=0 blocked=0 "
              "retryable=0 retry_ids= attempted_scopes=0 usable_scopes=0 "
              "unusable_scopes=0 remaining_scopes=0")
        return 0

    entries = _load_atomic_entries()
    try:
        local_snapshots = transaction_filer_snapshots(
            TRANSACTION_DIR,
            [str(target["filer_id"]) for target in targets],
            start,
            end,
        )
    except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
        log.error("Cannot read exact local transaction identities: %s", exc)
        return 1
    if transaction_snapshot_id(TRANSACTION_DIR) != transaction_id:
        log.error("Local transaction shards changed while identities were loaded.")
        return 1
    local_digests = {
        fid: row["filer_transaction_digest"]
        for fid, row in local_snapshots.items()
    }
    if set(local_digests) != {
        str(target["filer_id"]) for target in targets
    }:
        log.error("The atomic scope plan has a filer without a local identity digest.")
        return 1

    deadline = time.monotonic() + args.max_minutes * 60 \
        if args.max_minutes else None
    attempted = 0
    done = 0
    unusable = 0
    attempted_scopes = 0
    usable_scopes = 0
    unusable_scopes = 0
    retry_scope_ids: set[str] = set()
    consecutive_failed_scopes = 0
    blocked = False
    fatal = False
    browser_needs_restart = False
    budget_stopped = False

    with sync_playwright() as p:
        browser, context, page = F.setup_browser_retrying(p)
        try:
            for targets_in_scope in groups:
                if search_budget is not None:
                    try:
                        estimate = estimate_scope_searches(
                            [str(target["filer_id"]) for target in targets_in_scope], entries)
                        search_budget.require_capacity(estimate if estimate is not None else 1)
                    except SearchBudgetError as exc:
                        log.warning("Search budget stopped admission between scopes: %s", exc)
                        budget_stopped = True
                        break
                # A soft deadline is a scope admission check. Once admitted,
                # every member gets the same chance to complete; the job-level
                # timeout remains the hard backstop.
                if deadline is not None and time.monotonic() >= deadline:
                    log.info(
                        "Time budget reached between scopes — stopping with %d "
                        "complete scopes.", usable_scopes,
                    )
                    break
                if transaction_snapshot_id(TRANSACTION_DIR) != transaction_id:
                    log.error("Transaction snapshot changed between atomic scopes.")
                    fatal = True
                    break
                if browser_needs_restart:
                    log.warning("Restarting the browser between atomic scopes.")
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    browser, context, page = F.setup_browser_retrying(p)
                    browser_needs_restart = False
                attempted_scopes += 1
                staged_results: list[dict] = []
                collection_starts: dict[str, str] = {}
                member_failures: dict[str, str] = {}
                retryable_failure = False

                for target in targets_in_scope:
                    if search_budget is not None:
                        try:
                            search_budget.require_capacity(1)
                        except SearchBudgetError as exc:
                            log.warning("Atomic scope stopped by search budget: %s", exc)
                            member_failures = {
                                str(member["filer_id"]): "search_budget"
                                for member in targets_in_scope
                            }
                            budget_stopped = True
                            browser_needs_restart = False
                            break
                    if browser_needs_restart:
                        log.warning("Restarting the browser inside the admitted scope.")
                        try:
                            browser.close()
                        except Exception:  # noqa: BLE001
                            pass
                        browser, context, page = F.setup_browser_retrying(p)
                        browser_needs_restart = False
                    fid = str(target["filer_id"])
                    log.info("=== Atomic diff filer %s ===", fid)
                    attempted += 1
                    collection_started_at = utc_timestamp()
                    collection_starts[fid] = collection_started_at
                    failure_reason: str | None = None
                    try:
                        # No per-member soft deadline: cutting a multi-ID scope
                        # in half would create unusable, churn-prone evidence.
                        with SC.measure_search_submissions() as search_counter:
                            theirs = orestar_ids(
                                page,
                                fid,
                                start,
                                end,
                                deadline=None,
                                context=context,
                                raise_partition_error=True,
                            )
                        searches_used = search_counter.count
                    except SearchBudgetError as exc:
                        log.warning("Atomic scope stopped by search budget: %s", exc)
                        member_failures = {
                            str(member["filer_id"]): "search_budget"
                            for member in targets_in_scope
                        }
                        budget_stopped = True
                        browser_needs_restart = False
                        break
                    except PartitionMismatchError as exc:
                        log.warning(
                            "Filer %s: deterministic partition refusal (%s)",
                            fid,
                            exc,
                        )
                        failure_reason = "partition_mismatch"
                        theirs = None
                    except F.SessionExpiredError as exc:
                        log.warning("Filer %s: session expired (%s)", fid, exc)
                        failure_reason = "session_expired"
                        retryable_failure = True
                        theirs = None

                    if theirs is None:
                        failure_reason = failure_reason or "unusable_window"
                        member_failures[fid] = failure_reason
                        if failure_reason in RETRYABLE_GATE_FAILURES:
                            retryable_failure = True
                            # The breaker is evaluated only after the scope.
                            # Restart lazily, and only if another admitted
                            # member or scope is actually going to run.
                            browser_needs_restart = True
                        continue

                    local = local_snapshots[fid]
                    ours = local.get("held_ids") or set()
                    superseded_by_us = local.get("superseded_ids") or set()
                    surplus = sorted(ours - set(theirs))
                    absent = set(theirs) - ours
                    amount_changed, amount_checked = _amount_drift(
                        theirs, local.get("held_amounts") or {},
                    )
                    result = {
                        "filer_id": fid,
                        "name": _target_name(entries, target),
                        "orestar": len(theirs),
                        "held": len(ours),
                        "complete": not surplus and not (absent - superseded_by_us),
                        "surplus": surplus,
                        "missing": sorted(absent - superseded_by_us),
                        "superseded": sorted(absent & superseded_by_us),
                        "amount_changed": amount_changed,
                        "amount_checked": amount_checked,
                        "live_originals": sorted(set(theirs) & superseded_by_us),
                        **_evidence_fields(
                            transaction_id,
                            local_digests[fid],
                            start,
                            end,
                            collection_started_at=collection_started_at,
                        ),
                    }
                    if searches_used > 0:
                        result["exact_search_count"] = searches_used
                    staged_results.append(result)

                if not member_failures:
                    try:
                        entries = _persist_usable_scope(
                            entries,
                            staged_results,
                            active_requirements,
                            transaction_id=transaction_id,
                            start=start,
                            end=end,
                        )
                    except AtomicSnapshotDrift as exc:
                        log.error("Atomic scope aborted: %s", exc)
                        fatal = True
                    except ValueError as exc:
                        log.error("Atomic scope refused before commit: %s", exc)
                        member_failures = {
                            str(target["filer_id"]): "evidence_certification_refusal"
                            for target in targets_in_scope
                        }

                if fatal:
                    break

                if member_failures:
                    unusable_scopes += 1
                    unusable += len(targets_in_scope)
                    try:
                        entries = _persist_failed_scope(
                            entries,
                            targets_in_scope,
                            member_failures,
                            transaction_id=transaction_id,
                            local_digests=local_digests,
                            start=start,
                            end=end,
                            collection_starts=collection_starts,
                        )
                    except (AtomicSnapshotDrift, ValueError) as exc:
                        log.error("Atomic failed-scope state was not saved: %s", exc)
                        fatal = True
                        break
                    if budget_stopped:
                        break
                    if retryable_failure:
                        retry_scope_ids.update(
                            str(target["filer_id"])
                            for target in targets_in_scope
                        )
                        consecutive_failed_scopes += 1
                    else:
                        consecutive_failed_scopes = 0
                    if consecutive_failed_scopes >= MAX_CONSECUTIVE_FAILURES:
                        blocked = True
                        log.warning(
                            "%d atomic scopes in a row hit a retryable refusal; "
                            "stopping at the scope boundary.",
                            consecutive_failed_scopes,
                        )
                        break
                    continue

                # Every member was certified and persisted in one scope save.
                ids = {str(result["filer_id"]) for result in staged_results}
                done += len(ids)
                usable_scopes += 1
                consecutive_failed_scopes = 0
                log.info(
                    "Atomic scope complete: filers=%s missing=%d surplus=%d "
                    "amount_changed=%d",
                    ",".join(sorted(ids)),
                    sum(len(result["missing"]) for result in staged_results),
                    sum(len(result["surplus"]) for result in staged_results),
                    sum(len(result["amount_changed"]) for result in staged_results),
                )
        finally:
            browser.close()

    retry_ids = [] if budget_stopped else sorted(retry_scope_ids)
    # A scope admitted but aborted during certification is still outstanding.
    # Count terminal scope outcomes, not merely site admission attempts.
    remaining_scopes = len(groups) - usable_scopes - unusable_scopes
    incomplete = remaining_scopes > 0
    log.info(
        "Atomic diff: %d/%d scopes usable; %d/%d filers committed; blocked %s",
        usable_scopes,
        attempted_scopes,
        done,
        attempted,
        "yes" if blocked else "no",
    )
    print(
        f"RUN_RESULT attempted={attempted} usable={done} unusable={unusable} "
        f"blocked={1 if blocked else 0} "
        f"retryable={1 if retry_ids else 0} "
        f"retry_ids={','.join(retry_ids)} "
        f"attempted_scopes={attempted_scopes} usable_scopes={usable_scopes} "
        f"unusable_scopes={unusable_scopes} remaining_scopes={remaining_scopes} "
        f"search_budget_exhausted={1 if budget_stopped else 0}"
    )
    return 1 if unusable_scopes or incomplete or fatal else 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    target_mode = ap.add_mutually_exclusive_group()
    target_mode.add_argument("--filer-ids", nargs="*", default=None)
    target_mode.add_argument("--flagged", action="store_true",
                             help="diff every currently-flagged committee")
    target_mode.add_argument(
        "--scope-plan",
        type=Path,
        default=None,
        help=("ready atomic-evidence JSON; preserve each canonical scope as "
              "one all-or-none persistence unit"),
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start-year", type=int, default=2006)
    ap.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help=("inclusive UTC query end (required and frozen for an exact "
              "remediation verification)"),
    )
    ap.add_argument("--max-minutes", type=int, default=70,
                    help="stop on our own terms, before the job timeout does it for us")
    ap.add_argument("--recheck", action="store_true",
                    help="re-diff committees already recorded")
    ap.add_argument(
        "--require-no-missing",
        action="store_true",
        help=("fail unless every explicitly targeted filer is freshly diffed "
              "and has no genuinely missing transaction IDs"),
    )
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    if args.report:
        return report()
    if args.scope_plan and not args.recheck:
        ap.error("--scope-plan requires --recheck for fresh evidence")
    if args.scope_plan and args.start_year != 2006:
        ap.error("--scope-plan requires --start-year=2006")
    if args.scope_plan and args.limit:
        ap.error("--scope-plan cannot be combined with --limit")
    if args.scope_plan and args.require_no_missing:
        ap.error("--scope-plan performs its own complete-scope verification")
    if args.scope_plan and args.end_date is None:
        ap.error("--scope-plan requires a frozen --end-date")
    if args.require_no_missing and not args.filer_ids:
        ap.error("--require-no-missing requires explicit --filer-ids")
    if args.require_no_missing and not args.recheck:
        ap.error("--require-no-missing requires --recheck for fresh evidence")
    if args.require_no_missing and args.start_year != 2006:
        ap.error("--require-no-missing requires --start-year=2006")
    if args.limit < 0:
        ap.error("--limit must be zero or positive")
    if args.require_no_missing and args.limit:
        ap.error("--require-no-missing cannot be combined with --limit")
    if args.require_no_missing and args.end_date is None:
        ap.error("--require-no-missing requires a frozen --end-date")

    if args.scope_plan:
        try:
            return _run_atomic_scope_plan(args)
        except AtomicEvidenceError as exc:
            log.error("Atomic scope plan refused: %s", exc)
            return 1

    if args.filer_ids:
        targets = [{"filer_id": f, "name": ""} for f in args.filer_ids]
    elif args.flagged:
        targets = SC._flagged_committees()
    else:
        log.error("Give --filer-ids or --flagged.")
        return 2

    entries = _load()
    active_requirements = _active_paired_requirements()
    if not args.recheck:
        # A refusal is not a measurement. Retry first-attempt failures rather
        # than treating the mere presence of a JSON object as completed work.
        targets = [t for t in targets
                   if (entries.get(str(t["filer_id"])) or {}).get("complete") is None]
    elif not args.filer_ids:
        # The rolling flagged sweep needs staleness ordering and same-day
        # suppression. An explicit --filer-ids --recheck is a deliberate force
        # request (and is useful for a small post-deploy canary), so preserve
        # exactly the caller's list instead of silently filtering it.
        targets = _prioritise(targets, entries)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        log.info("Nothing to diff.")
        return 0

    start = date(args.start_year, 1, 1)
    end = args.end_date or datetime.now(timezone.utc).date()
    if end < start:
        ap.error("--end-date must not precede --start-year")
    verification_started_at = time.time()
    transaction_id = _current_transaction_snapshot_id()
    if transaction_id is None:
        log.error("Refusing to write coverage evidence without an exact local snapshot.")
        return 1
    try:
        local_snapshots = transaction_filer_snapshots(
            TRANSACTION_DIR,
            [str(target["filer_id"]) for target in targets], start, end
        )
    except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
        log.error("Cannot read exact local transaction identities: %s", exc)
        return 1
    if transaction_snapshot_id(TRANSACTION_DIR) != transaction_id:
        log.error("Local transaction shards changed while identities were loaded.")
        return 1
    local_digests = {
        fid: row["filer_transaction_digest"]
        for fid, row in local_snapshots.items()
    }
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    done = 0
    attempted = 0
    unusable = 0
    unusable_reasons: dict[str, str] = {}
    consecutive_failures = 0
    blocked = False
    budget_exhausted = False
    successful_ids: set[str] = set()

    with sync_playwright() as p:
        browser, _ctx, page = F.setup_browser_retrying(p)
        try:
            for t in targets:
                if deadline and time.monotonic() > deadline:
                    log.info("Time budget reached — stopping with %d diffed.", done)
                    break
                fid = str(t["filer_id"])
                log.info("=== Diffing filer %s %s ===", fid, t.get("name", ""))
                attempted += 1
                collection_started_at = utc_timestamp()
                failure_reason = "unusable_window"
                deterministic_refusal = False
                try:
                    with SC.measure_search_submissions() as search_counter:
                        theirs = orestar_ids(
                            page, fid, start, end, deadline=deadline, context=_ctx,
                            raise_partition_error=True,
                        )
                except CollectionDeadlineExceeded:
                    log.warning("Filer %s: time budget reached before a complete, "
                                "reconciled result", fid)
                    failure_reason = "time_budget"
                    budget_exhausted = True
                    theirs = None
                except PartitionMismatchError as exc:
                    log.warning("Filer %s: deterministic partition refusal (%s)",
                                fid, exc)
                    failure_reason = "partition_mismatch"
                    deterministic_refusal = True
                    theirs = None
                except F.SessionExpiredError as exc:
                    # survey_coverage already treats this as a recoverable
                    # runner/session condition. Keep the same semantics here
                    # instead of failing the job before RUN_RESULT is emitted.
                    log.warning("Filer %s: session expired (%s)", fid, exc)
                    failure_reason = "session_expired"
                    theirs = None
                if theirs is None:
                    # Preserve a previous usable result on a failed recheck. Its
                    # checked date remains the date of the evidence, while this
                    # attempt is recorded separately. First attempts remain an
                    # explicit unknown and override unsafe count-only evidence.
                    _record_failure(
                        entries,
                        t,
                        failure_reason,
                        transaction_id=transaction_id,
                        filer_digest=local_digests.get(fid),
                        start=start,
                        end=end,
                        collection_started_at=collection_started_at,
                    )
                    _save(entries)
                    unusable += 1
                    # Session/refusal failures may clear after an F5 cooldown.
                    # A proved partition mismatch or an exhausted time budget
                    # will not, so do not ask the workflow to repeat those.
                    unusable_reasons[fid] = failure_reason
                    if budget_exhausted:
                        log.info("Time budget reached inside filer %s — stopping cleanly.",
                                 fid)
                        break
                    if deterministic_refusal:
                        # A proved gap/overlap is local to this partition tree,
                        # not evidence that the runner's browser or IP is blocked.
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        blocked = True
                        log.warning("%d committees in a row unusable — stopping this "
                                    "runner before the F5 cooldown becomes a null cascade.",
                                    consecutive_failures)
                        break
                    # A broken browser/session should not poison the next
                    # committee. If the block is IP-wide the next refusal will
                    # trip the runner breaker; if it is session-local this
                    # gives the collector one clean chance to recover.
                    log.warning("Restarting the browser after an unusable result.")
                    try:
                        browser.close()
                    except Exception:                    # noqa: BLE001
                        pass
                    browser, _ctx, page = F.setup_browser_retrying(p)
                    continue
                consecutive_failures = 0
                local = local_snapshots.get(fid) or {}
                ours = local.get("held_ids") or set()
                superseded_by_us = local.get("superseded_ids") or set()
                surplus = sorted(ours - set(theirs))
                absent = set(theirs) - ours
                # Split what ORESTAR has and we do not into the two cases that
                # look identical to a count and mean opposite things.
                superseded = sorted(absent & superseded_by_us)
                missing = sorted(absent - superseded_by_us)
                # Same ID, different amount: a row ORESTAR edited in place.
                amount_changed, amount_checked = _amount_drift(
                    theirs, local.get("held_amounts") or {},
                )
                result = {
                    "filer_id": fid,
                    "name": _target_name(entries, t),
                    "orestar": len(theirs),
                    "held": len(ours),
                    # True only when the identities agree exactly. Unlike a
                    # count, this cannot be satisfied by a surplus cancelling a
                    # shortfall — the failure that made the count survey report
                    # Plumbers & Steamfitters PAC as "missing: 0" while it held
                    # sixteen rows ORESTAR had withdrawn.
                    "complete": not surplus and not missing,
                    "surplus": surplus,
                    "missing": missing,
                    # Rows ORESTAR still returns that we dropped on purpose.
                    # Recorded so the count is explainable rather than merely
                    # excused: held + superseded should equal ORESTAR's total.
                    "superseded": superseded,
                    # Identity says nothing about value. `complete` stays an
                    # identity verdict; these record whether the rows we hold
                    # still carry ORESTAR's current amounts.
                    "amount_changed": amount_changed,
                    "amount_checked": amount_checked,
                    # Originals ORESTAR still returns although an amendment we
                    # hold points at them. ORESTAR's default search omits the
                    # originals it expired, so these are live on ORESTAR and
                    # counted in its summary (Oregon Firearms Federation PAC
                    # 2041931 and its amendment 2041946 are both counted).
                    # Unlike `superseded`, this list survives our keeping the
                    # row, which is what lets the merge go on keeping it.
                    "live_originals": sorted(set(theirs) & superseded_by_us),
                    **_evidence_fields(
                        transaction_id,
                        local_digests[fid],
                        start,
                        end,
                        collection_started_at=collection_started_at,
                    ),
                }
                if search_counter.count > 0:
                    # Scheduling telemetry only: every ID above still came
                    # from the fresh, complete, reconciled collection.
                    result["exact_search_count"] = search_counter.count
                _store_usable_result(
                    entries,
                    result,
                    active_requirements=active_requirements,
                )
                log.info("Filer %s: ORESTAR %d, held %d, surplus %d, missing %d, "
                         "superseded %d, amount changed %d of %d compared",
                         fid, len(theirs), len(ours), len(surplus), len(missing),
                         len(superseded), len(amount_changed), amount_checked)
                _save(entries)
                done += 1
                successful_ids.add(fid)
        finally:
            browser.close()

    log.info("Diffed %d committees this run.", done)
    log.info("Attempted %d committees; usable %d; unusable %d; blocked %s",
             attempted, done, unusable, "yes" if blocked else "no")
    # Stable machine-readable line for the workflow's chain guard.
    certified_ids = {
        fid for fid in successful_ids
        if evidence_is_current(
            entries.get(fid),
            verification_started_at,
            require_precise=True,
            require_collection_started=True,
            strictly_after=True,
            transaction_snapshot_id=transaction_id,
            filer_transaction_digest=local_digests.get(fid),
            filer_digest_version=FILER_DIGEST_SCHEMA_VERSION,
            range_start=start,
            range_end=end,
        )
    }
    retry_ids = _retryable_gate_targets(
        args.filer_ids, entries, certified_ids, unusable_reasons,
    )
    print(f"RUN_RESULT attempted={attempted} usable={done} "
          f"unusable={unusable} blocked={1 if blocked else 0} "
          f"retryable={1 if retry_ids else 0} "
          f"retry_ids={','.join(retry_ids)}")
    if args.require_no_missing:
        failures = _remediation_verification_failures(
            entries,
            args.filer_ids,
            successful_ids,
            verification_started_at=verification_started_at,
            transaction_id=transaction_id,
            filer_digests=local_digests,
            start=start,
            end=end,
        )
        if failures:
            # A fresh usable diff settles the fate of that fetch tree even
            # when the overall multi-filer gate fails. Clean filers are done;
            # filers still missing IDs must be fetched from scratch next time.
            # Preserve progress only for targets whose verification itself was
            # unusable, so a later run can retry the check without re-fetching.
            if certified_ids:
                F.clear_identity_progress(sorted(certified_ids))
            for failure in failures:
                log.error("Identity remediation verification failed — %s", failure)
            print(f"REMEDIATION_VERIFY passed=0 failed={len(failures)}")
            return 1
        print(f"REMEDIATION_VERIFY passed={len(successful_ids)} failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
