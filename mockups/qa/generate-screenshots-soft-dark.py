from playwright.sync_api import sync_playwright
from pathlib import Path

html_path = Path(__file__).parent.parent / "ums-smart-revenue-command-center-soft-dark.html"
qa_dir = Path(__file__).parent
base_url = f"file:///{html_path.resolve().as_posix()}"

def wait_for_stable(page):
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

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
