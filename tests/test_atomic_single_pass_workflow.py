"""Exercise actual admission/planning shell with offline GitHub and collector spies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/atomic-balance-evidence.yml"


def step(name):
    text = WORKFLOW.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name:", start + 1)
    return text[start:] if end < 0 else text[start:end]


def body(name):
    return textwrap.dedent(step(name).split("        run: |\n", 1)[1])


def script(path, contents):
    path.write_text(contents)
    path.chmod(0o755)


@pytest.fixture
def shell(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    output = tmp_path / "outputs"
    api_calls = tmp_path / "api-calls"
    argv_log = tmp_path / "argv"
    # Execute the real helper, replacing only its GitHub transport. No collector
    # is reachable in these admission tests.
    script(binaries / "python", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    script(binaries / "gh", f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
with pathlib.Path(os.environ["API_CALLS"]).open("a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
row = {"id": 7, "run_attempt": 1, "workflow_id": 55,
       "path": ".github/workflows/atomic-balance-evidence.yml",
       "display_title": "Atomic balance evidence: balance-recovery-20260922",
       "status": "in_progress", "conclusion": None}
assert sys.argv[1:4] == ["api", "--method", "GET"]
endpoint = sys.argv[4]
if "/workflows/" in endpoint:
    assert endpoint.endswith("runs?per_page=100&page=1")
    print(json.dumps({"total_count": 1, "workflow_runs": [row]}))
else:
    assert endpoint.endswith("/actions/runs/7")
    print(json.dumps(row))
''')
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
           "GITHUB_OUTPUT": str(output), "API_CALLS": str(api_calls), "ARGV_LOG": str(argv_log),
           "GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1",
           "EFFORT_ID": "balance-recovery-20260922"}
    return {"env": env, "bin": binaries, "output": output, "calls": api_calls,
            "argv": argv_log, "tmp": tmp_path}


def execute(shell, name, **updates):
    return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", body(name)],
                          cwd=ROOT, env={**shell["env"], **updates},
                          text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize("passes,requested,excluded,authorized", [
    ("3", "", ["33", "191"], []),
    ("3", "19521", ["33", "191"], []),
])
def test_workflow_admits_with_actual_helper_and_mode_outputs(shell, passes, requested, excluded, authorized):
    result = execute(shell, "Admit this attempt against the persistent effort limit",
                     MAX_PASSES=passes, REQUESTED_FILER_IDS=requested)
    assert result.returncode == 0, result.stderr
    admitted = json.loads(result.stdout)
    assert admitted["admitted"] is True
    assert admitted["max_passes"] == int(passes)
    assert admitted["excluded_filer_ids"] == excluded
    assert admitted["single_pass_filer_ids"] == authorized
    assert admitted["attempts_used"] == 1 and admitted["max_searches"] == 45


@pytest.mark.parametrize("passes,requested", [
    ("1", ""), ("1", "191"), ("1", "33"), ("1", "191 33"), ("1", "192"),
    ("1", "191\n33"), ("1", "191\r33"), ("1", "191 191"),
    ("1", "191 --max-passes 3"), ("3", "191 --policy other.json"),
    ("3", "191 --effort-id replacement"), ("1", "0191"), ("01", "191"),
])
def test_invalid_or_option_shaped_input_fails_before_api_or_output(shell, passes, requested):
    result = execute(shell, "Admit this attempt against the persistent effort limit",
                     MAX_PASSES=passes, REQUESTED_FILER_IDS=requested)
    assert result.returncode != 0
    assert not shell["calls"].exists()
    assert not shell["output"].exists()


def install_collector_spies(shell):
    script(shell["bin"] / "python", f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
pathlib.Path(os.environ["ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]))
assert sys.argv[1:3] == ["scraper/atomic_balance_evidence.py", "plan"]
out = sys.argv[sys.argv.index("--output") + 1]
pathlib.Path(out).write_text(json.dumps({"scopes": [{"filer_ids": ["191"]}],
    "selected_scope_count": 1, "remaining_scope_count": 0, "transaction_snapshot_id": "frozen"}))
''')
    script(shell["bin"] / "jq", f"#!{sys.executable}\n" + '''
import json, sys
value = json.load(open(sys.argv[-1]))
query = sys.argv[-2]
if "join" in query:
    print(" ".join(sorted({fid for scope in value["scopes"] for fid in scope["filer_ids"]})))
else:
    print(value[query.removeprefix(".")])
''')


@pytest.mark.parametrize("passes,excluded,authorized", [("1", "33", "191"), ("3", "33 191", "")])
def test_planner_shell_preserves_admitted_mode_exclusions_and_scope_cap(shell, passes, excluded, authorized):
    install_collector_spies(shell)
    result = execute(shell, "Plan complete canonical scopes", MAX_PASSES=passes,
                     REQUESTED_FILER_IDS="191", MAX_SCOPES="99", POLICY_MAX_SCOPES="12",
                     MAX_SEARCHES="45", EXCLUDED_FILER_IDS=excluded,
                     SINGLE_PASS_FILER_IDS=authorized, PLAN_PATH=str(shell["tmp"] / "plan.json"))
    assert result.returncode == 0, result.stderr
    argv = json.loads(shell["argv"].read_text())
    assert argv[:6] == ["scraper/atomic_balance_evidence.py", "plan", "--max-scopes", "12", "--max-searches", "45"]
    start = argv.index("--max-passes")
    end = argv.index("--search-cost-hints")
    assert argv[start:end] == ["--max-passes", passes, "--single-pass-filer-ids", *authorized.split()]
    start = argv.index("--exclude-filer-ids")
    end = argv.index("--output")
    assert argv[start:end] == ["--exclude-filer-ids", *excluded.split()]
    assert argv[-2:] == ["--filer-ids", "191"]


def test_workflow_default_reservation_and_late_admission_keep_shared_budget():
    text = WORKFLOW.read_text()
    inputs = text.split("      max_passes:\n", 1)[1].split("      max_scopes:\n", 1)[0]
    assert 'default: "3"' in inputs and "type: choice" in inputs
    assert '          - "3"\n          - "1"' in inputs
    assert "run-name: 'Atomic balance evidence: ${{ inputs.effort_id }}'" in text
    order = [text.index("      - name: " + name) for name in [
        "Wait for other ORESTAR jobs", "Refresh branch after coordination wait",
        "Admit this attempt against the persistent effort limit", "Initialize shared exact search budget",
        "Hydrate one immutable evidence window", "Plan complete canonical scopes",
        "Capture fresh summaries for planned scopes"]]
    assert order == sorted(order)
    assert 'fail-on-timeout: "true"' in text


def test_stabilizer_and_both_plans_use_the_admitted_reservation():
    for name in ("Plan complete canonical scopes", "Replan after recovering empty-plan aggregation"):
        block = step(name)
        assert "MAX_PASSES: ${{ steps.effort.outputs.max_passes }}" in block
        assert "SINGLE_PASS_FILER_IDS: ${{ steps.effort.outputs.single_pass_filer_ids }}" in block
        assert '--max-passes "$MAX_PASSES" --single-pass-filer-ids "${SINGLE_PASS[@]}"' in block
    stabilize = step("Stabilize captured cash against fresh exact evidence")
    assert '--max-passes "${{ steps.effort.outputs.max_passes }}"' in stabilize
    assert "steps.verify.outputs.certified_scopes == steps.plan.outputs.selected_scopes" in stabilize
    assert 'steps.stabilize.outcome }}" != "success"' in step("Enforce truthful terminal status")
    chain = step("Continue bounded evidence chain")
    assert "success() && !cancelled()" in chain
    assert "inputs.recovery_mode != 'identity_backfill'" in chain
    assert "inputs.filer_ids == ''" in chain
    assert "steps.effort.outputs.max_passes == '3'" in chain
    assert "atomic_evidence_effort.py handoff" in chain
    assert "-f max_passes=3" in chain
