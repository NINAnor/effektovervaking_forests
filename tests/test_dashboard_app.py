"""Browser test of the static dashboard (app/) against a freshly baked tiny dataset.

Skipped when Playwright or a launchable chromium is missing. No network: the
basemap tiles are answered with 204.
"""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest
from test_dashboard import make_source

from effektovervaking_forests import dashboard

sync_api = pytest.importorskip("playwright.sync_api")

APP = Path(__file__).resolve().parents[1] / "app"
PREFIX_ARGS = ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A copy of app/ with data/ baked from the synthetic source, served over HTTP."""
    root = tmp_path_factory.mktemp("site")
    shutil.copytree(
        APP, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("data")
    )
    src = make_source(tmp_path_factory.mktemp("src"))
    dashboard.bake(str(src), root / "data")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=PREFIX_ARGS)
        except Exception as e:  # no chromium installed / cannot start
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def _open(browser, url):
    ctx = browser.new_context(bypass_csp=True)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/tile.openstreetmap.org/**", lambda r: r.fulfill(status=204))
    page.goto(url)
    # Loaded = data status set (OK or error) and the charts drawn or given up on.
    page.wait_for_function(
        "document.getElementById('data-status').textContent !== 'Loading data…'"
    )
    return ctx, page, errors


def _rewrite(root: Path, name: str, fn):
    path = root / "data" / name
    orig = path.read_text()
    path.write_text(fn(orig))
    return lambda: path.write_text(orig)


def test_dashboard_renders_and_layers_toggle(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        status = page.locator("#data-status")
        assert "error" not in (status.get_attribute("class") or "")
        assert "Data generated" in status.inner_text()
        for chart in ("#chart-cover", "#chart-alerts"):
            assert page.locator(f"{chart} svg").count() >= 1
        assert page.locator("#summary-cards .summary-card").count() == 4

        page.select_option("#filter-alert-type", "all")
        page.wait_for_function(
            "pointLayers.size === 4"
            " && [...pointLayers.keys()].every(k => map.getLayer(k))"
        )
        vis = page.evaluate(
            "[...pointLayers.keys()].map(k => map.getLayoutProperty(k, 'visibility'))"
        )
        assert vis == ["visible"] * 4
        assert not errors
    finally:
        ctx.close()


def test_schema_mismatch_shows_error(browser, site) -> None:
    root, url = site

    def bump(s):
        d = json.loads(s)
        d["schema"] = 999
        return json.dumps(d)

    restore = _rewrite(root, "index.json", bump)
    try:
        ctx, page, _ = _open(browser, url)
        status = page.locator("#data-status")
        assert "error" in status.get_attribute("class")
        assert "schema 999" in status.inner_text()
        ctx.close()
    finally:
        restore()


def test_missing_points_file_errors_but_charts_render(browser, site) -> None:
    root, url = site
    path = root / "data" / "points_olivinskog_loss.json"
    saved = path.read_bytes()
    path.unlink()
    try:
        ctx, page, _ = _open(browser, url)
        page.wait_for_selector("#data-status.error")
        assert "points" in page.locator("#data-status").inner_text().lower()
        for chart in ("#chart-cover", "#chart-alerts"):
            assert page.locator(f"{chart} svg").count() >= 1
        ctx.close()
    finally:
        path.write_bytes(saved)
