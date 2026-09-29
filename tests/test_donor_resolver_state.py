"""State-source contracts for destructive donor re-resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scraper"))

import resolve_donors  # noqa: E402


class _Cursor:
    def execute(self, _query):
        return None

    def fetchall(self):
        return [("123", "Friends of Example")]


class _Connection:
    def cursor(self):
        return _Cursor()


def test_committee_namespace_uses_database_when_generated_index_is_absent(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(resolve_donors, "FILER_INDEX", tmp_path / "missing.json")
    monkeypatch.setattr(
        resolve_donors.sb,
        "require_dashboard_cache",
        lambda key: [{
            "filer_id": "123",
            "slug": "friends-of-example",
            "name": "Friends of Example",
            "candidate_name": "Example, Jane",
        }],
    )

    namespace = resolve_donors.load_committee_namespace(_Connection())

    assert namespace["by_id"]["123"] == {
        "slug": "friends-of-example",
        "name": "Friends of Example",
    }
    assert namespace["by_norm"][resolve_donors.norm_name("Friends of Example")] == "123"
    assert namespace["candidate_by_norm"][resolve_donors.norm_name("Example, Jane")] == (
        "friends-of-example"
    )


def test_committee_namespace_refuses_empty_database_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(resolve_donors, "FILER_INDEX", tmp_path / "missing.json")
    monkeypatch.setattr(
        resolve_donors.sb, "require_dashboard_cache", lambda _key: []
    )

    try:
        resolve_donors.load_committee_namespace(_Connection())
    except RuntimeError as exc:
        assert "empty or malformed" in str(exc)
    else:
        raise AssertionError("empty filer_index must fail before donor-table replacement")


def test_committee_namespace_prefers_database_when_local_index_is_partial(
    tmp_path, monkeypatch,
) -> None:
    local = tmp_path / "partial.json"
    local.write_text('[{"filer_id":"999","slug":"partial","name":"Partial"}]')
    monkeypatch.setattr(resolve_donors, "FILER_INDEX", local)
    monkeypatch.setattr(resolve_donors.sb, "sync_enabled", lambda: True)
    monkeypatch.setattr(
        resolve_donors.sb,
        "require_dashboard_cache",
        lambda key: [{
            "filer_id": "123",
            "slug": "current",
            "name": "Current Committee",
        }],
    )

    namespace = resolve_donors.load_committee_namespace(_Connection())

    assert set(namespace["by_id"]) == {"123"}


def test_review_queue_database_failure_is_not_hidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(resolve_donors, "REVIEW_QUEUE", tmp_path / "queue.json")
    monkeypatch.setattr(
        resolve_donors.sb,
        "upsert_dashboard_cache",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        resolve_donors.write_review_queue([])
