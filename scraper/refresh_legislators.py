"""Refresh the reviewed current-member asset from official chamber rosters.

Run weekly through the roster PR workflow, or manually before opening a PR.
Failures preserve the previous asset; no partial chamber roster is published.
"""
import json
import re
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCES = {
    'house': 'https://www.oregonlegislature.gov/house/Pages/representativesall.aspx',
    'senate': 'https://www.oregonlegislature.gov/senate/Pages/senatorsall.aspx',
}
OUTPUT = Path(__file__).resolve().parents[1] / 'docs/assets/current_legislators.json'
CHAIR_SOURCE = 'https://olis.oregonlegislature.gov/liz/committees/assignments/committee'
CHAIR_OUTPUT = OUTPUT.with_name('current_committee_chairs.json')


def parse_members(html, chamber):
    prefix = 'Representative' if chamber == 'house' else 'Senator'
    names = []
    for heading in BeautifulSoup(html, 'html.parser').find_all('h3'):
        text = re.sub(r'[\u200b-\u200d\ufeff]', '', heading.get_text(' ', strip=True)).strip()
        match = re.match(rf'^{prefix}\s+(.+)$', text)
        if match:
            names.append(re.sub(r'\s+', ' ', match[1]).strip())
    maximum = 60 if chamber == 'house' else 30
    if not maximum - 5 <= len(names) <= maximum or len(set(names)) != len(names):
        raise ValueError(f'Unexpected {chamber} roster: {len(names)} members; refusing to replace asset')
    return sorted(names)


# OLIS lists 34 chairs and 62 co-chairs, and a co-chairship is mostly a Ways
# and Means thing: the full committee has two, and so does every one of its
# subcommittees and the Emergency Board's. Counting those as committee chairs
# put Emerson Levy and Paul Evans in the roster as chairs of Natural Resources
# and Public Safety, when what they co-chair is a Ways and Means subcommittee.
#
# So a co-chairship counts only for the full Ways and Means, whose co-chairs
# really do sit alongside the Speaker and the Majority Leader. Every other
# co-chairship is left out.
FULL_WAYS_AND_MEANS = 'JWM'


def parse_chairs(html):
    members = {}
    soup = BeautifulSoup(html, 'html.parser')
    for li in soup.select('ul.no-list-style > li'):
        text = re.sub(r'\s+', ' ', li.get_text(' ', strip=True))
        link = li.find('a')
        role = re.search(r' - (Co-)?Chair$', text)
        if not link or not role:
            continue
        label = link.get_text(' ', strip=True)
        match = re.match(r'^(Representative|Senator|House Majority Leader|Senate Majority Leader|Speaker|President)\s+(.+)$', label)
        if not match:
            raise ValueError(f'Unknown chair title: {label}')
        chamber = 'house' if match[1] in ('Representative', 'House Majority Leader', 'Speaker') else 'senate'
        heading = li.parent.parent.find('strong')
        if not heading:
            raise ValueError('Committee heading missing')
        heading_link = heading.find('a')
        code = ''
        if heading_link:
            code_match = re.search(r'/Committees/([A-Za-z0-9]+)/', heading_link.get('href') or '')
            code = code_match[1] if code_match else ''
        if role[1] and code != FULL_WAYS_AND_MEANS:
            continue                      # a co-chair of anything else is not a chair
        name = match[2].strip()
        members.setdefault((chamber, name), set()).add(heading.get_text(' ', strip=True))
    if not 15 <= len(members) <= 90:
        raise ValueError(f'Unexpected chair roster: {len(members)} members')
    return [{'chamber': chamber, 'name': name, 'committees': sorted(committees)}
            for (chamber, name), committees in sorted(members.items())]


def write_if_changed(path, field, data, sources):
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous.get(field) == data:
        return
    path.write_text(json.dumps({'verified_on': date.today().isoformat(), 'sources': sources,
                                field: data}, ensure_ascii=False, indent=2) + '\n')


def main():
    members = {}
    for chamber, url in SOURCES.items():
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        members[chamber] = parse_members(response.text, chamber)
    response = requests.get(CHAIR_SOURCE, timeout=30)
    response.raise_for_status()
    chairs = parse_chairs(response.text)
    # Validate all inputs before changing either reviewed asset.
    write_if_changed(OUTPUT, 'members', members, SOURCES)
    write_if_changed(CHAIR_OUTPUT, 'chairs', chairs, {'assignments': CHAIR_SOURCE})
    print('Validated current members and committee chairs; review any roster diff before merging')


if __name__ == '__main__':
    main()
