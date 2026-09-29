"""Contracts for keeping generated pipeline state out of Git."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from scraper.balance_snapshot import transaction_snapshot_id


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pipeline_state", ROOT / "scripts" / "pipeline_state.py"
)
assert SPEC and SPEC.loader
pipeline_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline_state)


def _seed(root: Path) -> None:
    (root / "data" / "transactions").mkdir(parents=True)
    (root / "data" / "transactions" / "txn_2026.csv.gz").write_bytes(b"ledger")
    (root / "data" / "fetched_windows.json").write_text("[]")
    (root / "data" / "earliest_balances.json").write_text("{}")
    (root / "data" / "coverage_diff.json").write_text("[]")


def _snapshot_with_payloads(root: Path):
    manifest = pipeline_state.publish(
        root,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    payloads = {}
    for profile in pipeline_state.PROFILE_NAMES:
        archive_path = root / f"{profile}.tar.gz"
        pipeline_state._build_profile_archive(root, profile, archive_path)
        payloads[manifest["profiles"][profile]["archive"]["object"]] = (
            archive_path.read_bytes()
        )
    for shard in manifest["profiles"]["transactions"]["shards"]:
        payloads[shard["object"]] = (root / shard["path"]).read_bytes()
    return manifest, payloads


class _DatabaseCursor:
    def __init__(self, current, *, cas_succeeds=True):
        self.current = current
        self.cas_succeeds = cas_succeeds
        self.executed = []
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(query.split()).lower()
        self.executed.append((normalized, params))
        if normalized.startswith("select data from dashboard_cache"):
            self._next = (self.current,) if self.current is not None else None
        elif " returning key" in normalized:
            self._next = (
                (pipeline_state.MANIFEST_CACHE_KEY,)
                if self.cas_succeeds
                else None
            )

    def fetchone(self):
        result, self._next = self._next, None
        return result


class _DatabaseConnection:
    def __init__(self, current, *, cas_succeeds=True):
        self.db_cursor = _DatabaseCursor(current, cas_succeeds=cas_succeeds)
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return self.db_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def test_profile_archives_are_deterministic_and_strict(tmp_path: Path) -> None:
    _seed(tmp_path)
    # These old per-run/generated files must not become durable state.
    (tmp_path / "data" / "backfilled_filers.txt").write_text("123\n")
    (tmp_path / "data" / "completed_backfills.txt").write_text("123\n")
    (tmp_path / "data" / "earliest_balances_remaining.txt").write_text("42")
    (tmp_path / "data" / "review_queue.json").write_text("[]")
    (tmp_path / "data" / "aggregated").mkdir()
    (tmp_path / "data" / "aggregated" / "summary.json").write_text("{}")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    members = pipeline_state._build_profile_archive(tmp_path, "transactions", first)
    pipeline_state._build_profile_archive(tmp_path, "transactions", second)

    assert first.read_bytes() == second.read_bytes()
    assert set(members) == {"data/fetched_windows.json"}
    for profile in pipeline_state.PROFILE_NAMES:
        selected = {p.relative_to(tmp_path).as_posix()
                    for p in pipeline_state._profile_paths(tmp_path, profile)}
        assert "data/completed_backfills.txt" not in selected
        assert "data/earliest_balances_remaining.txt" not in selected
        assert "data/review_queue.json" not in selected
        assert not any(path.startswith("data/aggregated/") for path in selected)
    auxiliary = {
        path.relative_to(tmp_path).as_posix()
        for path in pipeline_state._profile_paths(tmp_path, "auxiliary")
    }
    assert "data/backfilled_filers.txt" in auxiliary


def test_auxiliary_profile_preserves_filer_keyed_leadership_mapping(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    leadership = tmp_path / "data" / "leadership_roles.json"
    leadership.write_text(
        '{"123":{"role_title":"Speaker of the House",'
        '"legislator_name":"Example Person"}}'
    )
    archive = tmp_path / "auxiliary.tar.gz"

    members = pipeline_state._build_profile_archive(tmp_path, "auxiliary", archive)

    assert "data/leadership_roles.json" in members


def test_dry_run_builds_complete_content_addressed_manifest(tmp_path: Path) -> None:
    _seed(tmp_path)
    manifest = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )

    assert set(manifest["profiles"]) == set(pipeline_state.PROFILE_NAMES)
    shard = manifest["profiles"]["transactions"]["shards"][0]
    assert shard["path"] == "data/transactions/txn_2026.csv.gz"
    assert shard["object"].endswith(shard["sha256"])
    assert (
        manifest["profiles"]["transactions"]["transaction_snapshot_id"]
        == transaction_snapshot_id(tmp_path / "data" / "transactions")
    )
    assert manifest["parent_generation"] is None


def test_manifest_rejects_paths_outside_allowlist(tmp_path: Path) -> None:
    _seed(tmp_path)
    manifest = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    manifest = json.loads(json.dumps(manifest))
    archive = manifest["profiles"]["auxiliary"]["archive"]
    archive["members"]["../secret"] = {"sha256": "0" * 64, "size": 1}
    with pytest.raises(pipeline_state.StateError, match="Unsafe auxiliary member"):
        pipeline_state._validate_manifest(manifest)


def test_push_requires_the_generation_that_was_pulled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    remote = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _config: remote)

    with pytest.raises(pipeline_state.StateError, match="not pulled"):
        pipeline_state.publish(
            tmp_path,
            ("auxiliary",),
            bootstrap=False,
            dry_run=False,
            garbage_collect=False,
        )


def test_push_rejects_a_profile_not_hydrated_from_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    remote = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    pipeline_state._write_base(tmp_path, remote, ("transactions",))
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: remote)

    with pytest.raises(pipeline_state.StateError, match="not hydrated.*auxiliary"):
        pipeline_state.publish(
            tmp_path,
            ("auxiliary",),
            bootstrap=False,
            dry_run=False,
            garbage_collect=False,
        )


def test_pull_removes_omitted_members_and_records_hydrated_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    manifest, payloads = _snapshot_with_payloads(tmp_path)
    stale = tmp_path / "data" / "candidate_filings.json"
    stale.write_text('{"stale":true}')
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: manifest)
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )

    pipeline_state.pull(tmp_path, ("auxiliary",))

    assert not stale.exists()
    quarantined = list(
        (tmp_path / ".pipeline-state-quarantine").glob(
            "*/data/candidate_filings.json"
        )
    )
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == '{"stale":true}'
    base = json.loads((tmp_path / pipeline_state.BASE_FILE).read_text())
    assert base["hydrated_profiles"] == ["auxiliary"]
    assert base["transaction_snapshot_id"] == manifest["profiles"]["transactions"][
        "transaction_snapshot_id"
    ]
    assert (tmp_path / "data" / "earliest_balances.json").read_text() == "{}"
    unowned = tmp_path / "data" / "unowned.txt"
    unowned.write_text("keep")
    pipeline_state.pull(tmp_path, ("auxiliary",))
    assert unowned.read_text() == "keep"


def test_same_generation_pulls_union_hydrated_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    manifest, payloads = _snapshot_with_payloads(tmp_path)
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: manifest)
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )

    pipeline_state.pull(tmp_path, ("summaries",))
    pipeline_state.pull(tmp_path, ("auxiliary",))

    base = json.loads((tmp_path / pipeline_state.BASE_FILE).read_text())
    assert base["hydrated_profiles"] == ["summaries", "auxiliary"]


def test_new_generation_pull_resets_hydrated_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    first, payloads = _snapshot_with_payloads(tmp_path)
    current = {"manifest": first}
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(
        pipeline_state, "_read_manifest", lambda _root: current["manifest"]
    )
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )
    pipeline_state.pull(tmp_path, ("summaries",))

    second = json.loads(json.dumps(first))
    second["generation"] = str(uuid.uuid4())
    second["parent_generation"] = first["generation"]
    current["manifest"] = second
    pipeline_state.pull(tmp_path, ("auxiliary",))

    base = json.loads((tmp_path / pipeline_state.BASE_FILE).read_text())
    assert base["generation"] == second["generation"]
    assert base["hydrated_profiles"] == ["auxiliary"]


def test_pull_verifies_everything_before_removing_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    manifest, payloads = _snapshot_with_payloads(tmp_path)
    stale = tmp_path / "data" / "candidate_filings.json"
    stale.write_text("stale")
    pipeline_state._write_base(tmp_path, manifest, ("auxiliary",))
    base_before = (tmp_path / pipeline_state.BASE_FILE).read_bytes()
    auxiliary_object = manifest["profiles"]["auxiliary"]["archive"]["object"]
    payloads[auxiliary_object] = b"corrupt"
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: manifest)
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )

    with pytest.raises(pipeline_state.StateError, match="checksum"):
        pipeline_state.pull(tmp_path, ("auxiliary",))
    assert stale.read_text() == "stale"
    assert (tmp_path / pipeline_state.BASE_FILE).read_bytes() == base_before


def test_pull_restores_overwritten_and_quarantined_files_if_base_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    manifest, payloads = _snapshot_with_payloads(tmp_path)
    overwritten = tmp_path / "data" / "coverage_diff.json"
    overwritten.write_text("old local coverage")
    stale = tmp_path / "data" / "candidate_filings.json"
    stale.write_text("stale")
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: manifest)
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )
    monkeypatch.setattr(
        pipeline_state,
        "_write_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pipeline_state.StateError("base failed")
        ),
    )

    with pytest.raises(pipeline_state.StateError, match="base failed"):
        pipeline_state.pull(tmp_path, ("auxiliary",))
    assert overwritten.read_text() == "old local coverage"
    assert stale.read_text() == "stale"


def test_pull_restores_every_destination_if_second_install_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    candidate = tmp_path / "data" / "candidate_filings.json"
    candidate.write_text("remote candidate")
    manifest, payloads = _snapshot_with_payloads(tmp_path)
    pipeline_state._write_base(tmp_path, manifest, ("auxiliary",))
    base_before = (tmp_path / pipeline_state.BASE_FILE).read_bytes()
    coverage = tmp_path / "data" / "coverage_diff.json"
    candidate.write_text("old local candidate")
    coverage.write_text("old local coverage")
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: manifest)
    monkeypatch.setattr(
        pipeline_state, "_download", lambda _config, name: payloads.get(name)
    )
    real_replace = pipeline_state.os.replace
    installs = 0
    failed = False

    def fail_second_install(source, target):
        nonlocal installs, failed
        source_path, target_path = Path(source), Path(target)
        from_staging = any(
            part.startswith("pipeline-state-pull-") for part in source_path.parts
        )
        into_data = target_path.is_relative_to(tmp_path / "data")
        if from_staging and into_data:
            installs += 1
            if installs == 2 and not failed:
                failed = True
                raise OSError("injected second install failure")
        return real_replace(source, target)

    monkeypatch.setattr(pipeline_state.os, "replace", fail_second_install)

    with pytest.raises(pipeline_state.StateError, match="second install failure"):
        pipeline_state.pull(tmp_path, ("auxiliary",))
    assert candidate.read_text() == "old local candidate"
    assert coverage.read_text() == "old local coverage"
    assert (tmp_path / pipeline_state.BASE_FILE).read_bytes() == base_before


def test_commit_takes_advisory_lock_and_uses_generation_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    previous = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    manifest = json.loads(json.dumps(previous))
    manifest["generation"] = str(uuid.uuid4())
    manifest["parent_generation"] = previous["generation"]
    connection = _DatabaseConnection(previous)
    uploads = []
    monkeypatch.setattr(pipeline_state, "_connect_database", lambda _root: connection)
    monkeypatch.setattr(
        pipeline_state,
        "_upload",
        lambda _config, name, payload, content_type, *, upsert: uploads.append(name),
    )

    pipeline_state._commit_manifest(
        tmp_path,
        ("url", "key", "bucket"),
        manifest,
        previous["generation"],
    )

    queries = connection.db_cursor.executed
    assert queries[0][0].startswith("select pg_advisory_lock")
    update = next((query, params) for query, params in queries if query.startswith("update"))
    assert "data->>'generation' = %s" in update[0]
    assert update[1][-1] == previous["generation"]
    assert queries[-1][0].startswith("select pg_advisory_unlock")
    assert uploads == [
        f"{pipeline_state._prefix()}/previous.json",
        f"{pipeline_state._prefix()}/latest.json",
    ]
    assert (connection.commits, connection.rollbacks, connection.closes) == (2, 0, 1)


def test_commit_rejects_generation_changed_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    expected = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    current = json.loads(json.dumps(expected))
    current["generation"] = str(uuid.uuid4())
    manifest = json.loads(json.dumps(expected))
    manifest["generation"] = str(uuid.uuid4())
    manifest["parent_generation"] = expected["generation"]
    connection = _DatabaseConnection(current)
    monkeypatch.setattr(pipeline_state, "_connect_database", lambda _root: connection)
    monkeypatch.setattr(
        pipeline_state,
        "_upload",
        lambda *_args, **_kwargs: pytest.fail("stale publisher must not move mirrors"),
    )

    with pytest.raises(pipeline_state.StateError, match="advanced"):
        pipeline_state._commit_manifest(
            tmp_path,
            ("url", "key", "bucket"),
            manifest,
            expected["generation"],
        )
    assert (connection.commits, connection.rollbacks, connection.closes) == (1, 1, 1)


def test_mirror_failure_does_not_roll_back_authoritative_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _seed(tmp_path)
    previous = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    manifest = json.loads(json.dumps(previous))
    manifest["generation"] = str(uuid.uuid4())
    manifest["parent_generation"] = previous["generation"]
    connection = _DatabaseConnection(previous)
    monkeypatch.setattr(pipeline_state, "_connect_database", lambda _root: connection)
    monkeypatch.setattr(
        pipeline_state,
        "_upload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pipeline_state.StateError("mirror unavailable")
        ),
    )

    pipeline_state._commit_manifest(
        tmp_path,
        ("url", "key", "bucket"),
        manifest,
        previous["generation"],
    )

    assert "Storage manifest mirror deferred" in capsys.readouterr().err
    assert (connection.commits, connection.rollbacks, connection.closes) == (2, 0, 1)


def test_database_errors_and_malformed_rows_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pipeline_state,
        "_connect_database",
        lambda _root: (_ for _ in ()).throw(pipeline_state.StateError("database down")),
    )
    with pytest.raises(pipeline_state.StateError, match="database down"):
        pipeline_state._read_manifest(tmp_path)
    with pytest.raises(pipeline_state.StateError, match="Unsupported or malformed"):
        pipeline_state._manifest_from_database({"not": "a manifest"})


def test_legacy_base_without_schema_or_scope_is_rejected(tmp_path: Path) -> None:
    (tmp_path / pipeline_state.BASE_FILE).write_text('{"generation":"legacy"}\n')
    assert pipeline_state._read_base(tmp_path) is None


def test_automatic_garbage_collection_is_disabled(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(pipeline_state.StateError, match="garbage collection is disabled"):
        pipeline_state.publish(
            tmp_path,
            pipeline_state.PROFILE_NAMES,
            bootstrap=True,
            dry_run=True,
            garbage_collect=True,
        )


def test_immutable_upload_failure_prevents_manifest_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path)
    remote = pipeline_state.publish(
        tmp_path,
        pipeline_state.PROFILE_NAMES,
        bootstrap=True,
        dry_run=True,
        garbage_collect=False,
    )
    pipeline_state._write_base(tmp_path, remote, pipeline_state.PROFILE_NAMES)
    monkeypatch.setattr(pipeline_state, "_config", lambda _root: ("url", "key", "bucket"))
    monkeypatch.setattr(pipeline_state, "_read_manifest", lambda _root: remote)
    monkeypatch.setattr(
        pipeline_state,
        "_upload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pipeline_state.StateError("immutable upload failed")
        ),
    )
    monkeypatch.setattr(
        pipeline_state,
        "_commit_manifest",
        lambda *_args, **_kwargs: pytest.fail("CAS must follow all immutable uploads"),
    )

    with pytest.raises(pipeline_state.StateError, match="immutable upload failed"):
        pipeline_state.publish(
            tmp_path,
            ("auxiliary",),
            bootstrap=False,
            dry_run=False,
            garbage_collect=False,
        )


def test_script_help_works_outside_repository(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pipeline_state.py"), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_status_cli_accepts_no_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(pipeline_state, "status", lambda root: seen.append(root))

    assert pipeline_state.main(["status"]) == 0
    assert len(seen) == 1
