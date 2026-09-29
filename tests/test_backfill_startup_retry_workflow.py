"""Executable contracts for automatic identity-startup retry handoff.

The parent fetch runs once. Only a proved, pre-work ORESTAR startup failure
may dispatch a child workflow. Retry metadata is packed into ``end_date`` so
``backfill.yml`` stays within GitHub's ten-input ``workflow_dispatch`` limit.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "backfill.yml"
AWAIT_ACTION = ROOT / ".github" / "actions" / "await-orestar" / "action.yml"
RETRY_SCRIPT = ROOT / "scripts" / "retry_backfill_startup.sh"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify-filers.yml"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
STARTUP_MARKER = "REMEDIATION_STARTUP_EXHAUSTED attempts=3"

SCRAPER_DIR = ROOT / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import fetch as F  # noqa: E402


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _step_section(step_name: str) -> str:
    workflow = WORKFLOW.read_text()
    marker = f"      - name: {step_name}"
    start = workflow.index(marker)
    next_step = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if next_step < 0 else workflow[start:next_step]


def _run_block(step_name: str) -> str:
    section = WORKFLOW.read_text()[
        WORKFLOW.read_text().index(f"      - name: {step_name}"):
    ]
    lines = section.splitlines()
    run_index = next(
        index for index, line in enumerate(lines) if line.strip() == "run: |"
    )
    run_indent = len(lines[run_index]) - len(lines[run_index].lstrip())
    body: list[str] = []
    for line in lines[run_index + 1:]:
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent <= run_indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)).strip()


def _render(block: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1)
        assert expression in values, f"unrendered workflow expression: {expression}"
        return values[expression]

    rendered = EXPRESSION.sub(replace, block)
    assert "${{" not in rendered
    return rendered


def _output_values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def _await_run_block() -> str:
    source = AWAIT_ACTION.read_text()
    _, literal = source.split("      run: |", 1)
    return textwrap.dedent(literal).strip()


def _fake_fetch_env(
    tmp_path: Path, output: str, status: int,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "xvfb-run",
        "#!/usr/bin/env bash\nprintf '%b\\n' \"$FAKE_FETCH_OUTPUT\"\n"
        "exit \"$FAKE_FETCH_STATUS\"\n",
    )
    output_path = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output_path),
        "FAKE_FETCH_OUTPUT": output,
        "FAKE_FETCH_STATUS": str(status),
    }
    return env, output_path


@pytest.mark.parametrize(
    ("fetch_output", "status", "identity_mode", "is_auto", "expected"),
    [
        (
            STARTUP_MARKER,
            1,
            "true",
            "true",
            {"startup_retryable": "1", "progress": "0", "retry": "0"},
        ),
        (
            STARTUP_MARKER,
            1,
            "true",
            "false",
            {"startup_retryable": "0", "progress": "0", "retry": "0"},
        ),
        (
            STARTUP_MARKER,
            1,
            "false",
            "true",
            {"startup_retryable": "0", "progress": "0", "retry": "0"},
        ),
        (
            "database connection failed",
            17,
            "true",
            "true",
            {"startup_retryable": "0", "progress": "0", "retry": "0"},
        ),
        (
            "Browser setup failed (attempt 3/3): Page.goto timed out",
            1,
            "true",
            "true",
            {"startup_retryable": "0", "progress": "0", "retry": "0"},
        ),
        (
            f"{STARTUP_MARKER}\\n"
            "REMEDIATION_RESULT progress=2 retry=1 completed=0 incomplete=1",
            1,
            "true",
            "true",
            {"startup_retryable": "0", "progress": "2", "retry": "1"},
        ),
        (
            "REMEDIATION_RESULT progress=4 retry=1 completed=0 incomplete=1",
            0,
            "true",
            "true",
            {"startup_retryable": "0", "progress": "4", "retry": "1"},
        ),
    ],
)
def test_fetch_runs_once_publishes_evidence_and_preserves_exit(
    tmp_path, fetch_output, status, identity_mode, is_auto, expected,
) -> None:
    env, output_path = _fake_fetch_env(tmp_path, fetch_output, status)
    log_path = tmp_path / "backfill.log"
    block = _render(
        _run_block("Backfill ORESTAR data"),
        {
            "steps.resolve.outputs.resolved": "21544",
            "steps.resolve.outputs.identity_mode": identity_mode,
            "steps.resolve.outputs.is_auto": is_auto,
            "steps.resolve.outputs.identity_end_date": "2026-09-03",
            "steps.resolve.outputs.identity_resume": "true",
            "inputs.start_year": "2006",
            "inputs.date_field": "tran",
            "inputs.end_year": "",
        },
    ).replace("/tmp/backfill.log", str(log_path))

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == status
    values = _output_values(output_path)
    assert {key: values[key] for key in expected} == expected
    # There is no same-job wrapper: the one xvfb invocation is authoritative.
    assert result.stdout.count(fetch_output.split("\\n", 1)[0]) == 1


def test_fetch_preserves_a_tee_failure(tmp_path) -> None:
    env, output_path = _fake_fetch_env(tmp_path, "fetch succeeded", 0)
    _write_executable(
        tmp_path / "bin" / "tee",
        "#!/usr/bin/env bash\n/bin/cat >/dev/null\nexit 23\n",
    )
    block = _render(
        _run_block("Backfill ORESTAR data"),
        {
            "steps.resolve.outputs.resolved": "21544",
            "steps.resolve.outputs.identity_mode": "true",
            "steps.resolve.outputs.is_auto": "true",
            "steps.resolve.outputs.identity_end_date": "2026-09-03",
            "steps.resolve.outputs.identity_resume": "true",
            "inputs.start_year": "2006",
            "inputs.date_field": "tran",
            "inputs.end_year": "",
        },
    ).replace("/tmp/backfill.log", str(tmp_path / "backfill.log"))

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    assert _output_values(output_path)["startup_retryable"] == "0"


def _helper_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    call_log = tmp_path / "calls"
    dispatch = tmp_path / "dispatch"
    _write_executable(
        dispatch,
        "#!/usr/bin/env bash\n"
        "{ printf 'dispatch'; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$CALL_LOG\"\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nprintf 'sleep\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n",
    )
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "BACKFILL_RETRY_DISPATCH_SCRIPT": str(dispatch),
    }, call_log


def _parse_dispatch_fields(line: str) -> tuple[list[str], dict[str, str]]:
    fields = line.split("\t")
    prefix = fields[:4]
    assert fields[0] == "dispatch"
    values: dict[str, str] = {}
    rest = fields[4:]
    assert len(rest) % 2 == 0
    for flag, assignment in zip(rest[0::2], rest[1::2]):
        assert flag == "-f"
        key, value = assignment.split("=", 1)
        values[key] = value
    return prefix, values


def test_parent_dispatches_explicit_child_and_preserves_chain_inputs(
    tmp_path,
) -> None:
    env, call_log = _helper_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT),
            "0", "main", "21544 21770", "2006", "2026-09-03",
            "tran", "7", "true", "true", "true", "21544 21770",
            "123456", "1",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    calls = call_log.read_text().splitlines()
    assert len(calls) == 1
    prefix, values = _parse_dispatch_fields(calls[0])
    assert prefix == ["dispatch", "backfill.yml", "--ref", "main"]
    assert values == {
        "filer_ids": "21544 21770",
        "start_year": "2006",
        "end_date": "startup:2026-09-03:1:123456:1:true",
        "date_field": "tran",
        "chain_index": "7",
        "identity_remediation": "true",
        "resume_auto": "true",
        "reset_auto": "true",
        "verification_filer_ids": "21544 21770",
    }
    # The parent dispatches and ends; only the child performs the cooldown.
    assert all(not line.startswith("sleep\t") for line in calls)


def test_second_retry_preserves_base_chain_and_increments_only_retry_index(
    tmp_path,
) -> None:
    env, call_log = _helper_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT),
            "1", "main", "21544", "2006", "2026-09-03",
            "tran", "7", "true", "false", "false", "21544",
            "234567", "2",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    _, values = _parse_dispatch_fields(call_log.read_text().strip())
    assert values["chain_index"] == "7"
    assert values["end_date"] == "startup:2026-09-03:2:234567:2:false"


def test_retry_limit_two_dispatches_nothing(tmp_path) -> None:
    env, call_log = _helper_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT),
            "2", "main", "21544", "2006", "2026-09-03",
            "tran", "7", "true", "true", "false", "21544",
            "345678", "3",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "retry limit reached (2)" in result.stdout.lower()
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("argument_index", "bad_value"),
    [
        (0, "08"),
        (2, "auto"),
        (2, "21544,nope"),
        (2, "21544\t21770"),
        (8, "maybe"),
        (9, "maybe"),
        (10, "99999"),
        (10, "21544\n21770"),
    ],
)
def test_retry_helper_rejects_invalid_or_inconsistent_state(
    tmp_path, argument_index, bad_value,
) -> None:
    env, call_log = _helper_env(tmp_path)
    arguments = [
        "0", "main", "21544", "2006", "2026-09-03", "tran", "7",
        "true", "true", "false", "21544", "123456", "1",
    ]
    arguments[argument_index] = bad_value

    result = subprocess.run(
        ["bash", str(RETRY_SCRIPT), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("listed_runs", "backfill_jobs", "expected"),
    [
        (
            "90\tHistorical Backfill startup retry "
            "startup:2026-09-03:1:123456:1:true",
            "1",
            "false",
        ),
        (
            "90\tHistorical Backfill startup retry "
            "startup:2026-09-03:1:123456:1:true",
            "0",
            "true",
        ),
        (
            "110\tHistorical Backfill startup retry "
            "startup:2026-09-03:1:123456:1:true",
            "1",
            "true",
        ),
        (
            "90\tHistorical Backfill startup retry unrelated-envelope",
            "1",
            "true",
        ),
        ("", "0", "true"),
    ],
)
def test_logical_retry_election_coalesces_only_an_earlier_identical_child(
    tmp_path, listed_runs, backfill_jobs, expected,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'/jobs?'*|*'/jobs?per_page='*) printf '%s\\n' \"$BACKFILL_JOBS\" ;;\n"
        "  *) printf '%s\\n' \"$LISTED_RUNS\" ;;\n"
        "esac\n",
    )
    output_path = tmp_path / "github-output"
    block = _run_block("Coalesce duplicate startup retries")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HANDOFF": "startup:2026-09-03:1:123456:1:true",
            "LISTED_RUNS": listed_runs,
            "BACKFILL_JOBS": backfill_jobs,
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "100",
            "GITHUB_OUTPUT": str(output_path),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert _output_values(output_path)["proceed"] == expected


def test_nonretry_election_does_not_query_actions(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\necho 'unexpected gh call' >&2\nexit 99\n",
    )
    output_path = tmp_path / "github-output"
    result = subprocess.run(
        [
            "bash", "-e", "-o", "pipefail", "-c",
            _run_block("Coalesce duplicate startup retries"),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HANDOFF": "2026-09-03",
            "GITHUB_OUTPUT": str(output_path),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert _output_values(output_path) == {"proceed": "true"}


def test_retry_step_is_cancellation_guarded_and_requires_proved_startup() -> None:
    section = _step_section("Retry transient identity-backfill startup")
    condition = section.split("run: |", 1)[0]

    assert "always()" in condition
    assert "!cancelled()" in condition
    assert "steps.fetch.outcome == 'failure'" in condition
    assert "steps.fetch.outputs.startup_retryable == '1'" in condition


def test_retry_step_passes_resolved_not_auto_state(tmp_path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        scripts / "retry_backfill_startup.sh",
        "#!/usr/bin/env bash\n"
        "{ printf 'retry'; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$CALL_LOG\"\n",
    )
    block = _render(
        _run_block("Retry transient identity-backfill startup"),
        {
            "steps.resolve.outputs.startup_retry": "0",
            "github.ref_name": "main",
            "steps.resolve.outputs.resolved": "21544",
            "inputs.start_year": "2006",
            "steps.fetch.outputs.end_date": "2026-09-03",
            "inputs.date_field": "tran",
            "steps.resolve.outputs.chain": "7",
            "steps.resolve.outputs.is_auto": "true",
            "steps.resolve.outputs.identity_resume": "true",
            "inputs.reset_auto": "false",
            "inputs.resume_progress": "false",
            "steps.resolve.outputs.verification_ids": "21544",
            "github.run_id": "123456",
            "github.run_attempt": "1",
        },
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={**os.environ, "CALL_LOG": str(calls)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert calls.read_text().strip().split("\t") == [
        "retry", "0", "main", "21544", "2006", "2026-09-03",
        "tran", "7", "true", "true", "false", "21544", "123456", "1",
    ]


def test_resolve_unpacks_handoff_without_changing_base_chain(tmp_path) -> None:
    output_path = tmp_path / "github-output"
    block = _render(
        _run_block("Resolve filer IDs (auto mode)"),
        {
            "inputs.filer_ids": "21544",
            "inputs.identity_remediation": "true",
            "inputs.resume_auto": "true",
            "steps.startup_handoff.outputs.end_date || inputs.end_date": "2026-09-03",
            "inputs.verification_filer_ids": "21544",
            "inputs.chain_index || '1'": "7",
            "steps.startup_handoff.outputs.retry_index || '0'": "1",
            "steps.startup_handoff.outputs.parent_run": "123456",
            "steps.startup_handoff.outputs.identity_resume || 'false'": "true",
            "inputs.reset_auto": "false",
            "inputs.resume_progress": "false",
        },
    )

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    values = _output_values(output_path)
    assert values["resolved"] == "21544"
    assert values["identity_end_date"] == "2026-09-03"
    assert values["verification_ids"] == "21544"
    assert values["identity_resume"] == "true"
    assert values["startup_retry"] == "1"
    assert values["startup_parent"] == "123456"


@pytest.mark.parametrize(
    ("auto_status", "expected"),
    [
        ("blocked", "provenance-blocked"),
        ("idle", "without a completion claim"),
    ],
)
def test_empty_auto_retrigger_never_claims_discrepancies_complete(
    tmp_path, auto_status, expected,
) -> None:
    block = _run_block("Re-trigger if backfill incomplete")
    values = {
        "steps.resolve.outputs.is_auto": "true",
        "steps.resolve.outputs.resolved": "",
        "steps.resolve.outputs.identity_mode": "identity",
        "steps.resolve.outputs.auto_status": auto_status,
    }
    block = EXPRESSION.sub(
        lambda match: values.get(match.group(1), ""),
        block,
    )

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert expected in result.stdout
    assert "all discrepancies addressed" not in result.stdout.lower()


def test_resolve_carries_selector_status_to_retrigger() -> None:
    resolve = _run_block("Resolve filer IDs (auto mode)")
    retrigger = _run_block("Re-trigger if backfill incomplete")

    assert "AUTO_STATUS=$(cat /tmp/auto_backfill_status.txt" in resolve
    assert 'echo "auto_status=$AUTO_STATUS" >> "$GITHUB_OUTPUT"' in resolve
    assert 'AUTO_STATUS="${{ steps.resolve.outputs.auto_status }}"' in retrigger


@pytest.mark.parametrize(
    ("parent_conclusion", "latest_attempt", "expected_status"),
    [
        ("failure", "1", 0),
        ("cancelled", "1", 1),
        ("timed_out", "1", 1),
        ("success", "1", 1),
        ("failure", "2", 1),
    ],
)
def test_child_authenticates_exact_failed_parent_attempt(
    tmp_path, parent_conclusion, latest_attempt, expected_status,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "printf 'gh\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
        "case \"$*\" in\n"
        "  *'/attempts/1'*) printf '123456\\t1\\t.github/workflows/backfill.yml\\tworkflow_dispatch\\tcompleted\\t%s\\n' \"$FAKE_PARENT_CONCLUSION\" ;;\n"
        "  *) printf '%s\\tcompleted\\t%s\\n' \"$FAKE_LATEST_ATTEMPT\" \"$FAKE_PARENT_CONCLUSION\" ;;\n"
        "esac\n",
    )
    output_path = tmp_path / "github-output"
    block = _run_block("Validate identity-backfill startup retry handoff")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(calls),
        "FAKE_PARENT_CONCLUSION": parent_conclusion,
        "FAKE_LATEST_ATTEMPT": latest_attempt,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_OUTPUT": str(output_path),
        "HANDOFF": "startup:2026-09-03:1:123456:1:true",
        "IDENTITY_MODE": "true",
        "RESUME_AUTO": "true",
        "FILER_IDS": "21544",
        "VERIFICATION_IDS": "21544",
        "RESET_AUTO": "false",
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    gh_call = calls.read_text().strip()
    assert "actions/runs/123456/attempts/1" in gh_call
    if expected_status == 0:
        assert _output_values(output_path) == {
            "end_date": "2026-09-03",
            "retry_index": "1",
            "parent_run": "123456",
            "parent_attempt": "1",
            "identity_resume": "true",
        }


@pytest.mark.parametrize(
    "handoff",
    [
        "startup:2026-09-03:1:123456:1:true:",
        "startup:2026-09-03:1:123456:1:true::",
        "startup:2026-09-03:1:123456:1",
        "startup:2026-09-03:1:123456:1:maybe",
    ],
)
def test_child_rejects_malformed_handoff_before_api_access(
    tmp_path, handoff,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\nprintf 'called\\n' > \"$CALL_LOG\"\nexit 99\n",
    )
    result = subprocess.run(
        [
            "bash", "-e", "-o", "pipefail", "-c",
            _run_block("Validate identity-backfill startup retry handoff"),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(calls),
            "HANDOFF": handoff,
            "IDENTITY_MODE": "true",
            "RESUME_AUTO": "true",
            "FILER_IDS": "21544",
            "VERIFICATION_IDS": "21544",
            "RESET_AUTO": "false",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert not calls.exists()


def test_child_takes_full_configured_cooldown(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nprintf 'sleep\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n",
    )
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\nprintf '1\\tcompleted\\tfailure\\n'\n",
    )
    block = _render(
        _run_block("Hold identity-backfill startup retry cooldown"),
        {"steps.resolve.outputs.startup_retry": "1"},
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(calls),
            "COOLDOWN_SECONDS": "7",
            "GITHUB_REPOSITORY": "owner/repo",
            "SOURCE_RUN": "123456",
            "SOURCE_ATTEMPT": "1",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert calls.read_text().splitlines() == ["sleep\t7"]


def test_child_rejects_a_parent_rerun_during_cooldown(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\nprintf '2\\tcompleted\\tsuccess\\n'\n",
    )
    block = _render(
        _run_block("Hold identity-backfill startup retry cooldown"),
        {"steps.resolve.outputs.startup_retry": "1"},
    )

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COOLDOWN_SECONDS": "7",
            "GITHUB_REPOSITORY": "owner/repo",
            "SOURCE_RUN": "123456",
            "SOURCE_ATTEMPT": "1",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "became stale during cooldown" in result.stdout


def test_retry_child_has_unique_group_and_cooldown_is_immediately_before_fetch() -> None:
    workflow = WORKFLOW.read_text()
    envelope = "startup:2026-09-03:1:123456:1:true"
    assert "startsWith(inputs.end_date, 'startup:')" in workflow
    assert "format('Historical Backfill startup retry {0}', inputs.end_date)" in workflow
    assert "format('orestar-scrape-retry-{0}', inputs.end_date)" in workflow
    # Both display title and group are keyed by the entire six-part logical
    # envelope, not the physical run ID. Duplicate dispatches therefore share
    # one election identity while unrelated retries remain independent.
    assert len(envelope.split(":")) == 6

    names = re.findall(r"^      - name: (.+)$", workflow, re.MULTILINE)
    validation = names.index("Validate identity-backfill startup retry handoff")
    dependencies = names.index("Install Python dependencies")
    assert validation < dependencies
    cooldown = names.index("Hold identity-backfill startup retry cooldown")
    assert names[cooldown + 1] == "Backfill ORESTAR data"

    cooldown_section = _step_section(
        "Hold identity-backfill startup retry cooldown"
    )
    assert 'COOLDOWN_SECONDS: "1200"' in cooldown_section

    validation = _step_section(
        "Validate identity-backfill startup retry handoff"
    ).split("run: |", 1)[0]
    assert "FILER_IDS: ${{ inputs.filer_ids }}" in validation
    assert "IDENTITY_MODE: ${{ inputs.identity_remediation }}" in validation
    assert "RESUME_AUTO: ${{ inputs.resume_auto }}" in validation
    assert "RESET_AUTO: ${{ inputs.reset_auto }}" in validation
    assert "VERIFICATION_IDS: ${{ inputs.verification_filer_ids }}" in validation

    assert "needs: elect_retry" in workflow
    assert "if: needs.elect_retry.outputs.proceed == 'true'" in workflow


@pytest.mark.parametrize(
    ("display_title", "expected_status"),
    [
        (
            "Historical Backfill startup retry "
            "startup:2026-09-03:1:123456:1:true",
            1,
        ),
        ("Historical Backfill (one-time)", 0),
    ],
)
def test_await_never_fails_open_across_an_older_cooldown_retry(
    tmp_path, display_title, expected_status,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\nprintf '100\\n'\n",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *actions/runs/100*) printf '2026-09-03T00:00:01Z\\n' ;;\n"
        "  *) printf '50\\t.github/workflows/backfill.yml\\t2026-09-03T00:00:00Z\\tHistorical Backfill\\t%s\\n' \"$DISPLAY_TITLE\" ;;\n"
        "esac\n",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _await_run_block()],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DISPLAY_TITLE": display_title,
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "100",
            "MAX_WAIT": "0",
            "POLL": "0",
            "FAIL_ON_TIMEOUT": "false",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    if expected_status:
        assert "refusing to overlap" in result.stdout
    else:
        assert "proceeding anyway" in result.stdout


@pytest.mark.parametrize(
    ("preactive_state", "preactive_updated", "expected_status"),
    [
        ("queued", "2026-09-03T00:00:00Z", 1),
        ("pending", "2026-09-03T00:00:00Z", 1),
        ("waiting", "2026-09-03T00:00:00Z", 1),
        ("queued", "2026-01-01T00:00:00Z", 0),
    ],
)
def test_await_blocks_a_fresh_preactive_retry_but_ignores_a_zombie(
    tmp_path, preactive_state, preactive_updated, expected_status,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'24 hours ago'*) printf '2026-09-02T00:00:00Z\\n' ;;\n"
        "  *) printf '100\\n' ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *actions/runs/100*) printf '2026-09-03T00:00:01Z\\n' ;;\n"
        "  *status=in_progress*) exit 0 ;;\n"
        "  *'created=>='*) printf '50\\t.github/workflows/backfill.yml\\t2026-09-03T00:00:00Z\\tHistorical Backfill\\tHistorical Backfill startup retry startup:2026-09-03:1:123456:1:true\\t%s\\t%s\\n' \"$PREACTIVE_STATE\" \"$PREACTIVE_UPDATED\" ;;\n"
        "esac\n",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _await_run_block()],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PREACTIVE_STATE": preactive_state,
            "PREACTIVE_UPDATED": preactive_updated,
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "100",
            "MAX_WAIT": "0",
            "POLL": "0",
            "FAIL_ON_TIMEOUT": "false",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    if expected_status:
        assert "refusing to overlap" in result.stdout
    else:
        assert "Ignoring stale pre-active ORESTAR run 50" in result.stdout


def test_await_cannot_miss_a_queued_to_active_transition(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    queued_was_queried = tmp_path / "queued-was-queried"
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'24 hours ago'*) printf '2026-09-02T00:00:00Z\\n' ;;\n"
        "  *) printf '100\\n' ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *actions/runs/100*) printf '2026-09-03T00:00:01Z\\n' ;;\n"
        "  *'created=>='*) printf 'yes' > \"$QUEUED_WAS_QUERIED\" ;;\n"
        "  *status=in_progress*)\n"
        "    if [ -f \"$QUEUED_WAS_QUERIED\" ]; then\n"
        "      printf '50\\t.github/workflows/daily-refresh.yml\\t2026-09-03T00:00:00Z\\tDaily\\tDaily Data Refresh\\tin_progress\\t2026-09-03T00:00:00Z\\n'\n"
        "    fi ;;\n"
        "esac\n",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _await_run_block()],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "QUEUED_WAS_QUERIED": str(queued_was_queried),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "100",
            "MAX_WAIT": "0",
            "POLL": "0",
            "FAIL_ON_TIMEOUT": "true",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "refusing to overlap" in result.stdout


def test_await_clears_cooldown_strictness_after_a_successful_poll(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    date_calls = tmp_path / "date-calls"
    api_calls = tmp_path / "api-calls"
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'24 hours ago'*) printf '2026-09-02T00:00:00Z\\n' ;;\n"
        "  *)\n"
        "    N=$(cat \"$DATE_CALLS\" 2>/dev/null || printf '0')\n"
        "    N=$((N + 1)); printf '%s' \"$N\" > \"$DATE_CALLS\"\n"
        "    if [ \"$N\" -le 2 ]; then printf '100\\n'; else printf '200\\n'; fi ;;\n"
        "esac\n",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *actions/runs/100*) printf '2026-09-03T00:00:02Z\\n' ;;\n"
        "  *'created=>='*) exit 0 ;;\n"
        "  *status=in_progress*)\n"
        "    N=$(cat \"$API_CALLS\" 2>/dev/null || printf '0')\n"
        "    N=$((N + 1)); printf '%s' \"$N\" > \"$API_CALLS\"\n"
        "    if [ \"$N\" -eq 1 ]; then\n"
        "      printf '50\\t.github/workflows/backfill.yml\\t2026-09-03T00:00:00Z\\tRetry\\tHistorical Backfill startup retry startup:2026-09-03:1:123456:1:true\\tin_progress\\t2026-09-03T00:00:00Z\\n'\n"
        "    fi\n"
        "    printf '60\\t.github/workflows/daily-refresh.yml\\t2026-09-03T00:00:01Z\\tDaily\\tDaily Data Refresh\\tin_progress\\t2026-09-03T00:00:01Z\\n' ;;\n"
        "esac\n",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _await_run_block()],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DATE_CALLS": str(date_calls),
            "API_CALLS": str(api_calls),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "100",
            "MAX_WAIT": "1",
            "POLL": "0",
            "FAIL_ON_TIMEOUT": "false",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "proceeding anyway" in result.stdout


def test_await_scope_and_tracking_require_this_fetch_success() -> None:
    await_section = _step_section("Wait for other ORESTAR jobs")
    assert 'fail-on-timeout: "true"' in await_section

    track = _step_section("Track successfully backfilled filers (auto mode)")
    condition = track.split("run: |", 1)[0]
    assert "steps.fetch.outcome == 'success'" in condition

    fetch = _run_block("Backfill ORESTAR data")
    assert fetch.index("rm -f data/completed_backfills.txt") < fetch.index(
        "xvfb-run --auto-servernum python scraper/fetch.py"
    )

    candidate = (ROOT / ".github" / "workflows" / "candidate-filings.yml").read_text()
    candidate_wait = candidate[candidate.index("uses: ./.github/actions/await-orestar"):]
    candidate_wait = candidate_wait.split("\n      - name:", 1)[0]
    assert 'fail-on-timeout: "true"' in candidate_wait
    assert "Requeue after coordination timeout" in candidate
    assert candidate.index("Requeue after coordination timeout") < candidate.index(
        "Scrape candidate filings"
    )

    await_action = AWAIT_ACTION.read_text()
    verify = VERIFY_WORKFLOW.read_text()
    assert ".github/workflows/verify-filers.yml" in await_action
    assert "uses: ./.github/actions/await-orestar" in verify
    assert verify.index("uses: ./.github/actions/await-orestar") < verify.index(
        "xvfb-run --auto-servernum python scraper/verify_filer.py"
    )
    assert verify.index("git reset --hard FETCH_HEAD") < verify.index(
        "xvfb-run --auto-servernum python scraper/verify_filer.py"
    )


@pytest.mark.parametrize(
    "exc",
    [
        F.PlaywrightTimeout("Page.goto: Timeout 60000ms exceeded"),
        F.SessionExpiredError("redirected during startup"),
    ],
)
def test_identity_initial_transient_failure_emits_exact_marker(
    monkeypatch, capsys, exc,
) -> None:
    def fail(_playwright):
        raise exc

    monkeypatch.setattr(F, "setup_browser_retrying", fail)

    with pytest.raises(type(exc)):
        F._setup_initial_filer_browser(object(), identity_remediation=True)

    assert capsys.readouterr().out.splitlines() == [STARTUP_MARKER]


def test_identity_marker_requires_exhausting_all_three_setup_attempts(
    monkeypatch, capsys,
) -> None:
    attempts = 0

    def fail(_playwright):
        nonlocal attempts
        attempts += 1
        raise F.PlaywrightTimeout("Page.goto: Timeout 60000ms exceeded")

    monkeypatch.setattr(F, "setup_browser", fail)
    monkeypatch.setattr(F.time, "sleep", lambda _seconds: None)

    with pytest.raises(F.PlaywrightTimeout):
        F._setup_initial_filer_browser(object(), identity_remediation=True)

    assert attempts == 3
    assert capsys.readouterr().out.splitlines() == [STARTUP_MARKER]


@pytest.mark.parametrize(
    ("identity_remediation", "exc"),
    [
        (False, F.PlaywrightTimeout("Page.goto: Timeout 60000ms exceeded")),
        (True, RuntimeError("chromium executable is missing")),
        (True, RuntimeError("ORESTAR search form did not load after navigation")),
    ],
)
def test_nonidentity_or_structural_initial_failure_emits_no_marker(
    monkeypatch, capsys, identity_remediation, exc,
) -> None:
    def fail(_playwright):
        raise exc

    monkeypatch.setattr(F, "setup_browser_retrying", fail)

    with pytest.raises(type(exc)):
        F._setup_initial_filer_browser(
            object(), identity_remediation=identity_remediation,
        )

    assert capsys.readouterr().out == ""
