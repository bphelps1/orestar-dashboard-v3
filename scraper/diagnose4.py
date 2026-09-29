"""
diagnose4.py — Run with a VISIBLE browser (headless=False) to confirm
the page renders and capture form selectors.

A Chrome window will open on your screen — that's expected.
Don't close it until the script finishes.

Run:  python3 scraper/diagnose4.py
"""

import time
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://secure.sos.state.or.us/orestar/gotoPublicTransactionSearch.do"

with sync_playwright() as p:
    print("Opening visible Chrome browser (don't close it)...")
    browser = p.chromium.launch(
        headless=False,   # visible window — bypasses bot detection
        args=["--start-maximized"],
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        accept_downloads=True,
        no_viewport=True,
    )
    page = context.new_page()

    print(f"Navigating to ORESTAR...")
    page.goto(SEARCH_URL, timeout=60000)

    # Wait for page to fully render (JS challenge + app load)
    print("Waiting 8 seconds for JavaScript to fully render...")
    time.sleep(8)

    print(f"\nURL:   {page.url}")
    print(f"Title: {page.title()}")

    # Take a screenshot so we can see what rendered
    screenshot_path = "/Users/brad/Desktop/ClaudeProjects/files_for_claude/orestar_rendered.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"Screenshot saved to: {screenshot_path}")

    # Print all form elements
    print("\n--- FORM ELEMENTS ---")
    elements = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input, select, button, textarea').forEach(el => {
            const opts = el.tagName === 'SELECT'
                ? [...el.options].slice(0,8).map(o => o.value + '|' + o.text.trim())
                : [];
            out.push({
                tag:   el.tagName,
                name:  el.name || '',
                id:    el.id   || '',
                type:  el.type || '',
                value: (el.value||'').slice(0,40),
                opts:  opts,
            });
        });
        return out;
    }""")
    for el in elements:
        print(el)

    # CSRF token
    print("\n--- CSRF TOKEN ---")
    csrf = page.evaluate("""() => {
        const inp = document.querySelector('input[name="OWASP_CSRFTOKEN"]');
        if (inp) return inp.value;
        const links = [...document.querySelectorAll('[href*="OWASP_CSRFTOKEN"]')];
        if (links.length) {
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }
        // Search all text on page
        const bodyText = document.body.innerHTML;
        const m2 = bodyText.match(/OWASP_CSRFTOKEN[='":\\s]+([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})/);
        return m2 ? m2[1] : null;
    }""")
    print(f"CSRF token: {csrf}")

    # ── If we have form elements, try a search ──────────────────────────────
    form_count = len(elements)
    print(f"\nTotal form elements found: {form_count}")

    if form_count > 0 and csrf:
        print("\n--- ATTEMPTING SEARCH ---")
        try:
            # Fill transaction type = Contribution
            if page.locator('select[name="cneSearchTranType"]').count():
                page.select_option('select[name="cneSearchTranType"]', 'C')
                print("Set tran type = C")

            # Fill start date
            for sel in ['input[name="cneSearchTranFiledStartDate"]', 'input[name="cneSearchTranStartDate"]']:
                if page.locator(sel).count():
                    page.fill(sel, '01/01/2025')
                    print(f"Set start date via {sel}")
                    break

            # Fill end date
            for sel in ['input[name="cneSearchTranFiledEndDate"]', 'input[name="cneSearchTranEndDate"]']:
                if page.locator(sel).count():
                    page.fill(sel, '01/07/2025')
                    print(f"Set end date via {sel}")
                    break

            # Click search
            for sel in ['input[value="Search"]', 'input[name="cneSearchButtonName"]',
                        'button:has-text("Search")', 'input[type="submit"]']:
                if page.locator(sel).count():
                    page.click(sel)
                    print(f"Clicked search via {sel}")
                    break

            time.sleep(5)
            print(f"After search URL: {page.url}")

            # Look for export link
            export_links = page.evaluate("""() =>
                [...document.querySelectorAll('a')].filter(a =>
                    (a.href||'').includes('Xcel') || (a.innerText||'').includes('Export')
                ).map(a => ({href: a.href, text: a.innerText.trim()}))
            """)
            print(f"Export links: {export_links}")

            if export_links:
                print("\nTrying download...")
                with page.expect_download(timeout=30000) as dl:
                    page.click(f'a[href="{export_links[0]["href"]}"]')
                d = dl.value
                print(f"*** SUCCESS! Downloaded: {d.suggested_filename} ***")

        except Exception as e:
            print(f"Error: {e}")

        # Screenshot after search
        page.screenshot(path=screenshot_path.replace(".png", "_after_search.png"), full_page=True)
        print(f"\nPost-search screenshot saved.")
    else:
        print("\nPage still empty with visible browser.")
        print("First 1000 chars of page HTML:")
        print(page.content()[:1000])

    print("\nClosing browser in 3 seconds...")
    time.sleep(3)
    browser.close()

print("\n" + "=" * 70)
print("DONE — paste output + check screenshot at:")
print(screenshot_path)
print("=" * 70)
