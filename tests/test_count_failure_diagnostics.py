"""Count-poll diagnostics preserve unknown results and the existing request budget."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))
import diff_coverage as DC
import survey_coverage as SC
from search_budget import ENVIRONMENT_KEY, SearchBudget, SearchBudgetExceeded

START = date(2022, 12, 7)
END = date(2023, 5, 1)
RESULT_URL = (
    "https://private-user:private-password@secure.sos.state.or.us/orestar;"
    "JSESSIONID_ORESTAR=private-session/gotoPublicTransactionSearchResults.do;"
    "route=private-route?OWASP_CSRFTOKEN=private-csrf#private-fragment"
)


class CountPage:
    def __init__(self, clock, bodies, *, result_url=RESULT_URL):
        self.clock = clock
        self.bodies = bodies
        self.result_url = result_url
        self.url = SC.F.SEARCH_URL
        self.searches = 0
        self.read_times = []
        self.waits = []
        self.body_timeouts = []

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_selector(self, *_args, **_kwargs):
        pass

    def fill(self, *_args, **_kwargs):
        pass

    def select_option(self, *_args, **_kwargs):
        pass

    def click(self, *_args, **_kwargs):
        self.searches += 1
        self.url = self.result_url

    def wait_for_url(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock["now"] += milliseconds / 1000

    def inner_text(self, selector, **kwargs):
        assert selector == "body"
        self.read_times.append(self.clock["now"])
        self.body_timeouts.append(kwargs.get("timeout"))
        if callable(self.bodies):
            return self.bodies(self)
        return self.bodies[min(len(self.read_times) - 1, len(self.bodies) - 1)]


@pytest.fixture
def clock(monkeypatch):
    value = {"now": 0.0}
    monkeypatch.setattr(SC.time, "monotonic", lambda: value["now"])
    return value


def diagnostics(caplog):
    prefix = "COUNT_READ_EXHAUSTED "
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.name == SC.__name__ and record.getMessage().startswith(prefix)]


def test_budget_stops_before_46th_search_click(monkeypatch, tmp_path, clock):
    budget = SearchBudget.initialize(tmp_path / "budget.json")
    monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
    monkeypatch.setattr(SC, "_return_to_form", lambda *_args: None)
    page = CountPage(clock, ["1 records found"])
    for _ in range(45):
        assert SC.orestar_count(page, "10", START, END) == 1
    with pytest.raises(SearchBudgetExceeded):
        SC.orestar_count(page, "10", START, END)
    assert page.searches == 45
    assert budget.used == 45


def test_ambiguous_click_failure_consumes_budget(monkeypatch, tmp_path, clock):
    budget = SearchBudget.initialize(tmp_path / "budget.json")
    monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
    monkeypatch.setattr(SC, "_return_to_form", lambda *_args: None)
    page = CountPage(clock, ["1 records found"])
    def failed_click(*_args, **_kwargs):
        raise SC.PlaywrightTimeout("submission timed out")
    page.click = failed_click
    with SC.measure_search_submissions() as measured:
        with pytest.raises(SC.PlaywrightTimeout):
            SC.orestar_count(page, "10", START, END)
    assert measured.count == 1
    assert budget.used == 1


def test_submission_measurement_without_budget_does_not_limit_queries(monkeypatch, clock):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    page = CountPage(clock, ["1 records found"])
    with SC.measure_search_submissions() as measured:
        for _ in range(51):
            assert SC.orestar_count(page, "10", START, END) == 1
    assert page.searches == measured.count == 51
    with SC.measure_search_submissions() as next_collection:
        assert SC.orestar_count(page, "20", START, END) == 1
    assert next_collection.count == 1
    assert measured.count == 51


def test_budget_refusal_never_counts_a_submission(monkeypatch, tmp_path, clock):
    budget = SearchBudget.initialize(tmp_path / "budget.json", 1)
    monkeypatch.setenv(ENVIRONMENT_KEY, str(budget.path))
    page = CountPage(clock, ["1 records found"])
    with SC.measure_search_submissions() as measured:
        assert SC.orestar_count(page, "10", START, END) == 1
        with pytest.raises(SearchBudgetExceeded):
            SC.orestar_count(page, "10", START, END)
    assert page.searches == measured.count == budget.used == 1


def test_deadline_before_click_is_not_counted(monkeypatch, clock):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    page = CountPage(clock, ["1 records found"])
    with SC.measure_search_submissions() as measured:
        with pytest.raises(SC.SearchDeadlineExceeded):
            SC.orestar_count(page, "10", START, END, deadline=0.1)
    assert page.searches == measured.count == 0


def test_unknown_count_logs_metadata_without_another_read_or_search(clock, caplog):
    caplog.set_level(logging.WARNING)
    body = "Unexpected results template private-body-secret"
    page = CountPage(clock, [body])

    with SC.measure_search_submissions() as measured:
        assert SC.orestar_count(page, "33", START, END, "C", "5", "9.99", "J") is None

    [event] = diagnostics(caplog)
    assert event == {
        "filer_id": "33",
        "window": {"tran_type": "C", "start": "2022-12-07", "end": "2023-05-01",
                   "amt_from": "5", "amt_to": "9.99", "payee_prefix": "J"},
        "poll_elapsed_seconds": 20.0,
        "url_host": "secure.sos.state.or.us",
        "url_path": "/orestar/gotoPublicTransactionSearchResults.do",
        "body_length": len(body),
        "challenge_signal": False,
        "maintenance_signal": False,
        "server_error_signal": False,
    }
    assert page.searches == 1
    assert measured.count == 1
    assert len(page.read_times) == 40
    assert page.waits == [250, 400] + [500] * 40
    assert clock["now"] == pytest.approx(20.65)
    assert "private-" not in caplog.text
    assert "OWASP_CSRFTOKEN" not in caplog.text
    assert "JSESSIONID" not in caplog.text


@pytest.mark.parametrize("final_body, expected", [
    ("1,234 records found", 1234), ("No records found", 0),
])
def test_late_real_answer_still_returns_without_failure_diagnostics(
    clock, caplog, final_body, expected,
):
    page = CountPage(clock, ["Loading"] * 39 + [final_body])
    assert SC.orestar_count(page, "33", START, END) == expected
    assert page.searches == 1
    assert len(page.read_times) == 40
    assert page.waits == [250] + [500] * 39
    assert clock["now"] == 19.75
    assert diagnostics(caplog) == []


@pytest.mark.parametrize("body, signals", [
    ("Checking your browser. private-challenge-token", (True, False, False)),
    ("The requested URL was rejected. Your support ID is private-support-token", (True, False, False)),
    ("Scheduled maintenance. Please check back later.", (False, True, False)),
    ("HTTP Status 500 - Internal Server Error private-stack-token", (False, False, True)),
    ("503 Service Unavailable", (False, False, True)),
    ("HTTP Status: 502 private-stack-token", (False, False, True)),
    ("Gateway Timeout", (False, False, True)),
    ("F5 Error Challenge Maintenance PAC $500.00", (False, False, False)),
    ("", (False, False, False)),
])
def test_recognized_signals_do_not_reclassify_unknown(clock, caplog, body, signals):
    page = CountPage(clock, [body])
    assert SC.orestar_count(page, "33", START, END) is None
    [event] = diagnostics(caplog)
    assert tuple(event[name] for name in (
        "challenge_signal", "maintenance_signal", "server_error_signal",
    )) == signals
    assert event["body_length"] == len(body)
    assert page.searches == 1
    assert len(page.read_times) == 40
    assert "private-" not in caplog.text


@pytest.mark.parametrize("url, host, path", [
    (RESULT_URL, "secure.sos.state.or.us", "/orestar/gotoPublicTransactionSearchResults.do"),
    ("https://secure.sos.state.or.us/orestar%3bjsessionid=private-one/results.do"
     "%253BJSESSIONID=private-two?token=private-three#private-four",
     "secure.sos.state.or.us", "/orestar/results.do"),
    ("https://secure.sos.state.or.us/orestar;JSESSIONID=private-one%2fprivate-two/results.do",
     "secure.sos.state.or.us", "/orestar/results.do"),
    ("https://secure.sos.state.or.us/results.do%3fcsrf=private-one%23private-two",
     "secure.sos.state.or.us", "/results.do"),
    ("https://secure.sos.state.or.us/results.do%253fOWASP_CSRFTOKEN=private-query",
     "secure.sos.state.or.us", "/results.do"),
    ("https://secure.sos.state.or.us/results.do%25253bJSESSIONID=private-session",
     "secure.sos.state.or.us", "/results.do"),
    ("https://secure.sos.state.or.us/results.do%2523private-fragment",
     "secure.sos.state.or.us", "/results.do"),
    ("https:private-user:private-password@secure.sos.state.or.us/results.do",
     "unknown", "unknown"),
    ("https:///private-user:private-password@secure.sos.state.or.us/results.do",
     "unknown", "unknown"),
    ("//secure.sos.state.or.us/private-path", "unknown", "unknown"),
    ("ftp://secure.sos.state.or.us/private-path", "unknown", "unknown"),
    ("https://secure.sos.state.or.us:private-port/private-path", "unknown", "unknown"),
    ("https://[private-broken-host/path?private-query", "unknown", "unknown"),
    ("//private-user:private-password@secure.sos.state.or.us/results.do",
     "unknown", "unknown"),
    ("https://secure.sos.state.or.us/results.do%252fprivate-session",
     "secure.sos.state.or.us", "unknown"),
])
def test_url_metadata_strips_session_and_query_material(url, host, path):
    event = SC._count_failure_page_diagnostics(url, "")
    assert event["url_host"] == host
    assert event["url_path"] == path
    assert "private-" not in json.dumps(event)


def test_absolute_deadline_preserves_exception_and_does_not_read_for_diagnostics(clock, caplog):
    page = CountPage(clock, ["Checking your browser private-body"])
    with pytest.raises(SC.SearchDeadlineExceeded):
        SC.orestar_count(page, "33", START, END, deadline=2.25)
    assert page.searches == 1
    assert page.read_times == [0.25, 0.75, 1.25, 1.75]
    assert page.body_timeouts == [2000, 1500, 1000, 500]
    assert clock["now"] == 2.25
    assert diagnostics(caplog) == []
    assert "private-" not in caplog.text


def test_unreadable_child_still_discards_previously_collected_scope_rows(
    monkeypatch, clock, caplog,
):
    # The actual shared reader answers the root, one complete leaf, then fails
    # the remaining leaf. The real identity tree must discard the partial ID.
    page = CountPage(clock, lambda page: (
        "2 records found" if page.searches == 1 else
        "1 records found" if page.searches == 2 else
        "Unexpected template private-body"
    ))
    monkeypatch.setattr(DC.F, "ORESTAR_ROW_CAP", 1)
    monkeypatch.setattr(DC, "_order_children_by_local_cost", lambda _fid, children: children)
    exported = []

    def export(*_args):
        exported.append("first-leaf-id")
        return {"first-leaf-id": {}}

    monkeypatch.setattr(DC, "_export_rows", export)
    split = date(2022, 12, 8)
    result = DC.orestar_ids(page, "33", START, END, context=object(),
                            seed_windows=[(START, START), (split, END)])
    assert result is None
    assert exported == ["first-leaf-id"]
    assert page.searches == 3
    assert len(page.read_times) == 42
    [event] = diagnostics(caplog)
    assert event["window"]["start"] == "2022-12-08"
    assert event["window"]["end"] == "2023-05-01"
    assert "private-" not in caplog.text
