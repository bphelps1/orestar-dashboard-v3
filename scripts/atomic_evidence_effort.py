#!/usr/bin/env python3
"""Read-only admission to a named, repository-defined atomic evidence effort.

Every GitHub run attempt carrying the effort's exact display title is charged,
including failures, cancellations, and reruns. Workflow chain counters are not
an authority for this operational budget; these caps are not provider quotas.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / ".github" / "atomic-evidence-policy.json"
WORKFLOW = "atomic-balance-evidence.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
TITLE_PREFIX = "Atomic balance evidence: "
PAGE_SIZE = 100
OPTIONAL_POLICY_FIELDS = {"single_pass_filer_ids"}
POLICY_FIELDS = {
    "version", "effort_id", "enabled", "max_attempts", "max_scopes",
    "max_searches", "excluded_filer_ids",
}


class EffortError(RuntimeError):
    """The effort's identity or remaining budget cannot be established safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise EffortError("Duplicate JSON field makes the effort data ambiguous")
        value[key] = item
    return value


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise EffortError(f"{label} must be a positive integer")
    return value


def _environment_int(value: str | None, label: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise EffortError(f"{label} must be a positive integer")
    return int(value)


def _filer_ids(value: Any, label: str) -> list[str]:
    if (not isinstance(value, (list, tuple))
            or any(not isinstance(fid, str) or not re.fullmatch(r"[1-9][0-9]*", fid)
                   for fid in value)
            or len(set(value)) != len(value)):
        raise EffortError(f"{label} must contain unique positive numeric strings")
    return list(value)


def validate_policy(value: Any, effort_id: str | None = None, *, allow_disabled: bool = False) -> dict:
    if (not isinstance(value, dict) or not POLICY_FIELDS <= set(value)
            or set(value) - POLICY_FIELDS - OPTIONAL_POLICY_FIELDS):
        raise EffortError("Malformed atomic evidence policy fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise EffortError("Unsupported atomic evidence policy version")
    configured_id = value["effort_id"]
    if (not isinstance(configured_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", configured_id)
            or len(configured_id) > 80):
        raise EffortError("Malformed policy effort_id")
    if effort_id is not None and (not isinstance(effort_id, str) or effort_id != configured_id):
        raise EffortError("Requested effort_id does not match the policy")
    if type(value["enabled"]) is not bool:
        raise EffortError("Policy enabled must be a boolean")
    if not value["enabled"] and not allow_disabled:
        raise EffortError("The atomic evidence effort is not enabled")
    for field in ("max_attempts", "max_scopes", "max_searches"):
        _positive_int(value[field], field)
    if value["max_searches"] > 45:
        raise EffortError("max_searches exceeds the planner's 45-search limit")
    if value["max_scopes"] > 100:
        raise EffortError("max_scopes exceeds the planner's 100-scope limit")
    for field in ("excluded_filer_ids", "single_pass_filer_ids"):
        if not isinstance(value.get(field, []), list):
            raise EffortError(f"{field} must be a list")
    excluded = _filer_ids(value["excluded_filer_ids"], "excluded_filer_ids")
    single_pass = _filer_ids(value.get("single_pass_filer_ids", []), "single_pass_filer_ids")
    return {**value, "excluded_filer_ids": excluded, "single_pass_filer_ids": single_pass}


def _admission_mode(policy: dict, max_passes: int, requested_filer_ids: Any) -> dict:
    if type(max_passes) is not int or max_passes not in (1, 3):
        raise EffortError("max_passes must be exactly 1 or 3")
    requested = _filer_ids(requested_filer_ids, "Requested filer IDs")
    authorized = policy["single_pass_filer_ids"] if max_passes == 1 else []
    if max_passes == 1 and (not requested or not set(requested) <= set(authorized)):
        raise EffortError("Single-pass admission requires explicit policy-authorized filer IDs")
    # Only this explicit attempt receives the exception. Canonical expansion and
    # the one-complete-scope guard are independently enforced by the planner.
    return {
        "max_passes": max_passes,
        "single_pass_filer_ids": list(authorized),
        "excluded_filer_ids": [fid for fid in policy["excluded_filer_ids"]
                               if fid not in authorized],
    }


def load_policy(path: Path, effort_id: str | None = None, *, allow_disabled: bool = False) -> dict:
    try:
        value = json.loads(Path(path).read_text(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise EffortError("Cannot read a valid atomic evidence policy") from exc
    return validate_policy(value, effort_id, allow_disabled=allow_disabled)


def gh_json(endpoint: str) -> Any:
    """Read GitHub JSON without printing response bodies or credential details."""
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            text=True, capture_output=True, check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EffortError("GitHub API request failed") from exc
    if result.returncode != 0:
        raise EffortError("GitHub API request failed")
    try:
        return json.loads(result.stdout, object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as exc:
        raise EffortError("GitHub API returned invalid JSON") from exc


def _read(api_reader: Callable[[str], Any], endpoint: str) -> Any:
    try:
        return api_reader(endpoint)
    except EffortError:
        raise
    except Exception as exc:
        raise EffortError("GitHub API read failed") from exc


def _run_record(value: Any) -> dict:
    if not isinstance(value, dict):
        raise EffortError("Malformed GitHub workflow run")
    for field in ("id", "run_attempt", "workflow_id"):
        _positive_int(value.get(field), f"Workflow run {field}")
    if value.get("path") != WORKFLOW_PATH:
        raise EffortError("GitHub run does not belong to the atomic evidence workflow")
    if not isinstance(value.get("display_title"), str) or not value["display_title"]:
        raise EffortError("Workflow run display_title is missing")
    if not isinstance(value.get("status"), str) or not value["status"]:
        raise EffortError("Workflow run status is missing")
    if value.get("conclusion") is not None and not isinstance(value["conclusion"], str):
        raise EffortError("Malformed workflow run conclusion")
    return value


def _repository_prefix(repository: str) -> str:
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)):
        raise EffortError("Malformed GITHUB_REPOSITORY")
    return f"repos/{repository}/actions"


def _workflow_history(
    repository: str, api_reader: Callable[[str], Any], *, workflow_id: int | None = None,
) -> dict[int, dict]:
    prefix = _repository_prefix(repository)
    seen: dict[int, dict] = {}
    total_count = None
    page = 1
    while True:
        payload = _read(api_reader, f"{prefix}/workflows/{WORKFLOW}/runs?per_page={PAGE_SIZE}&page={page}")
        if (not isinstance(payload, dict) or type(payload.get("total_count")) is not int
                or payload["total_count"] < 0 or not isinstance(payload.get("workflow_runs"), list)):
            raise EffortError("Malformed GitHub workflow pagination")
        if total_count is None:
            total_count = payload["total_count"]
        elif total_count != payload["total_count"]:
            raise EffortError("Workflow history changed during pagination")
        rows = payload["workflow_runs"]
        if len(rows) > PAGE_SIZE:
            raise EffortError("GitHub workflow page exceeded the requested size")
        for value in rows:
            row = _run_record(value)
            if workflow_id is not None and row["workflow_id"] != workflow_id:
                raise EffortError("Workflow history contains a different workflow identity")
            if row["id"] in seen:
                raise EffortError("Duplicate GitHub run makes the effort count ambiguous")
            workflow_id = row["workflow_id"]
            seen[row["id"]] = row
        if len(seen) > total_count:
            raise EffortError("Workflow history exceeds GitHub's reported total")
        if len(rows) < PAGE_SIZE or len(seen) == total_count:
            if len(seen) != total_count:
                raise EffortError("GitHub workflow history is incomplete")
            break
        page += 1

    return seen


def _budget_result(policy: dict, history: dict[int, dict]) -> dict:
    title = TITLE_PREFIX + policy["effort_id"]
    attempts_used = sum(row["run_attempt"] for row in history.values()
                        if row["display_title"] == title)
    return {
        "effort_id": policy["effort_id"], "enabled": policy["enabled"],
        "max_attempts": policy["max_attempts"], "attempts_used": attempts_used,
        "attempts_remaining": max(0, policy["max_attempts"] - attempts_used),
        "max_scopes": policy["max_scopes"], "max_searches": policy["max_searches"],
        "excluded_filer_ids": list(policy["excluded_filer_ids"]),
        "max_passes": 3, "single_pass_filer_ids": [],
    }


def handoff(
    policy: Any, *, repository: str, effort_id: str | None = None,
    api_reader: Callable[[str], Any] = gh_json,
) -> dict:
    """Read whether one new request fits; admission remains authoritative later."""
    policy = validate_policy(policy, effort_id, allow_disabled=True)
    result = _budget_result(policy, _workflow_history(repository, api_reader))
    result["can_dispatch"] = policy["enabled"] and result["attempts_used"] < policy["max_attempts"]
    return result


def admit(
    policy: Any, *, effort_id: str, repository: str,
    run_id: int, run_attempt: int, max_passes: int = 3,
    requested_filer_ids: Any = (), api_reader: Callable[[str], Any] = gh_json,
) -> dict:
    """Admit the current attempt only after reading the complete workflow history."""
    if not isinstance(effort_id, str) or not effort_id:
        raise EffortError("Admission requires an explicit effort_id")
    policy = validate_policy(policy, effort_id)
    mode = _admission_mode(policy, max_passes, requested_filer_ids)
    _positive_int(run_id, "Current run ID")
    _positive_int(run_attempt, "Current run attempt")
    prefix = _repository_prefix(repository)
    current_endpoint = f"{prefix}/runs/{run_id}"
    current = _run_record(_read(api_reader, current_endpoint))
    title = TITLE_PREFIX + effort_id
    if (current["id"] != run_id or current["run_attempt"] != run_attempt
            or current["display_title"] != title):
        raise EffortError("Current GitHub run does not match this effort and attempt")

    seen = _workflow_history(repository, api_reader, workflow_id=current["workflow_id"])

    listed_current = seen.get(run_id)
    if (listed_current is None
            or listed_current["run_attempt"] != run_attempt
            or listed_current["display_title"] != title):
        raise EffortError("Current effort attempt is missing or inconsistent in workflow history")
    # A rerun starting during pagination must not be mistaken for this attempt.
    latest_current = _run_record(_read(api_reader, current_endpoint))
    if any(latest_current[key] != current[key]
           for key in ("id", "run_attempt", "workflow_id", "display_title")):
        raise EffortError("Current GitHub attempt changed during admission")
    result = _budget_result(policy, seen)
    if result["attempts_used"] > policy["max_attempts"]:
        raise EffortError(
            f"Atomic evidence effort exhausted: {result['attempts_used']} attempts exceed "
            f"the {policy['max_attempts']}-attempt policy")
    return {"admitted": True, **result, **mode}


def _emit(value: dict, output_path: str | None) -> None:
    if output_path:
        try:
            with Path(output_path).open("a") as output:
                for key, item in value.items():
                    if isinstance(item, list):
                        item = " ".join(item)
                    elif isinstance(item, bool):
                        item = str(item).lower()
                    output.write(f"{key}={item}\n")
        except OSError as exc:
            raise EffortError("Cannot write GitHub step outputs") from exc
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("admit", "handoff", "policy"))
    parser.add_argument("--effort-id")
    parser.add_argument("--max-passes", choices=("1", "3"), default="3")
    parser.add_argument("--filer-ids", nargs="*", default=[])
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    try:
        if args.command != "admit" and (args.max_passes != "3" or args.filer_ids):
            raise EffortError("Only admission may request an explicit capture mode")
        policy = load_policy(args.policy, args.effort_id, allow_disabled=args.command != "admit")
        if args.command == "admit":
            result = admit(
                policy, effort_id=args.effort_id, max_passes=int(args.max_passes),
                requested_filer_ids=args.filer_ids,
                repository=os.environ.get("GITHUB_REPOSITORY", ""),
                run_id=_environment_int(os.environ.get("GITHUB_RUN_ID"), "GITHUB_RUN_ID"),
                run_attempt=_environment_int(os.environ.get("GITHUB_RUN_ATTEMPT"), "GITHUB_RUN_ATTEMPT"),
            )
        elif args.command == "handoff":
            result = handoff(policy, repository=os.environ.get("GITHUB_REPOSITORY", ""),
                             effort_id=args.effort_id)
        else:
            result = {"admitted": False, **policy}
        _emit(result, os.environ.get("GITHUB_OUTPUT"))
    except EffortError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
