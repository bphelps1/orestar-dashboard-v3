"""Transactions ORESTAR deleted after we fetched them.

Deleting a transaction files a separate record: status "Deleted", a Tran ID of
its own, the filing date of the deletion, and an Original Id naming the row it
deletes. ORESTAR's default search hides these records, so for years none
reached us and every deleted row stayed counted. ChamberPAC's 2026
contributions are the verified case: $10,000 from Sellwood Ventures, 5697866,
deleted by 5768551 on 08/18/2026, left us $10,000 above ORESTAR. The fixtures
below copy that export (fetched with "view deleted transactions" ticked). No
browser or database is used.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import fetch as F  # noqa: E402
import pipeline_state  # noqa: E402
import process as P  # noqa: E402


@pytest.fixture(autouse=True)
def _deletion_log(tmp_path, monkeypatch):
    """Every test writes its deletion record to a temporary file, never data/."""
    path = tmp_path / "orestar_deletions.json"
    monkeypatch.setattr(P, "DELETIONS_PATH", path)
    return path


def _row(tran_id, status, amount, filed, *, original=None, tran_date="06/22/2026",
         payee="Sellwood Ventures, LLC", tran_type="C", filer="5067"):
    return {
        "tran_id": tran_id, "original id": original or tran_id, "tran status": status,
        "tran_date": tran_date, "filed_date": filed, "amount": amount,
        "contributor_payee": payee, "sub_type": "Cash Contribution",
        "tran_type": tran_type, "filer id": filer, "filer": "ChamberPAC",
    }


def _chamberpac(dates: bool = False) -> pd.DataFrame:
    """Our copy after the merge: the stale original plus the fetched deletion."""
    def f(iso):
        return dt.date.fromisoformat(iso) if dates else iso
    return pd.DataFrame([
        _row("5697471", "Original", 250.0, f("2026-06-22"), payee="Laura A Hansen"),
        _row("5697866", "Original", 10000.0, f("2026-07-22")),
        _row("5699117", "Original", 10000.0, f("2026-06-24")),
        _row("5768551", "Deleted", 10000.0, f("2026-08-18"), original="5697866"),
    ])


def test_a_deletion_removes_the_row_it_names_and_nothing_else() -> None:
    df, removed, entries = P._apply_deletions(_chamberpac())

    assert sorted(df["tran_id"]) == ["5697471", "5699117"]
    assert removed == {"5697866"}                  # the deletion record never reached storage
    (entry,) = entries
    assert entry["deletion_id"] == "5768551" and entry["original_id"] == "5697866"
    (kept,) = entry["removed"]
    # The removed row is kept in full, every column.
    assert kept["tran_id"] == "5697866" and kept["amount"] == "10000.0"
    assert kept["contributor_payee"] == "Sellwood Ventures, LLC"
    assert entry["deletion"]["filed_date"] == "2026-08-18"


def test_both_merge_paths_date_forms_order_the_same_way() -> None:
    # process() carries datetime.date objects; the merge-only path, strings.
    for dates in (False, True):
        df, removed, _ = P._apply_deletions(_chamberpac(dates))
        assert removed == {"5697866"}


def test_a_deleted_chain_loses_its_amendments_too() -> None:
    # Rule 2 keeps the newest amendment of a chain; a deleted chain keeps none.
    df = pd.DataFrame([
        _row("100", "Original", 50.0, "2026-01-05"),
        _row("200", "Amended", 55.0, "2026-02-01", original="100"),
        _row("300", "Deleted", 55.0, "2026-03-01", original="100"),
    ])
    out, removed = P._drop_superseded(df)
    assert out.empty
    assert removed == {"100", "200"}


def test_a_later_amendment_outranks_an_older_deletion() -> None:
    df = pd.DataFrame([
        _row("100", "Original", 50.0, "2026-01-05"),
        _row("300", "Deleted", 50.0, "2026-02-01", original="100"),
        _row("400", "Amended", 60.0, "2026-03-01", original="100"),
    ])
    out, removed, entries = P._apply_deletions(df)
    assert sorted(out["tran_id"]) == ["100", "400"]      # rule 1 retires 100 next
    assert entries[0]["outranked_by_later_version"] is True
    assert entries[0]["removed"] == []
    final, _ = P._drop_superseded(df)
    assert list(final["tran_id"]) == ["400"]


def test_an_original_orestar_still_returns_is_kept() -> None:
    out, removed, entries = P._apply_deletions(_chamberpac(), keep_live={"5697866"})
    assert "5697866" in set(out["tran_id"]) and "5697866" not in removed
    assert entries[0]["kept_live"] == ["5697866"]


def test_a_deletion_of_a_row_we_never_held_changes_nothing_else() -> None:
    df = pd.DataFrame([
        _row("5699117", "Original", 10000.0, "2026-06-24"),
        _row("5814405", "Deleted", 150.0, "2026-09-17", original="5808066",
             payee="Anthony Martin"),
    ])
    out, removed, entries = P._apply_deletions(df)
    assert list(out["tran_id"]) == ["5699117"]
    assert removed == set() and entries[0]["removed"] == []


def test_frames_without_deletions_are_untouched() -> None:
    df = _chamberpac()[lambda d: d["tran status"] != "Deleted"]
    out, removed, entries = P._apply_deletions(df)
    assert out.equals(df) and removed == set() and entries == []


def test_drop_superseded_records_what_it_removed(_deletion_log) -> None:
    path = _deletion_log

    out, removed = P._drop_superseded(_chamberpac())
    assert removed == {"5697866"}
    (entry,) = json.loads(path.read_text())
    assert entry["removed"][0]["tran_id"] == "5697866" and entry["first_seen"]

    # The deletion arrives again after its row is gone: the record keeps the
    # row it removed the first time.
    again = _chamberpac()[lambda d: d["tran_id"] != "5697866"]
    P._drop_superseded(again)
    (entry,) = json.loads(path.read_text())
    assert [r["tran_id"] for r in entry["removed"]] == ["5697866"]


def test_no_deletions_writes_nothing(_deletion_log) -> None:
    path = _deletion_log
    P._drop_superseded(_chamberpac()[lambda d: d["tran status"] != "Deleted"])
    assert not path.exists()


def test_committees_list_each_deletion_by_both_ids(_deletion_log) -> None:
    path = _deletion_log
    P._drop_superseded(_chamberpac())

    shown = P._deletions_by_filer_id()
    (item,) = shown["5067"]
    assert item == {
        "deletion_id": "5768551", "deleted_on": "2026-08-18",
        "removed_ids": ["5697866"], "filer_id": "5067",
        "tran_date": "2026-06-22", "year": 2026, "tran_type": "C",
        "sub_type": "Cash Contribution", "contributor_payee": "Sellwood Ventures, LLC",
        "amount": 10000.0,
    }


def test_deletions_that_removed_nothing_are_not_shown(_deletion_log) -> None:
    path = _deletion_log
    P._drop_superseded(pd.DataFrame([
        _row("5814405", "Deleted", 150.0, "2026-09-17", original="5808066"),
    ]))
    assert path.exists() and P._deletions_by_filer_id() == {}


def test_every_search_asks_for_deletion_records() -> None:
    for fn in (F.download_week, F.download_filer_window):
        assert "_include_deleted(page)" in inspect.getsource(fn), fn.__name__

    class Page:
        checked = []

        def check(self, selector, timeout=None):
            self.checked.append(selector)

    F._include_deleted(Page())
    assert Page.checked == ['input[name="viewDeletedTransactions"]']

    class Broken:
        def check(self, *a, **k):
            raise RuntimeError("no such checkbox")

    F._include_deleted(Broken())                   # a missing option never fails a fetch


def test_held_counts_include_applied_deletion_records(_deletion_log, monkeypatch) -> None:
    # ORESTAR's count for a window now includes its deletion records; the merge
    # never stores them, so the "already held" check adds the applied ones.
    path = _deletion_log
    df = _chamberpac()
    df["tran_date"] = "06/22/2026"
    P._drop_superseded(df)
    monkeypatch.setattr(F, "DELETIONS_LOG", path)
    monkeypatch.setattr(F, "_DELETIONS_CACHE", None)

    june = (dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    assert F._recorded_deletions("5067", "C", *june, None, None, None) == 1
    assert F._recorded_deletions("5067", "ALL", *june, None, None, None) == 1
    assert F._recorded_deletions("5067", "E", *june, None, None, None) == 0
    assert F._recorded_deletions("9999", "C", *june, None, None, None) == 0
    assert F._recorded_deletions("5067", "C", dt.date(2026, 7, 1),
                                 dt.date(2026, 7, 31), None, None, None) == 0
    assert F._recorded_deletions("5067", "C", *june, "5000", None, None) == 1
    assert F._recorded_deletions("5067", "C", *june, None, "9999.99", None) == 0
    assert F._recorded_deletions("5067", "C", *june, None, None, "Sellwood") == 1
    assert F._recorded_deletions("5067", "C", *june, None, None, "Misc") == 0


def test_the_deletion_record_travels_with_the_shards() -> None:
    assert "data/orestar_deletions.json" in pipeline_state.PROFILE_FILES["transactions"]
