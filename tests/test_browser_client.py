"""Real Chromium against the workstation client: open the printed address, add a Spark, paste its keys,
use node pages through the client, see the client's own Integrations additions, disconnect.

Node A is the in-process demo cluster (dry-run), reached with a direct connection. Skipped when
Playwright or its Chromium is not installed.
"""

from __future__ import annotations

import asyncio

import pytest

playwright = pytest.importorskip("playwright.async_api")
uvicorn = pytest.importorskip("uvicorn")

from twinspark.client.app import create_client_app, new_session_key  # noqa: E402
from twinspark.client.session import ClientSession  # noqa: E402
from twinspark.client.ssh import free_local_port  # noqa: E402
from twinspark.demo import DemoCluster  # noqa: E402
from twinspark.security import SecretsVault  # noqa: E402

from .test_browser_live_refresh import browser  # noqa: E402,F401 - the fixture


async def _serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="none"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    return server, task


async def _modal_button(page, label):
    await page.locator(".modal-actions button", has_text=label).click()


async def test_the_client_gui_connects_to_node_a_and_passes_its_pages_through(browser, tmp_path,  # noqa: F811
                                                                                monkeypatch):
    monkeypatch.setenv("TSM_CLIENT_KEYRING", "off")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))         # the Hermes card looks here: nothing installed
    (tmp_path / "home").mkdir()
    async with DemoCluster() as demo:
        inference = SecretsVault(demo.a_layout.secrets).get("inference_api_key")
        session = ClientSession(home=tmp_path / "client")
        key = new_session_key()
        port = free_local_port(18765)
        server, task = await _serve(create_client_app(session, key), port)
        try:
            page = await browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{port}/#k={key}")
            await page.wait_for_selector('.view[data-live="connection"]', timeout=15000)
            assert "#k=" not in page.url and page.url.endswith("#/connection")    # the key left the address bar
            assert await page.locator('#nav a[data-nav="connection"]').count() == 1

            # add node A with the dialog: a direct connection, then the keys dialog
            await page.locator('[data-act="conn-add"]').first.click()
            await page.fill("#cn-name", "lab")
            await page.select_option("#cn-mode", "direct")
            await page.fill("#cn-url", demo.url)
            await page.fill("#cn-host", "127.0.0.1")
            await _modal_button(page, "Add and connect")
            await page.wait_for_selector("#k-mgmt", timeout=15000)
            await page.fill("#k-mgmt", demo.key)
            await page.fill("#k-inf", inference)
            await _modal_button(page, "Save keys")
            await page.wait_for_selector(".item.active", timeout=20000)
            await page.wait_for_function(
                "() => document.querySelectorAll('#client-states .dot.good').length >= 2", timeout=15000)
            body = await page.inner_text("#main")
            assert "Client connected" in body and "Agents compatible" in body and "Model ready" in body
            assert f"127.0.0.1:{demo.gateway_port}/v1" in body                   # learned from node A

            # node A's pages come through the client, with node A's key added by the client
            await page.evaluate("location.hash = '#/profiles'")
            await page.wait_for_selector("text=Profiles", timeout=15000)
            await page.evaluate("location.hash = '#/integrations'")
            await page.wait_for_selector("#integration-local-test", timeout=15000)
            assert await page.input_value("#integration-url") == f"http://127.0.0.1:{demo.gateway_port}/v1"
            await page.wait_for_selector("#hermes-card .tag", timeout=15000)
            await page.wait_for_function(
                "() => /not installed/.test(document.querySelector('#hermes-card').textContent)", timeout=15000)

            # disconnect: node pages explain the connection instead of failing
            await page.evaluate("location.hash = '#/connection'")
            await page.locator('[data-act="conn-disconnect"]').click()
            await page.wait_for_function("() => /not connected/.test(document.querySelector('#conn').textContent)",
                                         timeout=15000)
            await page.evaluate("location.hash = '#/profiles'")
            await page.wait_for_selector("text=Not connected to the Sparks", timeout=15000)
            assert not errors, errors

            # node A's own GUI stays as it was: no client pages
            node_page = await browser.new_page()
            await node_page.goto(f"{demo.url}/")
            await node_page.wait_for_selector("#gate:not(.hidden)", timeout=15000)
            await node_page.evaluate("k => { sessionStorage.setItem('tsm_key', k); }", demo.key)
            await node_page.reload()
            await node_page.wait_for_selector("#nav a", timeout=15000)
            assert await node_page.locator('#nav a[data-nav="connection"]').count() == 0
        finally:
            server.should_exit = True
            await task
            await session.aclose()
