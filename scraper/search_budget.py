"""A fail-closed, workflow-wide budget for exact search submissions.

This is a conservative operating bound, not an asserted ORESTAR quota. The
workflow initializes one ledger and all collector subprocesses inherit its
absolute path. Reserving before submission also counts ambiguous failures.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from balance_snapshot import COVERAGE_EVIDENCE_VERSION, exact_coverage_result_shape_is_valid

ENVIRONMENT_KEY = "ORESTAR_SEARCH_BUDGET_PATH"
DEFAULT_LIMIT = 45


class SearchBudgetError(RuntimeError):
    """The configured budget cannot safely authorize another search."""


class SearchBudgetExceeded(SearchBudgetError):
    """The workflow's search budget cannot admit the requested work."""


class SearchBudget:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise SearchBudgetError("Search budget path must be absolute")

    @classmethod
    def from_environment(cls) -> SearchBudget | None:
        if ENVIRONMENT_KEY not in os.environ:
            return None
        path = os.environ[ENVIRONMENT_KEY]
        if not path:
            raise SearchBudgetError("Configured search budget path is empty")
        budget = cls(path)
        budget.state()  # Missing or malformed configured ledgers never reset.
        return budget

    @contextmanager
    def _locked(self):
        try:
            with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        except (OSError, ValueError) as exc:
            raise SearchBudgetError(f"Cannot access search budget: {exc}") from exc

    def _read(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise SearchBudgetError(f"Cannot read configured search budget: {exc}") from exc
        if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
                or type(state.get("limit")) is not int or not 1 <= state["limit"] <= DEFAULT_LIMIT
                or type(state.get("used")) is not int
                or not 0 <= state["used"] <= state["limit"]
                or not isinstance(state.get("submissions"), list)
                or len(state["submissions"]) != state["used"]
                or any(not isinstance(item, dict) or not isinstance(item.get("filer_id"), str)
                       or not isinstance(item.get("window"), dict) for item in state["submissions"])):
            raise SearchBudgetError("Malformed configured search budget")
        return state

    def _write(self, state: dict) -> None:
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                             prefix=self.path.name + ".", delete=False) as out:
                name = out.name
                json.dump(state, out, sort_keys=True)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    @classmethod
    def initialize(cls, path: Path | str, limit: int = DEFAULT_LIMIT) -> SearchBudget:
        if type(limit) is not int or not 1 <= limit <= DEFAULT_LIMIT:
            raise SearchBudgetError(f"Search budget limit must be between 1 and {DEFAULT_LIMIT}")
        budget = cls(path)
        with budget._locked():
            if budget.path.exists():
                raise SearchBudgetError("Search budget already exists; refusing to reset it")
            budget._write({"version": 1, "limit": limit, "used": 0, "submissions": []})
        return budget

    def state(self) -> dict:
        with self._locked():
            return self._read()

    @property
    def used(self) -> int:
        return self.state()["used"]

    @property
    def remaining(self) -> int:
        state = self.state()
        return state["limit"] - state["used"]

    def require_capacity(self, searches: int) -> None:
        if type(searches) is not int or searches < 0:
            raise SearchBudgetError("Invalid planned search cost")
        state = self.state()
        if searches > state["limit"] - state["used"]:
            raise SearchBudgetExceeded(
                f"Search budget cannot admit {searches} submissions: "
                f"{state['used']}/{state['limit']} already reserved")

    def consume(self, filer_id: str, window: dict) -> int:
        with self._locked():
            state = self._read()
            if state["used"] >= state["limit"]:
                raise SearchBudgetExceeded(
                    f"Search budget exhausted at {state['used']}/{state['limit']}")
            state["used"] += 1
            state["submissions"].append({"filer_id": str(filer_id), "window": window})
            self._write(state)
        logging.getLogger(__name__).info(
            "EXACT_SEARCH_BUDGET used=%d limit=%d filer_id=%s",
            state["used"], state["limit"], filer_id,
        )
        return state["used"]


def estimate_scope_searches(filer_ids, entries: dict) -> int | None:
    """Estimate from successful full-range observations; never certify from it.

    Observed costs are scheduling hints and cannot guarantee future source
    shape. An unmeasured over-cap filer remains unknown. The hard ledger still
    protects the workflow when a previously small source grows or splits.
    """
    total = 0
    for raw_fid in filer_ids:
        fid = str(raw_fid)
        entry = entries.get(fid)
        if not isinstance(entry, dict):
            return None
        history = entry.get("usable_history")
        observations = [entry] + (history if isinstance(history, list) else [])
        measured, root_costs = [], []
        unmeasured_large = False
        for index, row in enumerate(observations):
            if (not isinstance(row, dict) or str(row.get("filer_id")) != fid
                    or not exact_coverage_result_shape_is_valid(row)
                    or row.get("evidence_version") != COVERAGE_EVIDENCE_VERSION
                    or row.get("range_start") != "2006-01-01"):
                continue
            try:
                if date.fromisoformat(row.get("range_end", "")) < date(2006, 1, 1):
                    continue
            except (TypeError, ValueError):
                continue
            count = row.get("exact_search_count")
            if type(count) is int and count > 0:
                measured.append(count)
            elif type(row.get("orestar")) is int and 0 <= row["orestar"] <= 4999:
                root_costs.append(1)
            else:
                if index == 0:
                    # A newly observed large source without measurements may
                    # not borrow the cost of an older, smaller source.
                    return None
                unmeasured_large = True
        costs = measured + root_costs if measured else ([] if unmeasured_large else root_costs)
        if not costs:
            return None
        total += max(costs)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--path", type=Path, required=True)
    initialize.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    try:
        SearchBudget.initialize(args.path, args.limit)
    except SearchBudgetError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
