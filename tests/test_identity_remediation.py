"""Regression tests for exact-identity missing-row remediation."""

from __future__ import annotations

import csv
import gzip
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import diff_coverage as DC  # noqa: E402
import fetch as F  # noqa: E402
from balance_snapshot import (  # noqa: E402
    transaction_filer_snapshots,
    transaction_snapshot_id,
)


class _ResultPage:
    def __init__(self, texts=None):
        self.url = f"{F.BASE_URL}/gotoPublicTransactionSearchResults.do"
        self._texts = list(texts or ["1 records found"])

    def fill(self, *_args, **_kwargs):
        return None

    def select_option(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def click(self, *_args, **_kwargs):
        return None

    def wait_for_url(self, *_args, **_kwargs):
        return None

    def inner_text(self, *_args, **_kwargs):
        if len(self._texts) > 1:
            return self._texts.pop(0)
        return self._texts[0]

    def evaluate(self, *_args, **_kwargs):
        return "token"


class _Context:
    def cookies(self):
        return []


class _Response:
    headers = {"Content-Type": "application/octet-stream"}
    content = b"fresh export"


def test_browser_setup_retries_transient_initial_navigation(monkeypatch) -> None:
    expected = (object(), object(), object())
    attempts = []
    sleeps = []

    def setup(_playwright):
        attempts.append(True)
        if len(attempts) < 3:
            raise F.PlaywrightTimeout("Page.goto timed out")
        return expected

    monkeypatch.setattr(F, "setup_browser", setup)
    monkeypatch.setattr(F.time, "sleep", sleeps.append)

    assert F.setup_browser_retrying(object()) is expected
    assert len(attempts) == 3
    assert sleeps == [15, 30]


def test_results_count_polls_and_accepts_zero() -> None:
    delayed = _ResultPage(["Loading…", "Still loading…", "1,234 records found"])
    assert F._read_results_count(delayed) == 1234
    assert F._read_results_count(_ResultPage(["No records found"])) == 0


def test_record_counts_replace_stale_observations(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "_raw"
    raw.mkdir()
    monkeypatch.setattr(F, "RAW_DIR", raw)
    key = ("C", "2026-01-01", "2026-01-02", "None", "None", "None", "1")
    other = ("E", "2026-01-01", "2026-01-02", "None", "None", "None", "2")
    (tmp_path / "record_counts.json").write_text(json.dumps([
        {"key": list(key), "reported": 10},
        {"key": list(other), "reported": 7},
    ]))
    F.RECORD_COUNTS.clear()
    F.RECORD_COUNTS[key] = 12

    F._flush_record_counts()

    rows = {tuple(row["key"]): row["reported"] for row in json.loads(
        (tmp_path / "record_counts.json").read_text()
    )}
    assert rows == {key: 12, other: 7}
    F.RECORD_COUNTS.clear()


def test_identity_progress_round_trip_and_targeted_reset(tmp_path, monkeypatch) -> None:
    path = tmp_path / "identity.json"
    monkeypatch.setattr(F, "IDENTITY_PROGRESS_FILE", path)
    one = ("C", "2026-01-01", "2026-01-01", "None", "None", "None", "1")
    two = ("E", "2026-01-01", "2026-01-01", "None", "None", "None", "2")
    F._save_identity_progress({two: 4, one: 3})

    assert F._identity_progress() == {one: 3, two: 4}
    assert F._identity_progress(reset_filers=["1"]) == {two: 4}
    assert json.loads(path.read_text()) == [{"key": list(two), "reported": 4}]


def test_partition_tree_requires_every_child_and_exact_sum() -> None:
    parent = ("ALL", "a", "b", "None", "None", "None", "1")
    left = ("C", "a", "b", "None", "None", "None", "1")
    right = ("E", "a", "b", "None", "None", "None", "1")
    branches = {parent: [left, right]}

    missing = F._identity_tree_failures({parent: 5, left: 2}, branches)
    mismatch = F._identity_tree_failures(
        {parent: 5, left: 2, right: 2}, branches
    )
    assert missing[0][1] == "missing"
    assert mismatch[0][1] == "mismatch"
    assert F._identity_tree_errors({parent: 5, left: 2, right: 3}, branches) == []


def test_invalid_partition_subtree_is_removed_for_fresh_retry() -> None:
    parent = ("ALL", "a", "b", "None", "None", "None", "1")
    left = ("C", "a", "b", "None", "None", "None", "1")
    right = ("E", "a", "b", "None", "None", "None", "1")
    unrelated = ("ALL", "a", "b", "None", "None", "None", "2")
    progress = {parent: 5, left: 2, right: 2, unrelated: 7}

    removed = F._discard_identity_subtrees(
        progress, {parent: [left, right]}, [parent]
    )

    assert removed == 3
    assert progress == {unrelated: 7}


def test_repeated_partition_failure_deactivates_entire_filer() -> None:
    one_root = ("ALL", "a", "b", "None", "None", "None", "1")
    one_child = ("C", "a", "b", "None", "None", "None", "1")
    other = ("ALL", "a", "b", "None", "None", "None", "2")
    progress = {one_root: 5, one_child: 5, other: 7}

    assert F._discard_identity_filer(progress, "1") == 2
    assert progress == {other: 7}


def test_identity_failure_counts_round_trip_and_clear_with_progress(
    tmp_path, monkeypatch,
) -> None:
    progress_path = tmp_path / "progress.json"
    failure_path = tmp_path / "failures.json"
    monkeypatch.setattr(F, "IDENTITY_PROGRESS_FILE", progress_path)
    monkeypatch.setattr(F, "IDENTITY_FAILURE_FILE", failure_path)
    one = ("ALL", "a", "b", "None", "None", "None", "1")
    two = ("ALL", "a", "b", "None", "None", "None", "2")
    F._save_identity_progress({one: 5, two: 7})
    F._save_identity_failures({one: 2, two: 1})

    F.clear_identity_progress(["1"])

    assert F._identity_progress() == {two: 7}
    assert F._identity_failures() == {two: 1}


def test_exact_cap_is_complete_only_with_matching_fresh_count() -> None:
    assert not F._needs_filer_split(Path("leaf"), F.ORESTAR_ROW_CAP,
                                    F.ORESTAR_ROW_CAP)
    assert F._needs_filer_split(Path("leaf"), F.ORESTAR_ROW_CAP, None)
    assert F._needs_filer_split(F.CAPPED, F.ORESTAR_ROW_CAP,
                                F.ORESTAR_ROW_CAP + 1)


def test_force_cap_uses_live_count_without_held_skip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: 5000)
    monkeypatch.setattr(F, "_held_rows", lambda *_args: pytest.fail("held skip used"))
    monkeypatch.setattr(F.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: pytest.fail("export requested"))

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is F.CAPPED


def test_force_mismatch_quarantines_old_cache(tmp_path, monkeypatch) -> None:
    canonical = F._filer_window_path(
        tmp_path, "1", "ALL", date(2026, 1, 1), date(2026, 1, 2),
        None, None, None,
    )
    canonical.write_bytes(b"old export")
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: 2)
    monkeypatch.setattr(F, "_validate_download", lambda _path: 1)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: _Response())

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is None
    assert not canonical.exists()
    assert (tmp_path / ".identity_stale" / canonical.name).read_bytes() == b"old export"


def test_proved_exact_cap_is_marked_for_merger(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: F.ORESTAR_ROW_CAP)
    monkeypatch.setattr(F, "_validate_download", lambda _path: F.ORESTAR_ROW_CAP)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: _Response())

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is not None
    assert result.name.startswith("verified_filer1_")
    assert result.exists()


def _run_selector(
    tmp_path: Path,
    diff_rows,
    survey_rows=None,
    done="",
    incomplete="",
    progress_rows=None,
    details=None,
    legacy_exact=False,
    transaction_rows=None,
    post_evidence_rows=None,
    diff_transform=None,
    shard_fieldnames=None,
    with_status=False,
):
    data = tmp_path / "data"
    (data / "aggregated" / "filers").mkdir(parents=True)
    transactions = data / "transactions"
    transactions.mkdir()
    shard = transactions / "txn_2026.csv.gz"
    fieldnames = shard_fieldnames or [
        "tran_id", "original id", "tran_date", "filer id", "amount",
    ]
    all_ids = {
        str(row.get("filer_id") or "")
        for row in list(diff_rows) + list(survey_rows or [])
        if row.get("filer_id")
    }
    all_ids.update(
        str(fid)
        for detail in (details or [])
        for fid in (detail.get("filer_ids") or [])
        if str(fid)
    )
    initial_rows = transaction_rows
    if initial_rows is None:
        initial_rows = [
            {
                "tran_id": f"held-{fid}",
                "original id": f"held-{fid}",
                "tran_date": "09/01/2026",
                "filer id": fid,
                "amount": "1.00",
            }
            for fid in sorted(all_ids)
        ]

    def write_shard(rows):
        with gzip.open(shard, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    write_shard(initial_rows)
    snapshot_id = transaction_snapshot_id(transactions)
    (data / "aggregated" / "balance_snapshot_source.json").write_text(
        json.dumps({"transaction_snapshot_id": snapshot_id})
    )
    diff_rows = [dict(row) for row in diff_rows]
    captured_at = 1_788_220_800  # 2026-09-01T00:00:00Z
    if not legacy_exact:
        snapshots = transaction_filer_snapshots(
            transactions,
            [row.get("filer_id") for row in diff_rows],
            date(2006, 1, 1),
            date(2026, 9, 2),
        )
        for row in diff_rows:
            if row.get("complete") is None:
                continue
            fid = str(row.get("filer_id") or "")
            row.setdefault("missing", [])
            row.setdefault("surplus", [])
            row.setdefault("superseded", [])
            row.setdefault("held", len(snapshots[fid]["held_ids"]))
            row.setdefault(
                "orestar",
                row["held"]
                - len(row["surplus"])
                + len(row["missing"])
                + len(row["superseded"]),
            )
            row.setdefault("evidence_version", 2)
            row.setdefault("filer_digest_version", 2)
            row.setdefault("collection_started_at", "2026-09-02T00:00:00Z")
            row.setdefault("checked_at", "2026-09-02T00:00:00.000001Z")
            row.setdefault("transaction_snapshot_id", snapshot_id)
            row.setdefault(
                "filer_transaction_digest",
                snapshots[fid]["filer_transaction_digest"],
            )
            row.setdefault("range_start", "2006-01-01")
            row.setdefault("range_end", "2026-09-02")

    details = json.loads(json.dumps(list(details or [])))
    for detail in details:
        comparison = detail.get("orestar_comparison") or {}
        if comparison.get("status") == "paired":
            comparison.setdefault("captured_at", captured_at)
            comparison.setdefault("filer_ids", detail.get("filer_ids"))
            if comparison.get("app_transaction_snapshot_id") in (None, "placeholder"):
                comparison["app_transaction_snapshot_id"] = snapshot_id
            detail["orestar_comparison"] = comparison
    represented = {
        str(fid)
        for detail in details
        for fid in (detail.get("filer_ids") or [])
    }
    for row in diff_rows:
        fid = str(row.get("filer_id") or "")
        if not fid or fid in represented:
            continue
        details.append({
            "slug": f"exact-{fid}",
            "name": row.get("name") or fid,
            "filer_ids": [fid],
            "orestar_comparison": {
                "status": "paired",
                "captured_at": captured_at,
                "app_transaction_snapshot_id": snapshot_id,
                "filer_ids": [fid],
            },
        })
        represented.add(fid)
    (data / "aggregated" / "filer_index.json").write_text(json.dumps([
        {
            "slug": row["slug"],
            "name": row.get("name", row["slug"]),
            "filer_id": (row.get("filer_ids") or [""])[0],
        }
        for row in details
    ]))
    for row in details:
        (data / "aggregated" / "filers" / f"{row['slug']}.json").write_text(
            json.dumps(row)
        )
    if post_evidence_rows is not None:
        write_shard(post_evidence_rows)
    post_snapshot_id = transaction_snapshot_id(transactions)
    if diff_transform is not None:
        diff_transform(diff_rows, snapshot_id, post_snapshot_id)
    (data / "coverage_diff.json").write_text(json.dumps(diff_rows))
    if survey_rows is not None:
        (data / "coverage_survey.json").write_text(json.dumps(survey_rows))
    if done:
        (data / "backfilled_filers.txt").write_text(done)
    if incomplete:
        (data / "incomplete_backfills.txt").write_text(incomplete)
    if progress_rows is not None:
        (data / "identity_remediation_windows.json").write_text(
            json.dumps(progress_rows)
        )
    ids_path = tmp_path / "ids.txt"
    mode_path = tmp_path / "mode.txt"
    end_path = tmp_path / "end.txt"
    resume_path = tmp_path / "resume.txt"
    status_path = tmp_path / "status.txt"
    env = os.environ.copy()
    env["AUTO_BACKFILL_OUTPUT"] = str(ids_path)
    env["AUTO_BACKFILL_MODE_OUTPUT"] = str(mode_path)
    env["AUTO_BACKFILL_END_DATE_OUTPUT"] = str(end_path)
    env["AUTO_BACKFILL_RESUME_OUTPUT"] = str(resume_path)
    env["AUTO_BACKFILL_STATUS_OUTPUT"] = str(status_path)
    result = subprocess.run(
        [sys.executable, str(SCRAPER_DIR / "auto_backfill_ids.py")],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    values = (
        ids_path.read_text() if ids_path.exists() else None,
        mode_path.read_text().strip(),
        end_path.read_text().strip() if end_path.exists() else None,
        resume_path.read_text().strip(),
    )
    if with_status:
        return (*values, status_path.read_text().strip(), result.stdout)
    return values


def test_count_and_dollar_evidence_never_authorize_mutation(tmp_path) -> None:
    detail = {
        "slug": "candidate",
        "name": "Candidate Committee",
        "filer_ids": ["40"],
        "orestar_comparison": {
            "status": "paired",
            "actionable": True,
            "delta_at_capture": 125.0,
            "app_transaction_snapshot_id": "placeholder",
        },
    }

    ids, mode, _end, _resume, status, output = _run_selector(
        tmp_path / "dollar", [], details=[detail], with_status=True,
    )
    assert ids is None
    assert mode == "identity"
    assert status == "blocked"
    assert "report-only" in output

    ids, mode, _end, _resume, status, output = _run_selector(
        tmp_path / "rows",
        [],
        survey_rows=[{"filer_id": "40", "missing": 1, "name": "Candidate"}],
        details=[detail],
        with_status=True,
    )
    assert ids is None
    assert mode == "identity"
    assert status == "blocked"
    assert "does not authorize mutation" in output


def test_exact_selector_overrides_historical_done_and_batches_one(tmp_path) -> None:
    ids, mode, end, resume = _run_selector(tmp_path, [
        {"filer_id": "1", "complete": False, "missing": ["a"], "name": "one"},
        {"filer_id": "2", "complete": False, "missing": ["a", "b"], "name": "two"},
    ], done="2\n")
    assert ids == "2"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"


def test_usable_exact_result_suppresses_stale_count_survey(tmp_path) -> None:
    ids, mode, _end, _resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": [], "surplus": ["x"]}],
        [{"filer_id": "1", "missing": 99, "name": "stale"}],
    )
    assert ids is None
    assert mode == "identity"


@pytest.mark.parametrize(
    "override",
    [
        {"checked_at": "2026-09-01"},
        {"checked_at": "2026-08-31T23:59:59.999999Z"},
        {"transaction_snapshot_id": "sha256:stale"},
        {"range_start": "2007-01-01"},
        {"range_end": "2026-08-31"},
    ],
)
def test_exact_selector_rejects_imprecise_or_mismatched_evidence(
    tmp_path, override,
) -> None:
    row = {
        "filer_id": "1",
        "complete": False,
        "missing": ["x"],
        **override,
    }
    ids, mode, end, resume = _run_selector(tmp_path, [row])

    assert ids is None
    assert mode == "identity"
    assert end is None
    assert resume == "false"


@pytest.mark.parametrize(
    "row",
    [
        {"filer_id": "1", "complete": False, "missing": "x"},
        {"filer_id": "1", "complete": True, "missing": ["x"]},
        {"filer_id": "1", "complete": False, "missing": ["x", "x"]},
        {
            "filer_id": "1", "complete": False, "missing": ["x"],
            "surplus": ["x"],
        },
        {
            "filer_id": "1", "complete": False, "missing": ["x"],
            "superseded": ["x"],
        },
    ],
)
def test_malformed_exact_identity_row_fails_closed(tmp_path, row) -> None:
    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path, [row], with_status=True,
    )

    assert ids is None
    assert status == "blocked"


@pytest.mark.parametrize("legacy_exact", [False, True])
def test_multi_id_scope_requires_current_exact_evidence_for_every_component(
    tmp_path, legacy_exact,
) -> None:
    detail = {
        "slug": "combined",
        "name": "Combined committee",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            # The helper replaces this placeholder after creating its shard.
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    ids, mode, _end, _resume = _run_selector(
        tmp_path / "run",
        [{"filer_id": "1", "complete": False, "missing": ["x"]}],
        survey_rows=[{"filer_id": "2", "missing": 5}],
        details=[detail],
        legacy_exact=legacy_exact,
    )

    assert ids is None
    assert mode == "identity"


def test_multi_id_scope_can_automate_after_every_component_matches(tmp_path) -> None:
    detail = {
        "slug": "combined",
        "name": "Combined committee",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    ids, mode, end, resume = _run_selector(
        tmp_path / "run",
        [
            {"filer_id": "1", "complete": False, "missing": ["x"]},
            {"filer_id": "2", "complete": True, "missing": []},
        ],
        details=[detail],
    )

    assert ids == "1"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"


def test_unknown_exact_result_blocks_count_automation(tmp_path) -> None:
    ids, mode, _end, _resume, status, output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": None, "missing": []}],
        [{"filer_id": "1", "missing": 3, "name": "unknown"}],
        with_status=True,
    )
    assert ids is None
    assert mode == "identity"
    assert status == "blocked"
    assert "not a completion signal" in output


def _transaction_row(fid: str, tran_id: str) -> dict[str, str]:
    return {
        "tran_id": tran_id,
        "original id": tran_id,
        "tran_date": "09/01/2026",
        "filer id": fid,
        "amount": "1.00",
    }


def _exact_observation(
    base: dict,
    *,
    started: str,
    fingerprint: str,
    missing=(),
    surplus=(),
    digest: str | None = None,
    range_end: str | None = None,
) -> dict:
    """Build one nonrecursive precise observation from an enriched fixture."""
    result = dict(base)
    result.pop("usable_history", None)
    start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    completed = (start_dt + timedelta(microseconds=1)).astimezone(
        timezone.utc,
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    result.update({
        "complete": not missing and not surplus,
        "missing": list(missing),
        "surplus": list(surplus),
        "superseded": [],
        "orestar": (
            int(result.get("held") or 0) - len(surplus) + len(missing)
        ),
        "collection_started_at": started,
        "checked": completed[:10],
        "checked_at": completed,
        "transaction_snapshot_id": fingerprint,
    })
    if digest is not None:
        result["filer_transaction_digest"] = digest
    if range_end is not None:
        result["range_end"] = range_end
    return result


def test_unrelated_repair_keeps_untouched_scope_live(tmp_path) -> None:
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_other_repair = [
        *initial,
        _transaction_row("2", "two-repaired"),
    ]

    ids, mode, end, resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["missing-one"]}],
        transaction_rows=initial,
        post_evidence_rows=after_other_repair,
        with_status=True,
    )

    assert ids == "1"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"
    assert status == "selected"


def test_rediff_after_unrelated_global_change_uses_newest_anchored_result(
    tmp_path,
) -> None:
    """E2(G2,D) remains usable because history retains E1(G1,D)."""
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_other_repair = [*initial, _transaction_row("2", "two-repaired")]

    def overwrite_with_rediff(rows, paired_global, current_global):
        anchor = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["stale-missing"],
        )
        current = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=current_global, missing=["newest-missing"],
        )
        current["usable_history"] = [anchor]
        rows[0] = current

    ids, mode, end, resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        transaction_rows=initial,
        post_evidence_rows=after_other_repair,
        diff_transform=overwrite_with_rediff,
        with_status=True,
    )

    assert ids == "1"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"
    assert status == "selected"


def test_newer_clean_rediff_supersedes_stale_missing_history(tmp_path) -> None:
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_other_repair = [*initial, _transaction_row("2", "two-repaired")]

    def overwrite_with_clean(rows, paired_global, current_global):
        anchor = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["already-repaired"],
        )
        current = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=current_global, missing=[],
        )
        current["usable_history"] = [anchor]
        rows[0] = current

    ids, _mode, end, _resume, status, output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        survey_rows=[{"filer_id": "1", "missing": 9}],
        transaction_rows=initial,
        post_evidence_rows=after_other_repair,
        diff_transform=overwrite_with_clean,
        with_status=True,
    )

    assert ids is None
    assert end is None
    assert status == "idle"
    assert "no authorized missing rows" in output.lower()


def test_out_of_order_history_newer_than_top_supersedes_top_verdict(
    tmp_path,
) -> None:
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_other_repair = [*initial, _transaction_row("2", "two-repaired")]

    def put_newer_result_in_history(rows, paired_global, current_global):
        old_top = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["stale"],
        )
        newer_history = _exact_observation(
            rows[0], started="2026-09-04T00:00:00Z",
            fingerprint=current_global, missing=[],
        )
        old_top["usable_history"] = [newer_history]
        rows[0] = old_top

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        transaction_rows=initial,
        post_evidence_rows=after_other_repair,
        diff_transform=put_newer_result_in_history,
        with_status=True,
    )

    assert ids is None
    assert status == "idle"


def test_equal_query_start_with_conflicting_results_blocks_scope(tmp_path) -> None:
    def add_conflicting_tie(rows, paired_global, _current_global):
        top = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["one"],
        )
        conflict = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=[],
        )
        top["usable_history"] = [conflict]
        rows[0] = top

    ids, _mode, _end, _resume, status, output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        diff_transform=add_conflicting_tie,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"
    assert "not a completion signal" in output


def test_paired_global_claiming_two_scope_digests_blocks_scope(tmp_path) -> None:
    def add_digest_conflict(rows, paired_global, _current_global):
        top = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=paired_global, missing=["one"],
        )
        conflict = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["old"],
            digest="sha256:impossible-other-state",
        )
        top["usable_history"] = [conflict]
        rows[0] = top

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        diff_transform=add_digest_conflict,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda top, history: top.update(transaction_snapshot_id=""),
        lambda top, history: top.update(transaction_snapshot_id="   "),
        lambda top, history: history.update(transaction_snapshot_id=""),
        lambda top, history: history.update(
            filer_transaction_digest="   "
        ),
        lambda top, history: history.update(filer_id="2"),
    ],
)
def test_blank_global_or_wrong_history_owner_fails_closed(
    tmp_path, mutate,
) -> None:
    def corrupt(rows, paired_global, _current_global):
        top = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=paired_global, missing=["one"],
        )
        history = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["old"],
        )
        mutate(top, history)
        top["usable_history"] = [history]
        rows[0] = top

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        diff_transform=corrupt,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_whitespace_only_paired_global_fingerprint_fails_closed(tmp_path) -> None:
    detail = {
        "slug": "blank-paired-global",
        "filer_ids": ["1"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "   ",
            "actionable": True,
            "delta_at_capture": 10,
        },
    }
    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["one"]}],
        details=[detail],
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


@pytest.mark.parametrize(
    ("detail_ids", "captured_ids"),
    [
        (["1"], ["1", "2"]),
        (["1", "2"], ["1"]),
        (["1"], None),
        (["1"], "1"),
    ],
)
def test_detail_and_paired_capture_scope_must_match_exactly(
    tmp_path, detail_ids, captured_ids,
) -> None:
    detail = {
        "slug": "scope-drift",
        "filer_ids": detail_ids,
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "filer_ids": captured_ids,
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["one"]}],
        details=[detail],
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_history_missing_candidate_is_discovered_and_blocks_if_ambiguous(
    tmp_path,
) -> None:
    def hide_malformed_missing_in_history(rows, paired_global, _current_global):
        top = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=paired_global, missing=[],
        )
        history = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint="", missing=["historical-missing"],
        )
        top["usable_history"] = [history]
        rows[0] = top

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": True, "missing": []}],
        diff_transform=hide_malformed_missing_in_history,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_wrong_owner_on_legacy_nested_history_fails_closed(tmp_path) -> None:
    def add_wrong_legacy_owner(rows, paired_global, _current_global):
        top = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=paired_global, missing=["one"],
        )
        top["usable_history"] = [{
            "filer_id": "999",
            "complete": True,
            "missing": [],
            "surplus": [],
            "superseded": [],
            "checked": "2026-08-01",
        }]
        rows[0] = top

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        diff_transform=add_wrong_legacy_owner,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_multi_id_scope_uses_newest_result_from_each_anchored_lineage(
    tmp_path,
) -> None:
    detail = {
        "slug": "combined-history",
        "name": "Combined history committee",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_other_repair = [*initial, _transaction_row("9", "unrelated")]

    def overwrite_both(rows, paired_global, current_global):
        for index, row in enumerate(rows):
            anchor = _exact_observation(
                row, started="2026-09-02T00:00:00Z",
                fingerprint=paired_global,
                missing=[f"old-{index}"] if index == 0 else [],
            )
            current = _exact_observation(
                row, started="2026-09-03T00:00:00Z",
                fingerprint=current_global,
                missing=["new-one"] if index == 0 else [],
            )
            current["usable_history"] = [anchor]
            rows[index] = current

    ids, _mode, end, _resume, status, _output = _run_selector(
        tmp_path,
        [
            {"filer_id": "1", "complete": False, "missing": ["placeholder"]},
            {"filer_id": "2", "complete": True, "missing": []},
        ],
        details=[detail],
        transaction_rows=initial,
        post_evidence_rows=after_other_repair,
        diff_transform=overwrite_both,
        with_status=True,
    )

    assert ids == "1"
    assert end == "2026-09-02"
    assert status == "selected"


def test_multi_id_scope_selects_one_common_anchored_range(tmp_path) -> None:
    root = tmp_path
    detail = {
        "slug": "combined-range",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "placeholder",
        },
    }

    def put_different_ranges_on_top(rows, paired_global, _current_global):
        snapshots_r2 = transaction_filer_snapshots(
            root / "data" / "transactions", ["1"],
            date(2006, 1, 1), date(2026, 9, 3),
        )
        snapshots_r3 = transaction_filer_snapshots(
            root / "data" / "transactions", ["2"],
            date(2006, 1, 1), date(2026, 9, 4),
        )
        for index, (range_end, snapshots) in enumerate((
            ("2026-09-03", snapshots_r2),
            ("2026-09-04", snapshots_r3),
        )):
            row = rows[index]
            anchor = _exact_observation(
                row, started="2026-09-02T00:00:00Z",
                fingerprint=paired_global,
                missing=["common-missing"] if index == 0 else [],
            )
            top = _exact_observation(
                row, started=f"2026-09-0{3 + index}T00:00:00Z",
                fingerprint=paired_global,
                missing=[f"different-{index}"] if index == 0 else [],
                digest=snapshots[str(index + 1)]["filer_transaction_digest"],
                range_end=range_end,
            )
            top["usable_history"] = [anchor]
            rows[index] = top

    ids, _mode, end, _resume, status, _output = _run_selector(
        root,
        [
            {"filer_id": "1", "complete": False, "missing": ["placeholder"]},
            {"filer_id": "2", "complete": True, "missing": []},
        ],
        details=[detail],
        diff_transform=put_different_ranges_on_top,
        with_status=True,
    )

    assert ids == "1"
    assert end == "2026-09-02"
    assert status == "selected"


def test_active_remediation_range_wins_over_newer_anchored_range(
    tmp_path,
) -> None:
    root = tmp_path
    progress = [{
        "key": [
            "ALL", "2006-01-01", "2026-09-02",
            "None", "None", "None", "1",
        ],
        "reported": 1,
    }]

    def add_newer_range(rows, paired_global, _current_global):
        newer_digest = transaction_filer_snapshots(
            root / "data" / "transactions", ["1"],
            date(2006, 1, 1), date(2026, 9, 3),
        )["1"]["filer_transaction_digest"]
        active = _exact_observation(
            rows[0], started="2026-09-02T00:00:00Z",
            fingerprint=paired_global, missing=["active-missing"],
        )
        newer = _exact_observation(
            rows[0], started="2026-09-03T00:00:00Z",
            fingerprint=paired_global, missing=["newer-missing"],
            digest=newer_digest, range_end="2026-09-03",
        )
        newer["usable_history"] = [active]
        rows[0] = newer

    ids, _mode, end, resume, status, _output = _run_selector(
        root,
        [{"filer_id": "1", "complete": False, "missing": ["placeholder"]}],
        progress_rows=progress,
        diff_transform=add_newer_range,
        with_status=True,
    )

    assert ids == "1"
    assert end == "2026-09-02"
    assert resume == "true"
    assert status == "selected"


def test_conflicting_active_ranges_block_multi_id_scope(tmp_path) -> None:
    detail = {
        "slug": "combined-active",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    progress = [
        {"key": ["ALL", "2006-01-01", "2026-09-02",
                 "None", "None", "None", "1"], "reported": 1},
        {"key": ["ALL", "2006-01-01", "2026-09-03",
                 "None", "None", "None", "2"], "reported": 1},
    ]

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [
            {"filer_id": "1", "complete": False, "missing": ["one"]},
            {"filer_id": "2", "complete": True, "missing": []},
        ],
        details=[detail],
        progress_rows=progress,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


@pytest.mark.parametrize(
    "started",
    [
        "2026-08-31T23:59:59.999999Z",
        "2026-09-01T00:00:00Z",
    ],
)
def test_query_started_before_or_at_summary_blocks_even_if_it_ended_after(
    tmp_path, started,
) -> None:
    row = {
        "filer_id": "1",
        "complete": False,
        "missing": ["x"],
        "collection_started_at": started,
        "checked_at": "2026-09-02T00:00:00Z",
    }
    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path, [row], with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_same_scope_repair_invalidates_old_exact_evidence(tmp_path) -> None:
    initial = [_transaction_row("1", "one")]
    after_repair = [*initial, _transaction_row("1", "one-repaired")]

    ids, mode, end, resume, status, output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["one-repaired"]}],
        transaction_rows=initial,
        post_evidence_rows=after_repair,
        with_status=True,
    )

    assert ids is None
    assert mode == "identity"
    assert end is None
    assert resume == "false"
    assert status == "blocked"
    assert "not a completion signal" in output


def test_changed_multi_id_member_invalidates_whole_scope(tmp_path) -> None:
    detail = {
        "slug": "combined",
        "name": "Combined committee",
        "filer_ids": ["1", "2"],
        "orestar_comparison": {
            "status": "paired",
            "captured_at": 1_788_220_800,
            "app_transaction_snapshot_id": "placeholder",
        },
    }
    initial = [_transaction_row("1", "one"), _transaction_row("2", "two")]
    after_repair = [*initial, _transaction_row("2", "two-repaired")]

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [
            {"filer_id": "1", "complete": False, "missing": ["missing-one"]},
            {"filer_id": "2", "complete": True, "missing": []},
        ],
        details=[detail],
        transaction_rows=initial,
        post_evidence_rows=after_repair,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_duplicate_canonical_scope_claim_is_ambiguous(tmp_path) -> None:
    comparison = {
        "status": "paired",
        "captured_at": 1_788_220_800,
        "app_transaction_snapshot_id": "placeholder",
    }
    details = [
        {"slug": "first", "filer_ids": ["1"], "orestar_comparison": comparison},
        {"slug": "second", "filer_ids": ["1"], "orestar_comparison": comparison},
    ]

    ids, _mode, _end, _resume, status, _output = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["x"]}],
        details=details,
        with_status=True,
    )

    assert ids is None
    assert status == "blocked"


def test_selector_resumes_active_tree_before_larger_new_candidate(tmp_path) -> None:
    root = {
        "key": ["ALL", "2006-01-01", "2026-09-02", "None", "None", "None", "2"],
        "reported": 6000,
    }
    ids, mode, end, resume = _run_selector(
        tmp_path,
        [
            {"filer_id": "1", "complete": False, "missing": list("abcdefghij")},
            {"filer_id": "2", "complete": False, "missing": ["x"]},
        ],
        incomplete="2:5\n",
        progress_rows=[root],
    )
    assert ids == "2"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "true"
    assert (tmp_path / "data" / "incomplete_backfills.txt").read_text() == "2:5\n"


def test_stale_active_tree_range_is_not_resumed(tmp_path) -> None:
    root = {
        "key": ["ALL", "2006-01-01", "2026-08-31", "None", "None", "None", "1"],
        "reported": 6000,
    }
    ids, mode, end, resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["x"]}],
        progress_rows=[root],
    )

    assert ids == "1"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"


def test_legacy_clean_diff_cannot_authorize_weaker_count_automation(tmp_path) -> None:
    ids, mode, _end, _resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": True, "missing": [],
          "checked": "2026-08-01"}],
        [{"filer_id": "1", "missing": 2, "checked": "2026-09-01"}],
        legacy_exact=True,
    )
    assert ids is None
    assert mode == "identity"


def test_deferred_exact_missing_is_retried_after_other_identity_work(tmp_path) -> None:
    ids, mode, end, resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["x"]}],
        incomplete="1:3\n",
    )
    assert ids == "1"
    assert mode == "identity"
    assert end == "2026-09-02"
    assert resume == "false"


def test_identity_cli_rejects_partial_history_scope() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "fetch.py"),
            "--filer-ids", "1",
            "--identity-remediation",
            "--end-date", "2026-09-02",
            "--start-year", "2020",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --start-year=2006" in result.stderr


def test_verification_cli_requires_fresh_full_history() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
            "--recheck",
            "--start-year", "2020",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --start-year=2006" in result.stderr


def test_verification_cli_cannot_disable_recheck() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --recheck" in result.stderr


def test_verification_cli_requires_frozen_end_date() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
            "--recheck",
            "--start-year", "2006",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires a frozen --end-date" in result.stderr


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        ("-1", "--limit must be zero or positive"),
        ("1", "--require-no-missing cannot be combined with --limit"),
    ],
)
def test_verification_cli_cannot_limit_targets(limit, message) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
            "--recheck",
            "--limit", limit,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert message in result.stderr


def test_remediation_gate_allows_surplus_but_requires_fresh_zero_missing() -> None:
    entries = {
        "1": {"complete": False, "missing": [], "surplus": ["old"]},
        "2": {"complete": False, "missing": ["still-missing"]},
    }
    assert DC._remediation_verification_failures(entries, ["1"], {"1"}) == []
    assert DC._remediation_verification_failures(entries, ["1"], set()) == [
        "1: no fresh usable diff"
    ]
    assert DC._remediation_verification_failures(entries, ["2"], {"2"}) == [
        "2: 1 missing IDs remain"
    ]


def test_remediation_gate_requires_exact_current_run_provenance() -> None:
    started = 1_788_220_800
    exact = {
        "complete": False,
        "missing": [],
        "surplus": ["withdrawn"],
        "evidence_version": 2, "filer_digest_version": 2,
        "collection_started_at": "2026-09-01T00:00:00.000001Z",
        "checked_at": "2026-09-01T00:00:00.000001Z",
        "transaction_snapshot_id": "sha256:current",
        "filer_transaction_digest": "sha256:filer-current",
        "range_start": "2006-01-01",
        "range_end": "2026-09-01",
    }
    kwargs = {
        "verification_started_at": started,
        "transaction_id": "sha256:current",
        "filer_digests": {"1": "sha256:filer-current"},
        "start": date(2006, 1, 1),
        "end": date(2026, 9, 1),
    }

    assert DC._remediation_verification_failures(
        {"1": exact}, ["1"], {"1"}, **kwargs,
    ) == []
    for broken in (
        {**exact, "checked_at": "2026-09-01"},
        {**exact, "transaction_snapshot_id": "sha256:old"},
        {**exact, "filer_transaction_digest": "sha256:filer-old"},
        {**exact, "range_end": "2026-08-31"},
    ):
        assert DC._remediation_verification_failures(
            {"1": broken}, ["1"], {"1"}, **kwargs,
        ) == ["1: fresh diff provenance does not match verification"]


def test_gate_retry_targets_only_transient_inconclusive_filers() -> None:
    entries = {"clean": {"missing": []}}
    assert DC._retryable_gate_targets(
        ["clean", "retry"], entries, {"clean"},
        {"retry": "session_expired"},
    ) == ["retry"]
    assert DC._retryable_gate_targets(
        ["retry"], entries, set(), {"retry": "unusable_window"},
    ) == ["retry"]
    assert DC._retryable_gate_targets(
        ["first", "second", "not-attempted"], entries, set(),
        {"first": "session_expired", "second": "unusable_window"},
    ) == ["first", "second", "not-attempted"]


@pytest.mark.parametrize("reason", ["partition_mismatch", "time_budget"])
def test_gate_retry_rejects_structural_or_budget_refusals(reason) -> None:
    assert DC._retryable_gate_targets(
        ["stop"], {}, set(), {"stop": reason},
    ) == []


def test_gate_retry_rejects_any_fresh_missing_result() -> None:
    entries = {
        "missing": {"missing": ["123"]},
        "retry": {"missing": []},
    }
    assert DC._retryable_gate_targets(
        ["missing", "retry"], entries, {"missing"},
        {"retry": "session_expired"},
    ) == []
