"""Real cash aggregation converges through fresh, ordered evidence windows."""

from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scraper"))

import atomic_balance_evidence as ABE
import balance_snapshot as BS
import generate_activity_snapshot
import process as P
import stabilize_atomic_balances as SAB


class EvidenceWindow:
    def __init__(self, tmp_path, monkeypatch, members, *, surplus_amount=50):
        self.data = tmp_path / "data"
        self.aggregated = self.data / "aggregated"
        self.transactions = self.data / "transactions"
        self.transactions.mkdir(parents=True)
        self.members = members
        self.official_cash = {}
        self.rows = [self.row("1", "101", 100), self.row("1", "102", surplus_amount)]
        if "2" in members:
            self.rows.append(self.row("2", "201", 100))
        self.df = pd.DataFrame(self.rows)
        columns = ["tran_id", "original id", "tran_date", "filer id", "amount",
                   "tran_type", "sub_type"]
        with gzip.open(self.transactions / "txn_2026.csv.gz", "wt", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: row[k].strftime("%m/%d/%Y")
                                 if k == "tran_date" else row[k] for k in columns})
        self.snapshot = BS.transaction_snapshot_id(self.transactions)
        self.yearly = {}
        self.observations = []
        self.now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        window = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return window.now if tz else window.now.replace(tzinfo=None)

        monkeypatch.setattr(P, "datetime", Clock)
        monkeypatch.setattr(P, "DATA_DIR", self.data)
        monkeypatch.setattr(P, "AGG_DIR", self.aggregated)
        monkeypatch.setattr(P, "TRANS_DIR", self.transactions)
        monkeypatch.setattr(P, "_row_completeness", lambda: {})
        monkeypatch.setattr(P.supabase_sync, "bulk_upsert_filer_detail", lambda *_: None)
        monkeypatch.setattr(P.supabase_sync, "upsert_dashboard_cache", lambda *_: None)
        monkeypatch.setattr(P.supabase_sync, "get_dashboard_cache", lambda *_: {})
        monkeypatch.setattr(generate_activity_snapshot, "generate", lambda *_a, **_k: {
            "meta": {"total_candidates": 0}, "legislative_map": {},
        })

    @staticmethod
    def row(fid, tid, amount):
        return {
            "tran_id": tid, "original id": tid,
            "tran_date": pd.Timestamp("2026-01-01"),
            "filed_date": pd.Timestamp("2026-01-01"), "amount": amount,
            "tran_type": "C", "sub_type": "Cash Contribution",
            "contributor_payee": "Counterparty", "filer": "Committee",
            "filer id": fid, "year": 2026, "month": "2026-01",
            "book_type": "Individual", "is_out_of_state": False, "_undated": False,
        }

    def tick(self):
        self.now += timedelta(seconds=10)
        return self.now

    def aggregate(self):
        self.tick()
        empty = self.df.iloc[0:0].copy()
        P.aggregate_filers(self.df, self.df.copy(), empty, empty, empty, empty,
                           empty, "filer", "contributor_payee")
        self.source = json.loads((self.aggregated / BS.SOURCE_FILENAME).read_text())
        self.report = json.loads((self.aggregated / "balance_discrepancies.json").read_text())
        [detail] = (self.aggregated / "filers").glob("*.json")
        self.detail = json.loads(detail.read_text())
        assert self.source["transaction_snapshot_id"] == self.snapshot
        return self.source["scopes"][BS.scope_key(self.members)]["cash_on_hand"]

    def capture(self):
        self.tick()
        capture_id = BS.scope_key(self.members) + "@" + self.now.isoformat()
        for fid in self.members:
            official_cash = self.official_cash.get(fid, 100.0)
            summary = {"beginning_balance": 0.0, "ending_cash_balance": official_cash,
                       "contributions": official_cash, "expenditures": 0.0,
                       "other_receipts": 0.0, "other_disbursements": 0.0,
                       "balance_adjustments": 0.0}
            capture = BS.make_summary_capture(fid, 2026, summary,
                                             self.now.timestamp(), self.source, self.snapshot)
            capture["scope_capture_id"] = capture_id
            summary.update({"scrape_ts": self.now.timestamp(), "scope_capture_id": capture_id,
                            "calculation_version": BS.CALCULATION_VERSION,
                            "app_year_transaction_digest": capture["app_year_transaction_digest"]})
            self.yearly[fid] = {"ts": self.now.timestamp(), "years": {"2026": summary},
                                "comparison_capture": capture}
        (self.data / "orestar_yearly_summaries.json").write_text(json.dumps(self.yearly))

    def exact(self, *, absent=True):
        self.tick()
        end = self.now.date()
        snapshots = BS.transaction_filer_snapshots(
            self.transactions, self.members, date(2006, 1, 1), end,
        )
        previous = {r["filer_id"]: r for r in self.observations}
        self.observations = []
        for fid in self.members:
            surplus = ["102"] if fid == "1" and absent else []
            held = 2 if fid == "1" else 1
            item = {"filer_id": fid, "name": "Committee", "held": held,
                    "orestar": held - len(surplus), "complete": not surplus,
                    "missing": [], "surplus": surplus, "superseded": [],
                    "evidence_version": BS.COVERAGE_EVIDENCE_VERSION, "filer_digest_version": 2,
                    "collection_started_at": self.now.isoformat(),
                    "checked_at": (self.now + timedelta(seconds=1)).isoformat(),
                    "transaction_snapshot_id": self.snapshot,
                    "filer_transaction_digest": snapshots[fid]["filer_transaction_digest"],
                    "range_start": "2006-01-01", "range_end": end.isoformat()}
            old = previous.get(fid)
            if old:
                item["usable_history"] = [{k: v for k, v in old.items() if k != "usable_history"},
                                          *old.get("usable_history", [])]
            self.observations.append(item)
        (self.data / "coverage_diff.json").write_text(json.dumps(self.observations))

    def plan(self):
        self.tick()
        return ABE.build_plan(self.report, self.observations, self.source, self.transactions,
                              max_scopes=10, planned_at=self.now.isoformat(),
                              yearly_cache=self.yearly)

    def atomic_pass(self, plan, *, absent=True):
        self.capture()
        ready = ABE.ready_plan(plan, self.yearly, self.transactions, now=self.now)
        self.ready = ready
        before = json.loads(json.dumps(self.yearly))
        self.exact(absent=absent)
        verified = ABE.verify_plan(ready, self.observations, self.transactions)
        assert verified["certified_scope_count"] == 1
        cash = self.aggregate()
        assessment = ABE.assess_stabilization(ready, self.source, self.yearly, self.transactions)
        assert self.yearly == before  # Exact collection/aggregation never rewrites the capture.
        return cash, assessment


@pytest.mark.parametrize("members", [["1"], ["1", "2"]])
@pytest.mark.parametrize("new_absence_verdict", [True, False], ids=["still-absent", "returned"])
def test_summary_refresh_then_real_atomic_recapture_converges(
    tmp_path, monkeypatch, members, new_absence_verdict,
):
    window = EvidenceWindow(tmp_path, monkeypatch, members)
    expected = 100.0 * len(members)
    assert window.aggregate() == expected + 50

    # Establish a genuinely stable older summary/exact pair with the row omitted.
    window.capture()
    window.exact()
    assert window.aggregate() == expected
    window.capture()
    window.exact()
    assert window.aggregate() == expected

    # A later current-only refresh revokes the old exact proof. No transaction changed.
    window.now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    window.capture()
    assert window.aggregate() == expected + 50
    [refresh] = window.report["refresh_rows"]
    assert refresh["delta"] == 0
    assert refresh["app_balance_change_since_capture"] == 50

    plan = window.plan()
    assert plan["selected_scope_count"] == 1
    assert plan["scopes"][0]["needs_stabilization"] is True
    cash, assessment = window.atomic_pass(plan, absent=new_absence_verdict)

    if new_absence_verdict:
        assert cash == expected
        assert assessment["unsettled_filer_ids"] == members
        # Complete successful evidence can retry on the same day; it is not an F5 failure.
        second = window.plan()
        assert second["selected_scope_count"] == 1
        cash, assessment = window.atomic_pass(second)
        assert cash == expected
        assert window.report["flagged"] == 0
    else:
        # New exact evidence says the row returned: preserve its cash and the genuine delta.
        assert cash == expected + 50
        assert window.report["flagged"] == 1
        assert window.report["rows"][0]["delta"] == 50

    assert assessment["unsettled_scope_count"] == 0
    assert assessment["stable_scope_count"] == 1
    assert window.report["refresh_needed"] == 0
    assert BS.transaction_snapshot_id(window.transactions) == window.snapshot


@pytest.mark.parametrize("members", [["1"], ["1", "2"]])
def test_runner_recaptures_real_cash_then_publishes_stable_result(tmp_path, monkeypatch, members):
    window = EvidenceWindow(tmp_path, monkeypatch, members)
    expected = 100.0 * len(members)
    window.aggregate()
    window.capture()
    window.exact()
    window.aggregate()
    window.capture()
    window.exact()
    window.aggregate()
    window.now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    window.capture()
    assert window.aggregate() == expected + 50
    _, initial_assessment = window.atomic_pass(window.plan())
    assert initial_assessment["unsettled_scope_count"] == 1
    original_capture = window.ready["scopes"][0]["captured_at"]
    monkeypatch.setattr(ABE, "utc_timestamp", lambda: window.tick().isoformat())
    monkeypatch.setattr(ABE, "datetime", P.datetime)
    commands = []

    def collect(argv):
        command = argv[1]
        commands.append(command)
        if command == "scraper/fetch_earliest_balances.py":
            assert "--force" in argv and "--current-only" in argv
            window.capture()
        elif command == "scraper/diff_coverage.py":
            plan = json.loads(Path(argv[argv.index("--scope-plan") + 1]).read_text())
            assert plan["scopes"][0]["captured_at"] > original_capture
            assert plan["scopes"][0]["filer_ids"] == members
            window.exact()
        elif command == "scripts/pipeline_state.py":
            assert argv[2:] == ["push", "summaries", "auxiliary"]
        elif command == "scraper/process.py":
            window.aggregate()
        else:
            pytest.fail(f"Unexpected command: {argv}")
        return 0

    result = SAB.run_stabilization(window.ready, root=tmp_path,
                                   command_runner=collect,
                                   cache_reader=lambda _key: window.source)
    assert result["passes_completed"] == 2
    assert result["stable_scope_count"] == 1
    assert result["unsettled_scope_count"] == 0
    assert commands == ["scraper/fetch_earliest_balances.py", "scraper/diff_coverage.py",
                        "scripts/pipeline_state.py", "scraper/process.py"]
    assert window.report["refresh_needed"] == 0
    assert window.report["flagged"] == 0
    assert BS.transaction_snapshot_id(window.transactions) == window.snapshot


@pytest.mark.parametrize("change, reason", [
    ("token-only", None), ("cash", "cash_changed"),
    ("count", "transaction_count_changed"),
    ("annual", "fresh_annual_treatment_changed"),
])
def test_isolated_one_pass_assesses_real_published_window_without_recapture(
    tmp_path, monkeypatch, change, reason,
):
    # A zero-value returned transaction changes annual treatment without cash.
    window = EvidenceWindow(tmp_path, monkeypatch, ["1"],
                            surplus_amount=0 if change == "annual" else 50)
    window.aggregate()
    for _ in range(2):
        window.capture()
        window.exact()
        assert window.aggregate() == 100
    prior_capture_id = window.yearly["1"]["comparison_capture"]["scope_capture_id"]
    window.tick()
    plan = ABE.build_plan(
        window.report, window.observations, window.source, window.transactions,
        max_scopes=1, requested_ids=["1"], planned_at=window.now.isoformat(),
        yearly_cache=window.yearly, max_passes=1, single_pass_filer_ids=["1"],
        max_searches=45,
    )
    assert plan["reserved_exact_passes"] == plan["estimated_total_searches"] == 1
    cash, _ = window.atomic_pass(plan, absent=change not in ("cash", "annual"))
    assert cash == (150 if change == "cash" else 100)
    assert window.ready["reserved_exact_passes"] == 1
    assert window.ready["scopes"][0]["scope_capture_id"] != prior_capture_id
    if change == "count":
        # Count is a separately checked source invariant even if cash is stable.
        window.source["scopes"]["1"]["tran_count"] += 1
    original = json.dumps([window.ready, window.yearly, window.observations], sort_keys=True)

    def no_more_commands(argv):
        pytest.fail(f"One-pass assessment attempted another command: {argv}")

    kwargs = dict(root=tmp_path, max_passes=1, command_runner=no_more_commands,
                  cache_reader=lambda _key: window.source)
    if reason is None:
        result = SAB.run_stabilization(window.ready, **kwargs)
        assert result["stable_scope_count"] == 1
        assert window.report["refresh_needed"] == 0
    else:
        with pytest.raises(ABE.AtomicEvidenceError, match="did not stabilize after 1"):
            SAB.run_stabilization(window.ready, **kwargs)
        result = json.loads((tmp_path / ".atomic-stabilization" / "assessment.json").read_text())
        assert result["unsettled_scope_count"] == 1
        assert reason in result["scopes"][0]["reasons"]
    assert result["passes_completed"] == result["max_passes"] == 1
    assert json.dumps([window.ready, window.yearly, window.observations], sort_keys=True) == original


def test_empty_plan_recovery_exposes_scope_that_requires_new_evidence_window(tmp_path, monkeypatch):
    window = EvidenceWindow(tmp_path, monkeypatch, ["1"])
    assert window.aggregate() == 150
    window.capture()
    assert window.aggregate() == 150
    assert window.report["flagged"] == 1
    plan = window.plan()
    window.capture()
    ready = ABE.ready_plan(plan, window.yearly, window.transactions, now=window.now)
    window.exact()
    assert ABE.verify_plan(ready, window.observations, window.transactions)["certified_scope_count"] == 1
    # Evidence published; the process died before its cash changes were projected.
    assert window.plan()["selected_scope_count"] == 0
    assert window.aggregate() == 100
    assert window.report["refresh_needed"] == 1
    recovery_plan = window.plan()
    assert recovery_plan["selected_scope_count"] == 1
    assert recovery_plan["scopes"][0]["needs_stabilization"] is True
    _, assessment = window.atomic_pass(recovery_plan)
    assert assessment["stable_scope_count"] == 1
    assert window.report["refresh_needed"] == 0


@pytest.mark.parametrize("members", [["1"], ["1", "2"]])
def test_explicit_recovery_verifies_matching_scope_missing_exact_evidence(tmp_path, monkeypatch, members):
    window = EvidenceWindow(tmp_path, monkeypatch, members)
    window.official_cash = {"1": 150.0, "2": 100.0}
    expected = sum(window.official_cash[fid] for fid in members)
    assert window.aggregate() == expected
    window.capture()
    assert window.aggregate() == expected
    assert window.report["flagged"] == 0
    assert window.report["refresh_needed"] == 0
    assert not window.observations
    assert window.plan()["selected_scope_count"] == 0

    window.tick()
    explicit = ABE.build_plan(window.report, window.observations, window.source,
                              window.transactions, max_scopes=1,
                              requested_ids=["1"], planned_at=window.now.isoformat(),
                              yearly_cache=window.yearly)
    assert explicit["selected_scope_count"] == 1
    assert explicit["scopes"][0]["filer_ids"] == members
    assert explicit["scopes"][0]["delta"] == 0
    cash, assessment = window.atomic_pass(explicit, absent=False)
    assert cash == expected
    assert assessment["stable_scope_count"] == 1
    assert assessment["unsettled_scope_count"] == 0
    assert window.report["flagged"] == window.report["refresh_needed"] == 0
