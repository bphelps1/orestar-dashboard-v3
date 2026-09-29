"""
diagnose.py — Probe ORESTAR to find the correct form fields and export URL.

Run this locally and paste the output back so fetch.py can be fixed.

Usage:
    python scraper/diagnose.py
"""

import re
import sys
import requests

BASE_URL = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/gotoPublicTransactionSearch.do"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

# ── Step 1: Load the search form ─────────────────────────────────────────────
print("=" * 70)
print("STEP 1: Loading ORESTAR search form")
print("=" * 70)
resp = session.get(SEARCH_URL, timeout=30)
print(f"Status:  {resp.status_code}")
print(f"URL:     {resp.url}")
print(f"Cookies: {dict(session.cookies)}\n")

html = resp.text

# Extract form action
form_matches = re.findall(r'<form[^>]+>', html, re.IGNORECASE)
print("--- FORM TAGS ---")
for f in form_matches:
    print(f)

# Extract all input fields
print("\n--- INPUT FIELDS ---")
inputs = re.findall(r'<input[^>]+>', html, re.IGNORECASE)
for inp in inputs:
    print(inp)

# Extract select fields and options
print("\n--- SELECT FIELDS ---")
selects = re.findall(r'<select[^>]+>.*?</select>', html, re.IGNORECASE | re.DOTALL)
for sel in selects:
    # Just print the select tag and first few options
    lines = sel.strip().split('\n')
    for line in lines[:10]:
        print(line)
    if len(lines) > 10:
        print(f"  ... ({len(lines) - 10} more lines)")
    print()

# Look for export/excel references in JS
print("\n--- EXCEL/EXPORT REFERENCES IN PAGE ---")
for line in html.splitlines():
    low = line.lower()
    if any(kw in low for kw in ['xcel', 'excel', 'export', 'download', 'xls']):
        print(line.strip())

# ── Step 2: Try a minimal search POST ────────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 2: Attempting search POST with cneSearch* field names")
print("=" * 70)

# Try the most likely action URL patterns
for action_path in [
    "/orestar/cneSearch.do",
    "/orestar/publicTransactionSearch.do",
    "/orestar/searchTransactions.do",
]:
    params = {
        "cneSearchTranType": "C",
        "cneSearchTranSubType": "CA",
        "cneSearchTranFiledStartDate": "01/01/2025",
        "cneSearchTranFiledEndDate": "01/07/2025",
        "cneSearchPageIdx": "1",
        "cneSearchButtonName": "search",
    }
    try:
        r = session.post(f"https://secure.sos.state.or.us{action_path}", data=params, timeout=30)
        print(f"\nPOST {action_path}")
        print(f"  Status: {r.status_code}  Content-Type: {r.headers.get('Content-Type','?')}  Bytes: {len(r.content)}")
        if r.status_code == 200:
            # Print first 1000 chars of response to see what we got
            snippet = r.text[:1000].replace('\n', ' ').replace('\r', '')
            print(f"  Preview: {snippet[:300]}")
            # Check for result count indicators
            for kw in ['result', 'record', 'total', 'found', 'no result', 'error', 'exception']:
                hits = [l.strip() for l in r.text.splitlines() if kw.lower() in l.lower()][:3]
                if hits:
                    print(f"  [{kw}]: {hits[0]}")
    except Exception as e:
        print(f"\nPOST {action_path}  ERROR: {e}")

# ── Step 3: Try the export URL directly ──────────────────────────────────────
print("\n" + "=" * 70)
print("STEP 3: Trying export URLs")
print("=" * 70)

export_paths = [
    "/orestar/XcelCNESearch.do",
    "/orestar/xcelCNESearch.do",
    "/orestar/exportCNESearch.do",
    "/orestar/cneSearch.do?exportToExcel=true",
]

for path in export_paths:
    try:
        r = session.get(f"https://secure.sos.state.or.us{path}", timeout=30)
        ct = r.headers.get('Content-Type', '?')
        print(f"\nGET {path}")
        print(f"  Status: {r.status_code}  Content-Type: {ct}  Bytes: {len(r.content)}")
        if 'excel' in ct.lower() or 'spreadsheet' in ct.lower() or 'octet' in ct.lower():
            print("  *** GOT EXCEL/BINARY RESPONSE — THIS IS THE RIGHT URL ***")
        elif r.status_code == 200:
            print(f"  Preview: {r.text[:200].strip()}")
    except Exception as e:
        print(f"\nGET {path}  ERROR: {e}")

print("\n" + "=" * 70)
print("DONE — paste this entire output back to Claude")
print("=" * 70)
