"""Execute the projection guards and the shared lane's new writer cases offline."""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap

import pytest

from scripts import pipeline_state as state

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/refresh-balances.yml"
ACTION = ROOT / ".github/actions/await-orestar/action.yml"


def step(name):
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name:", start + 1)
    return text[start:] if end < 0 else text[start:end]


def guard(name):
    block = step(name).split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line)
    body = textwrap.dedent("\n".join(lines))
    assert body.startswith("python - <<'PY'\n")
    return body.split("\n", 1)[1].rsplit("\nPY", 1)[0]


def execute(name):
    exec(compile(guard(name), str(WORKFLOW), "exec"), {})


@pytest.fixture
def projection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT / "scraper"))
    import supabase_sync

    transaction_dir = tmp_path / "data/transactions"
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "txn_2026.csv.gz").write_bytes(b"immutable published rows")
    (tmp_path / "data/aggregated").mkdir()
    snapshot = state._transaction_snapshot_id(tmp_path)
    profiles = {}
    for profile in state.PROFILE_NAMES:
        name = f"data/{profile}_checkpoint.json"
        content = b'{"saved":true}'
        (tmp_path / name).write_bytes(content)
        profiles[profile] = {"archive": {"members": {name: {
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        }}}}
    profiles["transactions"]["transaction_snapshot_id"] = snapshot
    manifest = {"generation": "generation-one", "profiles": profiles}
    base = {"schema": 1, "generation": "generation-one", "transaction_snapshot_id": snapshot,
            "hydrated_profiles": list(state.PROFILE_NAMES)}
    caches = {}
    monkeypatch.setattr(state, "_read_base", lambda _root: copy.deepcopy(base))
    monkeypatch.setattr(state, "_read_manifest", lambda _root: copy.deepcopy(manifest))
    monkeypatch.setattr(supabase_sync, "require_dashboard_cache", lambda key: copy.deepcopy(caches[key]))
    frozen = tmp_path / "frozen.json"
    monkeypatch.setenv("PROJECTION_INPUTS_PATH", str(frozen))
    return {"root": tmp_path, "base": base, "manifest": manifest, "caches": caches,
            "snapshot": snapshot, "frozen": frozen}


def publish_fixture(p):
    execute("Validate and freeze projection inputs")
    # Model actual aggregation after the input check (the real run takes minutes).
    now = datetime.now(timezone.utc).replace(microsecond=0)
    frozen = json.loads(p["frozen"].read_text())
    frozen["started_at"] = (now - timedelta(seconds=2)).isoformat()
    p["frozen"].write_text(json.dumps(frozen))
    source = {"version": 2, "calculation_version": "cash-balance-v2",
              "transaction_snapshot_id": p["snapshot"], "scopes": {},
              "created_at": (now - timedelta(seconds=1)).isoformat()}
    report = {"schema_version": 2, "basis": "paired_capture_window_v1",
              "generated": now.replace(tzinfo=None).isoformat(),
              "flagged": 1, "rows": [{"filer_ids": ["1"], "delta": 25}],
              "refresh_needed": 0, "refresh_rows": [], "unpaired": 0, "unpaired_rows": [],
              "nonactionable": 0, "nonactionable_rows": []}
    for key, value in (("balance_snapshot_source", source), ("balance_discrepancies", report)):
        (p["root"] / f"data/aggregated/{key}.json").write_text(json.dumps(value))
        p["caches"][key] = value
    receipt = {"generation": "publication-one", "detail_count": 1}
    (p["root"] / "data/aggregated/balance_publication.json").write_text(json.dumps(receipt))
    p["caches"]["balance_publication"] = receipt
    return source, report


def test_complete_projection_checks_exact_published_results_without_rewriting_inputs(projection, capsys):
    source, report = publish_fixture(projection)
    before = {name: (projection["root"] / name).read_bytes()
              for profile in projection["manifest"]["profiles"].values()
              for name in profile["archive"]["members"]}
    execute("Verify published balance projection")
    output = capsys.readouterr().out
    result = json.loads(output.split("BALANCE_PROJECTION_RESULT ")[1])
    assert result["transaction_snapshot_id"] == projection["snapshot"]
    assert result["actionable"] == 1  # A successful projection does not claim closure.
    assert result["source_created_at"] == source["created_at"]
    assert result["report_generated"] == report["generated"]
    assert before == {name: (projection["root"] / name).read_bytes() for name in before}


@pytest.mark.parametrize("problem", ["generation", "profiles", "snapshot", "checkpoint", "raw_export"])
def test_input_guard_refuses_mixed_or_unmerged_state_before_projection(projection, problem):
    if problem == "generation":
        projection["manifest"]["generation"] = "newer-generation"
    elif problem == "profiles":
        projection["base"]["hydrated_profiles"].remove("summaries")
    elif problem == "snapshot":
        projection["base"]["transaction_snapshot_id"] = "sha256:" + "a" * 64
    elif problem == "checkpoint":
        (projection["root"] / "data/summaries_checkpoint.json").write_text("different")
    else:
        raw = projection["root"] / "data/_raw"
        raw.mkdir()
        (raw / "unmerged.xlsx").write_bytes(b"unmerged export")
    with pytest.raises(SystemExit):
        execute("Validate and freeze projection inputs")
    assert not projection["frozen"].exists()


@pytest.mark.parametrize("problem", ["manifest", "raw_shards", "checkpoint", "source_cache", "report_cache",
                                    "source_snapshot", "old_source", "old_report", "count", "schema"])
def test_result_guard_refuses_changed_state_or_stale_projection(projection, problem):
    source, report = publish_fixture(projection)
    if problem == "manifest":
        projection["manifest"]["generation"] = "newer-generation"
    elif problem == "raw_shards":
        (projection["root"] / "data/transactions/txn_2026.csv.gz").write_bytes(b"changed rows")
    elif problem == "checkpoint":
        (projection["root"] / "data/auxiliary_checkpoint.json").write_text("changed evidence")
    elif problem == "source_cache":
        projection["caches"]["balance_snapshot_source"] = {**source, "created_at": "2000-01-01T00:00:00Z"}
    elif problem == "report_cache":
        projection["caches"]["balance_discrepancies"] = {**report, "flagged": 0}
    else:
        if problem == "source_snapshot":
            source["transaction_snapshot_id"] = "sha256:" + "b" * 64
        elif problem == "old_source":
            source["created_at"] = "2000-01-01T00:00:00Z"
        elif problem == "old_report":
            report["generated"] = "2000-01-01T00:00:00"
        elif problem == "count":
            report["flagged"] = 0
        elif problem == "schema":
            report["schema_version"] = 1
        for key, value in (("balance_snapshot_source", source), ("balance_discrepancies", report)):
            (projection["root"] / f"data/aggregated/{key}.json").write_text(json.dumps(value))
    with pytest.raises(SystemExit):
        execute("Verify published balance projection")


def test_workflow_is_manual_aggregate_only_and_fails_closed_in_order():
    text = WORKFLOW.read_text()
    ordered = ["Wait for other ORESTAR jobs", "Refresh branch after coordination wait",
               "Hydrate published projection inputs", "Validate and freeze projection inputs",
               "Re-aggregate from published evidence", "Re-key donor aggregates onto resolved entities",
               "Verify published balance projection"]
    positions = [text.index(f"      - name: {name}\n") for name in ordered]
    assert positions == sorted(positions)
    assert "  workflow_dispatch:" in text and "  schedule:" not in text
    assert "actions: read" in text
    assert "balance-projection-${{ github.run_id }}" in text
    assert "cancel-in-progress: false" in text
    assert 'fail-on-timeout: "true"' in step(ordered[0])
    assert "continue-on-error" not in text and "always()" not in text
    assert 'git fetch --depth=1 origin "$GITHUB_REF_NAME"' in step(ordered[1])
    assert 'git reset --hard FETCH_HEAD' in step(ordered[1])
    assert 'pull transactions summaries auxiliary' in step(ordered[2])
    assert "python scraper/process.py" in step(ordered[4])
    assert "python scraper/refresh_donor_aggregates.py" in step(ordered[5])
    for forbidden in ("pipeline_state.py push", "gh workflow run", "dispatch_retry.sh", "xvfb-run",
                      "install_playwright", "scraper/fetch", "scraper/diff_coverage", "scraper/survey_coverage"):
        assert forbidden not in text
    calls = [node for node in ast.walk(ast.parse((ROOT / "scraper/process.py").read_text()))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "scrape_account_summaries"]
    assert not calls


@pytest.mark.parametrize("peer_path", ["refresh-balances.yml", "atomic-balance-evidence.yml"])
@pytest.mark.parametrize("peer_state", ["in_progress", "queued"])
def test_shared_lane_refuses_older_projection_or_atomic_writer(tmp_path, peer_path, peer_state):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scripts = {
        "date": '#!/usr/bin/env bash\nif [ "$1" = "+%s" ]; then echo 100; else echo 2026-09-13T10:00:00Z; fi\n',
        "gh": '''#!/usr/bin/env bash
case "$*" in
  *actions/runs/100*) echo 2026-09-14T10:00:00Z ;;
  *'created=>='*) if [ "$PEER_STATE" = queued ]; then printf '50\\t.github/workflows/%s\\t2026-09-14T09:00:00Z\\tOlder writer\\tOlder writer\\tqueued\\t2026-09-14T09:00:00Z\\n' "$PEER_PATH"; fi ;;
  *status=in_progress*) if [ "$PEER_STATE" = in_progress ]; then printf '50\\t.github/workflows/%s\\t2026-09-14T09:00:00Z\\tOlder writer\\tOlder writer\\tin_progress\\t2026-09-14T09:00:00Z\\n' "$PEER_PATH"; fi ;;
  *) exit 99 ;;
esac
''',
    }
    for name, source in scripts.items():
        path = fake_bin / name
        path.write_text(source)
        path.chmod(0o755)
    body = textwrap.dedent(ACTION.read_text().split("      run: |\n", 1)[1])
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", body],
                            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                                 "GITHUB_RUN_ID": "100", "GITHUB_REPOSITORY": "owner/repo",
                                 "MAX_WAIT": "0", "POLL": "1", "FAIL_ON_TIMEOUT": "true",
                                 "PEER_PATH": peer_path, "PEER_STATE": peer_state},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "refusing to overlap a state writer" in result.stdout


def test_projection_rejects_different_committed_generation(projection):
    publish_fixture(projection)
    projection["caches"]["balance_publication"]["generation"] = "another-publication"
    with pytest.raises(SystemExit, match="receipt differs"):
        execute("Verify published balance projection")
