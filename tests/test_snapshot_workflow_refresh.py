"""Regression tests for pairing ORESTAR work with a post-wait checkout."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parent.parent
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "daily-refresh.yml",
    ROOT / ".github" / "workflows" / "earliest-balances.yml",
)


def _step_section(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.stem)
def test_workflow_refreshes_checkout_immediately_after_coordination_wait(
    workflow_path: Path,
) -> None:
    workflow = workflow_path.read_text()
    wait_marker = "      - name: Wait for other ORESTAR jobs\n"
    refresh_marker = "      - name: Refresh branch after coordination wait\n"
    setup_marker = "      - name: Set up Python 3.11\n"

    assert workflow.count(refresh_marker) == 1
    assert workflow.index(wait_marker) < workflow.index(refresh_marker)
    assert workflow.index(refresh_marker) < workflow.index(setup_marker)

    # Coordination failures may requeue and terminate between the wait and
    # refresh. A cooled retry also authenticates its failed parent here. Every
    # branch before refresh is failure-only or retry-only; no stale checkout
    # can reach state mutation.
    between = workflow[
        workflow.index(wait_marker) + len(wait_marker) : workflow.index(refresh_marker)
    ]
    intervening = re.findall(r"^      - name: (.+)$", between, re.MULTILINE)
    expected_count = 3 if workflow_path.name == "earliest-balances.yml" else 2
    assert len(intervening) == expected_count
    assert intervening[0] == "Requeue after coordination timeout"
    assert intervening[1].startswith("Refuse uncoordinated")
    for name in intervening[:2]:
        assert "steps.orestar_wait.outcome == 'failure'" in _step_section(
            workflow, name
        )
    if expected_count == 3:
        assert intervening[2] == "Validate F5 retry handoff"
        assert "startsWith(inputs.retry_handoff, 'blocked:')" in _step_section(
            workflow, intervening[2]
        )
    assert '--ref "${{ github.ref_name }}"' in _step_section(
        workflow, "Requeue after coordination timeout"
    )

    refresh = _step_section(workflow, "Refresh branch after coordination wait")
    assert 'git fetch --depth=1 origin "$GITHUB_REF_NAME"' in refresh
    assert "git reset --hard FETCH_HEAD" in refresh
    assert refresh.index("git fetch --depth=1") < refresh.index(
        "git reset --hard FETCH_HEAD"
    )

    wait = _step_section(workflow, "Wait for other ORESTAR jobs")
    assert 'fail-on-timeout: "true"' in wait


def test_account_summary_job_budget_contains_wait_install_and_soft_stop() -> None:
    workflow = (ROOT / ".github/workflows/earliest-balances.yml").read_text()
    action = (ROOT / ".github/actions/await-orestar/action.yml").read_text()

    job_minutes = int(re.search(r"^    timeout-minutes: (\d+)$", workflow, re.M).group(1))
    scrape_minutes = int(re.search(r"--max-minutes (\d+)", workflow).group(1))
    install = _step_section(workflow, "Install Playwright browser + system dependencies")
    install_minutes = int(re.search(r"timeout-minutes: (\d+)", install).group(1))
    wait_minutes = int(re.search(
        r"max-wait-minutes:.*?default: \"(\d+)\"", action, re.S
    ).group(1))

    assert job_minutes >= wait_minutes + install_minutes + scrape_minutes + 20


def test_account_summary_breaker_cannot_finalize_the_sweep() -> None:
    workflow = (ROOT / ".github/workflows/earliest-balances.yml").read_text()
    scraper = (ROOT / "scraper/fetch_earliest_balances.py").read_text()

    scrape = _step_section(workflow, "Scrape account summaries")
    aggregate = _step_section(
        workflow, "Re-aggregate with updated beginning balances (final batch only)"
    )
    retrigger = _step_section(workflow, "Retrigger for next batch")
    breaker = scraper[scraper.index("if batch_blocked:"):]

    # The scraper's failure outcome stops chaining. The remaining-count file
    # keeps its literal progress value and cannot double as a stop sentinel.
    assert "id: scrape" in scrape
    assert "if: success()" in retrigger
    assert 'remaining_path.write_text("0")' not in breaker
    assert 'remaining_path.write_text(str(still_remaining))' in scraper
    assert "sys.exit(1)" in breaker
    assert breaker.index("sys.exit(1)") < breaker.index(
        "sweep_state.pop(sweep_mode, None)"
    )

    # A zero count alone is insufficient: finalization also requires the
    # account-summary scrape itself to have completed successfully.
    assert "steps.scrape.outcome == 'success'" in aggregate
    assert "steps.remaining.outputs.remaining == '0'" in aggregate


def test_admin_balance_payload_fails_closed_across_schema_rollout() -> None:
    script = (ROOT / "docs/admin/donors.js").read_text()
    html = (ROOT / "docs/admin/donors.html").read_text()

    assert 'blob.schema_version === BD_SCHEMA_VERSION' in script
    assert 'blob.basis === BD_BASIS' in script
    assert 'r.comparison_status === "paired"' in script
    assert 'blob.flagged != null ? blob.flagged : actionable.length' in script
    assert "blob.flagged ||" not in script
    assert "blob.unpaired_rows" in script
    assert 'value="unpaired"' in html
    # Refusals have no judged delta and therefore bypass the amount threshold.
    assert "if (!unpaired && Math.abs(judged) < min)" in script
