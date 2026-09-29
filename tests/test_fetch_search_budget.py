"""Fetch and exact collectors share real durable submission accounting offline."""
from __future__ import annotations

import io
import sys
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import fetch as F
import survey_coverage as SC
from search_budget import ENVIRONMENT_KEY, SearchBudget, SearchBudgetError, SearchBudgetExceeded

START, END = date(2006, 1, 1), date(2026, 9, 15)


class Page:
    def __init__(self, texts=None, budget=None, click_error=None):
        self.url = F.BASE_URL + "/gotoPublicTransactionSearchResults.do"
        self.texts = list(texts or ["No records found"])
        self.budget = budget
        self.click_error = click_error
        self.clicks = 0
        self.reads = 0
        self.used_at_click = []
        self.on_fill = None

    def fill(self, *_args, **_kwargs):
        if self.on_fill:
            action, self.on_fill = self.on_fill, None
            action()

    def select_option(self, *_args, **_kwargs): pass
    def wait_for_timeout(self, *_args, **_kwargs): pass
    def wait_for_url(self, *_args, **_kwargs): pass

    def click(self, selector, **_kwargs):
        assert selector == 'input[name="search"]'
        self.clicks += 1
        if self.budget:
            self.used_at_click.append(self.budget.used)
        if self.click_error:
            raise self.click_error

    def inner_text(self, *_args, **_kwargs):
        self.reads += 1
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    def evaluate(self, *_args, **_kwargs): return "offline-csrf-token"


CONTEXT = SimpleNamespace(cookies=lambda: [])


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    monkeypatch.setattr(F, "_return_to_search", lambda *_: None)
    monkeypatch.setattr(SC, "_return_to_form", lambda *_: None)
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_kw: pytest.fail("unexpected export"))
    monkeypatch.setattr(F, "_held_rows", lambda *_: None)
    monkeypatch.setattr(F, "_prior_counts", lambda: {})
    monkeypatch.setattr(F, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(F, "FETCHED_LOG", tmp_path / "fetched.json")
    monkeypatch.setattr(F, "FETCHED_LOG_TRN", tmp_path / "fetched-tran.json")
    monkeypatch.setattr(F, "IDENTITY_PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(F, "IDENTITY_FAILURE_FILE", tmp_path / "failures.json")
    monkeypatch.setattr(F, "RECORD_COUNTS", {})


def configure(tmp_path, monkeypatch, limit=45):
    budget = SearchBudget.initialize(tmp_path / "budget.json", limit)
    monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
    return budget


def download(mode, page, raw, **kwargs):
    if mode == "filer":
        return F.download_filer_window(page, CONTEXT, "5667", START, END, raw, **kwargs)
    return F.download_week(page, CONTEXT, START, END, "C", raw, **kwargs)


def workbook_bytes():
    wb = Workbook()
    wb.active.append(["tran_id", "amount"])
    wb.active.append(["100", 10])
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


@pytest.mark.parametrize("mode", ["week", "filer"])
def test_actual_submission_reserved_before_click_and_export_adds_nothing(mode, tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch)
    page = Page(["1 records found"], budget)
    exports = []

    def export(*_args, **_kwargs):
        exports.append(True)
        return SimpleNamespace(headers={"Content-Type": "application/octet-stream"}, content=workbook_bytes())

    monkeypatch.setattr(F.requests, "get", export)
    result = download(mode, page, F.RAW_DIR)
    assert result.is_file() and F._validate_download(result) == 1
    assert page.used_at_click == [1] and len(exports) == 1 and budget.used == 1
    item = budget.state()["submissions"][0]
    assert item["filer_id"] == ("5667" if mode == "filer" else "ALL")
    assert item["window"]["collector"] == "fetch"
    assert item["window"]["date_field"] == ("tran" if mode == "filer" else "filed")


def test_targeted_root_children_and_repeat_each_consume_without_charging_polls(tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch)
    page = Page(["Loading", "Still loading", "5,500 records found"], budget)
    assert download("filer", page, F.RAW_DIR, force=True) is F.CAPPED
    assert page.reads == 3 and budget.used == 1
    page.texts = ["No records found"]
    for _ in range(2):
        assert download("filer", page, F.RAW_DIR, force=True, tran_type="E",
                        amt_from="0", amt_to="100", payee_prefix="A") is F.EMPTY
    assert page.used_at_click == [1, 2, 3]
    windows = [row["window"] for row in budget.state()["submissions"]]
    assert windows[0]["tran_type"] == "ALL"
    assert windows[1] == windows[2]
    assert (windows[1]["tran_type"], windows[1]["amt_from"], windows[1]["amt_to"],
            windows[1]["payee_prefix"]) == ("E", "0", "100", "A")


def test_fetch_then_exact_share_the_same_ledger(tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch, 2)
    page = Page(budget=budget)
    assert download("filer", page, F.RAW_DIR) is F.EMPTY
    assert SC._orestar_count(page, "5667", START, END) == 0
    assert page.used_at_click == [1, 2]
    with pytest.raises(SearchBudgetExceeded):
        SC._orestar_count(page, "5667", START, END)
    with pytest.raises(SearchBudgetExceeded):
        download("filer", page, F.RAW_DIR)
    assert page.clicks == budget.used == 2


@pytest.mark.parametrize("mode", ["week", "filer"])
def test_46th_submission_refused_before_click(mode, tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch)
    page = Page(budget=budget)
    # A missing export token leaves no cache file, so each real date search
    # remains a distinct attempted submission even when repeated verbatim.
    page.evaluate = lambda *_a, **_kw: None
    for _ in range(45):
        download(mode, page, F.RAW_DIR)
    with pytest.raises(SearchBudgetExceeded):
        download(mode, page, F.RAW_DIR)
    assert page.clicks == budget.used == 45
    assert page.used_at_click == list(range(1, 46))


@pytest.mark.parametrize("mode", ["week", "filer"])
@pytest.mark.parametrize("config", ["empty", "missing", "corrupt", "invalid"])
def test_configured_bad_ledger_refuses_before_interaction(mode, config, tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    if config == "corrupt": path.write_text("{")
    if config == "invalid": path.write_text("{}")
    monkeypatch.setenv(ENVIRONMENT_KEY, "" if config == "empty" else str(path))
    page = Page()
    page.on_fill = lambda: pytest.fail("form touched before configuration refusal")
    with pytest.raises(SearchBudgetError): download(mode, page, F.RAW_DIR)
    assert page.clicks == 0 and not F.RAW_DIR.exists()


@pytest.mark.parametrize("mode", ["week", "filer"])
def test_ledger_corruption_during_form_fill_propagates_without_session_retry(mode, tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch)
    page = Page()
    page.on_fill = lambda: budget.path.write_text("{")
    page.url = "https://example.invalid/redirect"
    with pytest.raises(SearchBudgetError): download(mode, page, F.RAW_DIR)
    assert page.clicks == 0 and budget.path.read_text() == "{"


@pytest.mark.parametrize("mode", ["week", "filer"])
def test_ambiguous_failed_click_and_repeated_attempt_keep_both_reservations(mode, tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch, 2)
    page = Page(budget=budget, click_error=F.PlaywrightTimeout("ambiguous submission"))
    assert download(mode, page, F.RAW_DIR) is None
    assert download(mode, page, F.RAW_DIR) is None
    with pytest.raises(SearchBudgetExceeded): download(mode, page, F.RAW_DIR)
    assert page.used_at_click == [1, 2] and budget.used == 2


def test_unconfigured_fetch_remains_unlimited(tmp_path):
    page = Page()
    for _ in range(46): assert download("filer", page, F.RAW_DIR) is F.EMPTY
    assert page.clicks == 46 and not (tmp_path / "budget.json").exists()


@pytest.mark.parametrize("mode", ["week", "filer"])
def test_valid_cached_export_does_not_consume_an_exhausted_budget(mode, tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch, 1)
    budget.consume("previous", {})
    F.RAW_DIR.mkdir()
    path = (F._filer_window_path(F.RAW_DIR, "5667", "ALL", START, END, None, None, None)
            if mode == "filer" else F.RAW_DIR / f"C_{START}_{END}.xlsx")
    path.write_bytes(workbook_bytes())
    page = Page()
    assert download(mode, page, F.RAW_DIR) == path
    assert page.clicks == 0 and budget.used == 1


def install_driver(monkeypatch, pages):
    setups = []
    browser = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(F, "sync_playwright", lambda: nullcontext(object()))

    def setup(*_args):
        page = pages[min(len(setups), len(pages) - 1)]
        setups.append(page)
        return browser, CONTEXT, page

    monkeypatch.setattr(F, "setup_browser", setup)
    monkeypatch.setattr(F, "setup_browser_retrying", setup)
    return setups


def test_budget_exhaustion_in_session_retry_escapes_backfill_driver(tmp_path, monkeypatch, capsys):
    budget = configure(tmp_path, monkeypatch, 1)
    first = Page(budget=budget, click_error=F.SessionExpiredError("after submission"))
    second = Page(budget=budget)
    setups = install_driver(monkeypatch, [first, second])
    with pytest.raises(SearchBudgetExceeded):
        F.backfill_filers(["5667"], end_date=END, identity_remediation=True)
    assert len(setups) == 2 and first.clicks == 1 and second.clicks == 0
    assert budget.used == 1
    assert "REMEDIATION_RESULT" not in capsys.readouterr().out
    assert not (tmp_path / "completed_backfills.txt").exists()


def test_driver_root_and_child_use_same_budget_and_stop_before_next_child(tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch, 2)
    page = Page(budget=budget)
    click = page.click

    def submit(*args, **kwargs):
        click(*args, **kwargs)
        page.texts = ["5,001 records found" if page.clicks == 1 else "No records found"]

    page.click = submit
    setups = install_driver(monkeypatch, [page])
    with pytest.raises(SearchBudgetExceeded):
        F.backfill_filers(["5667"], end_date=END, identity_remediation=True)
    assert len(setups) == 1 and page.used_at_click == [1, 2]
    assert [item["window"]["tran_type"] for item in budget.state()["submissions"]] == ["ALL", "C"]
    assert not (tmp_path / "completed_backfills.txt").exists()


def test_existing_session_retry_can_succeed_without_refunding_failed_click(tmp_path, monkeypatch):
    budget = configure(tmp_path, monkeypatch, 2)
    first = Page(budget=budget, click_error=F.SessionExpiredError("after submission"))
    second = Page(budget=budget)
    setups = install_driver(monkeypatch, [first, second])
    F.backfill_filers(["5667"], end_date=END, identity_remediation=True)
    assert len(setups) == 2 and first.used_at_click == [1] and second.used_at_click == [2]
    assert budget.used == 2
    assert (tmp_path / "completed_backfills.txt").read_text() == "5667\n"


def test_budget_exhaustion_escapes_date_driver_without_browser_retry(tmp_path, monkeypatch, caplog):
    budget = configure(tmp_path, monkeypatch, 1)
    budget.consume("previous", {})
    page = Page()
    setups = install_driver(monkeypatch, [page])
    with pytest.raises(SearchBudgetExceeded): F._fetch_range(END, END)
    assert len(setups) == 1 and page.clicks == 0
    assert "Fetch complete" not in caplog.text


@pytest.mark.parametrize("driver", ["week", "filer"])
def test_invalid_configuration_refuses_driver_before_setup_or_mutation(driver, tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "")
    monkeypatch.setattr(F, "sync_playwright", lambda: pytest.fail("browser setup reached"))
    with pytest.raises(SearchBudgetError):
        if driver == "week": F._fetch_range(END, END)
        else: F.backfill_filers(["5667"], end_date=END, identity_remediation=True)
    assert not F.RAW_DIR.exists()
