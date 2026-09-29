"""Executable contracts for fail-fast account-summary F5 recovery."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "earliest-balances.yml"
AWAIT_ACTION = ROOT / ".github" / "actions" / "await-orestar" / "action.yml"
RETRY_SCRIPT = ROOT / "scripts" / "retry_account_summary.sh"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

SCRAPER_DIR = ROOT / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import fetch_earliest_balances as FEB  # noqa: E402


def _run_block(step_name: str) -> str:
    lines = WORKFLOW.read_text().splitlines()
    marker = f"      - name: {step_name}"
    start = lines.index(marker)
    run_line = next(i for i in range(start + 1, len(lines))
                    if lines[i].strip() == "run: |")
    end = next(
        (i for i in range(run_line + 1, len(lines))
         if lines[i].startswith("      - name: ")
         or re.match(r"^  [A-Za-z_][A-Za-z0-9_-]*:$", lines[i])),
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


class _ChallengePage:
    def goto(self, *_args, **_kwargs):
        return None

    def content(self):
        return "<html><title>Checking your browser</title></html>"


class _MalformedPage(_ChallengePage):
    def content(self):
        return "<html><title>Service unavailable</title></html>"


class _ChallengeThenMalformedPage(_ChallengePage):
    def __init__(self):
        self.reads = 0

    def content(self):
        self.reads += 1
        if self.reads == 1:
            return "<html><script src='/TSPD/abc'></script></html>"
        return "<html><title>Service unavailable</title></html>"


def test_exhausted_initial_challenge_has_a_narrow_exception(monkeypatch) -> None:
    monkeypatch.setattr(FEB, "PAGE_LOAD_ATTEMPTS", 2)
    monkeypatch.setattr(FEB, "CHALLENGE_WAIT", 0.001)
    monkeypatch.setattr(FEB.time, "sleep", lambda _seconds: None)

    with pytest.raises(FEB.BotChallengeExhausted, match="21770"):
        FEB._load_summary_page(_ChallengePage(), "https://example.test", "21770")


def test_non_f5_page_does_not_authorize_a_retry(monkeypatch) -> None:
    monkeypatch.setattr(FEB, "PAGE_LOAD_ATTEMPTS", 1)
    monkeypatch.setattr(FEB, "CHALLENGE_WAIT", 0.001)
    monkeypatch.setattr(FEB.time, "sleep", lambda _seconds: None)

    assert FEB._load_summary_page(
        _MalformedPage(), "https://example.test", "21770"
    ) is None


def test_resolved_challenge_followed_by_bad_page_is_not_f5_exhaustion(
    monkeypatch,
) -> None:
    monkeypatch.setattr(FEB, "PAGE_LOAD_ATTEMPTS", 1)
    monkeypatch.setattr(FEB, "CHALLENGE_WAIT", 0.001)
    monkeypatch.setattr(FEB.time, "sleep", lambda _seconds: None)

    assert FEB._load_summary_page(
        _ChallengeThenMalformedPage(), "https://example.test", "21770"
    ) is None


def test_challenge_circuit_requires_consecutive_failures() -> None:
    streak = 0
    for challenged in (True, True, False, True, True, True):
        streak = FEB._next_challenge_streak(streak, challenged)
        if challenged is False:
            assert streak == 0
    assert streak == FEB.CONSECUTIVE_CHALLENGE_ABORT == 3


def test_generic_failure_breaker_cannot_authorize_a_cooled_retry() -> None:
    assert FEB._batch_block_state(4, 2, False) == (True, False)
    assert FEB._batch_block_state(3, 3, True) == (True, True)


@pytest.mark.parametrize(
    ("collector_output", "collector_status", "expected"),
    [
        (
            "ACCOUNT_SUMMARY_RESULT attempted=2 completed=0 failed=2 "
            "remaining=5176 blocked=1 f5_retryable=1",
            1,
            {"attempted": "2", "completed": "0", "failed": "2",
             "blocked": "1", "f5_retryable": "1"},
        ),
        (
            "23 of 23 filers failed (100%). ORESTAR is refusing this scraper",
            1,
            {"attempted": "0", "completed": "0", "failed": "0",
             "blocked": "0", "f5_retryable": "0"},
        ),
        (
            "ACCOUNT_SUMMARY_RESULT attempted=200 completed=200 failed=0 "
            "remaining=4976 blocked=0 f5_retryable=0",
            0,
            {"attempted": "200", "completed": "200", "failed": "0",
             "blocked": "0", "f5_retryable": "0"},
        ),
    ],
)
def test_scrape_step_exports_only_the_machine_f5_marker(
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
    env_path = tmp_path / "github-env"
    log_path = tmp_path / "account-summary.log"
    log_path.write_text(
        "ACCOUNT_SUMMARY_RESULT attempted=99 completed=0 failed=99 "
        "remaining=99 blocked=1 f5_retryable=1\n"
    )
    block = _render(
        _run_block("Scrape account summaries"),
        {
            "env.max_filers": "200",
            "env.force": "false",
            "env.current_only": "true",
            "env.max_age_days": "1",
            "env.refresh_before_ts": "1788929442",
            "env.filer_ids": "",
        },
    ).replace("/tmp/account-summary.log", str(log_path))
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_ENV": str(env_path),
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
    assert {key: values[key] for key in expected} == expected


def _retry_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    call_log = tmp_path / "calls"
    dispatch = tmp_path / "dispatch.sh"
    _write_executable(
        dispatch,
        "#!/usr/bin/env bash\n"
        "{ printf 'dispatch'; printf '\\t%s' \"$@\"; printf '\\n'; } >> \"$CALL_LOG\"\n",
    )
    return {
        **os.environ,
        "CALL_LOG": str(call_log),
        "ACCOUNT_SUMMARY_RETRY_DISPATCH_SCRIPT": str(dispatch),
    }, call_log


def test_retry_script_preserves_frozen_sweep_and_batch(tmp_path) -> None:
    env, call_log = _retry_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT), "0", "main", "200", "1", "true",
            "1788929442", "13", "34443345488", "1",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert call_log.read_text().rstrip().split("\t") == [
        "dispatch", "earliest-balances.yml", "--ref", "main",
        "-f", "max_filers=200", "-f", "filer_ids=",
        "-f", "max_age_days=1", "-f", "force=false",
        "-f", "current_only=true", "-f", "refresh_before_ts=1788929442",
        "-f", "chain_index=13", "-f",
        "retry_handoff=blocked:1:34443345488:1:13:1788929442:200:1:true",
    ]


def test_retry_script_stops_at_bound_without_dispatch(tmp_path) -> None:
    env, call_log = _retry_env(tmp_path)

    result = subprocess.run(
        [
            "bash", str(RETRY_SCRIPT), "2", "main", "200", "1", "true",
            "1788929442", "13", "34443345488", "1",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "retry limit reached (2)" in result.stdout
    assert not call_log.exists()


def test_retry_handoff_authenticates_exact_failed_attempt(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *attempts/1*) printf '34443345488\\t1\\t.github/workflows/earliest-balances.yml\\tworkflow_dispatch\\tmain\\tcompleted\\tfailure\\n' ;;\n"
        "  *) printf '1\\tcompleted\\tfailure\\n' ;;\n"
        "esac\n",
    )
    output_path = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF_NAME": "main",
        "GITHUB_OUTPUT": str(output_path),
        "HANDOFF": "blocked:1:34443345488:1:13:1788929442:200:1:true",
        "FILER_IDS": "",
        "FORCE": "false",
        "MAX_FILERS": "200",
        "MAX_AGE_DAYS": "1",
        "CURRENT_ONLY": "true",
        "REFRESH_BEFORE": "1788929442",
        "CHAIN_INDEX": "13",
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         _run_block("Validate F5 retry handoff")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert _output_values(output_path) == {
        "retry_index": "1",
        "parent_run": "34443345488",
        "parent_attempt": "1",
    }


@pytest.mark.parametrize(
    ("parent_conclusion", "latest_attempt", "source_branch", "expected_status"),
    [
        ("failure", "1", "main", 0),
        ("cancelled", "1", "main", 1),
        ("timed_out", "1", "main", 1),
        ("success", "1", "main", 1),
        ("failure", "2", "main", 1),
        ("failure", "1", "other-branch", 1),
    ],
)
def test_retry_handoff_rejects_stale_or_nonfailed_parent(
    tmp_path, parent_conclusion, latest_attempt, source_branch, expected_status
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *attempts/1*) printf '34443345488\\t1\\t.github/workflows/earliest-balances.yml\\tworkflow_dispatch\\t%s\\tcompleted\\t%s\\n' \"$FAKE_SOURCE_BRANCH\" \"$FAKE_PARENT_CONCLUSION\" ;;\n"
        "  *) printf '%s\\tcompleted\\t%s\\n' \"$FAKE_LATEST_ATTEMPT\" \"$FAKE_PARENT_CONCLUSION\" ;;\n"
        "esac\n",
    )
    output_path = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_PARENT_CONCLUSION": parent_conclusion,
        "FAKE_LATEST_ATTEMPT": latest_attempt,
        "FAKE_SOURCE_BRANCH": source_branch,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_REF_NAME": "main",
        "GITHUB_OUTPUT": str(output_path),
        "HANDOFF": "blocked:1:34443345488:1:13:1788929442:200:1:true",
        "FILER_IDS": "",
        "FORCE": "false",
        "MAX_FILERS": "200",
        "MAX_AGE_DAYS": "1",
        "CURRENT_ONLY": "true",
        "REFRESH_BEFORE": "1788929442",
        "CHAIN_INDEX": "13",
    }

    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         _run_block("Validate F5 retry handoff")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status


def test_retry_handoff_rejects_mutated_selector_before_api_access(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\nprintf 'called\\n' > \"$CALL_LOG\"\nexit 99\n",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         _run_block("Validate F5 retry handoff")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(calls),
            "HANDOFF": "blocked:1:34443345488:1:13:1788929442:200:1:true",
            "FILER_IDS": "",
            "FORCE": "false",
            "MAX_FILERS": "201",
            "MAX_AGE_DAYS": "1",
            "CURRENT_ONLY": "true",
            "REFRESH_BEFORE": "1788929442",
            "CHAIN_INDEX": "13",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert not calls.exists()


@pytest.mark.parametrize(
    ("latest_attempt", "latest_conclusion", "expected_status"),
    [("1", "failure", 0), ("2", "success", 1)],
)
def test_retry_cooldown_rechecks_parent_immediately_before_scrape(
    tmp_path, latest_attempt, latest_conclusion, expected_status
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nprintf 'sleep\\t%s\\n' \"$*\" >> \"$CALL_LOG\"\n",
    )
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\nprintf '%s\\tcompleted\\t%s\\n' \"$FAKE_LATEST_ATTEMPT\" \"$FAKE_LATEST_CONCLUSION\"\n",
    )
    block = _render(
        _run_block("Hold account-summary F5 cooldown"),
        {"steps.retry_handoff.outputs.retry_index": "1"},
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", block],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CALL_LOG": str(calls),
            "COOLDOWN_SECONDS": "7",
            "FAKE_LATEST_ATTEMPT": latest_attempt,
            "FAKE_LATEST_CONCLUSION": latest_conclusion,
            "GITHUB_REPOSITORY": "owner/repo",
            "SOURCE_RUN": "34443345488",
            "SOURCE_ATTEMPT": "1",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status
    assert calls.read_text().splitlines() == ["sleep\t7"]


@pytest.mark.parametrize(
    ("scrape_state", "earlier_conclusion", "expected_proceed"),
    [
        ("0\t0", "cancelled", "true"),
        ("0\t1", "cancelled", "false"),
        ("1\t1", "failure", "false"),
    ],
)
def test_retry_election_distinguishes_evicted_pending_from_entered_run(
    tmp_path, scrape_state, earlier_conclusion, expected_proceed
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "gh",
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *workflows/earliest-balances.yml/runs*) printf '100\\tAccount Summary F5 retry %s\\n' \"$HANDOFF\" ;;\n"
        "  *'/jobs?per_page=100'*) printf '%s\\n' \"$FAKE_SCRAPE_STATE\" ;;\n"
        "  *) printf '%s\\n' \"$FAKE_EARLIER_CONCLUSION\" ;;\n"
        "esac\n",
    )
    output_path = tmp_path / "github-output"
    handoff = "blocked:1:34443345488:1:13:1788929442:200:1:true"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         _run_block("Coalesce duplicate F5 retries")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "101",
            "GITHUB_OUTPUT": str(output_path),
            "HANDOFF": handoff,
            "FAKE_SCRAPE_STATE": scrape_state,
            "FAKE_EARLIER_CONCLUSION": earlier_conclusion,
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert _output_values(output_path)["proceed"] == expected_proceed


def test_retry_wiring_is_bounded_published_and_quiet() -> None:
    workflow = WORKFLOW.read_text()
    await_action = AWAIT_ACTION.read_text()
    retry_marker = "      - name: Queue cooled retry after F5 refusal"
    retry_section = workflow[workflow.index(retry_marker):]
    retry_condition = retry_section.split("run: |", 1)[0]

    assert "steps.scrape.outcome == 'failure'" in retry_condition
    assert "steps.scrape.outputs.f5_retryable == '1'" in retry_condition
    assert "steps.remaining.outcome == 'success'" in retry_condition
    assert "steps.remaining.outputs.remaining != '0'" in retry_condition
    assert "steps.summary_publish.outcome == 'success'" in retry_condition
    assert "always() && !cancelled()" in retry_condition
    assert "env.targeted != 'true'" in retry_condition
    assert "if: success()" in workflow[workflow.index(
        "      - name: Retrigger for next batch"
    ):].split("run: |", 1)[0]
    assert "Account Summary F5 retry " in await_action
    names = re.findall(r"^      - name: (.+)$", workflow, re.MULTILINE)
    cooldown = names.index("Hold account-summary F5 cooldown")
    assert names[cooldown + 1] == "Scrape account summaries"
    assert 'COOLDOWN_SECONDS: "1200"' in workflow
    assert "account-summary-retry-{0}" in workflow
    resolve_header = workflow[workflow.index(
        "      - name: Resolve sweep parameters"
    ):].split("run: |", 1)[0]
    assert "RAW_RETRY_HANDOFF: ${{ inputs.retry_handoff }}" in resolve_header
    resolve = _run_block("Resolve sweep parameters")
    assert 'RETRY_HANDOFF="$RAW_RETRY_HANDOFF"' in resolve
    assert 'RETRY_HANDOFF="${{ github.event.inputs.retry_handoff }}"' not in resolve
    publish_header = workflow[workflow.index(
        "      - name: Publish account-summary state"
    ):].split("run: ", 1)[0]
    assert "steps.summary_state.outcome == 'success'" in publish_header
    assert "steps.scrape.outcome == 'success'" in publish_header
    assert "steps.scrape.outcome == 'failure'" in publish_header

    coordination = workflow[workflow.index(
        "      - name: Requeue after coordination timeout"
    ):workflow.index("      - name: Refuse uncoordinated account-summary scrape")]
    assert '-f retry_handoff="${{ env.retry_handoff }}"' in coordination

    normal = workflow[workflow.index(
        "      - name: Retrigger for next batch"
    ):]
    assert "retry_handoff" not in normal
