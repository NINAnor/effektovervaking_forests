"""Browser test of the static dashboard (app/) against a freshly baked tiny dataset.

Skipped when Playwright or a launchable chromium is missing. No network: the
basemap tiles are answered with 204.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
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
    # Plain read/write: the kernel copy shutil uses fails with EAGAIN on CIFS.
    shutil.copytree(
        APP,
        root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("data"),
        copy_function=lambda s, d: Path(d).write_bytes(Path(s).read_bytes()),
    )
    # No S3 in tests: the condition-cell layer stays off.
    cfg = root / "config.js"
    cfg.write_text(
        re.sub(r"conditionCells: '[^']*'", "conditionCells: ''", cfg.read_text())
    )
    src = make_source(tmp_path_factory.mktemp("src"))
    dashboard.bake(str(src), root / "data")
    # Minting needs Earth Engine; a stub tile host stands in (routed in _open).
    (root / "data" / "radd_tiles.json").write_text(
        json.dumps(
            {
                "image_id": "europe_test",
                "version_date": "2026-11-01",
                "minted_at": 0,
                "layers": {
                    m: {"tiles": f"https://radd.test/{m}/{{z}}/{{x}}/{{y}}"}
                    for m in ("alert", "alert_date")
                },
            }
        )
    )
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
    # No bypass_csp: the page's CSP is under test too. Functions, not expression
    # strings, are passed to the page, since the CSP forbids eval.
    ctx = browser.new_context()
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "console",
        lambda m: "Content Security Policy" in m.text and errors.append(m.text),
    )
    page.route("**/tile.openstreetmap.org/**", lambda r: r.fulfill(status=204))
    page.route("**/server.arcgisonline.com/**", lambda r: r.fulfill(status=204))
    page.route("**/radd.test/**", lambda r: r.fulfill(status=204))
    page.goto(url)
    # Loaded = data status set (OK or error) and the charts drawn or given up on.
    page.wait_for_function(
        "() => document.getElementById('data-status').textContent !== 'Loading data…'"
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
            "() => pointLayers.size === 4"
            " && [...pointLayers.keys()].every(k => map.getLayer(k))"
        )
        vis = page.evaluate(
            "() => [...pointLayers.keys()]"
            ".map(k => map.getLayoutProperty(k, 'visibility'))"
        )
        assert vis == ["visible"] * 4

        # Stands recolour and relabel; no cell layer without a configured URL.
        page.select_option("#stand-color", "trajectory")
        assert "declining" in page.inner_text("#stand-legend")
        assert page.locator("#toggle-cells").is_hidden()
        page.check('input[name="basemap"][value="satellite"]')
        assert page.evaluate(
            "() => ['osm', 'satellite']"
            ".map(i => map.getLayoutProperty('base-' + i, 'visibility'))"
        ) == ["none", "visible"]
        page.uncheck("#toggle-alerts")
        assert page.evaluate(
            "() => [...pointLayers.keys()]"
            ".every(k => map.getLayoutProperty(k, 'visibility') === 'none')"
        )
        page.uncheck("#toggle-stands")
        assert (
            page.evaluate("() => map.getLayoutProperty('forest-fill', 'visibility')")
            == "none"
        )
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


def test_radd_tiles_and_click_card(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        # Satellite is the default basemap.
        assert (
            page.evaluate("() => map.getLayoutProperty('base-satellite', 'visibility')")
            == "visible"
        )
        assert "may have expired" in page.inner_text("#radd-note")
        page.check("#toggle-radd")
        page.wait_for_function(
            "() => map.getLayer('radd')"
            " && map.getLayoutProperty('radd', 'visibility') === 'visible'"
        )
        page.locator('.seg__opt:has(input[value="alert_date"])').click()
        assert "/alert_date/" in page.evaluate("() => map.getSource('radd').tiles[0]")

        # A stand: its trajectory and classification.
        page.evaluate("() => map.jumpTo({center: [11.46823, 59.4911], zoom: 16})")
        page.wait_for_timeout(1500)
        box = page.locator("#map").bounding_box()
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.click(*centre)
        page.wait_for_function("() => !document.getElementById('detail').hidden")
        body = page.inner_text("#detail-body")
        assert "Stand" in body and "trajectory: stable" in body
        assert page.locator("#detail .traj-chart circle").count() == 9
        page.click("#detail-close")
        assert page.locator("#detail").is_hidden()

        # An S-CCD pixel: a link to its Sentinel-2 time series.
        page.evaluate("() => map.jumpTo({center: [15.0001, 59.6], zoom: 17})")
        page.wait_for_timeout(1500)
        page.mouse.click(*centre)
        page.wait_for_function("() => !document.getElementById('detail').hidden")
        href = page.locator("#detail .ext-link").first.get_attribute("href")
        assert "lon=15.00010;lat=59.60000" in href
        assert not errors
    finally:
        ctx.close()
