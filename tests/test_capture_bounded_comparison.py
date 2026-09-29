"""The capture-bounded view of a yearly discrepancy.

ORESTAR's account summary states what had been FILED when it was read. Our
side keeps accruing filings after that. Comparing the two without bounding
attributes ordinary timing to missing data — which is how several committees
came to look like ORESTAR contradicting its own itemised transactions when
their rows had simply been filed that afternoon.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import process as P  # noqa: E402


def _at(iso: str) -> dict:
    return {"captured_at": datetime.fromisoformat(iso).timestamp()}


def test_capture_date_is_oregon_local_not_utc() -> None:
    """The bug that started this: 01:38 UTC is the PREVIOUS day in Oregon.

    Balance jobs run between 00:00 and 06:00 UTC, so a naive UTC date makes
    every filing made that Oregon day look as though it preceded the capture.
    Oregon Hospital PAC's eight rows filed 2026-09-14 summed to exactly the
    $31,128.00 gap against a capture stamped 2026-09-14 01:38 UTC.
    """
    day = P._capture_local_date(_at("2026-09-14T01:38:54+00:00"))
    assert day.isoformat() == "2026-09-13"


def test_capture_date_handles_a_daytime_pacific_capture() -> None:
    """Not every capture is overnight; 21:00 UTC is the same Oregon day."""
    day = P._capture_local_date(_at("2026-09-14T21:00:00+00:00"))
    assert day.isoformat() == "2026-09-14"


def test_capture_date_is_none_without_a_usable_instant() -> None:
    """No capture, no bound. Never silently defaults to 'now' or to today."""
    assert P._capture_local_date(None) is None
    assert P._capture_local_date({}) is None
    assert P._capture_local_date({"captured_at": None}) is None
    assert P._capture_local_date({"captured_at": "not-a-number"}) is None


def test_capture_date_survives_an_absurd_timestamp() -> None:
    """A corrupt epoch yields no bound rather than raising mid-aggregation."""
    assert P._capture_local_date({"captured_at": 1e30}) is None


def test_dst_boundary_uses_the_offset_in_force_that_day() -> None:
    """A fixed -7 or -8 offset would be wrong for half the year.

    January is PST (-8), so 05:00 UTC is the previous day; July is PDT (-7),
    so the same clock time is also the previous day but by a different offset.
    Using ZoneInfo rather than a constant is what keeps both correct.
    """
    assert P._capture_local_date(_at("2026-01-15T05:00:00+00:00")).isoformat() == "2026-01-14"
    assert P._capture_local_date(_at("2026-07-15T05:00:00+00:00")).isoformat() == "2026-07-14"
    # 08:00 UTC in January is 00:00 PST — the same day, just.
    assert P._capture_local_date(_at("2026-01-15T08:00:00+00:00")).isoformat() == "2026-01-15"
