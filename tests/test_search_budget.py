"""Durable search accounting is shared by passes and fails closed."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRAPER = Path(__file__).resolve().parents[1] / "scraper"
sys.path.insert(0, str(SCRAPER))
from search_budget import (ENVIRONMENT_KEY, SearchBudget, SearchBudgetError,
                           SearchBudgetExceeded, estimate_scope_searches)


def test_subprocess_passes_share_45_submissions_and_cannot_reset(tmp_path):
    path = tmp_path / "budget.json"
    budget = SearchBudget.initialize(path)
    env = {**os.environ, ENVIRONMENT_KEY: str(path), "PYTHONPATH": str(SCRAPER)}
    script = ("from search_budget import SearchBudget; "
              "b=SearchBudget.from_environment(); "
              "[b.consume('10', {}) for _ in range(23)]")
    first = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
    second = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
    assert first.returncode == 0
    assert second.returncode != 0
    assert b"SearchBudgetExceeded" in second.stderr
    assert budget.used == 45
    assert len(json.loads(path.read_text())["submissions"]) == 45
    with pytest.raises(SearchBudgetError, match="refusing to reset"):
        SearchBudget.initialize(path)
    assert budget.used == 45


def test_unconfigured_ordinary_collectors_remain_unlimited(monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    assert SearchBudget.from_environment() is None


@pytest.mark.parametrize("contents", [None, "{", "{}", '{"version": 1, "limit": 45, "used": 0}',
                                      '{"version": 1, "limit": 45, "used": 1, "submissions": []}'])
def test_missing_or_corrupt_configured_ledger_never_resets(tmp_path, monkeypatch, contents):
    path = tmp_path / "budget.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setenv(ENVIRONMENT_KEY, str(path))
    with pytest.raises(SearchBudgetError):
        SearchBudget.from_environment()
    assert (path.read_text() if path.exists() else None) == contents


def test_capacity_checks_are_read_only(tmp_path):
    budget = SearchBudget.initialize(tmp_path / "budget.json", 3)
    budget.consume("10", {})
    budget.require_capacity(2)
    with pytest.raises(SearchBudgetExceeded):
        budget.require_capacity(3)
    assert budget.used == 1


def test_initialize_and_read_refuse_a_limit_above_the_hard_ceiling(tmp_path):
    path = tmp_path / "budget.json"
    with pytest.raises(SearchBudgetError, match="between 1 and 45"):
        SearchBudget.initialize(path, 46)
    assert not path.exists()
    budget = SearchBudget.initialize(path)
    state = budget.state()
    state["limit"] = 46
    path.write_text(json.dumps(state))
    with pytest.raises(SearchBudgetError, match="Malformed"):
        budget.consume("10", {})


def _row(fid, count, searches=None):
    row = {"filer_id": fid, "complete": True, "missing": [], "surplus": [],
           "superseded": [], "orestar": count, "held": count,
           "filer_transaction_digest": "sha256:cost", "evidence_version": 2, "filer_digest_version": 2,
           "range_start": "2006-01-01", "range_end": "2026-09-14"}
    if searches is not None:
        row["exact_search_count"] = searches
    return row


def test_costs_cover_every_member_and_measurements_supersede_unmeasured_history():
    entries = {"10": _row("10", 5), "20": _row("20", 6279, 3)}
    entries["20"]["usable_history"] = [_row("20", 6279)]
    assert estimate_scope_searches(["10", "20"], entries) == 4
    assert estimate_scope_searches(["10", "20", "30"], entries) is None
    entries["20"].pop("exact_search_count")
    assert estimate_scope_searches(["10", "20"], entries) is None


def test_legacy_partial_and_malformed_rows_cannot_supply_costs():
    for update in ({"complete": None}, {"evidence_version": 1},
                   {"range_start": "2026-01-01"}, {"orestar": True},
                   {"missing": ["lost"]}):
        assert estimate_scope_searches(["10"], {"10": {**_row("10", 5), **update}}) is None


def test_current_unmeasured_large_source_cannot_borrow_old_small_cost():
    row = _row("10", 6000)
    row["usable_history"] = [_row("10", 5, 1)]
    assert estimate_scope_searches(["10"], {"10": row}) is None
