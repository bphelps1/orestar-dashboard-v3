"""Real cash aggregation and history ordering across exact digest versions."""

from __future__ import annotations

import copy
import csv
import gzip
import json
from datetime import date, timedelta

import pandas as pd
import pytest

from test_atomic_balance_stabilization_integration import BS, P, EvidenceWindow


CASH_CASES = [
    pytest.param("O", "Cash Balance Adjustment", -50, -50, id="negative-50-adjustment"),
    pytest.param("E", "Cash Expenditure", 9, -9, id="9-expenditure"),
]


class VersionedEvidenceWindow(EvidenceWindow):
    """Use real cash frames and raw source/derived labels in the shared fixture."""

    def __init__(self, tmp_path, monkeypatch, members, cash_case):
        tran_type, sub_type, amount, self.cash_effect = cash_case
        super().__init__(tmp_path, monkeypatch, members, surplus_amount=amount)
        self.df.loc[self.df["tran_id"] == "102", ["tran_type", "sub_type"]] = [
            tran_type, sub_type,
        ]
        self.df["contributor_payee_canonical"] = "Generated old label"
        self.df["_source_file"] = "first-export.xlsx"
        self.write_shard()

    def write_shard(self):
        columns = [
            "tran_id", "original id", "tran_date", "filed_date", "filer id",
            "filer", "amount", "tran_type", "sub_type", "contributor_payee",
            "contributor_payee_canonical", "_source_file",
        ]
        with gzip.open(self.transactions / "txn_2026.csv.gz", "wt", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in self.df.to_dict("records"):
                writer.writerow({
                    key: row[key].strftime("%m/%d/%Y")
                    if key in {"tran_date", "filed_date"} else row[key]
                    for key in columns
                })
        self.snapshot = BS.transaction_snapshot_id(self.transactions)

    def aggregate(self):
        self.tick()
        empty = self.df.iloc[0:0].copy()
        contributions = self.df[self.df["tran_type"] == "C"].copy()
        expenditures = self.df[self.df["tran_type"] == "E"].copy()
        adjustments = self.df[self.df["sub_type"] == "Cash Balance Adjustment"].copy()
        P.aggregate_filers(
            self.df, contributions, empty, expenditures, empty, empty, adjustments,
            "filer", "contributor_payee",
        )
        self.source = json.loads((self.aggregated / BS.SOURCE_FILENAME).read_text())
        self.report = json.loads((self.aggregated / "balance_discrepancies.json").read_text())
        [detail] = (self.aggregated / "filers").glob("*.json")
        self.detail = json.loads(detail.read_text())
        assert self.source["transaction_snapshot_id"] == self.snapshot
        return self.source["scopes"][BS.scope_key(self.members)]["cash_on_hand"]

    def exact(self, *, digest_version=2, legacy_without_metadata=False):
        self.tick()
        end = self.now.date()
        snapshots = BS.transaction_filer_snapshots(
            self.transactions, self.members, date(2006, 1, 1), end,
            digest_version=digest_version,
        )
        previous = {row["filer_id"]: row for row in self.observations}
        self.observations = []
        for fid in self.members:
            surplus = ["102"] if fid == "1" else []
            held = int((self.df["filer id"] == fid).sum())
            item = {
                "filer_id": fid, "name": "Committee", "held": held,
                "orestar": held - len(surplus), "complete": not surplus,
                "missing": [], "surplus": surplus, "superseded": [],
                "evidence_version": BS.COVERAGE_EVIDENCE_VERSION,
                "collection_started_at": self.now.isoformat(),
                "checked_at": (self.now + timedelta(seconds=1)).isoformat(),
                "transaction_snapshot_id": self.snapshot,
                "filer_transaction_digest": snapshots[fid]["filer_transaction_digest"],
                "range_start": "2006-01-01", "range_end": end.isoformat(),
            }
            if not legacy_without_metadata:
                item["filer_digest_version"] = digest_version
            old = previous.get(fid)
            if old:
                item["usable_history"] = [
                    {key: value for key, value in old.items() if key != "usable_history"},
                    *old.get("usable_history", []),
                ]
            self.observations.append(item)
        (self.data / "coverage_diff.json").write_text(json.dumps(self.observations))

    def settle(self, *, digest_version=2, legacy_without_metadata=False):
        expected = 100.0 * len(self.members)
        assert self.aggregate() == expected + self.cash_effect
        # Two real ordered windows settle the annual omission treatment and cash.
        for _ in range(2):
            self.capture()
            self.exact(digest_version=digest_version,
                       legacy_without_metadata=legacy_without_metadata)
            assert self.aggregate() == expected
        assert self.report["refresh_needed"] == 0
        assert self.detail["orestar_absent"]["count"] == 1
        return expected

    def rename_generated_label(self, fid="1"):
        index = self.df.index[self.df["filer id"] == fid][0]
        self.df.loc[index, "contributor_payee_canonical"] = "New label"
        self.write_shard()


def _cash_timeline(detail):
    return sum(row.get("cash_balance_net", 0) for row in detail["timeline"])


def _capture_bytes(window):
    return (window.data / "orestar_yearly_summaries.json").read_bytes()


@pytest.mark.parametrize("tran_type,sub_type,amount,cash_effect", CASH_CASES)
@pytest.mark.parametrize("members,renamed_member", [(["1"], "1"), (["1", "2"], "2")])
def test_v2_generated_label_edit_preserves_real_cash_and_annual_omission(
    tmp_path, monkeypatch, tran_type, sub_type, amount, cash_effect, members, renamed_member,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, members, (tran_type, sub_type, amount, cash_effect),
    )
    expected = window.settle()
    scope_key = BS.scope_key(members)
    before_scope = copy.deepcopy(window.source["scopes"][scope_key])
    before_capture = _capture_bytes(window)
    before_exact = (window.data / "coverage_diff.json").read_bytes()
    old_snapshot = window.snapshot
    old_timeline = _cash_timeline(window.detail)

    # In the combined scope, only the OTHER member changes its generated name.
    window.rename_generated_label(renamed_member)
    assert window.snapshot != old_snapshot
    assert window.aggregate() == expected

    after_scope = window.source["scopes"][scope_key]
    assert after_scope["app_scope_transaction_digest"] == before_scope["app_scope_transaction_digest"]
    assert after_scope["app_year_transaction_digests"] == before_scope["app_year_transaction_digests"]
    assert window.detail["orestar_absent"] == {"count": 1, "amount": amount}
    assert _cash_timeline(window.detail) == old_timeline == expected
    assert window.detail["tran_count"] == len(window.df)
    with gzip.open(window.transactions / "txn_2026.csv.gz", "rt", newline="") as handle:
        retained = {row["tran_id"]: row for row in csv.DictReader(handle)}
    assert float(retained["102"]["amount"]) == amount  # History retains the omitted row.
    assert retained["102"]["contributor_payee_canonical"] == "Generated old label"
    assert _capture_bytes(window) == before_capture  # No timestamps or captures rebased.
    assert (window.data / "coverage_diff.json").read_bytes() == before_exact
    assert window.report["refresh_needed"] == window.report["flagged"] == 0


@pytest.mark.parametrize("tran_type,sub_type,amount,cash_effect", CASH_CASES)
@pytest.mark.parametrize("field,value,extra_cash", [
    ("contributor_payee", "Different raw source payee", 0),
    ("amount", 101, 1),
    ("filed_date", pd.Timestamp("2026-01-02"), 0),
    ("original id", "previous-amended-row", 0),
])
def test_v2_substantive_source_edit_revokes_real_surplus_omission(
    tmp_path, monkeypatch, tran_type, sub_type, amount, cash_effect, field, value, extra_cash,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, ["1"], (tran_type, sub_type, amount, cash_effect),
    )
    expected = window.settle()
    old_years = copy.deepcopy(window.source["scopes"]["1"]["app_year_transaction_digests"])
    before_capture = _capture_bytes(window)
    window.df.loc[window.df["tran_id"] == "101", field] = value
    window.write_shard()

    assert window.aggregate() == expected + cash_effect + extra_cash
    assert not window.detail.get("orestar_absent")
    assert window.source["scopes"]["1"]["app_year_transaction_digests"] != old_years
    assert _capture_bytes(window) == before_capture


@pytest.mark.parametrize("tran_type,sub_type,amount,cash_effect", CASH_CASES)
@pytest.mark.parametrize("legacy_without_metadata", [True, False], ids=["implicit-v1", "explicit-v1"])
def test_legacy_v1_stays_strict_until_a_fresh_v2_window(
    tmp_path, monkeypatch, tran_type, sub_type, amount, cash_effect, legacy_without_metadata,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, ["1"], (tran_type, sub_type, amount, cash_effect),
    )
    expected = window.settle(digest_version=1, legacy_without_metadata=legacy_without_metadata)
    original_capture = _capture_bytes(window)
    old_capture_time = window.yearly["1"]["comparison_capture"]["captured_at"]
    window.rename_generated_label()

    # Neither omitted nor explicit legacy metadata receives a silent v2 upgrade.
    assert window.aggregate() == expected + cash_effect
    assert not window.detail.get("orestar_absent")
    assert _capture_bytes(window) == original_capture

    # Genuine later observations may use v2; old v1 records remain in history.
    for _ in range(2):
        window.capture()
        window.exact(digest_version=2)
        assert window.aggregate() == expected
    assert window.yearly["1"]["comparison_capture"]["captured_at"] > old_capture_time
    assert window.observations[0]["filer_digest_version"] == 2
    assert window.detail["orestar_absent"] == {"count": 1, "amount": amount}
    assert window.report["refresh_needed"] == window.report["flagged"] == 0


def _write_observations(window):
    (window.data / "coverage_diff.json").write_text(json.dumps(window.observations))


@pytest.mark.parametrize("verdict", ["clean", "missing-conflict", "unanchored"])
@pytest.mark.parametrize("legacy_without_metadata", [True, False], ids=["implicit-v1", "explicit-v1"])
def test_newer_legacy_query_supersedes_older_v2_surplus(
    tmp_path, monkeypatch, verdict, legacy_without_metadata,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, ["1"], ("O", "Cash Balance Adjustment", -50, -50),
    )
    expected = window.settle()
    before_capture = _capture_bytes(window)
    # This later query used the legacy algorithm. Its age, not algorithm number,
    # determines whether the earlier v2 observation may continue omitting cash.
    window.exact(digest_version=1, legacy_without_metadata=legacy_without_metadata)
    current = window.observations[0]
    assert current["usable_history"][0]["filer_digest_version"] == 2
    if verdict == "clean":
        current.update(surplus=[], complete=True, orestar=current["held"])
    elif verdict == "missing-conflict":
        current.update(missing=["999"], orestar=current["orestar"] + 1)
    else:
        current["transaction_snapshot_id"] = "sha256:new-unanchored-snapshot"
    _write_observations(window)

    assert window.aggregate() == expected - 50
    assert not window.detail.get("orestar_absent")
    assert _capture_bytes(window) == before_capture


@pytest.mark.parametrize("bad_version", [None, True, False, 1.0, 2.0, "2", 3, [], {}])
@pytest.mark.parametrize("location", ["latest", "history", "version-only-history"])
def test_malformed_explicit_version_cannot_fall_back_to_good_history(
    tmp_path, monkeypatch, bad_version, location,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, ["1"], ("E", "Cash Expenditure", 9, -9),
    )
    expected = window.settle()
    before_capture = _capture_bytes(window)
    current = window.observations[0]
    if location == "latest":
        current["filer_digest_version"] = bad_version
    elif location == "history":
        current["usable_history"][0]["filer_digest_version"] = bad_version
    else:
        current["usable_history"].append({"filer_digest_version": bad_version})
    _write_observations(window)

    assert window.aggregate() == expected - 9
    assert not window.detail.get("orestar_absent")
    assert _capture_bytes(window) == before_capture


@pytest.mark.parametrize("legacy_without_metadata", [True, False], ids=["implicit-v1", "explicit-v1"])
def test_mixed_versions_certify_complete_scope_and_keep_member_specific_guards(
    tmp_path, monkeypatch, legacy_without_metadata,
):
    window = VersionedEvidenceWindow(
        tmp_path, monkeypatch, ["1", "2"], ("O", "Cash Balance Adjustment", -50, -50),
    )
    expected = window.settle()
    before_capture = _capture_bytes(window)
    member_two_v2 = copy.deepcopy(window.observations[1])
    window.exact(digest_version=1, legacy_without_metadata=legacy_without_metadata)
    # Filer 1 has a later v1 query. Filer 2 retains its genuine post-capture v2
    # query on identical bounds; complete canonical membership is still proved.
    window.observations[1] = member_two_v2
    _write_observations(window)
    assert window.aggregate() == expected
    assert window.detail["orestar_absent"]["count"] == 1
    settled_years = copy.deepcopy(window.source["scopes"]["1|2"]["app_year_transaction_digests"])

    window.rename_generated_label("2")
    assert window.aggregate() == expected
    assert window.source["scopes"]["1|2"]["app_year_transaction_digests"] == settled_years
    assert _capture_bytes(window) == before_capture

    # The other member's legacy evidence cannot borrow v2's relaxed label rule.
    window.rename_generated_label("1")
    assert window.aggregate() == expected - 50
    assert not window.detail.get("orestar_absent")
    assert window.source["scopes"]["1|2"]["app_year_transaction_digests"] != settled_years
    assert _capture_bytes(window) == before_capture
