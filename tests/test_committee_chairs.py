"""A co-chairship is not a chairship, except of the full Ways and Means.

OLIS gives the full Ways and Means, each of its subcommittees and the
Emergency Board's two co-chairs apiece. Counting them all as committee chairs
listed Emerson Levy as chair of Natural Resources and Paul Evans as chair of
Public Safety, when what they co-chair is a Ways and Means subcommittee.
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("refresh_legislators", ROOT / "scraper" / "refresh_legislators.py")
rl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rl)


def page(*blocks):
    """The shape parse_chairs walks, copied from the OLIS markup: a <strong>
    heading and the member <ul> as siblings inside one <div>."""
    return "".join(
        f'''<div>
              <strong><a href="/liz/2025I1/Committees/{code}/Overview" target="_blank">{name}</a></strong>
              <ul class="no-list-style">{"".join(members)}</ul>
            </div>'''
        for code, name, members in blocks)


def member(title, name, role):
    return f'<li><a href="https://www.oregonlegislature.gov/x" target="_blank">{title} {name}</a> - {role}</li>'


def test_a_chair_of_an_ordinary_committee_counts():
    chairs = rl.parse_chairs(page(("HHC", "Health Care", [member("Representative", "Rob Nosse", "Chair")]),
                                  *_filler(20)))
    assert {"chamber": "house", "name": "Rob Nosse", "committees": ["Health Care"]} in chairs


def test_a_ways_and_means_subcommittee_co_chair_does_not():
    chairs = rl.parse_chairs(page(
        ("JWMNR", "Natural Resources", [member("Representative", "Emerson Levy", "Co-Chair")]),
        ("EBPS", "Public Safety", [member("Representative", "Paul Evans", "Co-Chair")]),
        *_filler(20)))
    named = {c["name"] for c in chairs}
    assert "Emerson Levy" not in named
    assert "Paul Evans" not in named


def test_the_full_ways_and_means_co_chairs_do():
    chairs = rl.parse_chairs(page(
        ("JWM", "Ways and Means", [member("Representative", "Tawna Sanchez", "Co-Chair")]),
        *_filler(20)))
    assert {"chamber": "house", "name": "Tawna Sanchez", "committees": ["Ways and Means"]} in chairs


def test_a_vice_chair_is_not_a_chair():
    with pytest.raises(ValueError):
        # Only the filler is left, which is below the sanity floor.
        rl.parse_chairs(page(("HCEE", "Climate", [member("Representative", "Bobby Levy", "Vice-Chair")])))


def _filler(n):
    """parse_chairs refuses a roster that looks too small to be real."""
    return [(f"C{i}", f"Committee {i}", [member("Senator", f"Member {i}", "Chair")]) for i in range(n)]
