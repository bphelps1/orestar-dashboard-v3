"""Executable contracts for exact-gate retry wiring in GitHub Actions."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "coverage-diff.yml"
RETRY_SCRIPT = ROOT / "scripts" / "retry_identity_gate.sh"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _run_block(step_name: str) -> str:
    """Extract one literal ``run: |`` block without adding a YAML dependency."""
    lines = WORKFLOW.read_text().splitlines()
    marker = f"      - name: {step_name}"
    start = lines.index(marker)
    run_line = next(i for i in range(start + 1, len(lines))
                    if lines[i].strip() == "run: |")
    end = next(
        (i for i in range(run_line + 1, len(lines))
         if lines[i].startswith("      - name: ")),
        len(lines),
    )
    return textwrap.dedent("\n".join(lines[run_line + 1:end]))


def _render(block: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1)
        assert expression in values, f"unrendered workflow expression: {expression}"
        return values[expression]

    rendered = EXPRESSION.sub(replace, block)
    assert "${{" not in rendered
    return rendered


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _output_values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.mark.parametrize(
    ("collector_output", "collector_status", "expected"),
    [
        (
            "Browser setup failed (attempt 3/3): Page.goto timed out",
            23,
            {
                "attempted": "0", "usable": "0", "retryable": "1",
                "retry_ids": "21770",
            },
        ),
        (
            "Browser setup failed (attempt 1/3): recovered before later crash",
            23,
            {
                "attempted": "0", "usable": "0", "retryable": "0",
                "retry_ids": "",
            },
        ),
        (
            "RUN_RESULT attempted=1 usable=1 unusable=0 blocked=0 retryable=0 retry_ids=",
            1,
            {
                "attempted": "1", "usable": "1", "retryable": "0",
                "retry_ids": "",
            },
        ),
        (
            "RUN_RESULT attempted=1 usable=0 unusable=1 blocked=0 retryable=1 retry_ids=21770",
            1,
            {
                "attempted": "1", "usable": "0", "retryable": "1",
                "retry_ids": "21770",
            },
        ),
        (
            "RUN_RESULT attempted=1 usable=0 unusable=1 blocked=0 retryable=0 retry_ids=",
            1,
            {
                "attempted": "1", "usable": "0", "retryable": "0",
                "retry_ids": "",
            },
        ),
    ],
)
def test_diff_step_publishes_evidence_before_returning_failure(
    tmp_path, collector_output, collector_status, expected
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "xvfb-run",
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$FAKE_COLLECTOR_OUTPUT\"\n"
        "exit \"$FAKE_COLLECTOR_STATUS\"\n",
    )
    output_path = tmp_path / "github-output"
    log_path = tmp_path / "diff.log"
    block = _render(
        _run_block("Diff coverage"),
        {
            "inputs.filer_ids": "21770",
            "inputs.limit": "0",
            "inputs.recheck": "true",
            "inputs.start_year": "2006",
            "inputs.end_date": "2026-09-05",
            "inputs.require_no_missing": "true",
        },
    ).replace("/tmp/diff.log", str(log_path))
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output_path),
        "FAKE_COLLECTOR_OUTPUT": collector_output,
        "FAKE_COLLECTOR_STATUS": str(collector_status),
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == collector_status
    values = _output_values(output_path)
    assert values["end_date"] == "2026-09-05"
    assert {key: values[key] for key in expected} == expected


def _retry_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    call_log = tmp_path / "calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nprintf 'sleep\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n",
    )
    dispatch = tmp_path / "dispatch.sh"
    _write_executable(
        dispatch,
        "#!/usr/bin/env bash\n"
        "{ printf 'dispatch'; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$CALL_LOG\"\n",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "IDENTITY_GATE_DISPATCH_SCRIPT": str(dispatch),
    }
    return env, call_log


def test_retry_script_holds_lane_and_preserves_exact_gate_inputs(tmp_path) -> None:
    env, call_log = _retry_env(tmp_path)
    before = int(subprocess.check_output(["date", "+%s"], text=True))

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT), "0", "main", "21770 22", "2006",
            "2026-09-05", "true", "123456",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    lines = call_log.read_text().splitlines()
    dispatch = lines[0].split("\t")
    assert dispatch[:-2] == [
        "dispatch",
        "coverage-diff.yml",
        "--ref",
        "main",
        "-f",
        "filer_ids=21770 22",
        "-f",
        "start_year=2006",
        "-f",
        "end_date=2026-09-05",
        "-f",
        "recheck=true",
        "-f",
        "require_no_missing=true",
        "-f",
        "resume_auto_backfill=true",
        "-f",
        "verification_retry=1",
        "-f",
        "verification_parent_run_id=123456",
    ]
    assert dispatch[-2] == "-f"
    assert dispatch[-1].startswith("verification_not_before=")
    not_before = int(dispatch[-1].split("=", 1)[1])
    assert before + 1200 <= not_before <= before + 1205
    assert lines[1] == "sleep\t1200"


def test_retry_script_stops_at_the_bound_without_sleep_or_dispatch(tmp_path) -> None:
    env, call_log = _retry_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT), "2", "main", "21770", "2006",
            "2026-09-05", "true", "123456",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "retry limit reached (2)" in result.stdout
    assert not call_log.exists()


@pytest.mark.parametrize(
    ("require_gate", "retryable", "commit_outcome", "expected_calls"),
    [
        ("true", "1", "success", 1),
        ("true", "0", "success", 0),
        ("false", "1", "success", 0),
        ("true", "1", "failure", 0),
    ],
)
def test_failed_diff_retries_only_a_saved_retryable_exact_gate(
    tmp_path, require_gate, retryable, commit_outcome, expected_calls
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    call_log = tmp_path / "calls"
    _write_executable(
        scripts / "retry_identity_gate.sh",
        "#!/usr/bin/env bash\n"
        "{ printf 'retry'; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$CALL_LOG\"\n",
    )
    block = _render(
        _run_block("Re-trigger if committees remain"),
        {
            "inputs.chain_index || '1'": "1",
            "steps.diff.outcome": "failure",
            "inputs.require_no_missing": require_gate,
            "inputs.filer_ids": "21770",
            "steps.diff.outputs.retryable": retryable,
            "steps.diff.outputs.retry_ids": "21770",
            "steps.commit.outcome": commit_outcome,
            "inputs.verification_retry || '0'": "0",
            "github.ref_name": "main",
            "inputs.start_year": "2006",
            "steps.diff.outputs.end_date": "2026-09-05",
            "inputs.resume_auto_backfill": "true",
            "github.run_id": "123456",
            "steps.cleanup.outcome": "skipped",
            "inputs.recheck": "true",
            "inputs.limit": "0",
            "steps.diff.outputs.usable": "0",
        },
    )
    env = {**os.environ, "CALL_LOG": str(call_log)}

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    assert len(calls) == expected_calls
    if calls:
        assert calls[0].split("\t") == [
            "retry", "0", "main", "21770", "2006", "2026-09-05",
            "true", "123456"
        ]


@pytest.mark.parametrize(
    ("parent_conclusion", "expected_status", "expected_sleep"),
    [
        ("failure", 0, "sleep\t100"),
        ("cancelled", 1, None),
        ("timed_out", 1, None),
    ],
)
def test_retry_handoff_honors_cooldown_but_stops_after_cancellation(
    tmp_path, parent_conclusion, expected_status, expected_sleep
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "printf 'completed\\t%s\\n' \"$FAKE_PARENT_CONCLUSION\"\n",
    )
    _write_executable(
        fake_bin / "date",
        "#!/usr/bin/env bash\nprintf '900\\n'\n",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nprintf 'sleep\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n",
    )
    block = _run_block("Honor exact-gate retry handoff")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "FAKE_PARENT_CONCLUSION": parent_conclusion,
        "GITHUB_REPOSITORY": "owner/repo",
        "SOURCE_RUN": "123456",
        "NOT_BEFORE": "1000",
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    assert calls == ([expected_sleep] if expected_sleep else [])


def test_retrigger_step_remains_cancellation_guarded() -> None:
    workflow = WORKFLOW.read_text()
    marker = "      - name: Re-trigger if committees remain"
    section = workflow[workflow.index(marker):]
    assert "if: always() && !cancelled()" in section.split("run: |", 1)[0]
