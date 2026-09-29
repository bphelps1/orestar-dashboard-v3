"""Re-reading lumped "Miscellaneous" rows, and measuring what it re-prices.

ORESTAR edits lumped "Miscellaneous ... $100 and under" rows in place: the
Tran ID, status and filed date stay the same while the amount changes (Eli for
Portland 5675002: $340 -> $830). A filed-date window is never fetched twice,
so the fetcher re-reads just these rows, and the merge measures every row the
re-read re-prices. No browser or database is used here.
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

import fetch as F  # noqa: E402
import process as P  # noqa: E402


def test_windows_partition_the_range_at_any_size() -> None:
    for days in (7, 28, 91):
        windows = list(F.week_windows(date(2006, 1, 1), date(2026, 9, 19), days))
        assert windows[0][0] == date(2006, 1, 1)
        assert windows[-1][1] == date(2026, 9, 19)
        for (a_start, a_end), (b_start, _) in zip(windows, windows[1:]):
            assert (b_start - a_end).days == 1
            assert (a_end - a_start).days == days - 1


def test_reread_tasks_ask_only_for_lumped_rows() -> None:
    tasks = F._range_tasks(date(2026, 7, 1), date(2026, 9, 19), F.MISC_TYPES,
                           F.MISC_RECENT_WINDOW_DAYS, F.MISC_PREFIX)
    assert {t[5] for t in tasks} == {"Miscellaneous"}
    assert {t[0] for t in tasks} == {"C", "E", "O", "OD", "OR"}
    # The rolling pass stays small: ten searches or so a day.
    recent = F._range_tasks(date(2026, 7, 25), date(2026, 9, 19), F.MISC_TYPES,
                            F.MISC_RECENT_WINDOW_DAYS, F.MISC_PREFIX)
    assert len(recent) <= 15


def test_remaining_count_mirrors_the_fetcher_and_its_own_log(tmp_path, monkeypatch, capsys) -> None:
    log = tmp_path / "fetched_windows_misc.json"
    monkeypatch.setattr(F, "FETCHED_LOG_MISC", log)
    tasks = F._range_tasks(date(2026, 1, 1), date.today(), F.MISC_TYPES,
                           F.MISC_REREAD_WINDOW_DAYS, F.MISC_PREFIX)

    assert F.count_misc_remaining(2026) == len(tasks)
    F._save_fetched({F._task_key(t) for t in tasks[:3]}, log)
    assert F.count_misc_remaining(2026) == len(tasks) - 3
    # Progress lives in its own log, never in the permanent fetch logs.
    assert log not in (F.FETCHED_LOG, F.FETCHED_LOG_TRN)
    capsys.readouterr()


def _offline_fetcher(tmp_path, monkeypatch, clock: dict, seconds_per_window: float):
    """Replace the browser with one that 'downloads' a window per call."""
    calls: list[tuple] = []

    def download_week(page, context, w_start, w_end, tran_type, raw_dir, *rest):
        calls.append((tran_type, w_start, w_end))
        clock["now"] += seconds_per_window
        path = tmp_path / f"{tran_type}_{w_start}.xls"
        path.write_text("x")
        return path

    monkeypatch.setattr(F, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(F, "FETCHED_LOG_MISC", tmp_path / "fetched_windows_misc.json")
    monkeypatch.setattr(F, "sync_playwright", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(F, "setup_browser", lambda p: (_Closable(), None, None))
    monkeypatch.setattr(F, "download_week", download_week)
    monkeypatch.setattr(F, "_validate_download", lambda path: 10)
    monkeypatch.setattr(F, "_flush_record_counts", lambda: None)
    monkeypatch.setattr(F.time, "monotonic", lambda: clock["now"])
    monkeypatch.delenv("ORESTAR_SEARCH_BUDGET_PATH", raising=False)
    return calls


class _Closable:
    def close(self) -> None:
        pass


def test_a_run_stops_at_its_budget_and_the_next_resumes(tmp_path, monkeypatch, capsys) -> None:
    # Without a budget, a run ORESTAR doesn't block reads until the job timeout
    # kills it, and a killed job never merges or publishes what it read.
    clock = {"now": 0.0}
    calls = _offline_fetcher(tmp_path, monkeypatch, clock, seconds_per_window=60)
    tasks = F._range_tasks(date(2026, 1, 1), date.today(), F.MISC_TYPES,
                           F.MISC_REREAD_WINDOW_DAYS, F.MISC_PREFIX)
    assert len(tasks) > 3

    F.run_misc_reread(2026, max_minutes=2.5)        # windows start at 0s, 60s, 120s
    assert len(calls) == 3
    assert F.count_misc_remaining(2026) == len(tasks) - 3   # all three kept

    F.run_misc_reread(2026, max_minutes=1)          # the next run picks up at task 4
    assert calls[3] == (tasks[3][0], tasks[3][1], tasks[3][2])
    assert F.count_misc_remaining(2026) == len(tasks) - 4
    capsys.readouterr()


def test_the_history_reread_is_budgeted_by_default(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(F, "run_misc_reread",
                        lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--mode=misc-reread"])
    F.main()
    assert seen["max_minutes"] == F.MISC_REREAD_MAX_MINUTES > 0

    monkeypatch.setattr(sys, "argv", ["fetch.py", "--mode=misc-reread", "--max-minutes=150"])
    F.main()
    assert seen["max_minutes"] == 150


def test_the_workflow_passes_a_budget_its_timeout_can_hold() -> None:
    text = (Path(__file__).parent.parent / ".github/workflows/misc-reread.yml").read_text()
    budget = int(text.split("--max-minutes=")[1].split()[0])
    timeout = int(text.split("timeout-minutes: ")[1].split()[0])
    # 25 minutes of coordination, 25 of browser install, and the merge and
    # publish must still fit after the fetch spends its whole budget.
    assert budget + 25 + 25 + 30 <= timeout


def test_a_rolling_reread_records_no_progress() -> None:
    F._save_fetched({("C", "2026-01-01", "2026-01-28")}, None)   # no-op, no error


def test_the_merge_measures_rows_a_download_repriced() -> None:
    existing = pd.DataFrame([
        {"tran_id": "5675002", "amount": 340.0},
        {"tran_id": "5686507", "amount": 65.0},
        {"tran_id": "5505204", "amount": 350.0},
    ])
    incoming = pd.DataFrame([
        {"tran_id": "5675002", "amount": "$830.00", "filer id": "23295",
         "contributor_payee": "Miscellaneous Cash Contributions $100 and under"},
        {"tran_id": "5686507", "amount": "215", "filer id": "23295",
         "contributor_payee": "Miscellaneous Cash Contributions $100 and under"},
        {"tran_id": "5505204", "amount": "350.00", "filer id": "23295",
         "contributor_payee": "A Donor"},
        {"tran_id": "9999999", "amount": "25", "filer id": "23295",
         "contributor_payee": "New Row"},
    ])

    result = P._amount_updates(existing, incoming)

    assert result["rows"] == 2
    assert result["miscellaneous_rows"] == 2
    assert result["net"] == 640.0
    assert {r["tran_id"]: (r["old"], r["new"]) for r in result["sample"]} == {
        "5675002": (340.0, 830.0), "5686507": (65.0, 215.0)}


def test_repriced_rows_are_appended_to_the_published_record(tmp_path, monkeypatch) -> None:
    path = tmp_path / "amount_updates.json"
    monkeypatch.setattr(P, "AMOUNT_UPDATES_PATH", path)
    existing = pd.DataFrame([{"tran_id": "1", "amount": 10.0}])

    P._record_amount_updates(existing, pd.DataFrame([{"tran_id": "1", "amount": "10"}]))
    assert not path.exists()                     # nothing re-priced, nothing written

    P._record_amount_updates(existing, pd.DataFrame([{"tran_id": "1", "amount": "12.5"}]))
    P._record_amount_updates(existing, pd.DataFrame([{"tran_id": "1", "amount": "11"}]))
    history = json.loads(path.read_text())
    assert [entry["net"] for entry in history] == [2.5, 1.0]
    assert all(entry["rows"] == 1 and entry["at"] for entry in history)
