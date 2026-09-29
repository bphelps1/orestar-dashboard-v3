"""Focused regression tests for coverage-diff refusal hardening.

These tests exercise only pure ordering/parsing/state helpers.  They make no
ORESTAR requests and do not require a database.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import diff_coverage as DC  # noqa: E402
import process as P  # noqa: E402


def _ids(targets: list[dict]) -> list[str]:
    return [str(target["filer_id"]) for target in targets]


def _assert_attempt_is_today(value: str) -> None:
    """Accept either a date or an ISO datetime for diagnostic timestamps."""
    assert datetime.fromisoformat(value).date() == datetime.now(timezone.utc).date()


def test_split_can_divide_a_two_day_window() -> None:
    start = date(2026, 8, 27)
    end = start + timedelta(days=1)

    assert DC._split(start, end) == [(start, start), (end, end)]


def test_current_snapshot_fingerprints_local_shards_even_if_source_trails(
    tmp_path, monkeypatch,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    (transactions / "txn_2026.csv.gz").write_bytes(b"exact shard bytes")
    source = tmp_path / "balance_snapshot_source.json"
    monkeypatch.setattr(DC, "TRANSACTION_DIR", transactions)
    monkeypatch.setattr(DC, "SNAPSHOT_SOURCE_PATH", source)

    expected = DC.transaction_snapshot_id(transactions)
    assert DC._current_transaction_snapshot_id() == expected
    source.write_text(json.dumps({"transaction_snapshot_id": "sha256:stale"}))
    assert DC._current_transaction_snapshot_id() == expected
    source.write_text(json.dumps({"transaction_snapshot_id": expected}))
    assert DC._current_transaction_snapshot_id() == expected


def test_usable_evidence_fields_are_precise_and_range_bound() -> None:
    fields = DC._evidence_fields(
        "sha256:exact",
        "sha256:filer-exact",
        date(2006, 1, 1),
        date(2026, 9, 5),
        collection_started_at="2026-09-05T12:34:55.123456Z",
        checked_at="2026-09-05T12:34:56.123456Z",
    )

    assert fields == {
        "evidence_version": 2, "filer_digest_version": 2,
        "checked": "2026-09-05",
        "collection_started_at": "2026-09-05T12:34:55.123456Z",
        "checked_at": "2026-09-05T12:34:56.123456Z",
        "transaction_snapshot_id": "sha256:exact",
        "filer_transaction_digest": "sha256:filer-exact",
        "range_start": "2006-01-01",
        "range_end": "2026-09-05",
    }


def _usable_result(
    filer_id: str,
    started: str,
    fingerprint: str,
    *,
    digest: str = "sha256:filer-state",
    range_end: str = "2026-09-02",
    missing=None,
    surplus=None,
    name: str = "Committee",
) -> dict:
    missing = list(missing or [])
    surplus = list(surplus or [])
    completed = started.replace("00Z", "01Z")
    return {
        "filer_id": filer_id,
        "name": name,
        "orestar": 1 + len(missing),
        "held": 1 + len(surplus),
        "complete": not missing and not surplus,
        "missing": missing,
        "surplus": surplus,
        "superseded": [],
        "evidence_version": 2, "filer_digest_version": 2,
        "collection_started_at": started,
        "checked": completed[:10],
        "checked_at": completed,
        "transaction_snapshot_id": fingerprint,
        "filer_transaction_digest": digest,
        "range_start": "2006-01-01",
        "range_end": range_end,
    }


def _active_requirement(
    *filer_ids: str,
    active_range_end: str | None = None,
) -> dict[str, dict]:
    requirement = {
        "transaction_snapshot_id": "sha256:G1",
        "captured_at": datetime.fromisoformat(
            "2026-09-01T00:00:00+00:00"
        ).timestamp(),
        "scope_ids": list(filer_ids),
        "active_range_end": active_range_end,
        "active_range_conflict": False,
    }
    return {filer_id: requirement for filer_id in filer_ids}


@pytest.mark.parametrize(
    ("detail_ids", "captured_ids"),
    [
        (["1"], ["1", "2"]),
        (["1", "2"], ["1"]),
        (["1"], None),
        (["1"], "1"),
    ],
)
def test_history_pinning_rejects_drifted_or_legacy_capture_scope(
    tmp_path, monkeypatch, detail_ids, captured_ids,
) -> None:
    filers = tmp_path / "filers"
    filers.mkdir()
    (filers / "scope.json").write_text(json.dumps({
        "filer_ids": detail_ids,
        "orestar_comparison": {
            "status": "paired",
            "captured_at": datetime.fromisoformat(
                "2026-09-01T00:00:00+00:00"
            ).timestamp(),
            "filer_ids": captured_ids,
            "app_transaction_snapshot_id": "sha256:G1",
        },
    }))
    monkeypatch.setattr(DC, "FILERS_DIR", filers)
    monkeypatch.setattr(DC, "IDENTITY_PROGRESS_PATH", tmp_path / "none.json")

    assert DC._active_paired_requirements() == {}


def test_successful_rediff_archives_the_paired_anchor() -> None:
    anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=["old"],
    )
    newer = _usable_result(
        "1", "2026-09-03T00:00:00Z", "sha256:G2", missing=["new"],
    )
    entries = {"1": anchor}

    stored = DC._store_usable_result(
        entries, newer, active_requirements=_active_requirement("1"),
    )

    assert stored["transaction_snapshot_id"] == "sha256:G2"
    assert stored["missing"] == ["new"]
    assert stored[DC.USABLE_HISTORY_KEY] == [anchor]


@pytest.mark.parametrize(
    "field",
    ["transaction_snapshot_id", "filer_transaction_digest"],
)
def test_usable_history_rejects_blank_provenance_identifiers(field) -> None:
    result = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=["one"],
    )
    result[field] = "   "

    with pytest.raises(ValueError, match="invalid provenance"):
        DC._store_usable_result(
            {}, result, active_requirements=_active_requirement("1"),
        )


def test_out_of_order_rediff_cannot_replace_a_newer_orestar_result() -> None:
    newest = _usable_result(
        "1", "2026-09-04T00:00:00Z", "sha256:G2", missing=["newest"],
    )
    older = _usable_result(
        "1", "2026-09-03T00:00:00Z", "sha256:G1", missing=["older"],
    )
    entries = {"1": newest}

    stored = DC._store_usable_result(
        entries, older, active_requirements=_active_requirement("1"),
    )

    assert stored["collection_started_at"] == newest["collection_started_at"]
    assert stored["missing"] == ["newest"]
    assert older in stored[DC.USABLE_HISTORY_KEY]


def test_history_is_bounded_while_anchor_and_lineage_head_survive() -> None:
    anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=["anchor"],
    )
    lineage_head = _usable_result(
        "1", "2026-09-03T00:00:00Z", "sha256:G2", missing=["head"],
    )
    entries = {"1": anchor}
    requirements = _active_requirement("1")
    DC._store_usable_result(
        entries, lineage_head, active_requirements=requirements,
    )
    for day in range(4, 16):
        DC._store_usable_result(
            entries,
            _usable_result(
                "1", f"2026-09-{day:02d}T00:00:00Z", f"sha256:G{day}",
                digest=f"sha256:state-{day}", range_end=f"2026-09-{day:02d}",
            ),
            active_requirements=requirements,
        )

    observations = [entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]
    assert len(observations) <= DC.USABLE_OBSERVATION_LIMIT
    assert any(row["transaction_snapshot_id"] == "sha256:G1"
               for row in observations)
    assert any(row["missing"] == ["head"] for row in observations)


def test_scope_aware_pruning_preserves_a_common_multi_id_anchor_range() -> None:
    requirements = _active_requirement("1", "2")
    first = _usable_result("1", "2026-09-02T00:00:00Z", "sha256:G1")
    second = _usable_result("2", "2026-09-02T00:00:00Z", "sha256:G1")
    entries = {"1": first, "2": second}

    for day in range(3, 15):
        DC._store_usable_result(
            entries,
            _usable_result(
                "1", f"2026-09-{day:02d}T00:00:00Z", "sha256:G1",
                digest=f"sha256:state-{day}", range_end=f"2026-09-{day:02d}",
            ),
            active_requirements=requirements,
        )

    observations = [entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]
    assert len(observations) <= DC.USABLE_OBSERVATION_LIMIT
    assert any(
        row["transaction_snapshot_id"] == "sha256:G1"
        and row["range_end"] == "2026-09-02"
        for row in observations
    )


def test_history_pruning_prefers_the_live_remediation_range() -> None:
    requirements = _active_requirement("1", active_range_end="2026-09-02")
    active_anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1",
        missing=["active"],
    )
    entries = {"1": active_anchor}

    for day in range(3, 15):
        DC._store_usable_result(
            entries,
            _usable_result(
                "1", f"2026-09-{day:02d}T00:00:00Z", "sha256:G1",
                digest=f"sha256:state-{day}", range_end=f"2026-09-{day:02d}",
            ),
            active_requirements=requirements,
        )

    observations = [entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]
    assert len(observations) <= DC.USABLE_OBSERVATION_LIMIT
    assert any(
        row["range_end"] == "2026-09-02"
        and row["missing"] == ["active"]
        for row in observations
    )


def test_equal_start_conflicts_are_both_retained_for_fail_closed_selection() -> None:
    anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=["one"],
    )
    conflict = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=[],
    )
    # Give the second completion a later instant while keeping the same query
    # start. Completion ordering must not silently resolve a verdict conflict.
    conflict["checked_at"] = "2026-09-02T00:00:02Z"
    entries = {"1": anchor}

    DC._store_usable_result(
        entries, conflict, active_requirements=_active_requirement("1"),
    )

    observations = [entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]
    tied = [
        row for row in observations
        if row["collection_started_at"] == "2026-09-02T00:00:00Z"
    ]
    assert len(tied) == 2
    assert {tuple(row["missing"]) for row in tied} == {(), ("one",)}


def test_conflicting_paired_digest_anchors_cannot_age_out() -> None:
    first_anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1",
        digest="sha256:D1", missing=["one"],
    )
    conflicting_anchor = _usable_result(
        "1", "2026-09-02T00:00:01Z", "sha256:G1",
        digest="sha256:D2", missing=["two"],
    )
    first_anchor[DC.USABLE_HISTORY_KEY] = [conflicting_anchor]
    entries = {"1": first_anchor}
    requirements = _active_requirement("1")

    for day in range(3, 15):
        DC._store_usable_result(
            entries,
            _usable_result(
                "1", f"2026-09-{day:02d}T00:00:00Z", f"sha256:G{day}",
                digest=f"sha256:other-{day}",
                range_end=f"2026-09-{day:02d}",
            ),
            active_requirements=requirements,
        )

    observations = [entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]
    anchors = [
        row for row in observations
        if row["transaction_snapshot_id"] == "sha256:G1"
        and row["range_end"] == "2026-09-02"
    ]
    assert len(observations) <= DC.USABLE_OBSERVATION_LIMIT
    assert {row["filer_transaction_digest"] for row in anchors} == {
        "sha256:D1", "sha256:D2",
    }


def test_history_bound_refuses_when_all_slots_are_required_pins() -> None:
    observations = []
    for index in range(4):
        digest = f"sha256:D{index}"
        observations.extend([
            _usable_result(
                "1", f"2026-09-02T00:00:0{index}Z", "sha256:G1",
                digest=digest, missing=[f"anchor-{index}"],
            ),
            _usable_result(
                "1", f"2026-09-03T00:00:0{index}Z", "sha256:G2",
                digest=digest, missing=[f"head-{index}"],
            ),
        ])
    entries = {"1": {**observations[0], DC.USABLE_HISTORY_KEY: observations[1:]}}
    incoming = _usable_result(
        "1", "2026-09-04T00:00:00Z", "sha256:G3",
        digest="sha256:unrelated-lane", range_end="2026-09-04",
    )

    with pytest.raises(ValueError, match="no bounded slot"):
        DC._store_usable_result(
            entries, incoming, active_requirements=_active_requirement("1"),
        )

    # Refusal happens before assignment, so the already-bounded record remains.
    assert len([entries["1"], *entries["1"][DC.USABLE_HISTORY_KEY]]) == 8


def test_failed_rediff_preserves_current_and_bounded_usable_history() -> None:
    anchor = _usable_result(
        "1", "2026-09-02T00:00:00Z", "sha256:G1", missing=["old"],
    )
    current = _usable_result(
        "1", "2026-09-03T00:00:00Z", "sha256:G2", missing=["current"],
    )
    current[DC.USABLE_HISTORY_KEY] = [anchor]
    entries = {"1": current}

    DC._record_failure(
        entries,
        {"filer_id": "1", "name": "Committee"},
        "session_expired",
        transaction_id="sha256:G3",
        filer_digest="sha256:filer-state",
        start=date(2006, 1, 1),
        end=date(2026, 9, 2),
        collection_started_at="2026-09-04T00:00:00Z",
        attempted_at="2026-09-04T00:00:01Z",
    )

    assert entries["1"]["missing"] == ["current"]
    assert entries["1"][DC.USABLE_HISTORY_KEY] == [anchor]
    assert entries["1"]["last_failure"] == "session_expired"


def test_exact_local_ids_are_read_from_the_fingerprinted_shards(
    tmp_path, monkeypatch,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    shard = transactions / "txn_2026.csv.gz"
    with gzip.open(shard, "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "tran_id", "original id", "tran_date", "filer id",
        ])
        writer.writeheader()
        writer.writerows([
            {"tran_id": "10", "original id": "10", "tran_date": "09/01/2026",
             "filer id": "1"},
            {"tran_id": "11", "original id": "10", "tran_date": "09/02/2026",
             "filer id": "1"},
            {"tran_id": "12", "original id": "12", "tran_date": "09/03/2026",
             "filer id": "1"},
            {"tran_id": "20", "original id": "20", "tran_date": "09/02/2026",
             "filer id": "2"},
        ])
    monkeypatch.setattr(DC, "TRANSACTION_DIR", transactions)

    rows = DC._local_held_ids(
        ["1", "2", "3"], date(2026, 9, 1), date(2026, 9, 2)
    )

    assert rows == {
        "1": ({"10", "11"}, {"10"}),
        "2": ({"20"}, set()),
        "3": (set(), set()),
    }


def test_exact_local_ids_refuse_an_unparseable_target_date(
    tmp_path, monkeypatch,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    shard = transactions / "txn_2026.csv.gz"
    with gzip.open(shard, "wt", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["tran_id", "original id", "tran_date", "filer id"],
        )
        writer.writeheader()
        writer.writerow({
            "tran_id": "10", "original id": "10",
            "tran_date": "not-a-date", "filer id": "1",
        })
    monkeypatch.setattr(DC, "TRANSACTION_DIR", transactions)

    with pytest.raises(ValueError, match="unparseable transaction date"):
        DC._local_held_ids(["1"], date(2006, 1, 1), date(2026, 9, 5))


def test_exact_local_ids_refuse_a_blank_target_date(
    tmp_path, monkeypatch,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    shard = transactions / "txn_2026.csv.gz"
    with gzip.open(shard, "wt", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["tran_id", "original id", "tran_date", "filer id"],
        )
        writer.writeheader()
        writer.writerow({
            "tran_id": "10", "original id": "10",
            "tran_date": "", "filer id": "1",
        })
    monkeypatch.setattr(DC, "TRANSACTION_DIR", transactions)

    with pytest.raises(ValueError, match="target transaction has no date"):
        DC._local_held_ids(["1"], date(2006, 1, 1), date(2026, 9, 5))


@pytest.mark.parametrize(
    ("omitted", "message"),
    [
        ("filer id", "filer id/filer_id"),
        ("original id", "original id/original_id"),
    ],
)
def test_every_transaction_shard_requires_identity_columns(
    tmp_path, omitted, message,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    fields = ["tran_id", "original id", "tran_date", "filer id"]
    with gzip.open(transactions / "txn_2025.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "tran_id": "10", "original id": "10",
            "tran_date": "09/01/2025", "filer id": "1",
        })
    bad_fields = [field for field in fields if field != omitted]
    with gzip.open(transactions / "txn_2026.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=bad_fields)
        writer.writeheader()
        # This is deliberately an unrelated filer. Schema omissions still
        # fail the whole certified snapshot rather than looking like no rows.
        writer.writerow({
            key: value for key, value in {
                "tran_id": "20", "original id": "20",
                "tran_date": "09/01/2026", "filer id": "2",
            }.items() if key in bad_fields
        })

    with pytest.raises(ValueError, match=message):
        DC.transaction_filer_snapshots(
            transactions, ["1"], date(2006, 1, 1), date(2026, 9, 5)
        )


def test_per_filer_digest_changes_only_for_that_filers_range_content(
    tmp_path,
) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    shard = transactions / "txn_2026.csv.gz"
    fields = ["tran_id", "original id", "tran_date", "filer id", "amount"]

    def write(rows):
        with gzip.open(shard, "wt", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    rows = [
        {"tran_id": "10", "original id": "10", "tran_date": "09/01/2026",
         "filer id": "1", "amount": "1.00"},
        {"tran_id": "20", "original id": "20", "tran_date": "09/01/2026",
         "filer id": "2", "amount": "2.00"},
    ]
    write(rows)
    before = DC.transaction_filer_snapshots(
        transactions, ["1", "2"], date(2006, 1, 1), date(2026, 9, 5)
    )
    write([*rows, {
        "tran_id": "21", "original id": "21", "tran_date": "09/02/2026",
        "filer id": "2", "amount": "3.00",
    }])
    after = DC.transaction_filer_snapshots(
        transactions, ["1", "2"], date(2006, 1, 1), date(2026, 9, 5)
    )

    assert (
        before["1"]["filer_transaction_digest"]
        == after["1"]["filer_transaction_digest"]
    )
    assert (
        before["2"]["filer_transaction_digest"]
        != after["2"]["filer_transaction_digest"]
    )


def test_narrowing_ladder_reaches_type_date_amount_and_prefix() -> None:
    start = date(2026, 8, 27)
    end = start + timedelta(days=1)

    typed = DC.F._narrow_filer("ALL", start, end, None, None, None)
    assert [window[0] for window in typed] == DC.F.TRAN_TYPES

    halves = DC.F._narrow_filer("C", start, end, None, None, None)
    assert [(window[1], window[2]) for window in halves] == [
        (start, start),
        (end, end),
    ]

    amount_bands = DC.F._narrow_filer("C", start, start, None, None, None)
    assert [(window[3], window[4]) for window in amount_bands] == DC.F.AMOUNT_BANDS

    prefixes = DC.F._narrow_filer("C", start, start, "25", "49.99", None)
    assert [window[5] for window in prefixes] == DC.F.PAYEE_PREFIXES
    assert DC.F._narrow_filer("C", start, start, "25", "49.99", "A") == []


def test_local_date_seeds_cover_the_entire_requested_range() -> None:
    start = date(2026, 1, 1)
    end = date(2026, 1, 10)

    windows = DC._build_date_seed_windows(
        start,
        end,
        [
            (date(2026, 1, 2), 3_000),
            (date(2026, 1, 4), 1_500),
            (date(2026, 1, 7), 5_000),
        ],
        target_rows=4_000,
    )

    assert windows == [
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 1, 4), date(2026, 1, 6)),
        (date(2026, 1, 7), date(2026, 1, 7)),
        (date(2026, 1, 8), date(2026, 1, 10)),
    ]
    assert DC._date_windows_cover(start, end, windows)


def test_parse_export_rows_extracts_exact_transaction_ids() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Tran ID", "Amount"])
    sheet.append([123, 10.0])
    sheet.append([456, 20.0])
    payload = io.BytesIO()
    workbook.save(payload)

    assert DC._parse_export_rows(payload.getvalue()) == {
        "123": {"amount": 10.0},
        "456": {"amount": 20.0},
    }


class _CountPage:
    def __init__(self) -> None:
        self.url = DC.F.SEARCH_URL
        self.filled: dict[str, str] = {}
        self.selected: dict[str, str] = {}
        self.wait_for_url_timeout: int | None = None

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        return None

    def fill(self, selector: str, value: str, **_kwargs) -> None:
        self.filled[selector] = value

    def select_option(self, selector: str, value: str, **_kwargs) -> None:
        self.selected[selector] = value

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def click(self, _selector: str, **_kwargs) -> None:
        self.url = f"{DC.F.BASE_URL}/gotoPublicTransactionSearchResults.do"

    def wait_for_url(self, *_args, **kwargs) -> None:
        self.wait_for_url_timeout = kwargs.get("timeout")
        return None

    def inner_text(self, _selector: str, **_kwargs) -> str:
        return "2 records found"


def test_shared_count_search_applies_every_narrowing_filter() -> None:
    page = _CountPage()
    day = date(2026, 8, 28)

    count = DC.SC.orestar_count(
        page, "23285", day, day, "C", "15", "15.99", "J"
    )

    assert count == 2
    assert page.selected['select[name="cneSearchTranType"]'] == "C"
    assert page.filled['input[name="cneSearchTranAmountFrom"]'] == "15"
    assert page.filled['input[name="cneSearchTranAmountTo"]'] == "15.99"
    assert page.filled['input[name="cneSearchContributorTxt"]'] == "J"
    assert page.selected[
        'select[name="cneSearchContributorTxtSearchType"]'
    ] == "S"


def test_count_search_bounds_its_wait_to_the_absolute_deadline(monkeypatch) -> None:
    page = _CountPage()
    monkeypatch.setattr(DC.SC.time, "monotonic", lambda: 10.0)

    assert DC.SC.orestar_count(
        page,
        "23285",
        date(2026, 8, 28),
        date(2026, 8, 28),
        deadline=12.0,
    ) == 2
    assert 0 < page.wait_for_url_timeout <= 2_000


def test_count_search_refuses_to_start_after_its_deadline(monkeypatch) -> None:
    page = _CountPage()
    monkeypatch.setattr(DC.SC.time, "monotonic", lambda: 10.0)

    with pytest.raises(DC.SC.SearchDeadlineExceeded):
        DC.SC.orestar_count(
            page,
            "23285",
            date(2026, 8, 28),
            date(2026, 8, 28),
            deadline=5.0,
        )


def test_playwright_timeout_at_deadline_becomes_search_deadline(monkeypatch) -> None:
    clock = {"now": 0.0}

    class TimeoutPage(_CountPage):
        def fill(self, *_args, **_kwargs):
            clock["now"] = 5.0
            raise DC.SC.PlaywrightTimeout("deadline-clipped fill")

    monkeypatch.setattr(DC.SC.time, "monotonic", lambda: clock["now"])

    with pytest.raises(DC.SC.SearchDeadlineExceeded):
        DC.SC.orestar_count(
            TimeoutPage(),
            "23285",
            date(2026, 8, 28),
            date(2026, 8, 28),
            deadline=5.0,
        )


def test_collect_window_requires_export_ids_to_match_reported_count(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 2), None)
    context = object()
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(DC, "_export_rows", lambda *_args: {"1": {}, "2": {}})

    complete = DC._collect_window(
        page, "123", date(2026, 8, 28), date(2026, 8, 28), context=context
    )
    assert complete == {"reported": 2, "rows": {"1": {}, "2": {}}}

    monkeypatch.setattr(DC, "_export_rows", lambda *_args: {"1": {}})
    assert DC._collect_window(
        page, "123", date(2026, 8, 28), date(2026, 8, 28), context=context
    ) is None


def test_browser_export_fallback_preserves_path_session(monkeypatch, tmp_path) -> None:
    payload = tmp_path / "export.xlsx"
    payload.write_bytes(b"valid-export")

    class Download:
        def path(self):
            return payload

    class DownloadInfo:
        value = Download()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Page:
        url = (
            f"{DC.F.BASE_URL}/gotoPublicTransactionSearchResults.do"
            ";JSESSIONID_ORESTAR=session-123?search=1"
        )
        goto_url = None

        def evaluate(self, _script):
            return "csrf-456"

        def expect_download(self, **_kwargs):
            return DownloadInfo()

        def goto(self, url, **_kwargs):
            self.goto_url = url
            raise DC.PlaywrightError("Page.goto: Download is starting")

    class Context:
        def cookies(self):
            return []

    class HtmlResponse:
        headers = {"Content-Type": "text/html"}
        content = b"<html>not an export</html>"

    page = Page()
    monkeypatch.setattr(DC.F.requests, "get", lambda *_args, **_kwargs: HtmlResponse())
    monkeypatch.setattr(
        DC,
        "_parse_export_rows",
        lambda content: {"1": {}} if content == b"valid-export" else None,
    )

    assert DC._export_rows(page, Context(), "23285", "test") == {"1": {}}
    assert page.goto_url == (
        f"{DC.F.EXPORT_URL};JSESSIONID_ORESTAR=session-123"
        "?OWASP_CSRFTOKEN=csrf-456"
    )


def test_parse_rows_accepts_accounting_parentheses_for_negative_amounts() -> None:
    text = (
        "3865001\t08/27/2026\tOriginal\tTest Committee\tState Bank\t"
        "Cash Balance Adjustment\t($1,268.50)"
    )

    assert DC._parse_rows(text) == {
        "3865001": {
            "date": "08/27/2026",
            "status": "Original",
            "payee": "State Bank",
            "sub_type": "Cash Balance Adjustment",
            "amount": -1268.50,
        }
    }


def test_prioritise_skips_a_usable_surplus_checked_today(monkeypatch) -> None:
    monkeypatch.setattr(DC, "_costs", lambda _filer_ids: {})
    targets = [
        {"filer_id": "recent", "name": "Recent surplus"},
        {"filer_id": "fresh", "name": "Never measured"},
    ]
    entries = {
        "recent": {
            "filer_id": "recent",
            "complete": False,
            "surplus": ["withdrawn-1"],
            "missing": [],
            "checked": date.today().isoformat(),
        }
    }

    ordered = _ids(DC._prioritise(targets, entries))

    assert "recent" not in ordered
    assert ordered == ["fresh"]


def test_prioritise_interleaves_null_failures_with_fresh_targets(monkeypatch) -> None:
    monkeypatch.setattr(DC, "_costs", lambda _filer_ids: {})
    targets = [
        {"filer_id": "failed-1", "name": "Failed one"},
        {"filer_id": "failed-2", "name": "Failed two"},
        {"filer_id": "fresh-1", "name": "Fresh one"},
        {"filer_id": "fresh-2", "name": "Fresh two"},
        {"filer_id": "usable", "name": "Usable clean"},
    ]
    entries = {
        # A legacy null may carry `checked`; use it as attempt timing without
        # ever treating it as usable evidence.
        "failed-1": {
            "filer_id": "failed-1",
            "complete": None,
            "checked": (date.today() - timedelta(days=2)).isoformat(),
        },
        "failed-2": {
            "filer_id": "failed-2",
            "complete": None,
            "last_attempt": (date.today() - timedelta(days=1)).isoformat(),
        },
        "usable": {
            "filer_id": "usable",
            "complete": True,
            "surplus": [],
            "missing": [],
            "checked": (date.today() - timedelta(days=1)).isoformat(),
        },
    }

    ordered = _ids(DC._prioritise(targets, entries))
    failure_positions = [ordered.index(fid) for fid in ("failed-1", "failed-2")]
    fresh_positions = [ordered.index(fid) for fid in ("fresh-1", "fresh-2")]

    assert set(ordered[:4]) == {"failed-1", "failed-2", "fresh-1", "fresh-2"}
    # Their position ranges overlap: neither category is exhausted before the
    # other begins.  This specifies interleaving without overfitting its ratio.
    assert min(failure_positions) < max(fresh_positions)
    assert min(fresh_positions) < max(failure_positions)
    assert ordered[-1] == "usable"


def test_prioritise_defers_a_failure_already_attempted_today(monkeypatch) -> None:
    monkeypatch.setattr(DC, "_costs", lambda _filer_ids: {})
    targets = [
        {"filer_id": "failed-today", "name": "Failed today"},
        {"filer_id": "fresh", "name": "Never measured"},
    ]
    entries = {
        "failed-today": {
            "filer_id": "failed-today",
            "complete": None,
            "last_attempt": date.today().isoformat(),
            "last_failure": "unusable_window",
        }
    }

    assert _ids(DC._prioritise(targets, entries)) == ["fresh"]


def test_prioritise_keeps_cost_order_and_one_giant_per_slice(monkeypatch) -> None:
    targets = [
        {"filer_id": "big-slow"},
        {"filer_id": "small-slow"},
        {"filer_id": "big-fast"},
        {"filer_id": "small-fast"},
    ]
    costs = {
        "big-slow": 9_000,
        "small-slow": 100,
        "big-fast": 6_000,
        "small-fast": 10,
    }
    monkeypatch.setattr(DC, "_costs", lambda _filer_ids: costs)

    assert _ids(DC._prioritise(targets, {})) == [
        "big-fast",
        "small-fast",
        "small-slow",
        "big-slow",
    ]


def test_record_failure_preserves_existing_usable_evidence() -> None:
    checked = (date.today() - timedelta(days=2)).isoformat()
    entries = {
        "19050": {
            "filer_id": "19050",
            "name": "Friends of Christine Drazan",
            "orestar": 4300,
            "held": 4273,
            "complete": False,
            "surplus": ["withdrawn-1", "withdrawn-2"],
            "missing": ["missing-1"],
            "superseded": ["superseded-1"],
            "checked": checked,
            "failure_count": 2,
        }
    }
    reason = "collected 42 of 97 rows"

    DC._record_failure(
        entries,
        {"filer_id": "19050", "name": "Friends of Christine Drazan"},
        reason,
    )

    saved = entries["19050"]
    assert saved["complete"] is False
    assert saved["surplus"] == ["withdrawn-1", "withdrawn-2"]
    assert saved["missing"] == ["missing-1"]
    assert saved["superseded"] == ["superseded-1"]
    assert saved["checked"] == checked
    assert saved["last_failure"] == reason
    assert saved["failure_count"] == 3
    _assert_attempt_is_today(saved["last_attempt"])


def test_target_name_preserves_known_name_for_id_only_runs() -> None:
    entries = {
        "23285": {
            "filer_id": "23285",
            "name": "Building a Stronger Oregon",
        }
    }

    assert DC._target_name(entries, {"filer_id": "23285", "name": ""}) == (
        "Building a Stronger Oregon"
    )
    assert DC._target_name(
        entries, {"filer_id": "23285", "name": "Updated committee name"}
    ) == "Updated committee name"


def test_record_failure_creates_an_unchecked_null_for_first_failure() -> None:
    entries: dict[str, dict] = {}
    reason = "results count could not be read"

    DC._record_failure(
        entries,
        {"filer_id": "24173", "name": "New committee"},
        reason,
    )

    saved = entries["24173"]
    assert saved["filer_id"] == "24173"
    assert saved["name"] == "New committee"
    assert saved["complete"] is None
    assert "checked" not in saved
    assert saved["checked_at"].endswith("Z")
    assert saved["last_failure"] == reason
    assert saved["failure_count"] == 1
    _assert_attempt_is_today(saved["last_attempt"])


def test_record_failure_persists_exact_attempt_provenance() -> None:
    entries: dict[str, dict] = {}
    DC._record_failure(
        entries,
        {"filer_id": "24173", "name": "New committee"},
        "unusable_window",
        transaction_id="sha256:exact",
        filer_digest="sha256:filer-exact",
        start=date(2006, 1, 1),
        end=date(2026, 9, 5),
        collection_started_at="2026-09-05T12:34:55.123456Z",
        attempted_at="2026-09-05T12:34:56.123456Z",
    )

    saved = entries["24173"]
    assert saved["complete"] is None
    assert saved["evidence_version"] == 2
    assert saved["checked"] == "2026-09-05"
    assert saved["collection_started_at"] == "2026-09-05T12:34:55.123456Z"
    assert saved["checked_at"] == "2026-09-05T12:34:56.123456Z"
    assert saved["transaction_snapshot_id"] == "sha256:exact"
    assert saved["filer_transaction_digest"] == "sha256:filer-exact"
    assert saved["range_start"] == "2006-01-01"
    assert saved["range_end"] == "2026-09-05"
    assert saved["last_attempt_at"] == saved["checked_at"]
    assert saved["last_attempt_collection_started_at"] == (
        "2026-09-05T12:34:55.123456Z"
    )
    assert saved["last_attempt_transaction_snapshot_id"] == "sha256:exact"
    assert saved["last_attempt_filer_transaction_digest"] == "sha256:filer-exact"


def test_row_diff_keeps_null_and_does_not_authorize_legacy_surplus(
    monkeypatch, tmp_path,
) -> None:
    rows = [
        {
            "filer_id": "failed-with-date",
            "complete": None,
            "checked": "2026-08-28",
        },
        {
            "filer_id": "failed-without-date",
            "complete": None,
            "last_attempt": "2026-08-28T12:34:56",
        },
        {
            "filer_id": "usable-surplus",
            "complete": False,
            "checked": "2026-08-27",
            "surplus": ["11", "12"],
        },
        {
            "filer_id": "usable-clean",
            "complete": True,
            "checked": "2026-08-26",
            "surplus": [],
            # Historical withdrawn IDs are audit evidence only. process.py
            # must project the current top-level result, not union history.
            "usable_history": [{
                "filer_id": "usable-clean",
                "complete": False,
                "surplus": ["historical-withdrawn"],
            }],
        },
    ]
    (tmp_path / "coverage_diff.json").write_text(json.dumps(rows))
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)

    complete, evidence_rows = P._row_diff()

    assert complete["failed-with-date"] == (None, rows[0])
    assert complete["failed-without-date"] == (None, rows[1])
    assert complete["usable-surplus"] == (False, rows[2])
    assert complete["usable-clean"] == (True, rows[3])
    # Loading evidence does not project a legacy surplus directly into the
    # balance-only omission map. Certification happens later against the
    # paired snapshot and current per-filer shard digest.
    assert evidence_rows == rows
    assert P._orestar_absent_for_filers(
        ["usable-surplus"], evidence={},
    ) == set()


def _page_text(start: int, count: int) -> str:
    return "\n".join(
        f"{i}\t08/28/2026\tOriginal\tTest Committee\tPayee {i}\t"
        f"Cash Contribution\t$1.00"
        for i in range(start, start + count)
    )


class _NextButton:
    def __init__(self, page) -> None:
        self.page = page

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.page.clicked = True


class _DelayedNextPage:
    """First page remains visible for one poll after Next is clicked."""

    def __init__(self, first: str, second: str | None) -> None:
        self.first = first
        self.second = second
        self.clicked = False
        self.after_click_reads = 0

    def inner_text(self, _selector: str) -> str:
        if not self.clicked:
            return self.first
        self.after_click_reads += 1
        if self.after_click_reads == 1 or self.second is None:
            return self.first
        return self.second

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def query_selector_all(self, _selector: str) -> list:
        return [] if self.second is None else [_NextButton(self)]


def _ladder_result(
    _page,
    _filer_id,
    start,
    end,
    tran_type="ALL",
    amt_from=None,
    amt_to=None,
    payee_prefix=None,
    _deadline=None,
    _context=None,
):
    """Two-row one-day fixture that requires the real four-rung ladder."""
    assert start == end
    if tran_type == "ALL":
        return {"reported": 2, "rows": None}
    if tran_type != "C":
        return {"reported": 0, "rows": {}}
    if amt_from is None and amt_to is None:
        return {"reported": 2, "rows": None}
    if (amt_from, amt_to) != ("25", "49.99"):
        return {"reported": 0, "rows": {}}
    if payee_prefix is None:
        return {"reported": 2, "rows": None}
    if payee_prefix == "A":
        return {"reported": 1, "rows": {"1": {}}}
    if payee_prefix == "Z":
        return {"reported": 1, "rows": {"2": {}}}
    return {"reported": 0, "rows": {}}


def test_cascade_collects_and_reconciles_every_ladder_rung(monkeypatch) -> None:
    day = date(2026, 8, 28)
    monkeypatch.setattr(DC, "_collect_window", _ladder_result)

    rows = DC.orestar_ids(object(), "123", day, day, seed_windows=[])

    assert rows == {"1": {}, "2": {}}


def test_unknown_prefix_gap_is_refused(monkeypatch) -> None:
    day = date(2026, 8, 28)

    def missing_unknown_prefix(*args, **kwargs):
        result = _ladder_result(*args, **kwargs)
        payee_prefix = args[7] if len(args) > 7 else kwargs.get("payee_prefix")
        if payee_prefix == "Z":
            return {"reported": 0, "rows": {}}
        return result

    assert "#" not in DC.F.PAYEE_PREFIXES
    monkeypatch.setattr(DC, "_collect_window", missing_unknown_prefix)

    assert DC.orestar_ids(object(), "123", day, day, seed_windows=[]) is None


def _install_overlap_sample_tree(
    monkeypatch,
    *,
    parent_reported: int,
    sample_cap: int,
    sample_ids: set[str] | None,
    leaves: list[tuple[str, int, set[str]]],
):
    """Install a one-day amount parent whose only children are prefixes."""
    day = date(2026, 8, 28)
    parent = ("C", day, day, "15", "15.99", None)
    children = [
        ("C", day, day, "15", "15.99", prefix)
        for prefix, _reported, _ids in leaves
    ]
    responses = {
        prefix: {"reported": reported, "rows": {tran_id: {} for tran_id in ids}}
        for prefix, reported, ids in leaves
    }
    calls: list[str] = []

    def narrow(*window):
        return children if tuple(window) == parent else []

    def collect(
        _page, _filer_id, _start, _end, _tran_type="ALL", _amt_from=None,
        _amt_to=None, payee_prefix=None, *_args, **_kwargs,
    ):
        if payee_prefix is None:
            return {"reported": parent_reported, "rows": None}
        calls.append(payee_prefix)
        return responses[payee_prefix]

    monkeypatch.setattr(DC, "UI_ROW_CAP", sample_cap)
    monkeypatch.setattr(DC.F, "ORESTAR_ROW_CAP", sample_cap)
    monkeypatch.setattr(DC.F, "_narrow_filer", narrow)
    monkeypatch.setattr(DC, "_collect_window", collect)
    monkeypatch.setattr(
        DC, "_export_tran_id_extremes", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        DC,
        "_export_rows",
        lambda *_args, **_kwargs: (
            None if sample_ids is None else {tran_id: {} for tran_id in sample_ids}
        ),
    )
    return parent, calls


def test_opposite_tran_id_exports_can_prove_parent_without_prefixes(
    monkeypatch,
) -> None:
    parent, calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=5,
        sample_cap=3,
        sample_ids=None,
        leaves=[("A", 1, {"unreachable"})],
    )
    monkeypatch.setattr(
        DC,
        "_export_tran_id_extremes",
        lambda *_args, **_kwargs: {
            "1": {}, "2": {}, "3": {}, "4": {}, "5": {},
        },
    )

    result = DC._collect_tree(
        object(), object(), "123", parent, None, 1, seed_windows=[]
    )

    assert result == {
        "reported": 5,
        "rows": {"1": {}, "2": {}, "3": {}, "4": {}, "5": {}},
    }
    assert calls == []


def test_opposite_tran_id_export_union_larger_than_parent_is_refused(
    monkeypatch,
) -> None:
    parent, _calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=4,
        sample_cap=3,
        sample_ids=None,
        leaves=[("A", 1, {"unreachable"})],
    )
    monkeypatch.setattr(
        DC,
        "_export_tran_id_extremes",
        lambda *_args, **_kwargs: {
            "1": {}, "2": {}, "3": {}, "4": {}, "5": {},
        },
    )

    with pytest.raises(DC.PartitionMismatchError, match="opposite Tran ID"):
        DC._collect_tree(
            object(), object(), "123", parent, None, 1, seed_windows=[]
        )


def test_tran_id_extreme_exports_union_ascending_and_descending(
    monkeypatch,
) -> None:
    directions: list[str] = []
    samples = iter(
        [
            {"1": {}, "2": {}, "3": {}},
            {"3": {}, "4": {}, "5": {}},
        ]
    )

    def sort(_page, _filer_id, _label, direction, *_args):
        directions.append(direction)
        return True

    monkeypatch.setattr(DC, "_goto_tran_id_sort", sort)
    monkeypatch.setattr(
        DC, "_export_rows", lambda *_args, **_kwargs: next(samples)
    )

    result = DC._export_tran_id_extremes(
        object(), object(), "123", "C 2026-08-28→2026-08-28", 5, 3, None
    )

    assert directions == ["asc", "desc"]
    assert set(result or {}) == {"1", "2", "3", "4", "5"}


def test_tran_id_sort_follows_exact_link_and_rechecks_parent_count(
    monkeypatch,
) -> None:
    href = (
        "/orestar/gotoPublicTransactionSearchResults.do?"
        "cneSearchButtonName=srtOrder&srtOrder=asc&by=RSN"
    )

    class Link:
        def get_attribute(self, name):
            assert name == "href"
            return href

    class Links:
        first = Link()

        @staticmethod
        def count():
            return 1

    class Page:
        url = "https://secure.sos.state.or.us/orestar/results"
        visited: list[tuple[str, str, int]] = []
        waits: list[int] = []

        @staticmethod
        def locator(selector):
            assert 'by=RSN' in selector
            assert 'srtOrder=asc' in selector
            return Links()

        @classmethod
        def wait_for_timeout(cls, milliseconds):
            cls.waits.append(milliseconds)

        @classmethod
        def goto(cls, url, *, wait_until, timeout):
            cls.url = url
            cls.visited.append((url, wait_until, timeout))

    monkeypatch.setattr(DC, "ORESTAR_REQUEST_DELAY", 0.25)
    monkeypatch.setattr(DC, "_read_current_results_count", lambda *_args: 5)

    assert DC._goto_tran_id_sort(
        Page, "123", "parent", "asc", 5, None
    ) is True
    assert Page.waits == [250]
    assert Page.visited == [
        (
            "https://secure.sos.state.or.us/orestar/"
            "gotoPublicTransactionSearchResults.do?"
            "cneSearchButtonName=srtOrder&srtOrder=asc&by=RSN",
            "domcontentloaded",
            60_000,
        )
    ]


def test_tran_id_extremes_are_skipped_when_two_exports_cannot_cover(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        DC,
        "_goto_tran_id_sort",
        lambda *_args, **_kwargs: pytest.fail("sort should not be attempted"),
    )

    assert DC._export_tran_id_extremes(
        object(), object(), "123", "parent", 7, 3, None
    ) is None


def test_capped_parent_sample_stops_after_union_proves_parent(monkeypatch) -> None:
    parent, calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=4,
        sample_cap=3,
        sample_ids={"1", "2", "3"},
        leaves=[
            ("A", 2, {"1", "4"}),
            ("B", 1, {"unreachable"}),
        ],
    )

    result = DC._collect_tree(
        object(), object(), "123", parent, None, 1, seed_windows=[]
    )

    assert result == {
        "reported": 4,
        "rows": {"1": {}, "2": {}, "3": {}, "4": {}},
    }
    assert calls == ["A"]


def test_capped_sample_cannot_hide_overlap_between_prefix_leaves(monkeypatch) -> None:
    parent, _calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=4,
        sample_cap=2,
        sample_ids={"1", "2"},
        leaves=[
            ("A", 2, {"1", "3"}),
            ("B", 2, {"3", "4"}),
        ],
    )

    with pytest.raises(DC.PartitionMismatchError, match="processed children"):
        DC._collect_tree(
            object(), object(), "123", parent, None, 1, seed_windows=[]
        )


def test_capped_sample_refuses_an_incomplete_union(monkeypatch) -> None:
    parent, _calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=5,
        sample_cap=3,
        sample_ids={"1", "2", "3"},
        leaves=[
            ("A", 2, {"1", "4"}),
            ("B", 1, {"2"}),
        ],
    )

    with pytest.raises(DC.PartitionMismatchError, match="parent reports 5"):
        DC._collect_tree(
            object(), object(), "123", parent, None, 1, seed_windows=[]
        )


def test_capped_sample_refuses_a_union_larger_than_parent(monkeypatch) -> None:
    parent, _calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=3,
        sample_cap=2,
        sample_ids={"1", "2"},
        leaves=[("A", 2, {"3", "4"})],
    )

    with pytest.raises(DC.PartitionMismatchError, match="overlap evidence"):
        DC._collect_tree(
            object(), object(), "123", parent, None, 1, seed_windows=[]
        )


def test_short_capped_sample_is_ignored_and_children_must_reconcile(monkeypatch) -> None:
    parent, calls = _install_overlap_sample_tree(
        monkeypatch,
        parent_reported=4,
        sample_cap=3,
        sample_ids={"1", "2"},
        leaves=[
            ("A", 2, {"1", "2"}),
            ("B", 2, {"3", "4"}),
        ],
    )

    result = DC._collect_tree(
        object(), object(), "123", parent, None, 1, seed_windows=[]
    )

    assert result["reported"] == 4
    assert set(result["rows"]) == {"1", "2", "3", "4"}
    assert calls == ["A", "B"]


def test_non_prefix_children_run_cheapest_first_and_hot_branch_last(
    monkeypatch,
) -> None:
    day = date(2026, 8, 28)
    parent = ("ALL", day, day, None, None, None)
    hot = ("C", day, day, None, None, None)
    cold = ("E", day, day, None, None, None)
    calls: list[str] = []

    monkeypatch.setattr(
        DC.F,
        "_narrow_filer",
        lambda *window: [hot, cold] if tuple(window) == parent else [],
    )
    monkeypatch.setattr(
        DC.F,
        "_held_rows",
        lambda _filer_id, tran_type, *_args: 5_662 if tran_type == "C" else 1,
    )

    def collect(
        _page, _filer_id, _start, _end, tran_type="ALL", *_args, **_kwargs,
    ):
        if tran_type == "ALL":
            return {"reported": 2, "rows": None}
        calls.append(tran_type)
        tran_id = "hot" if tran_type == "C" else "cold"
        return {"reported": 1, "rows": {tran_id: {}}}

    monkeypatch.setattr(DC, "_collect_window", collect)

    result = DC._collect_tree(
        object(), object(), "23285", parent, None, 1, seed_windows=[]
    )

    assert calls == ["E", "C"]
    assert set(result["rows"]) == {"cold", "hot"}


def test_duplicate_ids_across_children_are_refused(monkeypatch) -> None:
    day = date(2026, 8, 28)
    root = ("ALL", day, day, None, None, None)
    left = ("C", day, day, None, None, None)
    right = ("E", day, day, None, None, None)

    def narrow(*window):
        return [left, right] if tuple(window) == root else []

    def collect(
        _page, _filer_id, _start, _end, tran_type="ALL", *_args, **_kwargs
    ):
        if tran_type == "ALL":
            return {"reported": 2, "rows": None}
        return {"reported": 1, "rows": {"same-id": {}}}

    monkeypatch.setattr(DC.F, "_narrow_filer", narrow)
    monkeypatch.setattr(DC, "_collect_window", collect)

    assert DC.orestar_ids(object(), "123", day, day, seed_windows=[]) is None


def test_each_internal_parent_must_reconcile(monkeypatch) -> None:
    day = date(2026, 8, 28)
    root = ("ALL", day, day, None, None, None)
    parent = ("C", day, day, None, None, None)
    leaf = ("C", day, day, "25", "49.99", None)

    def narrow(*window):
        if tuple(window) == root:
            return [parent]
        if tuple(window) == parent:
            return [leaf]
        return []

    def collect(
        _page, _filer_id, _start, _end, tran_type="ALL", amt_from=None,
        *_args, **_kwargs,
    ):
        if tran_type == "ALL":
            return {"reported": 2, "rows": None}
        if amt_from is None:
            return {"reported": 2, "rows": None}
        return {"reported": 1, "rows": {"1": {}}}

    monkeypatch.setattr(DC.F, "_narrow_filer", narrow)
    monkeypatch.setattr(DC, "_collect_window", collect)

    assert DC.orestar_ids(object(), "123", day, day, seed_windows=[]) is None


def test_expired_deadline_prevents_the_root_search(monkeypatch) -> None:
    day = date(2026, 8, 28)
    monkeypatch.setattr(DC.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        DC,
        "_collect_window",
        lambda *_args, **_kwargs: pytest.fail("expired crawl started a search"),
    )

    with pytest.raises(DC.CollectionDeadlineExceeded):
        DC.orestar_ids(object(), "123", day, day, deadline=5.0, seed_windows=[])


def test_deadline_during_paging_prevents_another_next_click(monkeypatch) -> None:
    clock = {"now": 0.0}
    page = _DelayedNextPage(_page_text(1, 50), _page_text(51, 1))
    original_inner_text = page.inner_text

    def inner_text(selector):
        text = original_inner_text(selector)
        clock["now"] = 10.0
        return text

    page.inner_text = inner_text
    monkeypatch.setattr(DC.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args, **_kwargs: 51)

    with pytest.raises(DC.CollectionDeadlineExceeded):
        DC._collect_window(
            page, "123", date(2006, 1, 1), date(2026, 8, 28), deadline=5.0
        )
    assert page.clicked is False


def test_collect_window_waits_for_new_ids_after_next(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 50), _page_text(51, 1))
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args, **_kwargs: 51)

    result = DC._collect_window(page, "123", date(2006, 1, 1), date(2026, 8, 28))

    assert result is not None
    assert result["reported"] == 51
    assert len(result["rows"]) == 51
    assert "51" in result["rows"]


def test_collect_window_refuses_a_short_result_without_next(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 50), None)
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args, **_kwargs: 51)

    assert DC._collect_window(
        page, "123", date(2006, 1, 1), date(2026, 8, 28)
    ) is None


def test_collect_window_requires_exact_reported_count(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 2), None)
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args, **_kwargs: 1)

    assert DC._collect_window(
        page, "123", date(2006, 1, 1), date(2026, 8, 28)
    ) is None


def test_aggregate_evidence_covers_every_filer() -> None:
    summary_ts = datetime.fromisoformat("2026-08-28T12:00:00+00:00").timestamp()
    current = {"checked_at": "2026-08-28T13:00:00+00:00"}
    stale = {"checked_at": "2026-08-28T11:00:00+00:00"}

    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts, {"a": (True, current)}
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (None, current)},
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (True, stale)},
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (True, current)},
    ) is True
    assert P._aggregate_row_verdict(
        ["a"], summary_ts, {"a": (True, date(2026, 8, 28))}
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (False, current)},
    ) is False


def test_aggregate_exact_evidence_requires_current_snapshot_and_full_range() -> None:
    summary_ts = datetime.fromisoformat("2026-08-28T12:00:00+00:00").timestamp()
    exact = {
        "complete": True,
        "evidence_version": 2, "filer_digest_version": 2,
        "collection_started_at": "2026-08-28T12:59:59.000001Z",
        "checked_at": "2026-08-28T13:00:00.000001Z",
        "transaction_snapshot_id": "sha256:one",
        "range_start": "2006-01-01",
        "range_end": "2026-08-28",
    }

    assert P._aggregate_row_verdict(
        ["a"], summary_ts, {"a": (True, exact)},
        transaction_id="sha256:one",
    ) is True
    assert P._aggregate_row_verdict(
        ["a"], summary_ts,
        {"a": (True, {**exact, "transaction_snapshot_id": "sha256:old"})},
        transaction_id="sha256:one",
    ) is None
    assert P._aggregate_row_verdict(
        ["a"], summary_ts,
        {"a": (True, {**exact, "range_start": "2007-01-01"})},
        transaction_id="sha256:one",
    ) is None
    assert P._aggregate_row_verdict(
        ["a"], summary_ts,
        {"a": (True, {**exact, "checked_at": "2026-08-28"})},
        transaction_id="sha256:one",
    ) is None


def test_certified_absence_is_unioned_across_every_filer() -> None:
    evidence = {"a": {"1", "2"}, "b": {"2", "3"}}

    assert P._orestar_absent_for_filers(["a", "b"], evidence) == {
        "1", "2", "3",
    }
