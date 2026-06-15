# ============================================================================
# Purpose: Generate deterministic QA screenshots for the soft-dark mockup.
# Database/ORM: None.
# Standards: Local Playwright runner, deterministic output paths, no app logic.
# Blast Radius: Mockup QA artifacts only; no auth, finance, audit, Neo4j, exports.
# Connections:
#   - File: mockups/ums-smart-revenue-command-center-soft-dark.html
#     -> Source HTML captured into PNG QA views.
#   - File: Docs/09_SMART_DASHBOARD_UI.md -> Documents the soft-dark visual reference.
# ============================================================================
from pathlib import Path

from playwright.sync_api import sync_playwright

html_path = Path(__file__).parent.parent / "ums-smart-revenue-command-center-soft-dark.html"
qa_dir = Path(__file__).parent
base_url = f"file:///{html_path.resolve().as_posix()}"


# Contract: Wait for the local page and loaded webfonts to settle before capture.
# Parameters: page is a Playwright Page already navigating to the mockup view.
# Returns: None; blocks only until network and font readiness conditions finish.
def wait_for_stable(page):
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # Contract: Capture one viewport/hash/role variant into mockups/qa.
    # Parameters: name is the PNG filename; viewport is a Playwright viewport dict;
    # hash_name, full_page, and role select the mockup state and capture mode.
    # Returns: None; writes a PNG and closes the isolated browser context.
    def capture(name, viewport, hash_name="command", full_page=False, role=None):
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        page.goto(f"{base_url}#{hash_name}")
        wait_for_stable(page)
        if role:
            page.select_option("#roleSelect", role)
            page.dispatch_event("#roleSelect", "change")
            page.wait_for_timeout(350)
        page.screenshot(path=qa_dir / name, full_page=full_page)
        context.close()
        print(f"{name} saved")

    desktop = {"width": 1440, "height": 980}
    mobile = {"width": 390, "height": 900}

    # Contract: Emit the fixed nine-file QA baseline used by the soft-dark PR.
    # Side effects: overwrites the named PNG artifacts under mockups/qa.
    capture("ums-command-center-soft-dark-desktop.png", desktop)
    capture("ums-command-center-soft-dark-mobile.png", mobile, full_page=True)
    capture("ums-command-center-soft-dark-restricted.png", desktop, role="assistant")
    capture("ums-command-center-soft-dark-registry.png", desktop, "registry")
    capture("ums-command-center-soft-dark-close.png", desktop, "close")
    capture("ums-command-center-soft-dark-graph.png", desktop, "graph")
    capture("ums-command-center-soft-dark-exports.png", desktop, "exports")
    capture("ums-command-center-soft-dark-connectors.png", desktop, "connectors")
    capture("ums-command-center-soft-dark-audit.png", desktop, "audit")

    browser.close()
    print("All Soft Dark screenshots done!")
