#!/usr/bin/env python3
"""Run at most two further real summary/exact passes after workflow pass one.

Run under xvfb-run. Collectors share the original hydrated transaction shards;
no state pull or checkout refresh occurs between passes. Captures retain their
actual timestamps, including scopes that settled in an earlier pass.
"""
from __future__ import annotations

import argparse
import copy
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent))
import atomic_balance_evidence as ABE
from search_budget import SearchBudget, SearchBudgetError, estimate_scope_searches

ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL_PASSES = 3
# Seconds. The same budget the workflow steps give a bare process.py run: this
# step can legitimately run for hours, so its own timeout cannot catch a
# process.py hung on a Postgres backend the pooler has hidden (2026-09-23).
COMMAND_TIMEOUTS = {"scraper/process.py": 60 * 60}


def _scope_map(plan: dict) -> dict[tuple[str, ...], dict]:
    scopes = plan.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ABE.AtomicEvidenceError("Stabilization requires a nonempty ready plan")
    result, seen = {}, set()
    for scope in scopes:
        if not isinstance(scope, dict):
            raise ABE.AtomicEvidenceError("Malformed stabilization scope")
        ids = scope.get("filer_ids")
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(fid, str) or not fid.isdigit() for fid in ids)
                or ids != sorted(set(ids)) or seen.intersection(ids)):
            raise ABE.AtomicEvidenceError("Stabilization scopes must be complete and disjoint")
        result[tuple(ids)] = scope
        seen.update(ids)
    return result


def _replan(ready: dict, requested_ids: set[str]) -> dict:
    """Explicitly retry assessed scopes, including fresh annual-only changes.

    General discrepancy selection deliberately excludes missing-history-only
    rows. These scopes already passed assessment against the current source;
    retry them without widening that general selector or rewriting a capture.
    """
    current = _scope_map(ready)
    selected = [scope for key, scope in current.items() if requested_ids.intersection(key)]
    if requested_ids != {fid for scope in selected for fid in scope["filer_ids"]}:
        raise ABE.AtomicEvidenceError("Unsettled filers changed or split the original scopes")
    scopes = []
    for original in selected:
        scope = copy.deepcopy(original)
        scope["prior_captured_at"] = scope["captured_at"]
        scope["prior_transaction_snapshot_id"] = ready["transaction_snapshot_id"]
        for field in ("capture_started_at", "captured_at", "capture_day", "scope_capture_id",
                      "captured_app_cash_on_hand", "captured_app_tran_count"):
            scope.pop(field, None)
        scopes.append(scope)
    return {
        "version": ABE.PLAN_VERSION,
        "transaction_snapshot_id": ready["transaction_snapshot_id"],
        "planned_at": ABE.utc_timestamp(),
        "selected_scope_count": len(scopes),
        "reserved_exact_passes": ready.get("reserved_exact_passes", MAX_TOTAL_PASSES),
        "scopes": scopes,
    }


def run_stabilization(
    ready: dict, *, max_passes: int = MAX_TOTAL_PASSES,
    root: Path = ROOT, work_dir: Path | None = None,
    command_runner: Callable[[list[str]], int] | None = None,
    cache_reader: Callable[[str], Any] | None = None,
) -> dict:
    """Assess all original scopes, preserving valid partial work on failure.

    command_runner(argv) returns an exit code; cache_reader(key) reads the fresh
    dashboard cache. Both are injectable for offline ordering/failure tests.
    """
    if type(max_passes) is not int or not 1 <= max_passes <= MAX_TOTAL_PASSES:
        raise ABE.AtomicEvidenceError("max_passes must be between 1 and 3 total passes")
    root = Path(root)
    work_dir = Path(work_dir) if work_dir else root / ".atomic-stabilization"
    read_cache = cache_reader or ABE.supabase_sync.require_dashboard_cache
    transaction_dir = root / "data" / "transactions"
    yearly_path = root / "data" / "orestar_yearly_summaries.json"
    diff_path = root / "data" / "coverage_diff.json"
    combined = copy.deepcopy(ready)
    original_scopes = _scope_map(combined)
    # Legacy ready plans predate configurable reservation and imply the
    # original three-pass contract. Dropping metadata cannot authorize one.
    reserved = combined.get("reserved_exact_passes", MAX_TOTAL_PASSES)
    if type(reserved) is not int or reserved not in (1, 3):
        raise ABE.AtomicEvidenceError("Invalid reserved_exact_passes in ready plan")
    if max_passes != reserved:
        raise ABE.AtomicEvidenceError("max_passes must match the ready plan's reserved_exact_passes")
    snapshot = ABE._strict_snapshot_id(combined.get("transaction_snapshot_id"))
    try:
        search_budget = SearchBudget.from_environment()
    except SearchBudgetError as exc:
        raise ABE.AtomicEvidenceError(str(exc)) from exc

    def command(*args: str) -> int:
        argv = [sys.executable, *args]
        try:
            if command_runner is not None:
                return command_runner(argv)
            return subprocess.run(argv, cwd=root, check=False,
                                  timeout=COMMAND_TIMEOUTS.get(args[0])).returncode
        except subprocess.TimeoutExpired as exc:
            print(f"ERROR: {' '.join(argv)}: no result after {exc.timeout:.0f}s", file=sys.stderr)
            return 1
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"ERROR: {' '.join(argv)}: {exc}", file=sys.stderr)
            return 1

    passes = 1  # First summary, exact, publication and aggregation already ran.
    while True:
        assessment = ABE.assess_stabilization(
            combined, read_cache("balance_snapshot_source"),
            ABE._read_json(yearly_path, {}), transaction_dir,
        )
        if (assessment.get("transaction_snapshot_id") != snapshot
                or assessment.get("stable_scope_count", -1)
                + assessment.get("unsettled_scope_count", -1) != len(original_scopes)):
            raise ABE.AtomicEvidenceError("Assessment changed the original scopes or snapshot")
        result = {**assessment, "passes_completed": passes, "max_passes": max_passes}
        ABE._write_json(work_dir / "assessment.json", result)
        ABE._write_json(work_dir / "ready.json", combined)
        print("ATOMIC_STABILIZATION "
              f"passes={passes} stable_scopes={assessment['stable_scope_count']} "
              f"unsettled_scopes={assessment['unsettled_scope_count']}", flush=True)
        if assessment["unsettled_scope_count"] == 0:
            if assessment.get("unsettled_filer_ids"):
                raise ABE.AtomicEvidenceError("Stable assessment still lists unsettled filers")
            return result
        if passes >= max_passes:
            raise ABE.AtomicEvidenceError(
                f"Cash did not stabilize after {passes} total summary/exact passes")
        requested = assessment.get("unsettled_filer_ids")
        if not isinstance(requested, list) or not requested:
            raise ABE.AtomicEvidenceError("Unsettled assessment has no complete filer scopes")
        plan = _replan(combined, set(requested))
        planned_scopes = _scope_map(plan)
        if (plan["selected_scope_count"] != assessment["unsettled_scope_count"]
                or any(key not in original_scopes or
                       scope.get("app_scope_transaction_digest") !=
                       original_scopes[key].get("app_scope_transaction_digest")
                       for key, scope in planned_scopes.items())):
            raise ABE.AtomicEvidenceError("Replanning changed the original scope")
        if search_budget is not None:
            # Reserve room for every still-supported real pass before changing
            # any summary capture. Historical costs guide admission only; the
            # submission hook remains the hard backstop if the source grows.
            entries = ABE._diff_entries(ABE._read_json(diff_path, []))
            estimate = 0
            for scope in planned_scopes.values():
                scope_cost = estimate_scope_searches(scope["filer_ids"], entries)
                if scope_cost is None:
                    scope_cost = scope.get("estimated_exact_searches")
                if type(scope_cost) is not int or scope_cost < 1:
                    raise ABE.AtomicEvidenceError(
                        "Stabilization deferred: full-scope search cost is unknown")
                estimate += scope_cost
            try:
                search_budget.require_capacity(estimate * (max_passes - passes))
            except SearchBudgetError as exc:
                raise ABE.AtomicEvidenceError(f"Stabilization deferred before capture: {exc}") from exc
        next_pass = passes + 1
        plan_path = work_dir / f"pass-{next_pass}-plan.json"
        ready_path = work_dir / f"pass-{next_pass}-ready.json"
        report_path = work_dir / f"pass-{next_pass}-report.json"
        ABE._write_json(plan_path, plan)
        summary_status = command("scraper/fetch_earliest_balances.py", "--filer-ids",
                                 *sorted(set(requested)), "--force", "--current-only")
        next_ready = ABE.ready_plan(plan, ABE._read_json(yearly_path, {}), transaction_dir)
        ABE._write_json(ready_path, next_ready)
        selected_count = len(planned_scopes)
        all_ready = (next_ready.get("ready_scope_count") == selected_count
                     and next_ready.get("rejected_scope_count") == 0
                     and {tuple(s["filer_ids"]) for s in next_ready.get("scopes", [])}
                     == set(planned_scopes))
        diff_status = None
        if summary_status == 0 and all_ready:
            diff_status = command("scraper/diff_coverage.py", "--scope-plan", str(ready_path),
                                  "--recheck", "--start-year", "2006", "--end-date",
                                  next_ready["end_date"], "--max-minutes", "70")
        # A failed collector may still leave truthful partial state. Validation
        # must succeed before publishing; drift exceptions never publish.
        report = ABE.verify_plan(next_ready, ABE._read_json(diff_path, []), transaction_dir)
        ABE._write_json(report_path, report)
        if command("scripts/pipeline_state.py", "push", "summaries", "auxiliary") != 0:
            raise ABE.AtomicEvidenceError("Stabilization evidence publication failed")
        # Match the first workflow pass: publish and project truthful partial
        # evidence before enforcing failure. No failed pass can continue.
        if command("scraper/process.py") != 0:
            raise ABE.AtomicEvidenceError("Stabilization aggregation failed after publishing evidence")
        if summary_status != 0:
            raise ABE.AtomicEvidenceError("Summary capture failed; exact collection was not started")
        if not all_ready:
            raise ABE.AtomicEvidenceError("Not every stabilization scope produced a fresh paired summary")
        if diff_status != 0:
            raise ABE.AtomicEvidenceError("Stabilization exact collection failed")
        if (report.get("certified_scope_count") != selected_count
                or report.get("blocked_filer_count") != 0):
            raise ABE.AtomicEvidenceError("Not every stabilization scope has certifiable exact evidence")
        replacements = _scope_map(next_ready)
        combined["scopes"] = [replacements.get(tuple(scope["filer_ids"]), scope)
                              for scope in combined["scopes"]]
        # The original planned_at remains valid for the untouched captures. Each
        # exact query used its own ready plan's end_date; assessment does not
        # demand a new query for older settled scopes solely due to midnight.
        combined["end_date"] = max(combined["end_date"], next_ready["end_date"])
        passes = next_pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--max-passes", type=int, default=MAX_TOTAL_PASSES)
    args = parser.parse_args(argv)
    try:
        run_stabilization(ABE._load_plan(args.plan), max_passes=args.max_passes,
                          work_dir=args.plan.parent / "atomic-stabilization")
    except (ABE.AtomicEvidenceError, RuntimeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
