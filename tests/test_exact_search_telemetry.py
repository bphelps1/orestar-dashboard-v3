"""Successful exact collections measure real submission paths in both modes."""
from __future__ import annotations

import argparse
import copy
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import diff_coverage as DC
from search_budget import ENVIRONMENT_KEY, SearchBudget, estimate_scope_searches

SNAPSHOT = "sha256:" + "a" * 64
END = date(2026, 9, 14)


class ResultsPage:
    """Offline DOM responses; the real count/recursive collection code runs."""

    def __init__(self, results):
        self.results = iter(results)
        self.url = DC.F.SEARCH_URL
        self.searches = 0
        self.body_reads = 0
        self.current = None
        self.polls = 0

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_selector(self, *_args, **_kwargs):
        pass

    def fill(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, *_args):
        pass

    def click(self, selector, **_kwargs):
        assert selector == 'input[name="search"]'
        self.current = next(self.results)
        self.searches += 1
        self.polls = 0
        self.url = "https://secure.sos.state.or.us/orestar/gotoPublicTransactionSearchResults.do"

    def wait_for_url(self, *_args, **_kwargs):
        pass

    def inner_text(self, *_args, **_kwargs):
        self.body_reads += 1
        self.polls += 1
        return "Rendering" if self.polls < 3 else f"{self.current[0]} records found"


class Browser:
    def close(self):
        pass


class Playwright:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


def setup_collectors(monkeypatch, page, entries, filers):
    monkeypatch.setattr(DC, "sync_playwright", Playwright)
    monkeypatch.setattr(DC.F, "setup_browser_retrying", lambda _pw: (Browser(), object(), page))
    monkeypatch.setattr(DC, "transaction_snapshot_id", lambda _path: SNAPSHOT)
    monkeypatch.setattr(DC, "_current_transaction_snapshot_id", lambda: SNAPSHOT)
    monkeypatch.setattr(DC, "_load", lambda: entries)
    monkeypatch.setattr(DC, "_load_atomic_entries", lambda: entries)
    monkeypatch.setattr(DC, "_active_paired_requirements", lambda: {})
    monkeypatch.setattr(DC, "transaction_filer_snapshots", lambda *_args: {
        fid: {"held_ids": {str(n) for n in range(5001)} if fid == "10" else set(),
              "superseded_ids": set(), "filer_transaction_digest": "sha256:" + fid}
        for fid in filers
    })
    monkeypatch.setattr(DC, "_date_seed_windows", lambda *_args: [
        (date(2006, 1, 1), date(2025, 12, 31)), (date(2026, 1, 1), END)])
    monkeypatch.setattr(DC, "_order_children_by_local_cost", lambda _fid, children: children)
    # Only the export transport is mocked; each leaf must still reconcile to
    # its fresh count and both date children must reproduce the full root.
    monkeypatch.setattr(DC, "_export_rows", lambda *_args: page.current[1])
    snapshots = []
    monkeypatch.setattr(DC, "_save", lambda rows: snapshots.append(copy.deepcopy(rows)))
    return snapshots


@pytest.mark.parametrize("mode", ["ordinary", "atomic"])
def test_recursive_success_records_actual_searches_and_ignores_polls(monkeypatch, tmp_path, mode):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    page = ResultsPage([(5001, None), (2500, {str(n): {} for n in range(2500)}),
                        (2501, {str(n): {} for n in range(2500, 5001)}), (0, {})])
    entries = {}
    saved = setup_collectors(monkeypatch, page, entries, ["10", "20"])
    budget = None
    if mode == "ordinary":
        monkeypatch.setattr(sys, "argv", ["diff_coverage.py", "--filer-ids", "10", "20",
                                         "--recheck", "--require-no-missing",
                                         "--end-date", END.isoformat()])
        assert DC.main() == 0
    else:
        budget = SearchBudget.initialize(tmp_path / "budget.json")
        monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
        monkeypatch.setattr(DC, "_load_atomic_scope_plan", lambda *_args:
                            ([[{"filer_id": "10"}], [{"filer_id": "20"}]], {}))
        def store_scope(rows, results, *_args, **_kwargs):
            for result in results:
                DC._store_usable_result(rows, result, active_requirements={})
            DC._save(rows)
            return rows
        monkeypatch.setattr(DC, "_persist_usable_scope", store_scope)
        args = argparse.Namespace(start_year=2006, end_date=END,
                                  scope_plan=tmp_path / "ready.json", max_minutes=70)
        assert DC._run_atomic_scope_plan(args) == 0
    assert page.searches == 4
    assert page.body_reads == 12
    assert saved[-1]["10"]["orestar"] == 5001
    assert saved[-1]["10"]["complete"] is True
    assert saved[-1]["10"]["exact_search_count"] == 3
    assert saved[-1]["20"]["exact_search_count"] == 1
    assert saved[-1]["10"]["filer_digest_version"] == 2
    assert saved[-1]["20"]["filer_digest_version"] == 2
    assert DC._usable_history_record(saved[-1]["10"])["exact_search_count"] == 3
    assert estimate_scope_searches(["10", "20"], saved[-1]) == 4
    if budget is not None:
        assert budget.used == 4  # Measurement never consumes the ledger again.


@pytest.mark.parametrize("legacy", [True, False])
def test_failed_ordinary_recheck_preserves_prior_cost_and_proof(monkeypatch, legacy):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    prior = {"filer_id": "10", "complete": True, "orestar": 5001, "held": 5001,
             "missing": [], "surplus": [], "superseded": [], "evidence_version": 2, "filer_digest_version": 2,
             "filer_transaction_digest": "sha256:10", "transaction_snapshot_id": SNAPSHOT,
             "checked_at": "2026-09-13T12:00:01Z", "collection_started_at": "2026-09-13T12:00:00Z",
             "range_start": "2006-01-01", "range_end": "2026-09-13", "exact_search_count": 3}
    if legacy:
        prior.pop("filer_digest_version")
    entries = {"10": copy.deepcopy(prior)}
    page = ResultsPage([(2, {"one": {}})])  # Short export is still rejected.
    saved = setup_collectors(monkeypatch, page, entries, ["10"])
    monkeypatch.setattr(sys, "argv", ["diff_coverage.py", "--filer-ids", "10", "--recheck",
                                     "--end-date", END.isoformat()])
    DC.main()
    assert page.searches == 1
    assert saved[-1]["10"]["last_failure"] == "unusable_window"
    assert saved[-1]["10"]["exact_search_count"] == 3
    assert all(saved[-1]["10"][key] == value for key, value in prior.items())
    assert saved[-1]["10"]["last_attempt_filer_digest_version"] == 2
    if legacy:
        assert "filer_digest_version" not in saved[-1]["10"]
