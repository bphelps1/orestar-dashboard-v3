"""
diagnose2.py — Check if the CSRF token and search POST are accessible without a browser.

Run:  python3 scraper/diagnose2.py
"""

import re
import requests

BASE = "https://secure.sos.state.or.us/orestar"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

# ── Step 1: Fetch raw HTML and look for CSRF token ───────────────────────────
print("=" * 70)
print("STEP 1: Fetching raw HTML, looking for CSRF token")
print("=" * 70)

resp = session.get(f"{BASE}/gotoPublicTransactionSearch.do", timeout=30)
html = resp.text
print(f"Status: {resp.status_code}   Bytes: {len(resp.content)}")

# Look for the OWASP CSRF token anywhere in the page
tokens = re.findall(r'OWASP_CSRFTOKEN[="\s:]+([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})', html)
if tokens:
    csrf = tokens[0]
    print(f"\n*** CSRF TOKEN FOUND IN RAW HTML: {csrf} ***\n")
else:
    print("\nNo CSRF token found in raw HTML — page likely requires JavaScript.\n")
    csrf = None

# Also print the first 2000 chars of raw HTML so we can see what requests gets
print("--- First 2000 chars of raw HTML ---")
print(html[:2000])

# ── Step 2: Try search POST with CSRF token (if found) ───────────────────────
if csrf:
    print("\n" + "=" * 70)
    print("STEP 2: Trying search POST with CSRF token")
    print("=" * 70)

    for action in ["/orestar/cneSearch.do", "/orestar/publicTransactionSearch.do"]:
        data = {
            "OWASP_CSRFTOKEN":          csrf,
            "cneSearchTranType":         "C",
            "cneSearchTranSubType":      "CA",
            "cneSearchTranFiledStartDate": "01/01/2025",
            "cneSearchTranFiledEndDate":   "01/07/2025",
            "cneSearchPageIdx":          "1",
            "cneSearchButtonName":       "search",
        }
        r = session.post(f"https://secure.sos.state.or.us{action}", data=data, timeout=30)
        print(f"\nPOST {action}")
        print(f"  Status: {r.status_code}  Bytes: {len(r.content)}  CT: {r.headers.get('Content-Type','?')}")
        print(f"  Preview: {r.text[:300].replace(chr(10),' ')}")

    # Step 3: Try export after search
    print("\n" + "=" * 70)
    print("STEP 3: Trying export URL with CSRF token")
    print("=" * 70)
    r = session.get(f"{BASE}/XcelCNESearch", params={"OWASP_CSRFTOKEN": csrf}, timeout=60)
    ct = r.headers.get('Content-Type', '?')
    print(f"Status: {r.status_code}  Bytes: {len(r.content)}  CT: {ct}")
    if 'excel' in ct.lower() or 'spreadsheet' in ct.lower() or 'octet' in ct.lower() or 'zip' in ct.lower():
        print("*** SUCCESS — got binary/Excel response! ***")
    else:
        print(f"Preview: {r.text[:300].replace(chr(10),' ')}")
else:
    print("\nSkipping Steps 2-3 — need CSRF token first.")
    print("The page requires JavaScript to render. Playwright will be needed.")

print("\n" + "=" * 70)
print("DONE — paste full output back to Claude")
print("=" * 70)
