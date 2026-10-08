"""Real Chromium: the dashboard refreshes without recreating the view (no fade, focus and selection kept).

Skipped when Playwright or its Chromium is not installed. Runs against an in-process demo cluster.
"""

from __future__ import annotations

import pytest

playwright = pytest.importorskip("playwright.async_api")

from twinspark.demo import DemoCluster  # noqa: E402


def _executables() -> list:
    """Playwright's own browser first, then a Chromium installed next to it (any revision)."""
    import glob
    import os
    found = [None]
    if os.environ.get("TSM_TEST_CHROMIUM"):
        found.append(os.environ["TSM_TEST_CHROMIUM"])
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if root:
        found += sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux", "chrome")))
    return found


@pytest.fixture
async def browser():
    pw = await playwright.async_playwright().start()
    b, errors = None, []
    for exe in _executables():
        try:
            b = await pw.chromium.launch(executable_path=exe) if exe else await pw.chromium.launch()
            break
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            errors.append(str(exc).splitlines()[0])
    if b is None:
        await pw.stop()
        pytest.skip(f"Chromium not available: {errors}")
    yield b
    await b.close()
    await pw.stop()


async def test_the_dashboard_is_patched_in_place(browser):
    async with DemoCluster() as demo:
        page = await browser.new_page()
        await page.goto(f"{demo.url}/")
        await page.evaluate("k => { sessionStorage.setItem('tsm_key', k); location.hash = '#/dashboard'; }", demo.key)
        await page.reload()
        await page.wait_for_selector('.view[data-live="dashboard"]', timeout=15000)
        await page.evaluate("""() => {
            const v = document.querySelector('.view[data-live="dashboard"]');
            v.__marker = 'first';
            const sel = document.querySelector('#qs-profile');
            sel.focus();
            window.__routeRows = [...document.querySelectorAll('[data-key^="route:"], .grid.cols-2 > *')];
        }""")
        # wait for a real refresh (the next poll's requests answered), not a fixed time: slow machines vary
        async with page.expect_response(lambda r: r.url.endswith("/api/v1/profiles?summary=true"), timeout=30000):
            pass
        await page.wait_for_timeout(800)                                # let it render
        state = await page.evaluate("""() => {
            const v = document.querySelector('.view[data-live="dashboard"]');
            return {same: !!v && v.__marker === 'first',
                    focus: document.activeElement && document.activeElement.id,
                    running: v ? v.getAnimations().filter(a => a.playState === 'running').length : -1};
        }""")
        assert state == {"same": True, "focus": "qs-profile", "running": 0}, state
        await page.goto(f"{demo.url}/#/jobs")                          # navigating still renders a fresh view
        await page.wait_for_selector('.view[data-live="jobs"]', timeout=15000)
        assert await page.evaluate("() => document.querySelector('.view').__marker === undefined")
