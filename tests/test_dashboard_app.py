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
        assert page.locator("#chart-alerts svg").count() >= 1
        # One card per kind shown: loss only by default.
        assert page.locator("#summary-cards .summary-card").count() == 1

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
        page.check('input[name="basemap"][value="osm"]')
        assert page.evaluate(
            "() => ['osm', 'satellite']"
            ".map(i => map.getLayoutProperty('base-' + i, 'visibility'))"
        ) == ["visible", "none"]
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
        assert page.locator("#chart-alerts svg").count() >= 1
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
        page.wait_for_function("() => raddApplied.includes('/alert_date/')")

        # A stand: its trajectory and classification.
        page.evaluate("() => map.jumpTo({center: [11.46823, 59.4911], zoom: 16})")
        page.wait_for_timeout(1500)
        box = page.locator("#map").bounding_box()
        centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.click(*centre)
        page.wait_for_function("() => !document.getElementById('detail').hidden")
        body = page.inner_text("#detail-body")
        assert "Stand" in body and "change 2017–2025: stable" in body
        assert page.locator("#detail .traj-chart circle").count() == 9
        # The selected stand is outlined (no ring: nothing smaller was hit).
        assert page.evaluate("() => map.getFilter('sel-stand')[2]") != -1
        assert page.evaluate("() => map.getSource('sel')._data.features.length") == 0
        page.click("#detail-close")
        assert page.locator("#detail").is_hidden()
        # Opening the card moves focus to its close button; Escape closes it.
        page.mouse.click(*centre)
        page.wait_for_function("() => !document.getElementById('detail').hidden")
        assert page.evaluate("() => document.activeElement.id") == "detail-close"
        page.keyboard.press("Escape")
        assert page.locator("#detail").is_hidden()

        # An S-CCD pixel: a link to its Sentinel-2 time series.
        page.evaluate("() => map.jumpTo({center: [15.0001, 59.6], zoom: 17})")
        page.wait_for_timeout(1500)
        page.mouse.click(*centre)
        page.wait_for_function("() => !document.getElementById('detail').hidden")
        href = page.locator("#detail .ext-link").first.get_attribute("href")
        assert "lon=15.00010;lat=59.60000" in href
        # The ring sits on the pixel itself, on top of every layer.
        assert page.evaluate(
            "() => map.getSource('sel')._data.features[0].geometry.coordinates"
        ) == [15.0001, 59.6]
        assert page.evaluate("() => map.getStyle().layers.at(-1).id") == "sel"
        assert page.evaluate("() => map.getFilter('sel-stand')[2]") == -1
        assert not errors
    finally:
        ctx.close()


def test_period_calendar_presets_and_chart_markers(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        state = lambda k: page.evaluate(f"() => state.{k}")  # noqa: E731
        count = lambda: page.inner_text("#alert-count")  # noqa: E731
        page.wait_for_function("() => pointCols.size === 2")
        assert (state("from"), state("to")) == ("2024-01-15", "2024-03-10")
        assert count() == "(4)"  # two loss pixels per forest type

        page.fill("#filter-date-from", "2024-03-01")
        page.dispatch_event("#filter-date-from", "change")
        assert count() == "(2)"
        assert not page.is_checked('input[name="period"][value="all"]')
        page.locator('.seg__opt:has(input[name="period"][value="30"])').click()
        assert state("from") == "2024-02-10"  # 30 days back from the newest alert
        assert count() == "(4)"

        # The markers on the alert chart, by keyboard and by dragging.
        page.select_option("#chart-group", "monthly")
        page.focus('.brush-handle[data-end="from"]')
        page.keyboard.press("ArrowRight")
        assert state("from") == "2024-03-01"
        assert page.evaluate("() => document.activeElement.dataset.end") == "from"
        grip = page.locator('.brush-handle[data-end="to"] .grip').bounding_box()
        page.mouse.move(grip["x"] + 5, grip["y"] + 12)
        page.mouse.down()
        page.mouse.move(grip["x"] - 400, grip["y"] + 12, steps=5)
        page.mouse.up()
        assert state("to") == "2024-03-10"  # held one month after the start
        page.focus('.brush-handle[data-end="from"]')
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        assert state("from") == "2024-01-15"  # clamped to the first alert
        assert not errors
    finally:
        ctx.close()


def test_layer_info_dialog(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        page.click('[data-info="alerts"]')
        dlg = page.locator("#info")
        assert dlg.is_visible()
        assert page.inner_text("#info-title") == "Tree-cover alerts (S-CCD)"
        assert "Possible tree loss" in page.inner_text("#info-body .info-legend")
        assert "Sentinel-2" in page.inner_text("#info-body")
        page.click("#info-close")
        assert dlg.is_hidden()
        page.click('[data-info="guide"]')
        assert "Candidates, not proof" in page.inner_text("#info-body")
        page.keyboard.press("Escape")
        assert dlg.is_hidden()
        assert not errors
    finally:
        ctx.close()


def test_radd_pasted_map_link(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    page.route("**/earthengine.googleapis.com/**", lambda r: r.fulfill(status=204))
    try:
        page.click("#radd-section .about summary")
        page.fill("#radd-link", "https://example.com/tiles/{z}/{x}/{y}")
        page.click("#radd-link-form button[type=submit]")
        assert "Not an Earth Engine map link" in page.inner_text("#radd-link-msg")
        link = "https://earthengine.googleapis.com/v1/projects/p/maps/abc123-def/tiles/{z}/{x}/{y}"
        page.fill("#radd-link", link)
        page.click("#radd-link-form button[type=submit]")
        assert "pasted map link" in page.inner_text("#radd-note")
        assert page.locator("#radd-modes").is_hidden()
        page.check("#toggle-radd")
        page.wait_for_function("() => map.getLayer('radd')")
        assert page.evaluate("() => raddApplied") == link
        # Kept on reload; cleared back to the minted file.
        page.reload()
        page.wait_for_function(
            "() => typeof raddTiles !== 'undefined' && raddTiles?.pasted"
        )
        page.click("#radd-section .about summary")
        page.click("#radd-link-clear")
        assert "tiles made" in page.inner_text("#radd-note")
        assert not errors
    finally:
        ctx.close()


def test_c2c_card_text(browser, site) -> None:
    # No S3 in tests, so the layer itself is off; the card and popup text are checked.
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        assert page.locator("#c2c-section").is_hidden()
        html = page.evaluate(
            "() => [c2cSection({change_year: 2019.9956, duration: 0.999,"
            " magnitude: -0.31, hansen_lossyear: 2020, lon: 15, lat: 59.6}),"
            " c2cSection({change_year: 2024.9962, duration: 8, magnitude: -0.1,"
            " hansen_lossyear: null, lon: 15, lat: 59.6})]"
        )
        # Loss is dated to the first yearly image after the drop (X.996 -> X+1).
        assert "loss in 2020" in html[0] and "Loss year</dt><dd>2020<" in html[0]
        assert "Hansen" in html[0] and "weak" not in html[0]
        assert "loss in 2025" in html[1] and "undercounted" in html[1]
        assert "weak" in html[1] and "no loss mapped" in html[1]
        assert "Years stable before</dt><dd>8<" in html[1]
        assert not errors
    finally:
        ctx.close()


def test_opacity_slider_and_size_by_change(browser, site) -> None:
    _, url = site
    ctx, page, errors = _open(browser, url)
    try:
        page.wait_for_function("() => pointCols.size === 2")
        key = page.evaluate("() => [...pointLayers.keys()][0]")
        # Dots scale with the size of the change, from the baked magnitude.
        radius = page.evaluate(f"() => map.getPaintProperty('{key}', 'circle-radius')")
        assert "mag" in json.dumps(radius)
        page.fill('input[data-opacity="alerts"]', "50")
        op = page.evaluate(f"() => map.getPaintProperty('{key}', 'circle-opacity')")
        assert op[3:] == [15, 0.475, 18, 0.3]
        page.fill('input[data-opacity="stands"]', "0")
        assert (
            page.evaluate("() => map.getPaintProperty('forest-line', 'line-opacity')")
            == 0
        )
        assert not errors
    finally:
        ctx.close()
