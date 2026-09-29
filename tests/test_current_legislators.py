import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('roster', Path(__file__).resolve().parents[1] / 'scraper/refresh_legislators.py')
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)


def test_roster_parser_handles_sharepoint_whitespace_and_ignores_navigation():
    html = '<h3>Senate Committees</h3>' + ''.join(
        f'<h3>\u200b<a>Senator\u200b Test Person{i}</a></h3>' for i in range(30))
    names = roster.parse_members(html, 'senate')
    assert len(names) == 30
    assert 'Test Person0' in names


@pytest.mark.parametrize('html', ['<h3>Senator One Person</h3>', '<h3>Access Denied</h3>',
                                '<h3>Senator Same Person</h3>' * 30])
def test_partial_or_duplicate_rosters_are_rejected(html):
    with pytest.raises(ValueError):
        roster.parse_members(html, 'senate')


def test_committee_chairs_include_cochairs_but_not_vicechairs():
    html = ''.join(f'<div><strong>Committee {i}</strong><ul class="no-list-style">'
                   f'<li><a>Representative Person {i}</a> - Chair</li>'
                   '<li><a>Senator Not Chair</a> - Vice-Chair</li>'
                   '</ul></div>' for i in range(15))
    html += '<div><strong>Joint Committee</strong><ul class="no-list-style"><li><a>Senator Senate Person</a> - Co-Chair</li></ul></div>'
    members = roster.parse_chairs(html)
    assert len(members) == 16
    assert not any(m['name'] == 'Not Chair' for m in members)
    assert any(m['name'] == 'Senate Person' and m['chamber'] == 'senate' for m in members)
    with pytest.raises(ValueError):
        roster.parse_chairs('<h1>Access denied</h1>')
