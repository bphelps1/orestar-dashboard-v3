"""Contracts for same-job account-summary and exact-ID evidence batches."""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
SCRAPER_DIR = ROOT / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import atomic_balance_evidence as ABE  # noqa: E402
import balance_snapshot as BS  # noqa: E402


SNAPSHOT = "sha256:" + "a" * 64


def _source(*scopes: tuple[list[str], str]) -> dict:
    return {
        "version": 2,
        "calculation_version": "cash-balance-v2",
        "transaction_snapshot_id": SNAPSHOT,
        "scopes": {
            "|".join(sorted(ids)): {
                "filer_ids": ids,
                "app_scope_transaction_digest": digest,
            }
            for ids, digest in scopes
        },
    }


def _row(ids: list[str], *, delta: float, count: int, captured: float = 1000) -> dict:
    return {
        "name": "Committee " + "|".join(ids),
        "filer_id": ids[0],
        "filer_ids": ids,
        "delta": delta,
        "tran_count": count,
        "comparison_status": "paired",
        "transaction_snapshot_id": SNAPSHOT,
        "scrape_ts": captured,
        "closed": False,
        "newer_app_data": False,
    }


def _payload(rows: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "basis": "paired_capture_window_v1",
        "rows": rows,
    }


def test_plan_selects_whole_unanchored_scopes_and_skips_certified(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    seen = {}

    def certify(_rows, requirements, candidates, _transaction_dir):
        seen["requirements"] = requirements
        seen["candidates"] = set(candidates)
        return {"30": {"filer_id": "30"}}, set(), None

    monkeypatch.setattr(ABE, "certify_exact_scope_rows", certify)
    result = ABE.build_plan(
        _payload([
            _row(["10", "20"], delta=100, count=5),
            _row(["30"], delta=200, count=1),
        ]),
        [],
        _source((["10", "20"], "sha256:scope-a"),
                (["30"], "sha256:scope-b")),
        tmp_path,
        max_scopes=10,
        planned_at="2026-09-10T12:00:00.000000Z",
    )

    assert seen["candidates"] == {"10", "20", "30"}
    assert seen["requirements"]["10"] is seen["requirements"]["20"]
    assert result["already_anchored_scope_count"] == 1
    assert result["remaining_scope_count"] == 1
    assert result["scopes"][0]["filer_ids"] == ["10", "20"]


def test_plan_orders_old_attempts_then_cheaper_scopes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    monkeypatch.setattr(
        ABE,
        "certify_exact_scope_rows",
        lambda *_args, **_kwargs: ({}, {"10", "20"}, None),
    )
    diff = [
        {"filer_id": "10", "checked_at": "2026-09-09T00:00:00Z"},
        {"filer_id": "20", "checked_at": "2026-09-08T00:00:00Z"},
    ]
    result = ABE.build_plan(
        _payload([
            _row(["10"], delta=999, count=50),
            _row(["20"], delta=1, count=2),
        ]),
        diff,
        _source((["10"], "sha256:scope-a"),
                (["20"], "sha256:scope-b")),
        tmp_path,
        max_scopes=2,
        planned_at="2026-09-10T12:00:00.000000Z",
    )

    assert [scope["filer_ids"] for scope in result["scopes"]] == [["20"], ["10"]]


@pytest.mark.parametrize("filer_ids", [["10"], ["10", "20"]])
@pytest.mark.parametrize("requested_ids", [(), ("10",)])
def test_plan_refreshes_unchanged_scopes_after_unrelated_snapshot_change(
    monkeypatch, tmp_path, filer_ids, requested_ids,
) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    prior_snapshot = "sha256:" + "b" * 64
    unchanged = _row(filer_ids, delta=100, count=5)
    unchanged["transaction_snapshot_id"] = prior_snapshot
    changed = _row(["30"], delta=200, count=1)
    changed["transaction_snapshot_id"] = prior_snapshot
    changed["newer_app_data"] = True
    source = _source((filer_ids, "sha256:" + "c" * 64),
                     (["30"], "sha256:" + "d" * 64))
    kwargs = dict(
        balance_payload=_payload([unchanged, changed]),
        diff_rows=[],
        source=source,
        transaction_dir=tmp_path,
        max_scopes=10,
        requested_ids=requested_ids,
        planned_at="2026-09-12T12:00:00Z",
    )

    # An older global capture remains actionable when this scope is unchanged.
    # No existing exact evidence means the real certifier requires a new capture.
    result = ABE.build_plan(**kwargs)

    assert result["candidate_scope_count"] == 1
    assert result["remaining_scope_count"] == 1
    assert result["selected_scope_count"] == 1
    assert result["transaction_snapshot_id"] == SNAPSHOT
    assert result["scopes"][0]["filer_ids"] == filer_ids
    assert result["scopes"][0]["prior_transaction_snapshot_id"] == prior_snapshot

    # Only the previous capture may be old: the source used for the new window
    # must still match the transaction shards hydrated for this run.
    source["transaction_snapshot_id"] = prior_snapshot
    with pytest.raises(ABE.AtomicEvidenceError, match="does not match the local ledger"):
        ABE.build_plan(**kwargs)


def test_automatic_plan_defers_same_day_failure_but_explicit_target_overrides(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    monkeypatch.setattr(
        ABE, "certify_exact_scope_rows", lambda *_args, **_kwargs: ({}, set(), None)
    )
    diff = [{
        "filer_id": "10",
        "complete": None,
        "last_attempt_at": "2026-09-10T01:00:00Z",
    }]
    kwargs = dict(
        balance_payload=_payload([_row(["10"], delta=100, count=5)]),
        diff_rows=diff,
        source=_source((["10"], "sha256:scope-a")),
        transaction_dir=tmp_path,
        max_scopes=10,
        planned_at="2026-09-10T12:00:00Z",
    )

    automatic = ABE.build_plan(**kwargs)
    explicit = ABE.build_plan(**kwargs, requested_ids=["10"])

    assert automatic["remaining_scope_count"] == 1
    assert automatic["deferred_scope_count"] == 1
    assert automatic["selected_scope_count"] == 0
    assert explicit["selected_scope_count"] == 1


def test_explicit_scope_expansion_cannot_be_silently_truncated(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    monkeypatch.setattr(
        ABE, "certify_exact_scope_rows", lambda *_args, **_kwargs: ({}, set(), None)
    )

    with pytest.raises(ABE.AtomicEvidenceError, match="more scopes than max_scopes"):
        ABE.build_plan(
            _payload([
                _row(["10"], delta=1, count=1),
                _row(["20"], delta=1, count=1),
            ]),
            [],
            _source((["10"], "sha256:a"), (["20"], "sha256:b")),
            tmp_path,
            max_scopes=1,
            requested_ids=["10", "20"],
            planned_at="2026-09-10T12:00:00Z",
        )


def test_ready_keeps_only_complete_scopes_captured_after_plan(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)

    def comparison(ids, *_args, **_kwargs):
        fresh = ids == ["10", "20"]
        captured = 2000 if fresh else 900
        return {
            "status": "paired",
            "capture_started_at": captured,
            "captured_at": captured + 1,
            "app_transaction_snapshot_id": SNAPSHOT,
            "filer_ids": ids,
            "scope_digest_matches_capture": True,
            "orestar_data_changed_since_capture": False,
        }

    monkeypatch.setattr(ABE, "paired_comparison", comparison)
    plan = {
        "version": 1,
        "planned_at": "1970-01-01T00:16:40Z",  # epoch 1000
        "transaction_snapshot_id": SNAPSHOT,
        "scopes": [
            {"filer_ids": ["10", "20"], "app_scope_transaction_digest": "sha256:a"},
            {"filer_ids": ["30"], "app_scope_transaction_digest": "sha256:b"},
        ],
    }
    result = ABE.ready_plan(
        plan,
        {},
        tmp_path,
        now=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )

    assert result["ready_scope_count"] == 1
    assert result["scopes"][0]["filer_ids"] == ["10", "20"]
    assert result["rejected_scopes"] == [
        {"filer_ids": ["30"], "reason": "not_freshly_captured"}
    ]
    assert result["end_date"] == "2026-09-10"


def test_verify_counts_only_fully_certified_scopes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_args, **_kwargs: SNAPSHOT)
    captured = {}

    def certify(rows, requirements, candidates, _transaction_dir, **kwargs):
        captured["requirements"] = requirements
        captured["candidates"] = set(candidates)
        captured["ranges"] = kwargs["active_ranges"]
        by_id = {row["filer_id"]: row for row in rows}
        return {"10": by_id["10"], "20": by_id["20"], "30": by_id["30"]}, {"40"}, None

    monkeypatch.setattr(ABE, "certify_exact_scope_rows", certify)
    ready = {
        "version": 1,
        "planned_at": "1970-01-01T00:16:40Z",
        "transaction_snapshot_id": SNAPSHOT,
        "end_date": "2026-09-10",
        "ready_scope_count": 2,
        "scopes": [
            {
                "filer_ids": ["10", "20"],
                "capture_started_at": 1900,
                "captured_at": 2000,
                "capture_day": "1970-01-01",
                "app_scope_transaction_digest": "sha256:a",
            },
            {
                "filer_ids": ["30", "40"],
                "capture_started_at": 1900,
                "captured_at": 2000,
                "capture_day": "1970-01-01",
                "app_scope_transaction_digest": "sha256:b",
            },
        ],
    }
    rows = [
        {"filer_id": "10", "missing": ["m1"], "surplus": []},
        {"filer_id": "20", "missing": [], "surplus": ["s1", "s2"]},
        {"filer_id": "30", "missing": [], "surplus": []},
        {"filer_id": "40", "missing": [], "surplus": []},
    ]
    result = ABE.verify_plan(ready, rows, tmp_path)

    assert captured["candidates"] == {"10", "20", "30", "40"}
    assert captured["requirements"]["10"] is captured["requirements"]["20"]
    assert set(captured["ranges"].values()) == {"2026-09-10"}
    assert result["certified_scope_count"] == 1
    assert result["certified_filer_count"] == 2
    assert result["missing_id_count"] == 1
    assert result["surplus_id_count"] == 2
    assert result["blocked_filer_ids"] == ["30", "40"]


def test_ready_requirements_are_shared_and_pin_the_frozen_range() -> None:
    ready = {
        "version": 1,
        "planned_at": "2026-09-10T12:00:00Z",
        "transaction_snapshot_id": SNAPSHOT,
        "end_date": "2026-09-10",
        "ready_scope_count": 1,
        "scopes": [{
            "filer_ids": ["10", "20"],
            "capture_started_at": 1_789_041_601,
            "captured_at": 1_789_041_602,
            "capture_day": "2026-09-10",
            "app_scope_transaction_digest": "sha256:scope",
        }],
    }

    scopes, requirements, ranges = ABE.requirements_from_ready_plan(
        ready, SNAPSHOT
    )

    assert scopes == [["10", "20"]]
    assert requirements["10"] is requirements["20"]
    assert requirements["10"]["active_range_end"] == "2026-09-10"
    assert requirements["10"]["active_range_conflict"] is False
    assert ranges == {"10": "2026-09-10", "20": "2026-09-10"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"ready_scope_count": 2}, "scope count"),
        ({"planned_at": "2026-09-10"}, "explicitly UTC"),
        ({"scope_ids": ["20", "10"]}, "canonical"),
        ({"capture_started_at": 1_789_041_599}, "after planning"),
        ({"capture_day": "2026-09-09"}, "capture day"),
        ({"end_date": "2026-09-09"}, "before capture day"),
    ],
)
def test_ready_requirement_contract_rejects_malformed_windows(
    change, message,
) -> None:
    ready = {
        "version": 1,
        "planned_at": "2026-09-10T12:00:00Z",
        "transaction_snapshot_id": SNAPSHOT,
        "end_date": "2026-09-10",
        "ready_scope_count": 1,
        "scopes": [{
            "filer_ids": ["10", "20"],
            "capture_started_at": 1_789_041_601,
            "captured_at": 1_789_041_602,
            "capture_day": "2026-09-10",
            "app_scope_transaction_digest": "sha256:scope",
        }],
    }
    change = dict(change)
    scope_ids = change.pop("scope_ids", None)
    if scope_ids is not None:
        ready["scopes"][0]["filer_ids"] = scope_ids
    elif set(change).issubset({"capture_started_at", "capture_day"}):
        ready["scopes"][0].update(change)
    else:
        ready.update(change)

    with pytest.raises(ABE.AtomicEvidenceError, match=message):
        ABE.requirements_from_ready_plan(ready, SNAPSHOT)


def test_snapshot_drift_is_a_hard_failure(tmp_path) -> None:
    transactions = tmp_path / "transactions"
    transactions.mkdir()
    (transactions / "txn_2026.csv.gz").write_bytes(b"frozen bytes")

    with pytest.raises(ABE.AtomicEvidenceError, match="Transaction snapshot changed"):
        ABE._current_snapshot(transactions, SNAPSHOT)


def test_workflow_runs_both_collectors_without_a_second_pull() -> None:
    workflow = (ROOT / ".github" / "workflows" / "atomic-balance-evidence.yml").read_text()
    summary = workflow.index("      - name: Capture fresh summaries for planned scopes")
    diff = workflow.index("      - name: Diff exactly the freshly paired scopes")
    publish = workflow.index("      - name: Publish atomic evidence state")
    aggregate = workflow.index("      - name: Re-aggregate from durable evidence")

    assert summary < diff < publish < aggregate
    assert workflow.count("pipeline_state.py pull") == 1
    assert "pull transactions summaries auxiliary" in workflow
    assert "--force --current-only" in workflow
    diff_block = workflow[diff:publish]
    assert '--scope-plan "$READY_PATH"' in diff_block
    assert "--filer-ids" not in diff_block
    assert "--flagged" not in workflow
    assert "push summaries auxiliary" in workflow
    # Paired evidence can truthfully retain missing IDs; only the separate
    # identity-backfill branch requires a zero-missing terminal result.
    assert "require-no-missing" not in diff_block


def test_workflow_stops_partial_batches_before_any_successor_dispatch() -> None:
    workflow = (ROOT / ".github" / "workflows" / "atomic-balance-evidence.yml").read_text()
    summary = workflow.index("      - name: Capture fresh summaries for planned scopes")
    diff = workflow.index("      - name: Diff exactly the freshly paired scopes")
    terminal = workflow.index("      - name: Enforce truthful terminal status")
    successor = workflow.index("      - name: Continue bounded evidence chain")
    diff_block = workflow[diff:terminal]
    terminal_block = workflow[terminal:successor]
    successor_block = workflow[successor:]

    assert "steps.summaries.outcome == 'success'" in diff_block
    assert terminal < successor
    assert "steps.ready.outputs.ready_scopes" in terminal_block
    assert "steps.plan.outputs.selected_scopes" in terminal_block
    assert "steps.diff.outcome" in terminal_block
    assert "steps.verify.outputs.certified_scopes" in terminal_block
    assert "success() && !cancelled()" in successor_block
    assert "gh workflow run atomic-balance-evidence.yml" in successor_block
    assert "dispatch_retry.sh" not in successor_block
    assert "group: atomic-balance-evidence-${{ github.run_id }}" in workflow


def test_empty_atomic_plan_recovers_already_published_aggregation() -> None:
    workflow = (ROOT / ".github" / "workflows" / "atomic-balance-evidence.yml").read_text()
    recovery = workflow.split(
        "      - name: Recover aggregation when the atomic plan is empty", 1
    )[1].split("      - name: Enforce truthful terminal status", 1)[0]

    assert "steps.plan.outcome == 'success'" in recovery
    assert "steps.plan.outputs.filer_ids == ''" in recovery
    assert "python scraper/process.py" in recovery


def test_completed_current_summary_sweep_hands_off_only_after_publication() -> None:
    workflow = (ROOT / ".github" / "workflows" / "earliest-balances.yml").read_text()
    handoff = workflow.split(
        "      - name: Hand off completed current sweep to atomic evidence", 1
    )[1]

    assert "env.current_only == 'true'" in handoff
    assert "env.targeted != 'true'" in handoff
    assert "steps.remaining.outputs.remaining == '0'" in handoff
    assert "steps.final_aggregation.outcome == 'success'" in handoff
    assert "steps.summary_publish.outcome == 'success'" in handoff
    assert "gh workflow run atomic-balance-evidence.yml" in handoff
    assert "dispatch_retry.sh atomic-balance-evidence.yml" not in handoff


def test_atomic_workflow_is_in_shared_orestar_lane() -> None:
    action = (ROOT / ".github" / "actions" / "await-orestar" / "action.yml").read_text()
    assert ".github/workflows/atomic-balance-evidence.yml" in action


def _stabilization_window(monkeypatch, tmp_path, ids=None):
    ids = ids or ["10", "20"]
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_a, **_k: SNAPSHOT)
    planned = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    source = _source((ids, "sha256:scope"))
    source["created_at"] = "2026-09-12T11:59:00Z"
    source["scopes"]["|".join(ids)].update({
        "cash_on_hand": 100.0, "tran_count": len(ids),
        "app_year_transaction_digests": {"2026": "sha256:year"},
    })
    yearly = {}
    for index, fid in enumerate(ids):
        captured_at = planned.timestamp() + index + 1
        summary = {"ending_cash_balance": 100.0 / len(ids),
                   "scrape_ts": captured_at}
        capture = BS.make_summary_capture(fid, 2026, summary, captured_at,
                                          source, SNAPSHOT)
        capture["scope_capture_id"] = "|".join(ids) + "@fresh"
        yearly[fid] = {"comparison_capture": capture, "years": {"2026": {
            **summary, "scope_capture_id": capture["scope_capture_id"],
            "calculation_version": BS.CALCULATION_VERSION,
            "app_year_transaction_digest": "sha256:year",
        }}}
    plan = {"version": 1, "planned_at": planned.isoformat(),
            "transaction_snapshot_id": SNAPSHOT, "scopes": [{
                "filer_ids": ids, "app_scope_transaction_digest": "sha256:scope",
            }]}
    ready = ABE.ready_plan(plan, yearly, tmp_path, now=planned)
    source["created_at"] = "2026-09-12T12:01:00Z"
    return ready, source, yearly


def _refresh_payload(ids):
    return {**_payload([]), "refresh_rows": [{
        "name": "Changed cash", "filer_ids": ids,
        "comparison_status": "paired", "closed": False,
        "reason": "app_state_changed_since_capture",
        "app_data_changed_since_capture": True, "delta": 0,
        # This report is deliberately missing provenance; the real pair owns it.
    }]}


@pytest.mark.parametrize("ids", [["10"], ["10", "20"]])
def test_plan_admits_zero_delta_refresh_from_real_pair(monkeypatch, tmp_path, ids):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path, ids)
    source["scopes"]["|".join(ids)]["cash_on_hand"] = 150.0
    result = ABE.build_plan(_refresh_payload(ids), [], source, tmp_path,
                            max_scopes=10, yearly_cache=yearly,
                            planned_at="2026-09-12T12:02:00Z")
    assert result["selected_scope_count"] == 1
    [scope] = result["scopes"]
    assert scope["needs_stabilization"] is True
    assert scope["delta"] == 0
    assert scope["prior_transaction_snapshot_id"] == SNAPSHOT
    assert scope["prior_captured_at"] == ready["scopes"][0]["captured_at"]
    assert scope["filer_ids"] == ids


@pytest.mark.parametrize("fault", ["no-cache", "partial", "new-attempt",
                                     "mixed-capture", "overlap", "annual-only"])
def test_refresh_admission_refuses_missing_pair_proof(monkeypatch, tmp_path, fault):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path)
    payload = _refresh_payload(["10", "20"])
    if fault == "no-cache":
        yearly = None
    elif fault == "partial":
        yearly.pop("20")
    elif fault == "new-attempt":
        yearly["20"]["comparison_capture_attempt"] = {
            "captured_at": ready["scopes"][0]["captured_at"] + 1}
    elif fault == "mixed-capture":
        yearly["20"]["comparison_capture"]["scope_capture_id"] = "other"
    elif fault == "overlap":
        source["scopes"]["20|30"] = {"filer_ids": ["20", "30"]}
    else:
        payload["refresh_rows"][0]["reason"] = "annual_summary_years_unpaired"
    result = ABE.build_plan(payload, [], source, tmp_path, max_scopes=10,
                            yearly_cache=yearly,
                            planned_at="2026-09-12T12:02:00Z")
    assert result["selected_scope_count"] == 0


@pytest.mark.parametrize("proof, failed, expected", [
    ([], False, 0), (["10"], False, 0), (["10", "20"], False, 1),
    (["10", "20"], True, 0),
])
def test_same_day_refresh_requires_complete_proof_without_later_failure(
    monkeypatch, tmp_path, proof, failed, expected,
):
    _ready, source, yearly = _stabilization_window(monkeypatch, tmp_path)
    entries = [{"filer_id": fid, "complete": False,
                "checked_at": "2026-09-12T12:01:00Z"} for fid in ["10", "20"]]
    if failed:
        entries[1].update(last_failure="unusable_window",
                          last_attempt_at="2026-09-12T12:01:30Z")
    monkeypatch.setattr(ABE, "certify_exact_scope_rows", lambda *_a, **_k: (
        {fid: {"filer_id": fid} for fid in proof}, set(), None))
    result = ABE.build_plan(_refresh_payload(["10", "20"]), entries, source,
                            tmp_path, max_scopes=10, yearly_cache=yearly,
                            planned_at="2026-09-12T12:02:00Z")
    assert result["selected_scope_count"] == expected
    assert result["remaining_scope_count"] == 1
    assert result["deferred_scope_count"] == 1 - expected


@pytest.mark.parametrize("change, reason", [
    ("none", None), ("cash", "cash_changed"),
    ("count", "transaction_count_changed"),
    ("year", "fresh_annual_treatment_changed"),
])
def test_assessment_detects_settlement_without_rewriting_capture(
    monkeypatch, tmp_path, change, reason,
):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path)
    scope = source["scopes"]["10|20"]
    if change == "cash":
        scope["cash_on_hand"] = 150.0
    elif change == "count":
        scope["tran_count"] = 3
    elif change == "year":
        scope["app_year_transaction_digests"]["2026"] = "sha256:new-treatment"
    # A missing sibling or stale row in an untouched historical year must not
    # keep a successful recapture looping indefinitely.
    yearly["10"]["years"]["2012"] = {"scope_capture_id": "old", "scrape_ts": 1}
    original = copy.deepcopy((ready, source, yearly))
    result = ABE.assess_stabilization(ready, source, yearly, tmp_path)
    assert (ready, source, yearly) == original
    assert result["stable_scope_count"] == int(reason is None)
    assert result["unsettled_scope_count"] == int(reason is not None)
    assert result["unsettled_filer_ids"] == ([] if reason is None else ["10", "20"])
    assert result["scopes"][0]["reasons"] == ([] if reason is None else [reason])


@pytest.mark.parametrize("fault", [
    "source-version", "source-calculation", "source-snapshot", "source-old",
    "scope-partial", "scope-overlap", "scope-digest", "member-missing",
    "capture-time", "capture-cash", "capture-id", "new-attempt",
    "current-year-missing", "fresh-year-time", "year-source-missing",
])
def test_assessment_refuses_provenance_drift(monkeypatch, tmp_path, fault):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path)
    scope = source["scopes"]["10|20"]
    if fault == "source-version":
        source["version"] = 0
    elif fault == "source-calculation":
        source["calculation_version"] = "old"
    elif fault == "source-snapshot":
        source["transaction_snapshot_id"] = "sha256:" + "b" * 64
    elif fault == "source-old":
        source["created_at"] = "2026-09-12T11:59:00Z"
    elif fault == "scope-partial":
        scope["filer_ids"] = ["10"]
    elif fault == "scope-overlap":
        source["scopes"]["20|30"] = {"filer_ids": ["20", "30"]}
    elif fault == "scope-digest":
        scope["app_scope_transaction_digest"] = "sha256:changed"
    elif fault == "member-missing":
        yearly.pop("20")
    elif fault == "capture-time":
        yearly["20"]["comparison_capture"]["captured_at"] += 1
    elif fault == "capture-cash":
        for entry in yearly.values():
            entry["comparison_capture"]["app_cash_on_hand"] += 5
    elif fault == "capture-id":
        for entry in yearly.values():
            entry["comparison_capture"]["scope_capture_id"] = "other"
    elif fault == "new-attempt":
        yearly["20"]["comparison_capture_attempt"] = {
            "captured_at": ready["scopes"][0]["captured_at"] + 1}
    elif fault == "current-year-missing":
        yearly["20"]["years"].pop("2026")
    elif fault == "fresh-year-time":
        yearly["20"]["years"]["2026"]["scrape_ts"] = 1
    else:
        scope.pop("app_year_transaction_digests")
    with pytest.raises(ABE.AtomicEvidenceError):
        ABE.assess_stabilization(ready, source, yearly, tmp_path)


@pytest.mark.parametrize("ids", [["10"], ["10", "20"]])
def test_explicit_recovery_admits_matching_pair_absent_from_report(
    monkeypatch, tmp_path, ids,
):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path, ids)
    original = copy.deepcopy((source, yearly))
    # A failed exact attempt can leave no discrepancy or refresh row at all.
    failed = [{"filer_id": ids[0], "last_failure": "incomplete_collection",
               "last_attempt_at": "2026-09-12T12:01:30Z"}]
    result = ABE.build_plan(_payload([]), failed, source, tmp_path,
                            requested_ids=[ids[-1]], max_scopes=1,
                            yearly_cache=yearly,
                            planned_at="2026-09-12T12:02:00Z")
    assert result["selected_scope_count"] == 1
    assert result["remaining_scope_count"] == 1
    assert result["deferred_scope_count"] == 0
    [scope] = result["scopes"]
    assert scope["filer_ids"] == ids
    assert scope["delta"] == 0
    assert scope["prior_captured_at"] == ready["scopes"][0]["captured_at"]
    assert scope["prior_transaction_snapshot_id"] == SNAPSHOT
    assert (source, yearly) == original
    automatic = ABE.build_plan(_payload([]), failed, source, tmp_path,
                               max_scopes=1, yearly_cache=yearly,
                               planned_at="2026-09-12T12:02:00Z")
    assert automatic["candidate_scope_count"] == 0
    assert automatic["selected_scope_count"] == 0


@pytest.mark.parametrize("fault", [
    "no-cache", "missing-member", "partial-membership", "overlap",
    "source-version", "source-calculation", "source-snapshot", "source-digest",
    "capture-version", "capture-calculation", "capture-snapshot",
    "capture-membership", "capture-id-missing", "capture-id-mixed",
    "capture-time-future", "capture-time-nan", "capture-source-time-missing",
    "capture-source-time-future", "capture-cash-missing", "capture-count-fraction",
    "new-attempt",
])
def test_explicit_recovery_refuses_unproven_scope(monkeypatch, tmp_path, fault):
    ready, source, yearly = _stabilization_window(monkeypatch, tmp_path)
    payload = _payload([])
    current = source["scopes"]["10|20"]
    capture = yearly["20"]["comparison_capture"]
    if fault == "no-cache":
        yearly = None
    elif fault == "missing-member":
        yearly.pop("20")
    elif fault == "partial-membership":
        current["filer_ids"] = ["10"]
    elif fault == "overlap":
        source["scopes"]["20|30"] = {"filer_ids": ["20", "30"]}
    elif fault == "source-version":
        source["version"] = 0
    elif fault == "source-calculation":
        source["calculation_version"] = "old"
    elif fault == "source-snapshot":
        source["transaction_snapshot_id"] = "sha256:" + "b" * 64
    elif fault == "source-digest":
        current["app_scope_transaction_digest"] = "sha256:changed"
    elif fault == "capture-version":
        capture["version"] = 0
    elif fault == "capture-calculation":
        capture["calculation_version"] = "old"
    elif fault == "capture-snapshot":
        for entry in yearly.values():
            entry["comparison_capture"]["app_transaction_snapshot_id"] = "sha256:" + "b" * 64
    elif fault == "capture-membership":
        capture["app_scope_filer_ids"] = ["20"]
    elif fault == "capture-id-missing":
        for entry in yearly.values():
            entry["comparison_capture"].pop("scope_capture_id")
    elif fault == "capture-id-mixed":
        capture["scope_capture_id"] = "different"
    elif fault == "capture-time-future":
        capture["captured_at"] += 3600
    elif fault == "capture-time-nan":
        capture["captured_at"] = float("nan")
    elif fault == "capture-source-time-missing":
        capture.pop("app_snapshot_created_at")
    elif fault == "capture-source-time-future":
        capture["app_snapshot_created_at"] = "2026-09-12T12:01:00Z"
    elif fault == "capture-cash-missing":
        for entry in yearly.values():
            entry["comparison_capture"].pop("app_cash_on_hand")
    elif fault == "capture-count-fraction":
        capture["app_tran_count"] = 2.5
    elif fault == "new-attempt":
        yearly["20"]["comparison_capture_attempt"] = {
            "captured_at": ready["scopes"][0]["captured_at"] + 1}
    with pytest.raises(ABE.AtomicEvidenceError):
        ABE.build_plan(payload, [], source, tmp_path, max_scopes=1,
                       requested_ids=["10"], yearly_cache=yearly,
                       planned_at="2026-09-12T12:02:00Z")


def test_explicit_recovery_enforces_scope_limit_after_expansion(monkeypatch, tmp_path):
    _ready, source, yearly = _stabilization_window(monkeypatch, tmp_path, ["10", "20"])
    _other, other_source, other_yearly = _stabilization_window(monkeypatch, tmp_path, ["30"])
    source["scopes"].update(other_source["scopes"])
    yearly.update(other_yearly)
    with pytest.raises(ABE.AtomicEvidenceError, match="more scopes than max_scopes"):
        ABE.build_plan(_payload([]), [], source, tmp_path, max_scopes=1,
                       requested_ids=["20", "30"], yearly_cache=yearly,
                       planned_at="2026-09-12T12:02:00Z")
    result = ABE.build_plan(_payload([]), [], source, tmp_path, max_scopes=2,
                            requested_ids=["20", "30"], yearly_cache=yearly,
                            planned_at="2026-09-12T12:02:00Z")
    assert result["selected_scope_count"] == 2
    assert {tuple(row["filer_ids"]) for row in result["scopes"]} == {("10", "20"), ("30",)}


@pytest.mark.parametrize("delta", [0, 50])
def test_explicit_closed_scope_verification_uses_production_source_without_changing_automatic_selection(
    monkeypatch, tmp_path, delta,
):
    _ready, source, yearly = _stabilization_window(monkeypatch, tmp_path, ["10"])
    # Production's compact source omits closure metadata. A balanced closed
    # scope is also absent from all report lists; a nonzero closed difference
    # appears only among non-actionable rows. Both remain deliberate explicit
    # verification targets, while neither becomes an automatic candidate.
    closed_detail = {**source["scopes"]["10"], "closed": True}
    source = BS.build_source(SNAPSHOT, [closed_detail], created_at=source["created_at"])
    assert "closed" not in source["scopes"]["10"]
    yearly["10"]["comparison_capture"]["orestar_ending_cash_balance"] = 100 - delta
    yearly["10"]["years"]["2026"]["ending_cash_balance"] = 100 - delta
    payload = _payload([])
    if delta:
        payload["non_actionable_rows"] = [{
            "filer_ids": ["10"], "closed": True, "delta": delta,
            "reason": "closed_trailing_summary",
        }]
    original = copy.deepcopy((source, yearly, payload))
    kwargs = dict(balance_payload=payload, diff_rows=[], source=source,
                  transaction_dir=tmp_path, max_scopes=1, yearly_cache=yearly,
                  planned_at="2026-09-12T12:02:00Z")
    explicit = ABE.build_plan(**kwargs, requested_ids=["10"])
    assert explicit["selected_scope_count"] == 1
    assert explicit["scopes"][0]["filer_ids"] == ["10"]
    assert explicit["scopes"][0]["delta"] == delta
    automatic = ABE.build_plan(**kwargs)
    assert automatic["candidate_scope_count"] == 0
    assert automatic["selected_scope_count"] == 0
    assert (source, yearly, payload) == original



def _budget_plan(monkeypatch, tmp_path, *, requested=(), excluded=(), costs=None,
                 budget=9, hints=None, max_passes=3, single_pass_filer_ids=()):
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_a, **_k: SNAPSHOT)
    monkeypatch.setattr(ABE, "certify_exact_scope_rows",
                        lambda *_a, **_k: ({}, set(), None))
    if costs is not None:
        monkeypatch.setattr(ABE, "estimate_scope_searches",
                            lambda ids, entries: costs.get(ids[0]))
    return ABE.build_plan(
        _payload([_row(["10", "20"], delta=100, count=2),
                  _row(["30"], delta=50, count=3),
                  _row(["40"], delta=20, count=4)]),
        [], _source((["10", "20"], "sha256:" + "b" * 64),
                    (["30"], "sha256:" + "c" * 64),
                    (["40"], "sha256:" + "d" * 64)),
        tmp_path, max_scopes=12, planned_at="2026-09-14T12:00:00Z",
        requested_ids=requested, excluded_ids=excluded,
        max_searches=budget, search_cost_hints=hints,
        max_passes=max_passes, single_pass_filer_ids=single_pass_filer_ids,
    )


def test_search_budget_reserves_three_passes_for_every_physical_member(monkeypatch, tmp_path):
    plan = _budget_plan(monkeypatch, tmp_path,
                        costs={"10": 1, "20": 1, "30": 1, "40": 1})
    assert [s["filer_ids"] for s in plan["scopes"]] == [["10", "20"], ["30"]]
    assert plan["estimated_total_searches"] == 9
    assert plan["reserved_exact_passes"] == 3
    assert plan["budget_deferred_scopes"] == [{"filer_ids": ["40"], "reason": "search_budget"}]


def test_one_pass_admits_measured_24_search_scope_that_three_passes_cannot_fit(monkeypatch, tmp_path):
    args = dict(requested=["30"], costs={"30": 24}, budget=45, single_pass_filer_ids=["30"])
    plan = _budget_plan(monkeypatch, tmp_path, max_passes=1, **args)
    assert plan["reserved_exact_passes"] == 1
    assert plan["estimated_total_searches"] == 24
    assert plan["scopes"][0]["filer_ids"] == ["30"]
    assert plan["scopes"][0]["estimated_exact_searches"] == 24
    with pytest.raises(ABE.AtomicEvidenceError, match="cannot fit three exact passes"):
        _budget_plan(monkeypatch, tmp_path, max_passes=3, **args)


@pytest.mark.parametrize("limit", [0, 2, 4, True, 1.0, "1"])
def test_invalid_planning_pass_count_fails_before_snapshot_read(monkeypatch, tmp_path, limit):
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_a: pytest.fail("read before validation"))
    with pytest.raises(ABE.AtomicEvidenceError, match="must be 1 or 3"):
        ABE.build_plan({}, [], {}, tmp_path, max_scopes=1, max_passes=limit)


def test_one_pass_requires_explicit_ids_and_one_expanded_canonical_scope(monkeypatch, tmp_path):
    with pytest.raises(ABE.AtomicEvidenceError, match="requires explicit filer IDs"):
        _budget_plan(monkeypatch, tmp_path, max_passes=1, single_pass_filer_ids=["30"])
    with pytest.raises(ABE.AtomicEvidenceError, match="exactly one canonical scope"):
        _budget_plan(monkeypatch, tmp_path, max_passes=1, requested=["30", "40"],
                     single_pass_filer_ids=["30", "40"], costs={"30": 1, "40": 1})


def test_one_pass_requires_budget_before_snapshot_read(monkeypatch, tmp_path):
    monkeypatch.setattr(ABE, "_current_snapshot", lambda *_a: pytest.fail("read before validation"))
    with pytest.raises(ABE.AtomicEvidenceError, match="requires a configured search budget"):
        ABE.build_plan({}, [], {}, tmp_path, max_scopes=1, max_passes=1,
                       requested_ids=["191"], single_pass_filer_ids=["191"])


def test_one_pass_alias_cannot_omit_nonallowlisted_scope_member(monkeypatch, tmp_path):
    with pytest.raises(ABE.AtomicEvidenceError, match="Every expanded member"):
        _budget_plan(monkeypatch, tmp_path, max_passes=1, requested=["10"],
                     single_pass_filer_ids=["10"], costs={"10": 1, "20": 2})
    plan = _budget_plan(monkeypatch, tmp_path, max_passes=1, requested=["10"],
                        single_pass_filer_ids=["10", "20"], costs={"10": 1, "20": 2})
    assert plan["scopes"][0]["filer_ids"] == ["10", "20"]
    assert plan["estimated_total_searches"] == 3
    with pytest.raises(ABE.AtomicEvidenceError, match="contains deferred filers"):
        _budget_plan(monkeypatch, tmp_path, max_passes=1, requested=["10"],
                     single_pass_filer_ids=["10", "20"], excluded=["20"])


@pytest.mark.parametrize("cost", [None, 46])
def test_one_pass_preserves_unknown_cost_and_hard_search_limit_refusal(monkeypatch, tmp_path, cost):
    with pytest.raises(ABE.AtomicEvidenceError, match="cannot fit one exact pass"):
        _budget_plan(monkeypatch, tmp_path, max_passes=1, requested=["30"], budget=45,
                     single_pass_filer_ids=["30"], costs={"30": cost})


def test_one_pass_cli_forwards_validated_mode_and_allowlist(monkeypatch, tmp_path):
    seen = {}
    def plan(*_args, **kwargs):
        seen.update(kwargs)
        return {"transaction_snapshot_id": SNAPSHOT, "selected_scope_count": 1,
                "remaining_scope_count": 1}
    monkeypatch.setattr(ABE, "build_plan", plan)
    monkeypatch.setattr(ABE.supabase_sync, "require_dashboard_cache", lambda _key: {})
    monkeypatch.setattr(ABE, "_read_json", lambda *_args: {})
    assert ABE.main(["plan", "--max-passes", "1", "--filer-ids", "10",
                     "--single-pass-filer-ids", "10", "20", "--max-searches", "45",
                     "--output", str(tmp_path / "plan.json")]) == 0
    assert seen["max_passes"] == 1
    assert seen["requested_ids"] == ["10"]
    assert seen["single_pass_filer_ids"] == ["10", "20"]
    assert seen["max_searches"] == 45


def test_deferred_member_excludes_entire_scope_across_day_reset(monkeypatch, tmp_path):
    plan = _budget_plan(monkeypatch, tmp_path, excluded=["20"],
                        costs={"10": 1, "20": 1, "30": 1, "40": 1})
    assert plan["excluded_scope_count"] == 1
    assert [s["filer_ids"] for s in plan["scopes"]] == [["30"], ["40"]]
    with pytest.raises(ABE.AtomicEvidenceError, match="contains deferred filers"):
        _budget_plan(monkeypatch, tmp_path, requested=["10"], excluded=["20"],
                     costs={"10": 1, "20": 1, "30": 1, "40": 1})


def test_unknown_or_oversized_explicit_scope_fails_before_capture(monkeypatch, tmp_path):
    for cost in (None, 4):
        with pytest.raises(ABE.AtomicEvidenceError, match="cannot fit three exact passes"):
            _budget_plan(monkeypatch, tmp_path, requested=["30"], costs={"30": cost})
    plan = _budget_plan(monkeypatch, tmp_path, costs={"10": None, "20": 1, "30": 1, "40": 1})
    assert plan["budget_deferred_scopes"][0]["reason"] == "unknown_search_cost"
    assert [s["filer_ids"] for s in plan["scopes"]] == [["30"], ["40"]]


def test_reviewed_cost_hint_must_match_saved_observation(monkeypatch):
    monkeypatch.setattr(ABE, "estimate_scope_searches", lambda *_a: None)
    observation = {"filer_transaction_digest": "sha256:" + "a" * 64,
                   "range_start": "2006-01-01", "range_end": "2026-09-14",
                   "checked_at": "2026-09-14T03:27:28Z", "orestar": 6279}
    hint = {**observation, "exact_search_count": 3}
    assert ABE._scope_search_cost({"filer_ids": ["5667"]}, {"5667": observation}, {"5667": hint}) == 3
    for key, value in [("checked_at", "2026-09-14T05:00:00Z"), ("orestar", 6280),
                       ("filer_transaction_digest", "sha256:" + "b" * 64)]:
        changed = {**observation, key: value}
        assert ABE._scope_search_cost({"filer_ids": ["5667"]}, {"5667": changed}, {"5667": hint}) is None


def test_workflow_uses_one_admission_and_shared_budget_before_collectors():
    workflow = (ROOT / ".github/workflows/atomic-balance-evidence.yml").read_text()
    assert workflow.index("Admit this attempt") < workflow.index("Hydrate one immutable")
    assert workflow.index("Initialize shared exact search budget") < workflow.index("Capture fresh summaries")
    assert workflow.count("search_budget.py init") == 1
    assert "ORESTAR_SEARCH_BUDGET_PATH: /tmp/atomic-search-budget.json" in workflow
    assert workflow.count("--exclude-filer-ids") == 2
    assert "run-name: 'Atomic balance evidence: ${{ inputs.effort_id }}'" in workflow
    successor = workflow.split("      - name: Continue bounded evidence chain", 1)[1]
    assert 'handoff' in successor and '-f effort_id="$EFFORT_ID"' in successor
    summary = (ROOT / ".github/workflows/earliest-balances.yml").read_text()
    handoff = summary.split("      - name: Hand off completed current sweep", 1)[1]
    assert handoff.index("atomic_evidence_effort.py handoff") < handoff.index("gh workflow run")
    assert '-f effort_id="$EFFORT_ID"' in handoff
    assert '-f max_scopes=40' not in handoff
