import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def _step_block(text: str, name: str) -> str:
    marker = f"      - name: {name}"
    start = text.index(marker)
    end = text.find("\n      - name:", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def _concurrency_block(text: str) -> str:
    start = text.index("\nconcurrency:")
    end = text.index("\njobs:", start)
    return text[start:end]


@pytest.mark.parametrize(
    ("filename", "scrape_step", "required_dispatch_args"),
    [
        (
            "coverage-survey.yml",
            "Survey coverage",
            ("limit", "filer_ids", "recheck", "chain_index"),
        ),
        (
            "filer-metadata.yml",
            "Scrape filer metadata (party, office, type)",
            ("max_filers", "force", "filer_ids"),
        ),
        ("candidate-filings.yml", "Scrape candidate filings", ()),
        ("amendment-chains.yml", "Collect amendment chains", ("targets",)),
        ("misc-reread.yml", "Re-read lumped Miscellaneous rows", ("start_year", "chain_index")),
        (
            "backfill.yml",
            "Backfill ORESTAR data",
            ("filer_ids", "start_year", "date_field", "identity_remediation",
             "resume_auto", "resume_progress", "verification_filer_ids", "chain_index"),
        ),
        (
            "committee-contacts.yml",
            "Scrape committee contacts",
            ("max_committees", "max_age_days"),
        ),
        (
            "verify-filers.yml",
            "Verify filer transactions",
            ("filer_id", "discrepancy_threshold", "max_filers", "force"),
        ),
    ],
)
def test_orestar_workflow_requeues_same_request_instead_of_overlapping(
    filename: str,
    scrape_step: str,
    required_dispatch_args: tuple[str, ...],
) -> None:
    text = _workflow(filename)
    wait = _step_block(text, "Wait for other ORESTAR jobs")
    requeue = _step_block(text, "Requeue after coordination timeout")
    refuse = _step_block(
        text,
        next(
            line.removeprefix("      - name: ")
            for line in text.splitlines()
            if line.startswith("      - name: Refuse uncoordinated")
        ),
    )

    assert "id: orestar_wait" in wait
    assert "continue-on-error: true" in wait
    assert 'fail-on-timeout: "true"' in wait
    assert "steps.orestar_wait.outcome == 'failure'" in requeue
    assert f"dispatch_retry.sh {filename}" in requeue
    assert "--ref" in requeue
    for arg in required_dispatch_args:
        assert f'-f {arg}="' in requeue

    assert "steps.orestar_wait.outcome == 'failure'" in refuse
    assert "exit 1" in refuse
    assert text.index("Wait for other ORESTAR jobs") < text.index(
        "Requeue after coordination timeout"
    ) < text.index("Refuse uncoordinated") < text.index(
        "Refresh branch after coordination wait"
    ) < text.index(scrape_step)

    refresh = _step_block(text, "Refresh branch after coordination wait")
    assert 'git fetch --depth=1 origin "$GITHUB_REF_NAME"' in refresh
    assert "git reset --hard FETCH_HEAD" in refresh


@pytest.mark.parametrize(
    "filename",
    ["coverage-survey.yml", "filer-metadata.yml", "candidate-filings.yml",
     "amendment-chains.yml", "misc-reread.yml", "committee-contacts.yml",
     "backfill.yml"],
)
def test_requeued_workflow_has_non_evicting_pending_slot(filename: str) -> None:
    concurrency = _concurrency_block(_workflow(filename))
    # Either spelling of the same thing: a group of its own per run, so a
    # replacement never takes the pending slot of another waiting run.
    assert "github.run_id" in concurrency
    assert "cancel-in-progress: false" in concurrency


@pytest.mark.parametrize(
    ("filename", "step_name"),
    [
        ("coverage-survey.yml", "Publish survey state"),
        ("coverage-survey.yml", "Re-trigger if the survey is unfinished"),
        ("filer-metadata.yml", "Re-aggregate with updated metadata"),
        ("filer-metadata.yml", "Publish metadata state"),
        ("filer-metadata.yml", "Check if more filers remain and retrigger"),
        ("verify-filers.yml", "Upload verification reports"),
    ],
)
def test_always_cleanup_cannot_escape_failed_coordination(
    filename: str, step_name: str,
) -> None:
    block = _step_block(_workflow(filename), step_name)
    assert "always()" in block
    assert "steps.orestar_wait.outcome == 'success'" in block


def test_short_jobs_budget_for_wait_install_work_and_handoff() -> None:
    cases = {
        # coordination + browser install + collector + post-work/dispatch room
        "coverage-survey.yml": (30, 25, 70, 30),
        # coordination + browser install + short scrape/post-work/dispatch room
        "candidate-filings.yml": (25, 25, 0, 20),
        # coordination + browser install + 30m collection budget + room
        "amendment-chains.yml": (25, 25, 30, 20),
    }
    for filename, components in cases.items():
        match = re.search(
            r"^    timeout-minutes: ([0-9]+)$", _workflow(filename), re.MULTILINE,
        )
        assert match is not None
        assert int(match.group(1)) >= sum(components)


def test_survey_successor_preserves_recheck_mode() -> None:
    requeue = _step_block(
        _workflow("coverage-survey.yml"),
        "Re-trigger if the survey is unfinished",
    )
    assert '-f recheck="${{ inputs.recheck }}"' in requeue


def test_survey_uses_retry_safe_publisher() -> None:
    publish = _step_block(_workflow("coverage-survey.yml"), "Publish survey state")
    assert "scripts/pipeline_state.py push auxiliary" in publish
    assert "scripts/push_data.sh" not in publish
    assert "git pull --rebase" not in publish


def test_metadata_aggregation_cannot_bypass_failed_state_hydration() -> None:
    text = _workflow("filer-metadata.yml")
    hydrate = _step_block(text, "Hydrate pipeline state")
    assert "id: state" in hydrate
    for step_name in (
        "Re-aggregate with updated metadata",
        "Publish metadata state",
    ):
        block = _step_block(text, step_name)
        assert "always()" in block
        assert "steps.state.outcome == 'success'" in block


def test_backfill_cannot_publish_fetch_markers_before_rows_are_merged() -> None:
    publish = _step_block(_workflow("backfill.yml"), "Publish backfill state")
    assert "steps.process.outcome == 'success'" in publish


def test_every_scheduled_pipeline_is_disabled_until_cutover() -> None:
    scheduled = {
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if re.search(r"^  schedule:$", path.read_text(), re.MULTILINE)
    }
    assert scheduled == {
        "candidate-filings.yml",
        "coverage-diff.yml",
        "daily-refresh.yml",
        "donor-resolve.yml",
        "earliest-balances.yml",
        "filer-metadata.yml",
        "leadership-refresh.yml",
        "committee-contacts.yml",
    }
    gate = (
        "if: github.event_name == 'workflow_dispatch' || "
        "vars.PIPELINE_SCHEDULES_ENABLED == 'true'"
    )
    for filename in scheduled:
        assert gate in _workflow(filename)


def test_data_workflows_do_not_publish_generated_state_to_git() -> None:
    migrated = {
        "backfill.yml",
        "candidate-filings.yml",
        "coverage-diff.yml",
        "coverage-survey.yml",
        "daily-refresh.yml",
        "earliest-balances.yml",
        "filer-metadata.yml",
        "supabase-load.yml",
        "verify-filers.yml",
        "amendment-chains.yml",
        "misc-reread.yml",
    }
    for filename in migrated:
        assert "scripts/push_data.sh" not in _workflow(filename)


# A backfill chain that stopped is restarted by hand, and the restart begins at
# chain 1 — which clears the progress of every filer the chain had already
# finished. On 2026-09-19 that re-fetched 47 committees. resume_progress keeps
# the record; a requeue carries it through with every other input.
def test_a_stopped_identity_chain_can_be_restarted_without_losing_its_progress() -> None:
    text = _workflow("backfill.yml")
    assert "      resume_progress:" in text

    resolve = _step_block(text, "Resolve filer IDs (auto mode)")
    resume = resolve[resolve.index("inputs.resume_progress") - 400:]
    assert 'IDENTITY_MODE" = "true"' in resume and "IDENTITY_RESUME=true" in resume
    # The reset is what resuming avoids, and only identity mode ever resets.
    fetch = _step_block(text, "Backfill ORESTAR data")
    assert "--reset-identity-progress" in fetch
    assert 'steps.resolve.outputs.identity_resume }}" != "true"' in fetch


def test_a_requeued_backfill_is_the_same_request() -> None:
    requeue = _step_block(_workflow("backfill.yml"), "Requeue after coordination timeout")
    # chain_index passes through, so a chained run stays a chained run: its
    # identity progress is kept rather than cleared by a fresh chain 1.
    assert '-f chain_index="${{ inputs.chain_index || \'1\' }}"' in requeue
    # A startup-retry child is coalesced by run title, so it is never requeued.
    assert "!startsWith(inputs.end_date, 'startup:')" in requeue
