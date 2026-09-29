"""All-or-none exact coverage collection for atomic balance evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
SCRAPER_DIR = ROOT / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import diff_coverage as DC  # noqa: E402
from search_budget import ENVIRONMENT_KEY, SearchBudget


SNAPSHOT = "sha256:" + "a" * 64


def _requirements(*scopes: list[str]) -> dict[str, dict]:
    out = {}
    for ids in scopes:
        requirement = {
            "captured_at": 1000.0,
            "capture_day": "1970-01-01",
            "transaction_snapshot_id": SNAPSHOT,
            "scope_ids": ids,
            "active_range_end": "2026-09-10",
            "active_range_conflict": False,
        }
        for filer_id in ids:
            out[filer_id] = requirement
    return out


def _result(filer_id: str) -> dict:
    return {
        "filer_id": filer_id,
        "name": "",
        "orestar": 0,
        "held": 0,
        "complete": True,
        "surplus": [],
        "missing": [],
        "superseded": [],
        "evidence_version": 2, "filer_digest_version": 2,
        "collection_started_at": "2026-09-10T12:00:01Z",
        "checked": "2026-09-10",
        "checked_at": "2026-09-10T12:00:02Z",
        "transaction_snapshot_id": SNAPSHOT,
        "filer_transaction_digest": f"sha256:{filer_id}",
        "range_start": "2006-01-01",
        "range_end": "2026-09-10",
    }


def _args(tmp_path: Path, *, max_minutes: int = 70) -> argparse.Namespace:
    return argparse.Namespace(
        start_year=2006,
        end_date=date(2026, 9, 10),
        scope_plan=tmp_path / "ready.json",
        max_minutes=max_minutes,
    )


class _Browser:
    def close(self) -> None:
        pass


class _Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


def _runner_basics(monkeypatch, groups: list[list[dict]]) -> None:
    ids = [str(target["filer_id"]) for scope in groups for target in scope]
    requirements = _requirements(*[
        [str(target["filer_id"]) for target in scope] for scope in groups
    ])
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda _path: SNAPSHOT)
    monkeypatch.setattr(
        DC, "_load_atomic_scope_plan", lambda *_args: (groups, requirements)
    )
    monkeypatch.setattr(DC, "_load_atomic_entries", lambda: {})
    monkeypatch.setattr(
        DC,
        "transaction_filer_snapshots",
        lambda *_args: {
            filer_id: {
                "held_ids": set(),
                "superseded_ids": set(),
                "filer_transaction_digest": f"sha256:{filer_id}",
            }
            for filer_id in ids
        },
    )
    monkeypatch.setattr(DC, "sync_playwright", lambda: _Playwright())
    monkeypatch.setattr(
        DC.F, "setup_browser_retrying", lambda _pw: (_Browser(), object(), object())
    )
    monkeypatch.setattr(
        DC, "_current_transaction_snapshot_id",
        lambda: (_ for _ in ()).throw(AssertionError("stale DB path used")),
    )
    monkeypatch.setattr(
        DC, "_active_paired_requirements",
        lambda: (_ for _ in ()).throw(AssertionError("stale requirements used")),
    )


def test_scope_plan_preserves_canonical_name(monkeypatch, tmp_path) -> None:
    ready = {
        "version": 1,
        "planned_at": "2026-09-10T12:00:00Z",
        "transaction_snapshot_id": SNAPSHOT,
        "end_date": "2026-09-10",
        "ready_scope_count": 1,
        "scopes": [{
            "filer_ids": ["10", "20"],
            "name": "One Canonical Committee",
            "capture_started_at": 1_789_041_601,
            "captured_at": 1_789_041_602,
            "capture_day": "2026-09-10",
            "app_scope_transaction_digest": "sha256:scope",
        }],
    }
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(ready))

    groups, _requirements_by_id = DC._load_atomic_scope_plan(
        path, SNAPSHOT, date(2026, 9, 10)
    )

    assert groups == [[
        {"filer_id": "10", "name": "One Canonical Committee"},
        {"filer_id": "20", "name": "One Canonical Committee"},
    ]]


def test_complete_scope_is_certified_and_saved_once(monkeypatch) -> None:
    original = {"old": {"filer_id": "old", "complete": True}}
    requirements = _requirements(["10", "20"])
    stored_requirements = []
    saves = []

    def store(entries, result, *, active_requirements):
        stored_requirements.append(active_requirements)
        entries[result["filer_id"]] = result

    monkeypatch.setattr(DC, "_store_usable_result", store)
    monkeypatch.setattr(
        DC,
        "certify_exact_scope_rows",
        lambda _rows, _requirements, candidates, *_args, **_kwargs: (
            {filer_id: {} for filer_id in candidates}, set(), None
        ),
    )
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda _path: SNAPSHOT)
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))

    saved = DC._persist_usable_scope(
        original,
        [_result("10"), _result("20")],
        requirements,
        transaction_id=SNAPSHOT,
        start=date(2006, 1, 1),
        end=date(2026, 9, 10),
    )

    assert set(original) == {"old"}
    assert set(saved) == {"old", "10", "20"}
    assert saves == [saved]
    assert stored_requirements == [requirements, requirements]
    assert requirements["10"] is requirements["20"]


def test_budget_mid_scope_discards_staged_members_and_stops_without_restart(
    monkeypatch, tmp_path, capsys,
) -> None:
    groups = [[{"filer_id": "10"}, {"filer_id": "20"}, {"filer_id": "30"}],
              [{"filer_id": "40"}]]
    _runner_basics(monkeypatch, groups)
    budget = SearchBudget.initialize(tmp_path / "budget.json", 2)
    monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
    old = _result("10")
    monkeypatch.setattr(DC, "_load_atomic_entries", lambda: {"10": old})
    calls, setups, saves = [], [], []
    def setup(_pw):
        setups.append(True)
        return _Browser(), object(), object()
    def collect(_page, fid, *_args, **_kwargs):
        calls.append(fid)
        budget.consume(fid, {})
        if fid == "20":
            budget.consume(fid, {})  # New deeper child exceeds the estimate.
        return set()
    monkeypatch.setattr(DC.F, "setup_browser_retrying", setup)
    monkeypatch.setattr(DC, "orestar_ids", collect)
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))
    monkeypatch.setattr(DC, "_persist_usable_scope",
                        lambda *_args, **_kwargs: pytest.fail("partial scope certified"))
    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1
    assert calls == ["10", "20"]
    assert len(setups) == 1
    assert len(saves) == 1
    assert saves[0]["10"]["checked_at"] == old["checked_at"]
    assert saves[0]["30"]["complete"] is None
    assert saves[0]["30"]["last_attempt_collection_started_at"] is None
    assert all(saves[0][fid]["last_failure"] == "search_budget" for fid in ["10", "20", "30"])
    assert "40" not in saves[0]
    result = capsys.readouterr().out
    assert "retryable=0" in result and "search_budget_exhausted=1" in result


def test_missing_budget_stops_before_browser_or_state_reads(monkeypatch, tmp_path):
    monkeypatch.setenv(ENVIRONMENT_KEY, str(tmp_path / "missing.json"))
    monkeypatch.setattr(DC, "sync_playwright", lambda: pytest.fail("browser started"))
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda *_: pytest.fail("state read"))
    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1


def test_scope_certification_failure_saves_no_usable_member(monkeypatch) -> None:
    original = {"old": {"filer_id": "old", "complete": True}}
    monkeypatch.setattr(
        DC,
        "_store_usable_result",
        lambda entries, result, **_kwargs: entries.__setitem__(
            result["filer_id"], result
        ),
    )
    monkeypatch.setattr(
        DC,
        "certify_exact_scope_rows",
        lambda *_args, **_kwargs: ({"10": {}}, {"20"}, None),
    )
    saves = []
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))

    with pytest.raises(ValueError, match="did not certify"):
        DC._persist_usable_scope(
            original,
            [_result("10"), _result("20")],
            _requirements(["10", "20"]),
            transaction_id=SNAPSHOT,
            start=date(2006, 1, 1),
            end=date(2026, 9, 10),
        )

    assert set(original) == {"old"}
    assert saves == []


def test_second_member_storage_refusal_leaks_no_first_member(monkeypatch) -> None:
    original = {"old": {"filer_id": "old", "complete": True}}
    calls = []

    def store(entries, result, **_kwargs):
        calls.append(result["filer_id"])
        entries[result["filer_id"]] = result
        if result["filer_id"] == "20":
            raise ValueError("history pins are full")

    monkeypatch.setattr(DC, "_store_usable_result", store)
    saves = []
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))

    with pytest.raises(ValueError, match="history pins"):
        DC._persist_usable_scope(
            original,
            [_result("10"), _result("20")],
            _requirements(["10", "20"]),
            transaction_id=SNAPSHOT,
            start=date(2006, 1, 1),
            end=date(2026, 9, 10),
        )

    assert calls == ["10", "20"]
    assert set(original) == {"old"}
    assert saves == []


def test_failed_scope_preserves_prior_usable_rows_in_one_save(monkeypatch) -> None:
    original = {
        "10": {
            "filer_id": "10",
            "complete": False,
            "missing": ["old"],
            "checked_at": "2026-09-01T00:00:00Z",
        }
    }
    saves = []
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda _path: SNAPSHOT)
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))

    saved = DC._persist_failed_scope(
        original,
        [{"filer_id": "10"}, {"filer_id": "20"}],
        {"20": "session_expired"},
        transaction_id=SNAPSHOT,
        local_digests={"10": "sha256:10", "20": "sha256:20"},
        start=date(2006, 1, 1),
        end=date(2026, 9, 10),
        collection_starts={
            "10": "2026-09-10T12:00:01Z",
            "20": "2026-09-10T12:00:02Z",
        },
    )

    assert original["10"]["missing"] == ["old"]
    assert saved["10"]["missing"] == ["old"]
    assert saved["10"]["last_failure"] == "scope_incomplete"
    assert saved["20"]["complete"] is None
    assert saved["20"]["last_failure"] == "session_expired"
    assert saves == [saved]


def test_soft_deadline_is_checked_only_between_scopes(
    monkeypatch, tmp_path,
) -> None:
    groups = [
        [{"filer_id": "10"}, {"filer_id": "20"}],
        [{"filer_id": "30"}],
    ]
    _runner_basics(monkeypatch, groups)
    clock = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr(DC.time, "monotonic", lambda: next(clock))
    calls = []

    def collect(_page, filer_id, *_args, **kwargs):
        calls.append((filer_id, kwargs["deadline"]))
        return set()

    monkeypatch.setattr(DC, "orestar_ids", collect)
    commits = []
    monkeypatch.setattr(
        DC,
        "_persist_usable_scope",
        lambda entries, results, *_args, **_kwargs: (
            commits.append([result["filer_id"] for result in results]) or entries
        ),
    )

    assert DC._run_atomic_scope_plan(_args(tmp_path, max_minutes=1)) == 1
    assert calls == [("10", None), ("20", None)]
    assert commits == [["10", "20"]]


def test_retryable_breaker_stops_between_complete_scope_attempts(
    monkeypatch, tmp_path, capsys,
) -> None:
    groups = [
        [{"filer_id": "10"}, {"filer_id": "20"}],
        [{"filer_id": "30"}, {"filer_id": "40"}],
        [{"filer_id": "50"}],
    ]
    _runner_basics(monkeypatch, groups)
    monkeypatch.setattr(DC.time, "monotonic", lambda: 0.0)
    calls = []

    def collect(_page, filer_id, *_args, **_kwargs):
        calls.append(filer_id)
        return None if filer_id in {"10", "30"} else set()

    monkeypatch.setattr(DC, "orestar_ids", collect)
    failed = []
    monkeypatch.setattr(
        DC,
        "_persist_failed_scope",
        lambda entries, targets, reasons, **_kwargs: (
            failed.append(([target["filer_id"] for target in targets], reasons))
            or entries
        ),
    )

    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1
    assert calls == ["10", "20", "30", "40"]
    assert [item[0] for item in failed] == [["10", "20"], ["30", "40"]]
    assert "blocked=1" in capsys.readouterr().out


def test_last_member_failure_does_not_eagerly_restart_before_breaker(
    monkeypatch, tmp_path,
) -> None:
    groups = [
        [{"filer_id": "10"}],
        [{"filer_id": "20"}],
        [{"filer_id": "30"}],
    ]
    _runner_basics(monkeypatch, groups)
    monkeypatch.setattr(DC.time, "monotonic", lambda: 0.0)
    setups = []

    def setup(_pw):
        setups.append(True)
        return _Browser(), object(), object()

    monkeypatch.setattr(DC.F, "setup_browser_retrying", setup)
    monkeypatch.setattr(DC, "orestar_ids", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        DC, "_persist_failed_scope", lambda entries, *_args, **_kwargs: entries
    )

    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1
    # Initial browser plus one lazy restart before scope two. The second scope
    # trips the breaker, so no third browser is launched.
    assert len(setups) == 2


def test_snapshot_drift_after_failed_scope_stops_before_next_site_call(
    monkeypatch, tmp_path,
) -> None:
    groups = [
        [{"filer_id": "10"}],
        [{"filer_id": "20"}],
    ]
    _runner_basics(monkeypatch, groups)
    hashes = iter([SNAPSHOT, SNAPSHOT, SNAPSHOT, "sha256:" + "b" * 64])
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda _path: next(hashes))
    monkeypatch.setattr(DC.time, "monotonic", lambda: 0.0)
    calls = []
    monkeypatch.setattr(
        DC,
        "orestar_ids",
        lambda _page, filer_id, *_args, **_kwargs: calls.append(filer_id) or None,
    )
    monkeypatch.setattr(
        DC, "_persist_failed_scope", lambda entries, *_args, **_kwargs: entries
    )

    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1
    assert calls == ["10"]


def test_fatal_scope_drift_stops_before_later_scope(
    monkeypatch, tmp_path, capsys,
) -> None:
    groups = [
        [{"filer_id": "10"}, {"filer_id": "20"}],
        [{"filer_id": "30"}],
    ]
    _runner_basics(monkeypatch, groups)
    monkeypatch.setattr(DC.time, "monotonic", lambda: 0.0)
    calls = []
    monkeypatch.setattr(
        DC,
        "orestar_ids",
        lambda _page, filer_id, *_args, **_kwargs: calls.append(filer_id) or set(),
    )
    monkeypatch.setattr(
        DC,
        "_persist_usable_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DC.AtomicSnapshotDrift("snapshot changed")
        ),
    )

    assert DC._run_atomic_scope_plan(_args(tmp_path)) == 1
    assert calls == ["10", "20"]
    output = capsys.readouterr().out
    assert "usable_scopes=0" in output
    assert "unusable_scopes=0" in output
    assert "remaining_scopes=2" in output


def test_snapshot_change_after_collection_prevents_scope_save(monkeypatch) -> None:
    monkeypatch.setattr(
        DC, "transaction_snapshot_id", lambda _path: "sha256:" + "b" * 64
    )
    monkeypatch.setattr(
        DC,
        "_store_usable_result",
        lambda entries, result, **_kwargs: entries.__setitem__(
            result["filer_id"], result
        ),
    )
    monkeypatch.setattr(
        DC,
        "certify_exact_scope_rows",
        lambda _rows, _requirements, candidates, *_args, **_kwargs: (
            {filer_id: {} for filer_id in candidates}, set(), None
        ),
    )
    saves = []
    monkeypatch.setattr(DC, "_save", lambda entries: saves.append(entries))

    with pytest.raises(DC.AtomicSnapshotDrift, match="snapshot changed"):
        DC._persist_usable_scope(
            {},
            [_result("10"), _result("20")],
            _requirements(["10", "20"]),
            transaction_id=SNAPSHOT,
            start=date(2006, 1, 1),
            end=date(2026, 9, 10),
        )
    assert saves == []


def test_atomic_save_replace_failure_leaves_existing_json(monkeypatch, tmp_path) -> None:
    path = tmp_path / "coverage_diff.json"
    path.write_text('[{"filer_id":"old"}]\n')
    monkeypatch.setattr(DC, "DIFF_PATH", path)
    monkeypatch.setattr(
        DC.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        DC._save({"new": {"filer_id": "new", "complete": True}})

    assert json.loads(path.read_text()) == [{"filer_id": "old"}]
    assert list(tmp_path.glob(".coverage_diff.json.*.tmp")) == []


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        '{"filer_id":"10"}',
        '[{"filer_id":"10"},{"filer_id":"10"}]',
        '[{"complete":true}]',
    ],
)
def test_atomic_loader_refuses_lossy_existing_state(
    monkeypatch, tmp_path, contents,
) -> None:
    path = tmp_path / "coverage_diff.json"
    path.write_text(contents)
    monkeypatch.setattr(DC, "DIFF_PATH", path)

    with pytest.raises(DC.AtomicEvidenceError):
        DC._load_atomic_entries()

    assert path.read_text() == contents
