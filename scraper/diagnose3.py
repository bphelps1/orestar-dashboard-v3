"""
diagnose3.py — Use a real browser (Playwright) to inspect ORESTAR's form
and test a search + export.

Install first:
    pip3 install playwright
    python3 -m playwright install chromium

Run:
    python3 scraper/diagnose3.py
"""

from playwright.sync_api import sync_playwright
import re

SEARCH_URL = "https://secure.sos.state.or.us/orestar/gotoPublicTransactionSearch.do"

with sync_playwright() as p:
    print("Launching browser...")
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        accept_downloads=True,
    )
    page = context.new_page()

    # ── Step 1: Load search page ─────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 1: Loading ORESTAR search page (waiting for JS to render)...")
    print("=" * 70)
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)
    print(f"URL: {page.url}")
    print(f"Title: {page.title()}")

    # ── Step 2: Print all form elements ─────────────────────────────────────
    print("\n--- ALL FORM ELEMENTS ---")
    elements = page.evaluate("""() => {
        const results = [];
        // inputs
        document.querySelectorAll('input').forEach(el => {
            results.push({tag:'input', name:el.name, id:el.id, type:el.type, value:el.value.slice(0,50)});
        });
        // selects
        document.querySelectorAll('select').forEach(el => {
            const opts = [...el.options].map(o => o.value + '=' + o.text.trim()).slice(0, 10);
            results.push({tag:'select', name:el.name, id:el.id, options: opts});
        });
        // buttons
        document.querySelectorAll('button, input[type=submit], input[type=button]').forEach(el => {
            results.push({tag:'button', name:el.name, id:el.id, type:el.type, value:el.value, text:el.innerText});
        });
        // links with export/excel
        document.querySelectorAll('a').forEach(el => {
            if (el.href && (el.href.includes('Xcel') || el.href.includes('xcel') || el.href.includes('export') || el.href.includes('Export'))) {
                results.push({tag:'a', href:el.href, text:el.innerText.trim()});
            }
        });
        return results;
    }""")

    for el in elements:
        print(el)

    # ── Step 3: Extract CSRF token ───────────────────────────────────────────
    print("\n--- CSRF TOKEN ---")
    csrf = page.evaluate("""() => {
        const inp = document.querySelector('input[name="OWASP_CSRFTOKEN"]');
        if (inp) return inp.value;
        const meta = document.querySelector('meta[name="OWASP_CSRFTOKEN"]');
        if (meta) return meta.content;
        const links = [...document.querySelectorAll('[href*="OWASP_CSRFTOKEN"]')];
        if (links.length) {
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"]+)/);
            return m ? m[1] : null;
        }
        return null;
    }""")
    print(f"CSRF token: {csrf}")

    # ── Step 4: Try filling the form and searching ───────────────────────────
    print("\n" + "=" * 70)
    print("STEP 4: Trying to fill form and search (Jan 1-7, 2025, Contributions)")
    print("=" * 70)

    try:
        # Try common selectors for transaction type
        for sel in ['select[name="cneSearchTranType"]', '#cneSearchTranType', 'select']:
            if page.locator(sel).count() > 0:
                print(f"Found select with selector: {sel}")
                page.select_option(sel, value='C')
                break

        # Try filling start date
        for sel in ['input[name="cneSearchTranFiledStartDate"]', '#cneSearchTranFiledStartDate',
                    'input[name="cneSearchTranStartDate"]', '#cneSearchTranStartDate']:
            if page.locator(sel).count() > 0:
                print(f"Found start date with selector: {sel}")
                page.fill(sel, '01/01/2025')
                break

        # Try filling end date
        for sel in ['input[name="cneSearchTranFiledEndDate"]', '#cneSearchTranFiledEndDate',
                    'input[name="cneSearchTranEndDate"]', '#cneSearchTranEndDate']:
            if page.locator(sel).count() > 0:
                print(f"Found end date with selector: {sel}")
                page.fill(sel, '01/07/2025')
                break

        # Try clicking search button
        for sel in ['input[value="Search"]', 'button:has-text("Search")',
                    'input[name="cneSearchButtonName"]', '#cneSearchButtonName',
                    'input[type="submit"]']:
            if page.locator(sel).count() > 0:
                print(f"Found search button with selector: {sel}")
                page.click(sel)
                break

        print("Waiting for results...")
        page.wait_for_load_state("networkidle", timeout=30000)
        print(f"After search — URL: {page.url}")

        # Check for results
        body_text = page.inner_text("body")[:500]
        print(f"Page preview: {body_text[:300]}")

        # ── Step 5: Try export ───────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("STEP 5: Looking for Export link after search")
        print("=" * 70)

        export_links = page.evaluate("""() => {
            return [...document.querySelectorAll('a')].filter(a =>
                a.href.includes('Xcel') || a.href.includes('export') ||
                a.innerText.includes('Export') || a.innerText.includes('Excel')
            ).map(a => ({href: a.href, text: a.innerText.trim()}));
        }""")
        print(f"Export links found: {export_links}")

        if export_links:
            print("\nAttempting download...")
            with page.expect_download(timeout=30000) as dl_info:
                page.click(f'a[href="{export_links[0]["href"]}"]')
            dl = dl_info.value
            print(f"Download suggested filename: {dl.suggested_filename}")
            print(f"*** SUCCESS — Playwright can download from ORESTAR ***")
        else:
            print("No export link found on results page.")

    except Exception as e:
        print(f"Error during form interaction: {e}")

    # Print cookies for reference
    print("\n--- SESSION COOKIES ---")
    for c in context.cookies():
        print(f"  {c['name']} = {c['value'][:40]}...")

    browser.close()

print("\n" + "=" * 70)
print("DONE — paste full output back to Claude")
print("=" * 70)
