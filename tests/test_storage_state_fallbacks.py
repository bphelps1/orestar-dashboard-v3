"""Fail-closed contracts for clean checkouts without generated aggregates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scraper"))

import diff_coverage  # noqa: E402
import fetch_earliest_balances  # noqa: E402
import fetch_filer_metadata  # noqa: E402
import process  # noqa: E402
import supabase_sync  # noqa: E402
import verify_filer  # noqa: E402


class _RowsCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, _params=None):
        return None

    def fetchall(self):
        return self.rows


class _RowsConnection:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _RowsCursor(self.rows)


def _paired_detail(slug: str = "current", filer_id: str = "10") -> dict:
    return {
        "slug": slug,
        "name": "Current Committee",
        "filer_id": filer_id,
        "filer_ids": [filer_id],
        "closed": False,
        "orestar_comparison": {
            "status": "paired",
            "actionable": True,
            "delta_at_capture": 250.0,
            "captured_at": 1_800_000_000.0,
            "filer_ids": [filer_id],
            "app_transaction_snapshot_id": "sha256:G1",
        },
    }


def test_filer_projection_drops_stale_rows_and_requires_every_current_slug(
    monkeypatch,
) -> None:
    rows = [
        ("current", "Current Committee", "10", ["10"], {}, False),
        ("stale", "Stale Committee", "99", ["99"], {}, False),
    ]
    monkeypatch.setattr(supabase_sync, "sync_enabled", lambda: True)
    monkeypatch.setattr(
        supabase_sync,
        "require_dashboard_cache",
        lambda key: [{"slug": "current"}],
    )
    monkeypatch.setattr(
        supabase_sync, "_connect", lambda: _RowsConnection(rows)
    )

    assert [row["slug"] for row in supabase_sync.require_filer_comparison_details()] == [
        "current"
    ]

    monkeypatch.setattr(
        supabase_sync,
        "require_dashboard_cache",
        lambda key: [{"slug": "current"}, {"slug": "missing"}],
    )
    with pytest.raises(RuntimeError, match="missing 1 current filer"):
        supabase_sync.require_filer_comparison_details()


def test_diff_selector_prefers_database_over_partial_local_files(
    tmp_path, monkeypatch,
) -> None:
    local = tmp_path / "filers"
    local.mkdir()
    (local / "partial.json").write_text(json.dumps({"filer_ids": ["99"]}))
    monkeypatch.setattr(diff_coverage, "FILERS_DIR", local)
    monkeypatch.setattr(
        diff_coverage, "IDENTITY_PROGRESS_PATH", tmp_path / "missing.json"
    )
    monkeypatch.setattr(diff_coverage.supabase_sync, "sync_enabled", lambda: True)
    monkeypatch.setattr(
        diff_coverage.supabase_sync,
        "require_filer_comparison_details",
        lambda: [_paired_detail()],
    )

    requirements = diff_coverage._active_paired_requirements()
    assert set(requirements) == {"10"}
    assert requirements["10"]["transaction_snapshot_id"] == "sha256:G1"
    assert requirements["10"]["captured_at"] == 1_800_000_000.0
    assert requirements["10"]["scope_ids"] == ["10"]


def test_threshold_verifier_prefers_database_over_partial_local_files(
    tmp_path, monkeypatch,
) -> None:
    (tmp_path / "aggregated" / "filers").mkdir(parents=True)
    (tmp_path / "aggregated" / "filer_index.json").write_text("[]")
    monkeypatch.setattr(verify_filer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(verify_filer.supabase_sync, "sync_enabled", lambda: True)
    monkeypatch.setattr(
        verify_filer.supabase_sync,
        "require_filer_comparison_details",
        lambda: [_paired_detail()],
    )

    assert verify_filer.get_filers_with_discrepancies(100) == [
        ("10", "Current Committee", 250.0)
    ]


def test_clean_checkout_without_database_cannot_report_empty_success(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(fetch_filer_metadata, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fetch_filer_metadata.supabase_sync, "sync_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="filer_index is absent"):
        fetch_filer_metadata.get_all_filer_ids()

    monkeypatch.setattr(verify_filer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(verify_filer.supabase_sync, "sync_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="No filer_index"):
        verify_filer.get_filers_with_discrepancies(100)


def test_summary_pairing_uses_manifest_ledger_not_database_self_equality(
    tmp_path, monkeypatch,
) -> None:
    base = tmp_path / ".pipeline-state-base.json"
    base.write_text(json.dumps({"transaction_snapshot_id": "sha256:G0"}))
    monkeypatch.setattr(
        fetch_earliest_balances, "PIPELINE_STATE_BASE_PATH", base
    )
    assert fetch_earliest_balances._pipeline_transaction_snapshot_id() == "sha256:G0"
    assert fetch_earliest_balances._snapshot_source_ready(
        {
            "version": fetch_earliest_balances.FORMAT_VERSION,
            "calculation_version": fetch_earliest_balances.CALCULATION_VERSION,
            "transaction_snapshot_id": "sha256:G1",
            "scopes": {"10": {"filer_ids": ["10"]}},
        },
        fetch_earliest_balances._pipeline_transaction_snapshot_id(),
    ) is False


def test_aggregate_cache_database_failure_is_not_hidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(process, "AGG_DIR", tmp_path)
    monkeypatch.setattr(
        process.supabase_sync,
        "upsert_dashboard_cache",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        process._write_json("summary.json", {"ok": True})
    assert json.loads((tmp_path / "summary.json").read_text()) == {"ok": True}
